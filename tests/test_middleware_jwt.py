"""`middleware.py::_resolve_jwt_session` — Kế hoạch 2, A1. Pure-Python (không cần DB): chỉ verify
chữ ký + đọc header, không chạm `core.tenants`. Cùng `_FakeRequest`/`_FakeHeaders` style của
`test_middleware.py` (không import lại — 2 class 4 dòng, trùng có chủ đích, cùng lý do
`_CallistoEmbedding` trùng ở `scripts/e2e_smoke_eval.py`/`routes/*.py`)."""

from __future__ import annotations

from uuid import UUID

import pytest
from studio_app import jwt_auth
from studio_app.middleware import _resolve_jwt_session
from studio_app.settings import Settings
from studio_workbench.tenant_wall import ResolvedContext


class _FakeHeaders:
    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    def get(self, key: str) -> str | None:
        return self._data.get(key)


class _FakeRequest:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = _FakeHeaders(headers)


def _settings() -> Settings:
    return Settings(
        database_url="postgresql://unused/unused",
        database_url_admin="postgresql://unused/unused",
        jwt_secret="test-secret-middleware",
    )


_TENANT_ID = UUID("a0000000-0000-0000-0000-000000000001")


def test_no_authorization_header_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Không có header -> None, KHÔNG raise — khác hẳn "có header nhưng hỏng". `kit#129` §3.2:
    đường `x-tenant-id` cũ đã bị xoá hẳn, None ở đây nghĩa là "chưa đăng nhập", không còn ý nghĩa
    "rơi về đường dự phòng" nữa."""
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    result = _resolve_jwt_session(_FakeRequest({}))  # type: ignore[arg-type]
    assert result is None


def test_valid_bearer_token_resolves_full_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    token = jwt_auth.issue_token(ResolvedContext(tenant_id=_TENANT_ID, user="dozyboy", roles=["public"]))

    result = _resolve_jwt_session(_FakeRequest({"authorization": f"Bearer {token}"}))  # type: ignore[arg-type]

    assert result is not None
    assert result.tenant_id == _TENANT_ID
    assert result.user == "dozyboy"
    assert result.roles == ["public"]


def test_bearer_prefix_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """`bearer`/`Bearer`/`BEARER` đều phải hoạt động — HTTP header case-insensitive theo spec,
    không nên phụ thuộc client viết đúng hoa/thường."""
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    token = jwt_auth.issue_token(ResolvedContext(tenant_id=_TENANT_ID, user="dozyboy", roles=[]))

    result = _resolve_jwt_session(_FakeRequest({"authorization": f"bearer {token}"}))  # type: ignore[arg-type]
    assert result is not None
    assert result.tenant_id == _TENANT_ID


def test_missing_bearer_prefix_falls_through_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Header có mặt nhưng KHÔNG phải dạng `Bearer <token>` (vd `Basic ...`, hoặc token trần
    không có tiền tố) -> None, không raise — chỉ token THỰC SỰ được đọc ra mới có thể "present but
    broken"."""
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    result = _resolve_jwt_session(_FakeRequest({"authorization": "some-raw-token-no-prefix"}))  # type: ignore[arg-type]
    assert result is None


def test_broken_bearer_token_raises_invalid_token_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "Present but broken" phải LOUD (raise), không âm thầm rơi về đường cũ — khác hẳn "không có
    header nào cả". `tenant_context_middleware` (không test ở đây, cần ASGI thật) bắt exception
    này và trả 401."""
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    with pytest.raises(jwt_auth.InvalidTokenError):
        _resolve_jwt_session(_FakeRequest({"authorization": "Bearer not-a-real-jwt"}))  # type: ignore[arg-type]
