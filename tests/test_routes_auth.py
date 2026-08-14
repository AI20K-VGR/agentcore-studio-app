"""`routes/auth.py::demo_login` — Kế hoạch 2, A2. Cần DB thật (root `conftest.py`'s `admin_pool`)
vì route đọc `core.tenants` qua `get_pool()` (singleton, không phải fixture `pool`/`admin_pool` —
2 pool khác object nhưng cùng DSN/database nên dữ liệu nhất quán, xem docstring `conftest.py`).

Contract MỚI: `DemoLoginRequest` chỉ có `user` (email) — không còn `tenant`/`roles` cho client tự
khai. Mọi test dưới đây tra qua `routes.auth._DEMO_ACCOUNTS` (registry cứng), không hardcode lại
1 danh sách song song trong file test — nếu ai đổi `_DEMO_ACCOUNTS`, test này tự đi theo, không
âm thầm lệch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import HTTPException
from studio_app import jwt_auth
from studio_app.core._db import Pool, close_pools
from studio_app.routes.auth import _DEMO_ACCOUNTS, DemoLoginRequest, demo_login
from studio_app.settings import Settings


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    """`demo_login()` chạm `get_pool()` (singleton process-wide ở `studio_app.core._db`, KHÔNG
    phải fixture `pool`/`admin_pool` ở conftest) — nếu không đóng lại, pool giữ nguyên ref tới
    event loop của TEST NÀY, và test SAU (chạy trên loop mới, pytest-asyncio tạo 1 loop/test theo
    mặc định) sẽ vỡ với `ValueError: The future belongs to a different loop` khi chạm lại cùng
    singleton đó — bắt được thật khi chạy suite đầy đủ (`test_pool_split_roles` đỏ ngẫu nhiên tuỳ
    thứ tự test), không lộ ra khi chạy riêng file này. Cùng kỷ luật `try/finally: await
    close_pools()` mà `test_schema.py::test_pool_split_roles` đã tự áp cho chính nó."""
    yield
    await close_pools()


def _settings() -> Settings:
    return Settings(
        database_url="postgresql://unused/unused",
        database_url_admin="postgresql://unused/unused",
        jwt_secret="test-secret-routes-auth-at-least-32-bytes",
    )


async def test_demo_login_resolves_tenant_and_roles_from_registry(
    admin_pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KHÓA end-to-end của A2 (contract mới): `tenant`/`roles` KHÔNG đến từ request — tra đúng
    `_DEMO_ACCOUNTS["hr@ankor.vn"]` -> `("ankor", ["hr"])`. Token trả về phải verify lại ra ĐÚNG
    tenant_id thật trong `core.tenants` (không phải slug, không phải giá trị đoán)."""
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", ("ankor",))
        row = await cur.fetchone()
        assert row is not None
        ankor_id = row[0]

    expected_tenant_slug, expected_roles = _DEMO_ACCOUNTS["hr@ankor.vn"]
    assert expected_tenant_slug == "ankor"

    response = await demo_login(DemoLoginRequest(user="hr@ankor.vn"))

    assert response.tenant_id == str(ankor_id)
    assert response.user == "hr@ankor.vn"
    assert response.roles == expected_roles

    resolved = jwt_auth.verify_token(response.access_token)
    assert resolved.tenant_id == ankor_id
    assert resolved.user == "hr@ankor.vn"
    assert resolved.roles == expected_roles


async def test_demo_login_admin_account_gets_admin_role(admin_pool: Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    """`admin@ankor.vn` (registry) -> roles chứa "admin" — gán sẵn lúc "tạo tài khoản", không phải
    client tick lúc đăng nhập (đã bỏ hẳn field đó khỏi request)."""
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    async with admin_pool.connection() as conn:
        await conn.execute("INSERT INTO core.tenants (name) VALUES (%s)", ("ankor",))

    response = await demo_login(DemoLoginRequest(user="admin@ankor.vn"))

    assert "admin" in response.roles


async def test_demo_login_unknown_email_returns_404(admin_pool: Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Email KHÔNG nằm trong `_DEMO_ACCOUNTS` -> 404, không tự tạo tài khoản mới, không đoán
    tenant/roles nào cả."""
    del admin_pool  # bảng `core.tenants` cần tồn tại (schema đã ensure), nhưng để RỖNG có chủ đích
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)

    with pytest.raises(HTTPException) as exc_info:
        await demo_login(DemoLoginRequest(user="khong-ton-tai@ankor.vn"))
    assert exc_info.value.status_code == 404


async def test_demo_login_known_email_but_tenant_not_seeded_returns_404(
    admin_pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Email CÓ trong registry (`nhanvien@borea.vn` -> tenant "borea") nhưng `core.tenants` chưa
    seed "borea" -> vẫn 404, fail-closed đúng 2 lớp độc lập (registry + DB), không phải chỉ 1."""
    del admin_pool  # cố tình KHÔNG seed "borea" — đây chính là điều kiện cần test
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)

    with pytest.raises(HTTPException) as exc_info:
        await demo_login(DemoLoginRequest(user="nhanvien@borea.vn"))
    assert exc_info.value.status_code == 404
