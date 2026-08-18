"""`jwt_auth.py` — ký/verify JWT thật (Kế hoạch 2, A1/A2). Pure-Python, không cần DB thật: mọi
test tự dựng `Settings` (pydantic thường, không qua biến môi trường/`@lru_cache`) rồi
`monkeypatch` `get_settings` tại module `jwt_auth` — tránh phụ thuộc `STUDIO_DATABASE_URL` (bắt
buộc, không default) hay đụng cache toàn-process của `get_settings()` thật.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt as pyjwt
import pytest
from loguru import logger as loguru_logger
from studio_app import jwt_auth
from studio_app.settings import LlmProvider, Settings
from studio_workbench.tenant_wall import ResolvedContext

_TENANT_ID = UUID("a0000000-0000-0000-0000-000000000001")


def _settings(*, secret: str = "test-secret-at-least-32-bytes-long", expire_minutes: int = 480) -> Settings:
    return Settings(
        database_url="postgresql://unused/unused",
        database_url_admin="postgresql://unused/unused",
        jwt_secret=secret,
        jwt_expire_minutes=expire_minutes,
        llm_provider=LlmProvider.GEMINI,
    )


def test_issue_then_verify_roundtrips_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """KHÓA: token ký ra bởi `issue_token()` phải verify được lại đúng CẢ 3 trường
    (tenant_id/user/roles), không rơi rớt field nào qua vòng ký -> verify."""
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    session = ResolvedContext(tenant_id=_TENANT_ID, user="dozyboy@ankor.vn", roles=["public", "hr"])

    token = jwt_auth.issue_token(session)
    resolved = jwt_auth.verify_token(token)

    assert resolved.tenant_id == _TENANT_ID
    assert resolved.user == "dozyboy@ankor.vn"
    assert resolved.roles == ["public", "hr"]


def test_verify_rejects_wrong_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """KHÓA THẬT của "JWT thật": ký bằng khoá A, verify bằng khoá B (khác) -> PHẢI đỏ. Đây chính
    là bằng chứng "không ai giả mạo được token nếu không có `jwt_secret`" — nếu test này xanh mà
    lẽ ra phải đỏ, việc ký/verify không có tác dụng bảo vệ gì cả."""
    monkeypatch.setattr(jwt_auth, "get_settings", lambda: _settings(secret="secret-A-at-least-32-bytes-long!!"))
    session = ResolvedContext(tenant_id=_TENANT_ID, user="attacker", roles=[])
    token = jwt_auth.issue_token(session)

    monkeypatch.setattr(jwt_auth, "get_settings", lambda: _settings(secret="secret-B-at-least-32-bytes-long!!"))
    with pytest.raises(jwt_auth.InvalidTokenError):
        jwt_auth.verify_token(token)


def test_verify_rejects_expired_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    settings = _settings()
    now = datetime.now(UTC)
    expired_claims = {
        "tenant_id": str(_TENANT_ID),
        "user": "dozyboy@ankor.vn",
        "roles": [],
        "iat": now - timedelta(minutes=10),
        "exp": now - timedelta(minutes=1),  # hết hạn 1 phút trước
    }
    expired_token = pyjwt.encode(expired_claims, settings.jwt_secret, algorithm="HS256")

    with pytest.raises(jwt_auth.InvalidTokenError):
        jwt_auth.verify_token(expired_token)


def test_verify_rejects_missing_tenant_id_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Token CÓ chữ ký hợp lệ (ký đúng khoá) nhưng THIẾU claim bắt buộc — vẫn phải fail-closed,
    không được suy ra giá trị mặc định nào cho `tenant_id`."""
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    settings = _settings()
    now = datetime.now(UTC)
    token = pyjwt.encode(
        {"user": "dozyboy@ankor.vn", "roles": [], "iat": now, "exp": now + timedelta(hours=1)},
        settings.jwt_secret,
        algorithm="HS256",
    )

    with pytest.raises(jwt_auth.InvalidTokenError):
        jwt_auth.verify_token(token)


def test_verify_rejects_malformed_token_string(monkeypatch: pytest.MonkeyPatch) -> None:
    # Thiếu monkeypatch này (khác các bài hàng xóm) làm `Settings()` validate từ ENV THẬT thay vì
    # `_settings()` cố định — đỏ ở CI (`database_url`/`database_url_admin`/`jwt_secret` không set)
    # dù verify_token() không hề chạm DB — phát hiện qua review AIE-2 (PR#23 workbench, N-4).
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    with pytest.raises(jwt_auth.InvalidTokenError):
        jwt_auth.verify_token("not-a-real-jwt-at-all")


def test_verify_password_roundtrips_correct_and_wrong_plaintext() -> None:
    password_hash = jwt_auth.hash_password("correct-horse-battery-staple")
    assert jwt_auth.verify_password("correct-horse-battery-staple", password_hash) is True
    assert jwt_auth.verify_password("wrong-password", password_hash) is False


def test_verify_password_oversized_plaintext_returns_false_not_raise() -> None:
    """bcrypt giới hạn 72 byte — `bcrypt.checkpw` tự nó raise `ValueError` cho input dài hơn, phải
    chặn TRƯỚC khi lọt xuống bcrypt để không lộ oracle 500-vs-401 (review `app#17`, Chặn 2)."""
    password_hash = jwt_auth.hash_password("some-real-password")
    oversized = "a" * 73
    assert jwt_auth.verify_password(oversized, password_hash) is False


def test_verify_password_malformed_stored_hash_returns_false_not_raise() -> None:
    """`bcrypt.checkpw` raise `ValueError("Invalid salt")` (thực nghiệm xác nhận) cho
    `password_hash` KHÔNG đúng định dạng bcrypt, thay vì trả `False` — nếu để lọt, 1 dòng
    `core.users.password_hash` hỏng (không có CHECK constraint ràng ở DB) sẽ làm `login()` 500 cho
    ĐÚNG email đó nhưng 401 cho email không tồn tại, tự nó là 1 oracle khác biệt account tồn tại
    (review `app#17` đợt 3, Important)."""
    assert jwt_auth.verify_password("anything", "not-a-valid-bcrypt-hash") is False


def test_verify_password_oversized_plaintext_does_not_log_malformed_hash_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ghim ĐƯỜNG ĐI, không chỉ giá trị trả về: `verify_password` phải trả `False` cho ca >72 byte
    qua nhánh `if len(plain.encode("utf-8")) > 72:` RIÊNG, TRƯỚC khi chạm `except ValueError`. Nếu
    xoá nhánh pre-check đó, hành vi NGOÀI quan sát được (trả `False`, không raise) vẫn giữ nguyên —
    `bcrypt.checkpw` tự nó cũng raise `ValueError` cho input >72 byte, rơi vào `except ValueError`
    bên dưới — nên `test_verify_password_oversized_plaintext_returns_false_not_raise` ở trên KHÔNG
    bắt được mutant xoá pre-check đó (review `app#17`, "nên sửa" #3: thực nghiệm xác nhận mutant
    này cho `111 passed`, suite xanh nguyên). Tín hiệu DUY NHẤT phân biệt 2 đường: `except
    ValueError` bên dưới LUÔN log cảnh báo "hash không đúng định dạng bcrypt" — pre-check thì
    không log gì. Ghim bằng cách assert KHÔNG có cảnh báo nào cho ca >72 byte thật (khác ca hash
    hỏng, xem bài dưới) — sai đường sẽ log, làm bài này đỏ."""
    warnings: list[str] = []
    monkeypatch.setattr(loguru_logger, "warning", lambda msg: warnings.append(msg))
    password_hash = jwt_auth.hash_password("some-real-password")

    assert jwt_auth.verify_password("a" * 73, password_hash) is False
    assert warnings == []


def test_verify_password_malformed_hash_does_log_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Đối trọng của bài trên — ca hash hỏng THẬT (không phải >72 byte) phải đi qua `except
    ValueError` và log đúng 1 cảnh báo, khác ca >72 byte ở trên (không log gì)."""
    warnings: list[str] = []
    monkeypatch.setattr(loguru_logger, "warning", lambda msg: warnings.append(msg))

    assert jwt_auth.verify_password("anything", "not-a-valid-bcrypt-hash") is False
    assert len(warnings) == 1


def test_two_tenants_get_non_interchangeable_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity chống nhầm: token của tenant A không verify ra tenant B — tránh lỗi copy-paste kiểu
    `issue_token` lờ đi `session` truyền vào mà ký hằng số."""
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    other_tenant = uuid4()
    token_a = jwt_auth.issue_token(ResolvedContext(tenant_id=_TENANT_ID, user="a", roles=[]))
    token_b = jwt_auth.issue_token(ResolvedContext(tenant_id=other_tenant, user="b", roles=[]))

    assert jwt_auth.verify_token(token_a).tenant_id == _TENANT_ID
    assert jwt_auth.verify_token(token_b).tenant_id == other_tenant
