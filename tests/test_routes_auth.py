"""`routes/auth.py::login` — Kế hoạch 3. Cần DB thật (root `conftest.py`'s `admin_pool`) vì route
đọc `core.users` qua `get_pool()` (singleton, không phải fixture `pool`/`admin_pool` — 2 pool khác
object nhưng cùng DSN/database nên dữ liệu nhất quán, xem docstring `conftest.py`).

`demo_login()`/`_DEMO_ACCOUNTS`/`DemoLoginRequest` đã bị XOÁ khỏi `routes/auth.py` (không còn
đường đăng nhập không-mật-khẩu) — mọi test demo-login cũ trong file này bị xoá theo, không còn gì
để test. Xem docstring module `routes/auth.py` để biết lý do/quyết định.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import HTTPException
from studio_app import jwt_auth
from studio_app.core._db import Pool, close_pools
from studio_app.jwt_auth import hash_password
from studio_app.routes.auth import LoginRequest, login
from studio_app.settings import Settings


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    """`login()` chạm `get_pool()` (singleton process-wide ở `studio_app.core._db`, KHÔNG phải
    fixture `pool`/`admin_pool` ở conftest) — nếu không đóng lại, pool giữ nguyên ref tới event
    loop của TEST NÀY, và test SAU (chạy trên loop mới, pytest-asyncio tạo 1 loop/test theo mặc
    định) sẽ vỡ với `ValueError: The future belongs to a different loop` khi chạm lại cùng
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


async def test_login_succeeds_with_correct_password(admin_pool: Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Đường thật (Kế hoạch 3) end-to-end: seed 1 dòng `core.users` thật, đăng nhập đúng mật khẩu
    -> token verify được ra đúng tenant/roles đã seed."""
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", ("acme",))
        row = await cur.fetchone()
        assert row is not None
        tenant_id = row[0]
        await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, roles) VALUES (%s, %s, %s, %s)",
            (tenant_id, "real@acme.com", hash_password("correct-horse-battery-staple"), ["admin", "public"]),
        )

    response = await login(LoginRequest(email="real@acme.com", password="correct-horse-battery-staple"))

    assert response.tenant_id == str(tenant_id)
    assert response.roles == ["admin", "public"]
    resolved = jwt_auth.verify_token(response.access_token)
    assert resolved.tenant_id == tenant_id


async def test_login_rejects_wrong_password(admin_pool: Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", ("acme2",))
        row = await cur.fetchone()
        assert row is not None
        tenant_id = row[0]
        await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, roles) VALUES (%s, %s, %s, %s)",
            (tenant_id, "wrongpw@acme.com", hash_password("correct-password"), ["public"]),
        )

    with pytest.raises(HTTPException) as exc_info:
        await login(LoginRequest(email="wrongpw@acme.com", password="incorrect-password"))
    assert exc_info.value.status_code == 401


async def test_login_rejects_unknown_email(admin_pool: Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    del admin_pool
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    with pytest.raises(HTTPException) as exc_info:
        await login(LoginRequest(email="khong-ton-tai@acme.com", password="whatever123"))
    assert exc_info.value.status_code == 401


async def test_login_rejects_oversized_password_with_401_not_500_for_existing_email(
    admin_pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chặn 2, review `app#17`: mật khẩu >72 byte + email TỒN TẠI trước bản vá làm
    `bcrypt.checkpw` raise `ValueError` không ai bắt -> 500. Giờ `verify_password` tự chặn trước
    khi gọi bcrypt -> luôn 401, không bao giờ 500."""
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", ("acme3",))
        row = await cur.fetchone()
        assert row is not None
        tenant_id = row[0]
        await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, roles) VALUES (%s, %s, %s, %s)",
            (tenant_id, "oversized@acme.com", hash_password("short-password"), ["public"]),
        )

    with pytest.raises(HTTPException) as exc_info:
        await login(LoginRequest(email="oversized@acme.com", password="a" * 73))
    assert exc_info.value.status_code == 401


async def test_login_rejects_oversized_password_with_401_not_500_for_unknown_email(
    admin_pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Đối trọng bài trên: email KHÔNG tồn tại + mật khẩu >72 byte cũng phải 401 — cùng status,
    cùng đường (`verify_password` với `DUMMY_PASSWORD_HASH`), không lộ ra `row is None` bằng cách
    nào khác (short-circuit thời trước đã bỏ)."""
    del admin_pool
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    with pytest.raises(HTTPException) as exc_info:
        await login(LoginRequest(email="khong-ton-tai-oversized@acme.com", password="a" * 73))
    assert exc_info.value.status_code == 401
