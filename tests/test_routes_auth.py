"""`routes/auth.py::login` — Kế hoạch 3. Cần DB thật (root `conftest.py`'s `admin_pool`) vì route
đọc `core.users` qua `get_pool()` (singleton, không phải fixture `pool`/`admin_pool` — 2 pool khác
object nhưng cùng DSN/database nên dữ liệu nhất quán, xem docstring `conftest.py`).

`demo_login()`/`_DEMO_ACCOUNTS`/`DemoLoginRequest` đã bị XOÁ khỏi `routes/auth.py` (không còn
đường đăng nhập không-mật-khẩu) — mọi test demo-login cũ trong file này bị xoá theo, không còn gì
để test. Xem docstring module `routes/auth.py` để biết lý do/quyết định.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from fastapi import HTTPException
from starlette.requests import Request
from studio_app import jwt_auth, middleware, rate_limit
from studio_app.core._db import Pool, close_pools, get_pool
from studio_app.jwt_auth import hash_password
from studio_app.routes.auth import ChangePasswordRequest, LoginRequest, change_own_password, login
from studio_app.settings import Settings
from studio_workbench.tenant_wall import ResolvedContext


def _fake_request(client_host: str = "test-client") -> Request:
    """`login()` giờ nhận thêm `request: Request` (review `app#17`, Important #2, đợt 8 —
    rate-limit theo `request.client.host`) — test gọi thẳng hàm (không qua ASGI) phải tự dựng 1
    `Request` tối thiểu, đủ để `.client.host` đọc được, không cần scope ASGI đầy đủ."""
    return Request(scope={"type": "http", "client": (client_host, 0), "headers": []})


@pytest_asyncio.fixture(autouse=True)
async def _reset_rate_limiter() -> AsyncIterator[None]:
    """`rate_limit._buckets` là state module-level (KHÔNG phải ContextVar reset theo request) —
    không xoá giữa các bài, bài sau sẽ ăn hết token bucket bài trước để lại (tất cả test trong file
    này dùng CÙNG `client_host` mặc định), gây 429 giả trong test hoàn toàn không liên quan."""
    rate_limit.reset_all()
    yield


@asynccontextmanager
async def _simulate_request_connection() -> AsyncIterator[None]:
    """`login()` giờ đọc `core.users` qua `get_request_connection()` (review `app#17` đợt 3,
    Important — tránh mở 1 connection thứ 2 ngoài connection `tenant_context_middleware` đã giữ
    suốt đời request), nên test gọi thẳng hàm (không qua ASGI, khác `test_http_asgi.py`) phải tự
    set `_request_conn` contextvar trước khi gọi — cùng convention `middleware._request_session`
    mà `test_admin_routes.py`/`test_routes_runs.py` đã dùng cho `create_company`/`create_run`."""
    pool = await get_pool()
    async with pool.connection() as conn:
        token = middleware._request_conn.set(conn)
        try:
            yield
        finally:
            middleware._request_conn.reset(token)


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

    async with _simulate_request_connection():
        response = await login(
            LoginRequest(email="real@acme.com", password="correct-horse-battery-staple"), _fake_request()
        )

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
        async with _simulate_request_connection():
            await login(LoginRequest(email="wrongpw@acme.com", password="incorrect-password"), _fake_request())
    assert exc_info.value.status_code == 401


async def test_login_rejects_unknown_email(admin_pool: Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    del admin_pool
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    with pytest.raises(HTTPException) as exc_info:
        async with _simulate_request_connection():
            await login(LoginRequest(email="khong-ton-tai@acme.com", password="whatever123"), _fake_request())
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
        async with _simulate_request_connection():
            await login(LoginRequest(email="oversized@acme.com", password="a" * 73), _fake_request())
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
        async with _simulate_request_connection():
            await login(LoginRequest(email="khong-ton-tai-oversized@acme.com", password="a" * 73), _fake_request())
    assert exc_info.value.status_code == 401


async def test_login_regression_verify_password_always_called_even_for_unknown_email(
    admin_pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation-regression cho Chặn 2 nửa "timing" (review `app#17` đợt 2, mục 6): nếu ai revert
    về `if row is None or not verify_password(...)` (short-circuit `or` — CHÍNH bug ban đầu bài
    này vá), mọi test hiện tại (chỉ check status code, luôn 401 cả 2 nhánh) vẫn xanh, không bắt
    được — vì status code không đổi, chỉ THỜI GIAN đổi. Bài này ghim trực tiếp HÀNH VI GỌI HÀM
    (verify_password phải chạy đúng 1 lần dù email không tồn tại), không chỉ status code cuối."""
    del admin_pool
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)

    call_count = 0
    real_verify_password = jwt_auth.verify_password

    def _counting_verify_password(plain: str, password_hash: str) -> bool:
        nonlocal call_count
        call_count += 1
        return real_verify_password(plain, password_hash)

    monkeypatch.setattr("studio_app.routes.auth.verify_password", _counting_verify_password)

    with pytest.raises(HTTPException) as exc_info:
        async with _simulate_request_connection():
            await login(
                LoginRequest(email="khong-ton-tai-regression@acme.com", password="whatever123"), _fake_request()
            )

    assert exc_info.value.status_code == 401
    assert call_count == 1, "verify_password() phải được gọi ĐÚNG 1 lần kể cả khi email không tồn tại"


async def test_login_expands_admin_roles_to_all_tenant_sections(
    admin_pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quyết định qua AskUserQuestion: admin công ty mặc định thấy MỌI nội dung, không phải tự
    tick từng phòng ban. `core.users.roles` trong DB vẫn CHỈ lưu `["admin"]` — JWT phát ra mới là
    nơi mở rộng, đúng phạm vi `apps/studio` (filter nội dung nằm ở `packages/engine`, ngoài phạm
    vi sửa ở đây)."""
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", ("acme-admin-expand",))
        row = await cur.fetchone()
        assert row is not None
        tenant_id = row[0]
        await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, roles) VALUES (%s, %s, %s, %s)",
            (tenant_id, "admin-expand@acme.com", hash_password("password-123"), ["admin"]),
        )
        admin_row = await conn.execute("SELECT id FROM core.users WHERE email = %s", ("admin-expand@acme.com",))
        admin_id_row = await admin_row.fetchone()
        assert admin_id_row is not None
        await conn.execute(
            "INSERT INTO core.sections (tenant_id, name, created_by) VALUES (%s, %s, %s), (%s, %s, %s)",
            (tenant_id, "hr", admin_id_row[0], tenant_id, "finance", admin_id_row[0]),
        )

    async with _simulate_request_connection():
        response = await login(LoginRequest(email="admin-expand@acme.com", password="password-123"), _fake_request())

    assert set(response.roles) == {"admin", "hr", "finance"}

    # `core.users.roles` trong DB KHÔNG bị ghi đè — chỉ JWT phát ra mới mở rộng.
    async with admin_pool.connection() as conn:
        cur = await conn.execute("SELECT roles FROM core.users WHERE email = %s", ("admin-expand@acme.com",))
        row = await cur.fetchone()
    assert row is not None
    assert list(row[0]) == ["admin"]


async def test_login_does_not_expand_employee_roles(admin_pool: Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Đối chứng: nhân viên KHÔNG có `"admin"` thì roles JWT giữ nguyên đúng như DB — mở rộng chỉ
    áp dụng cho admin, không phải mọi tài khoản."""
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    async with admin_pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", ("acme-employee-no-expand",)
        )
        row = await cur.fetchone()
        assert row is not None
        tenant_id = row[0]
        await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, roles) VALUES (%s, %s, %s, %s)",
            (tenant_id, "employee-no-expand@acme.com", hash_password("password-123"), ["hr"]),
        )
        employee_row = await conn.execute(
            "SELECT id FROM core.users WHERE email = %s", ("employee-no-expand@acme.com",)
        )
        employee_id_row = await employee_row.fetchone()
        assert employee_id_row is not None
        await conn.execute(
            "INSERT INTO core.sections (tenant_id, name, created_by) VALUES (%s, %s, %s)",
            (tenant_id, "finance", employee_id_row[0]),
        )

    async with _simulate_request_connection():
        response = await login(
            LoginRequest(email="employee-no-expand@acme.com", password="password-123"), _fake_request()
        )

    assert response.roles == ["hr"]


async def test_login_rejects_deactivated_user(admin_pool: Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    """`is_active=false` (vô hiệu hoá qua `DELETE /api/admin/users/{id}`) phải chặn login CÙNG
    401 với sai mật khẩu — không lộ oracle "tài khoản này bị khoá" qua status code riêng."""
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", ("acme-deactivated",))
        row = await cur.fetchone()
        assert row is not None
        tenant_id = row[0]
        await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, roles, is_active) VALUES (%s, %s, %s, %s, false)",
            (tenant_id, "deactivated@acme.com", hash_password("correct-horse-battery-staple"), ["public"]),
        )

    with pytest.raises(HTTPException) as exc_info:
        async with _simulate_request_connection():
            await login(
                LoginRequest(email="deactivated@acme.com", password="correct-horse-battery-staple"), _fake_request()
            )
    assert exc_info.value.status_code == 401


def _set_session(*, tenant_id: object, user: str, roles: list[str]) -> object:
    session = ResolvedContext(tenant_id=tenant_id, user=user, roles=roles)  # type: ignore[arg-type]
    return middleware._request_session.set(session)


async def test_change_own_password_succeeds_and_new_password_logs_in(
    admin_pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", ("acme-changepw",))
        row = await cur.fetchone()
        assert row is not None
        tenant_id = row[0]
        await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, roles) VALUES (%s, %s, %s, %s)",
            (tenant_id, "changepw@acme.com", hash_password("old-password-123"), ["admin"]),
        )

    token = _set_session(tenant_id=tenant_id, user="changepw@acme.com", roles=["admin"])
    try:
        async with _simulate_request_connection():
            result = await change_own_password(
                ChangePasswordRequest(old_password="old-password-123", new_password="new-password-456")
            )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]
    assert result == {"detail": "Đã đổi mật khẩu."}

    # Mật khẩu MỚI phải đăng nhập được, mật khẩu CŨ không còn dùng được nữa.
    async with _simulate_request_connection():
        login_result = await login(
            LoginRequest(email="changepw@acme.com", password="new-password-456"), _fake_request()
        )
    assert login_result.tenant_id == str(tenant_id)

    with pytest.raises(HTTPException) as exc_info:
        async with _simulate_request_connection():
            await login(LoginRequest(email="changepw@acme.com", password="old-password-123"), _fake_request())
    assert exc_info.value.status_code == 401


async def test_change_own_password_rejects_wrong_old_password(
    admin_pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", ("acme-changepw-wrong",))
        row = await cur.fetchone()
        assert row is not None
        tenant_id = row[0]
        await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, roles) VALUES (%s, %s, %s, %s)",
            (tenant_id, "changepw-wrong@acme.com", hash_password("old-password-123"), ["admin"]),
        )

    token = _set_session(tenant_id=tenant_id, user="changepw-wrong@acme.com", roles=["admin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await change_own_password(
                    ChangePasswordRequest(old_password="totally-wrong-password", new_password="new-password-456")
                )
        assert exc_info.value.status_code == 401
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    # Mật khẩu CŨ vẫn phải còn dùng được — request bị từ chối không được ghi gì cả.
    async with _simulate_request_connection():
        login_result = await login(
            LoginRequest(email="changepw-wrong@acme.com", password="old-password-123"), _fake_request()
        )
    assert login_result.tenant_id == str(tenant_id)
