"""Test HTTP-level THẬT — qua ASGI (`httpx.AsyncClient` + `ASGITransport(app=create_app())`), gửi
request qua toàn bộ chồng middleware + FastAPI routing thật, KHÔNG gọi thẳng hàm route + set
ContextVar tay như mọi test khác trong repo (review `app#17` đợt 2, mục 5 — xác nhận: "chưa có tiền
lệ TestClient trong repo", `test_middleware.py:43`).

Lỗ cụ thể được nêu, giờ có test bảo vệ: thứ tự `tenant_context_middleware`/`CORSMiddleware` trong
`app.py:60-64` (đã fix 1 lần, review PR#5 DE M2) không có test nào khoá lại — ai đảo ngược thứ tự
đó, response lỗi (401 phát sinh TRONG tenant_context_middleware qua `get_request_session()`) sẽ
không đi qua CORS để được gắn header, và cả bộ test cũ (gọi thẳng hàm route) vẫn xanh vì không bài
nào build request HTTP thật để CORSMiddleware có cơ hội chạy.

`create_app()`'s lifespan (DDL + grant) KHÔNG được trigger ở đây — `admin_pool` fixture (root
`conftest.py`) đã tự `ensure_all_schemas`/`grant_app_privileges` trước khi yield, cùng DSN với
`get_pool()`/`get_admin_pool()` (singleton `studio_app.core._db`) routes dùng — 2 pool khác object,
cùng dữ liệu, đúng pattern `test_routes_auth.py` đã dùng.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from studio_app import jwt_auth
from studio_app.app import create_app
from studio_app.core._db import Pool, close_pools
from studio_app.jwt_auth import hash_password
from studio_app.settings import Settings


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    yield
    await close_pools()


def _settings() -> Settings:
    return Settings(
        database_url="postgresql://unused/unused",
        database_url_admin="postgresql://unused/unused",
        jwt_secret="test-secret-http-asgi-at-least-32-bytes",
    )


@pytest_asyncio.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_unauthenticated_admin_route_returns_401(client: AsyncClient, admin_pool: Pool) -> None:
    """`POST /api/admin/companies` KHÔNG kèm `Authorization` -> 401 thật qua ASGI, không phải suy
    diễn từ đọc `get_request_session()`. `admin_pool` chỉ để đảm bảo schema đã ensure trước khi
    request chạm route (route không cần dữ liệu gì cho ca này)."""
    del admin_pool
    res = await client.post(
        "/api/admin/companies",
        json={"company_name": "unauth-co", "admin_email": "x@x.com", "admin_password": "password123"},
    )
    assert res.status_code == 401


async def test_cors_header_present_on_error_response(client: AsyncClient, admin_pool: Pool) -> None:
    """Khoá đúng thứ tự middleware (`app.py:60-64`, review PR#5 DE M2). Dùng token HỎNG (không
    phải thiếu hẳn header) mới đúng kịch bản comment mô tả: `tenant_context_middleware` bắt
    `InvalidTokenError` và tự trả `JSONResponse(401)` NGAY, KHÔNG gọi `call_next()` — response đó
    chỉ có cơ hội được gắn header CORS nếu CORS nằm NGOÀI (bọc) `tenant_context_middleware`. Ca
    "thiếu hẳn Authorization" (như `test_unauthenticated_admin_route_returns_401`) KHÔNG phân biệt
    được 2 thứ tự — request đó luôn đi qua `call_next()` bình thường (session=None, không raise),
    401 phát sinh SAU, sâu bên trong FastAPI's exception handling, nên vẫn được gắn CORS dù đảo
    thứ tự — đã tự mutation-verify: đảo thứ tự middleware, phiên bản "thiếu header" KHÔNG bắt được
    gì (dương tính giả về độ phủ), chỉ phiên bản "token hỏng" dưới đây mới đỏ đúng chỗ."""
    del admin_pool
    res = await client.post(
        "/api/admin/companies",
        json={"company_name": "cors-co", "admin_email": "x@x.com", "admin_password": "password123"},
        headers={"Origin": "http://localhost:5173", "Authorization": "Bearer this-is-not-a-valid-jwt"},
    )
    assert res.status_code == 401
    assert res.headers.get("access-control-allow-origin") == "*"


async def test_oversized_password_422_does_not_echo_plaintext(client: AsyncClient, admin_pool: Pool) -> None:
    """Critical #1, review `app#17` đợt 2: Pydantic `ValidationError.errors()` echo NGUYÊN GIÁ TRỊ
    client gửi vào `input` (và `.ctx.error` cho validator tự viết) — FastAPI's mặc định handler
    cho `RequestValidationError` serialize thẳng list đó vào response 422, nghĩa là mật khẩu THẬT
    client vừa gõ lộ vào body response (dễ bị log lại ở reverse-proxy/APM/HAR). Request KHÔNG kèm
    Authorization — validation body chạy TRƯỚC khi FastAPI gọi vào hàm route (nên chưa chạm
    `get_request_session()`/`require_superadmin`), nên không cần token hợp lệ để trigger 422 này."""
    del admin_pool
    oversized_password = "a" * 73
    res = await client.post(
        "/api/admin/companies",
        json={"company_name": "leak-co", "admin_email": "x@x.com", "admin_password": oversized_password},
    )
    assert res.status_code == 422
    body_text = res.text
    assert oversized_password not in body_text
    detail = res.json()["detail"]
    password_errors = [e for e in detail if "password" in str(e.get("loc", ()))]
    assert password_errors, "phải có ít nhất 1 error item cho field password"
    for error in password_errors:
        assert "input" not in error
        assert "ctx" not in error


async def test_login_end_to_end_through_real_http(client: AsyncClient, admin_pool: Pool) -> None:
    """Đường thật, đầu-cuối qua HTTP: seed 1 dòng `core.users`, `POST /api/auth/login` bằng
    `httpx`, verify token trả về xài được để gọi tiếp 1 route cần auth (`/api/admin/companies`,
    kỳ vọng 403 vì role không phải superadmin — chứng minh token THẬT được middleware chấp nhận
    và giải mã đúng roles, không phải 401 vì thiếu/sai token)."""
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", ("http-e2e-co",))
        row = await cur.fetchone()
        assert row is not None
        tenant_id = row[0]
        await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, roles) VALUES (%s, %s, %s, %s)",
            (tenant_id, "http-e2e@acme.com", hash_password("http-e2e-password-123"), ["admin", "public"]),
        )

    login_res = await client.post(
        "/api/auth/login", json={"email": "http-e2e@acme.com", "password": "http-e2e-password-123"}
    )
    assert login_res.status_code == 200
    token = login_res.json()["access_token"]

    admin_res = await client.post(
        "/api/admin/companies",
        json={"company_name": "should-fail-co", "admin_email": "x@x.com", "admin_password": "password123"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert admin_res.status_code == 403  # role "admin", không phải "superadmin" — đúng, không phải 401
