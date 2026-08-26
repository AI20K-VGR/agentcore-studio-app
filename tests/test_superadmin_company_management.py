"""`routes/admin.py` — 4 route superadmin thao tác VÀO TRONG 1 công ty đã tạo (app#75).

Bài toán các route này giải: trước chúng, superadmin chỉ có đường ĐI VÀO (`POST /companies`).
Mọi route quản user đều scope theo tenant của NGƯỜI GỌI, mà superadmin đứng ở `__system__` — nên 1
công ty mất tài khoản admin (quên mật khẩu/nghỉ việc) là hỏng vĩnh viễn, chỉ chữa được bằng SQL
tay. Vì thế file này kiểm 2 nhóm bất biến, không chỉ nhóm 1:

1. Đường phục hồi có THẬT (xem được user, thêm được admin, đặt lại được mật khẩu, đổi được tên).
2. Đường phục hồi KHÔNG mở thêm cửa nào: company-admin không gọi được route nào ở đây, tạm khoá
   công ty chặn cả JWT CŨ chứ không chỉ đăng nhập mới, reset mật khẩu giết luôn phiên cũ.

Nhóm 2 là nhóm dễ xanh giả nhất — cùng lớp lỗ đã phải vá 2 lần cho `core.users.is_active` (chặn ở
`login` tưởng là đủ, thực ra JWT cũ vẫn sống tới 480 phút). Mỗi bài ở nhóm đó vì vậy đi qua
`fetch_fresh_identity` chứ không qua `login`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from starlette.requests import Request
from studio_app import middleware, rate_limit
from studio_app.authz import fetch_fresh_identity
from studio_app.core._db import Pool, close_pools, get_pool
from studio_app.jwt_auth import hash_password, verify_password
from studio_app.routes.admin import (
    AddCompanyAdminRequest,
    ResetPasswordRequest,
    UpdateCompanyRequest,
    add_company_admin,
    deactivate_company_user,
    list_companies,
    list_company_users,
    reactivate_company_user,
    reset_company_user_password,
    update_company,
)
from studio_app.routes.auth import LoginRequest, login
from studio_workbench.tenant_wall import ResolvedContext

SUPERADMIN_EMAIL = "root@agentcore.test"


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    yield
    await close_pools()


@pytest_asyncio.fixture(autouse=True)
async def _reset_rate_limiter() -> AsyncIterator[None]:
    """Chỉ cần cho các bài chạm `login()` — `rate_limit._buckets` là state module-level dùng chung
    cả process, bài trước để lại bucket cạn sẽ làm bài sau 429 giả (xem `test_routes_auth.py`)."""
    rate_limit.reset_all()
    yield


def _fake_request() -> Request:
    return Request(scope={"type": "http", "client": ("test-client", 0), "headers": []})


def _set_session(*, tenant_id: UUID, user: str, system_roles: list[str]) -> object:
    session = ResolvedContext(tenant_id=tenant_id, user=user, system_roles=system_roles)
    return middleware._request_session.set(session)


@asynccontextmanager
async def _simulate_request_connection() -> AsyncIterator[None]:
    """Một `async with` = một "request" thật: commit xảy ra lúc thoát khối, nên 2 lệnh gọi route
    liên tiếp trong 1 bài phải bọc RIÊNG từng khối (cùng convention `test_admin_routes.py`)."""
    pool = await get_pool()
    async with pool.connection() as conn:
        token = middleware._request_conn.set(conn)
        try:
            yield
        finally:
            middleware._request_conn.reset(token)


async def _seed_tenant(admin_pool: Pool, name: str, *, is_active: bool = True) -> UUID:
    async with admin_pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO core.tenants (name, is_active) VALUES (%s, %s) RETURNING id", (name, is_active)
        )
        row = await cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


async def _seed_user(
    admin_pool: Pool, tenant_id: UUID, email: str, system_roles: list[str], *, password: str = "not-a-real-hash"
) -> UUID:
    async with admin_pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, system_roles) VALUES (%s, %s, %s, %s) "
            "RETURNING id",
            (str(tenant_id), email, password, system_roles),
        )
        row = await cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


async def _seed_section(admin_pool: Pool, tenant_id: UUID, name: str, created_by: UUID) -> None:
    async with admin_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO core.sections (tenant_id, name, created_by) VALUES (%s, %s, %s)",
            (str(tenant_id), name, str(created_by)),
        )


async def _seed_superadmin(admin_pool: Pool, label: str) -> UUID:
    """Superadmin sống ở 1 tenant KHÁC công ty nghiệp vụ — đúng hình dạng thật
    (`scripts/seed_superadmin.py` đặt họ ở `core.tenants.name = '__system__'`). Bài test không
    dùng đúng tên `__system__` để mỗi bài có tenant riêng, tránh giẫm chân nhau khi chạy song song;
    điều duy nhất quan trọng là superadmin KHÔNG thuộc công ty đang thao tác."""
    system_tenant_id = await _seed_tenant(admin_pool, f"__system__-{label}")
    await _seed_user(admin_pool, system_tenant_id, f"{label}-{SUPERADMIN_EMAIL}", ["superadmin"])
    return system_tenant_id


# ---------------------------------------------------------------------------
# Nhóm 1 — đường phục hồi có thật
# ---------------------------------------------------------------------------


async def test_superadmin_lists_users_of_a_company_they_do_not_belong_to(admin_pool: Pool) -> None:
    """Bất biến gốc của app#75. `list_users` (route cũ) scope theo tenant NGƯỜI GỌI, nên superadmin
    gọi nó chỉ thấy chính mình — route mới phải thấy được người của công ty khác."""
    system_tenant_id = await _seed_superadmin(admin_pool, "list")
    company_id = await _seed_tenant(admin_pool, "ankor-list")
    await _seed_user(admin_pool, company_id, "boss@ankor.test", ["admin"])
    await _seed_user(admin_pool, company_id, "nhanvien@ankor.test", ["hr"])

    token = _set_session(tenant_id=system_tenant_id, user=f"list-{SUPERADMIN_EMAIL}", system_roles=["superadmin"])
    try:
        async with _simulate_request_connection():
            users = await list_company_users(str(company_id))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert {u.email for u in users} == {"boss@ankor.test", "nhanvien@ankor.test"}


async def test_list_company_users_never_returns_a_password_field(admin_pool: Pool) -> None:
    """Cùng bất biến `test_admin_routes.py::test_password_never_leaks_in_create_company_response`,
    áp cho route mới — `UserSummary` dùng chung nên bài này canh regression nếu ai đó thêm cột."""
    system_tenant_id = await _seed_superadmin(admin_pool, "leak")
    company_id = await _seed_tenant(admin_pool, "ankor-leak")
    await _seed_user(admin_pool, company_id, "boss@ankor-leak.test", ["admin"], password=hash_password("secret-pw"))

    token = _set_session(tenant_id=system_tenant_id, user=f"leak-{SUPERADMIN_EMAIL}", system_roles=["superadmin"])
    try:
        async with _simulate_request_connection():
            users = await list_company_users(str(company_id))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    dumped = users[0].model_dump()
    assert "password" not in dumped
    assert "password_hash" not in dumped
    assert "secret-pw" not in str(dumped)


async def test_superadmin_adds_a_second_admin_to_an_existing_company(admin_pool: Pool) -> None:
    """Ca "admin duy nhất của công ty nghỉ việc". Không route nào khác làm được: `create_company`
    409 vì `core.tenants.name` UNIQUE, `create_user` 400 vì superadmin không thuộc công ty nào."""
    system_tenant_id = await _seed_superadmin(admin_pool, "add")
    company_id = await _seed_tenant(admin_pool, "ankor-add")
    await _seed_user(admin_pool, company_id, "cu@ankor-add.test", ["admin"])

    token = _set_session(tenant_id=system_tenant_id, user=f"add-{SUPERADMIN_EMAIL}", system_roles=["superadmin"])
    try:
        async with _simulate_request_connection():
            created = await add_company_admin(
                str(company_id), AddCompanyAdminRequest(email="moi@ankor-add.test", password="mat-khau-du-dai")
            )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert created.tenant_id == str(company_id)
    assert created.system_roles == ["admin"]

    async with admin_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT tenant_id, system_roles, password_hash FROM core.users WHERE email = %s", ("moi@ankor-add.test",)
        )
        row = await cur.fetchone()
    assert row is not None
    assert UUID(str(row[0])) == company_id
    assert list(row[1]) == ["admin"]
    # Mật khẩu phải BĂM, không lưu thô — bài này bắt được cả ca ai đó "tối ưu" bỏ bcrypt đi.
    assert row[2] != "mat-khau-du-dai"
    assert verify_password("mat-khau-du-dai", row[2])


async def test_add_company_admin_duplicate_email_returns_409_not_500(admin_pool: Pool) -> None:
    """`core.users.email` UNIQUE TOÀN HỆ THỐNG — trùng phải là 409 có thông điệp đọc được, không
    phải `UniqueViolation` lọt lên thành 500 (cùng khuôn `create_company` đã chốt)."""
    system_tenant_id = await _seed_superadmin(admin_pool, "dup")
    company_id = await _seed_tenant(admin_pool, "ankor-dup")
    await _seed_user(admin_pool, company_id, "trung@ankor-dup.test", ["admin"])

    token = _set_session(tenant_id=system_tenant_id, user=f"dup-{SUPERADMIN_EMAIL}", system_roles=["superadmin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await add_company_admin(
                    str(company_id), AddCompanyAdminRequest(email="trung@ankor-dup.test", password="mat-khau-du-dai")
                )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 409


async def test_reset_password_lets_the_new_password_log_in(admin_pool: Pool) -> None:
    """Đường phục hồi đầy đủ cho ca "admin công ty quên mật khẩu": superadmin đặt lại, rồi chính
    tài khoản đó đăng nhập được bằng mật khẩu mới. Đi qua `login()` THẬT chứ không chỉ so hash —
    hash đúng mà `login` vẫn từ chối (vd vì `is_active`/tenant) thì đường phục hồi vẫn gãy."""
    system_tenant_id = await _seed_superadmin(admin_pool, "reset")
    company_id = await _seed_tenant(admin_pool, "ankor-reset")
    user_id = await _seed_user(
        admin_pool, company_id, "quen@ankor-reset.test", ["admin"], password=hash_password("mat-khau-cu")
    )

    token = _set_session(tenant_id=system_tenant_id, user=f"reset-{SUPERADMIN_EMAIL}", system_roles=["superadmin"])
    try:
        async with _simulate_request_connection():
            await reset_company_user_password(
                str(company_id), str(user_id), ResetPasswordRequest(new_password="mat-khau-moi")
            )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    async with _simulate_request_connection():
        result = await login(LoginRequest(email="quen@ankor-reset.test", password="mat-khau-moi"), _fake_request())
    assert result.user == "quen@ankor-reset.test"

    async with _simulate_request_connection():
        with pytest.raises(HTTPException) as exc_info:
            await login(LoginRequest(email="quen@ankor-reset.test", password="mat-khau-cu"), _fake_request())
    assert exc_info.value.status_code == 401


async def test_rename_company_keeps_the_same_tenant_id(admin_pool: Pool) -> None:
    """Đổi tên là đường DUY NHẤT dọn 1 công ty tạo nhầm (D3 — không có `DELETE`). Bài này ghim
    thêm: đổi tên KHÔNG tạo tenant mới, nên mọi user/section/dữ liệu đang trỏ `tenant_id` cũ vẫn
    nguyên chỗ."""
    system_tenant_id = await _seed_superadmin(admin_pool, "rename")
    company_id = await _seed_tenant(admin_pool, "Ankor Grroup")
    admin_id = await _seed_user(admin_pool, company_id, "boss@ankor-rename.test", ["admin"])
    await _seed_section(admin_pool, company_id, "hr", admin_id)

    token = _set_session(tenant_id=system_tenant_id, user=f"rename-{SUPERADMIN_EMAIL}", system_roles=["superadmin"])
    try:
        async with _simulate_request_connection():
            updated = await update_company(str(company_id), UpdateCompanyRequest(name="Ankor Group"))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert updated.tenant_id == str(company_id)
    assert updated.name == "Ankor Group"
    assert updated.user_count == 1
    assert updated.section_count == 1


async def test_list_companies_reports_user_and_section_counts(admin_pool: Pool) -> None:
    """D4 — 2 con số trả KÈM `GET /companies` thay vì endpoint thống kê riêng. Bài dùng 2 công ty
    có số lượng KHÁC NHAU: nếu subquery quên điều kiện `tenant_id`, cả hai sẽ ra cùng 1 con số
    (tổng toàn hệ thống) và bài đỏ — fixture đối xứng sẽ không bắt được lỗi đó."""
    system_tenant_id = await _seed_superadmin(admin_pool, "counts")
    small_id = await _seed_tenant(admin_pool, "ankor-counts-small")
    big_id = await _seed_tenant(admin_pool, "borea-counts-big")

    small_admin = await _seed_user(admin_pool, small_id, "a@small.test", ["admin"])
    await _seed_section(admin_pool, small_id, "hr", small_admin)

    big_admin = await _seed_user(admin_pool, big_id, "a@big.test", ["admin"])
    await _seed_user(admin_pool, big_id, "b@big.test", ["hr"])
    await _seed_user(admin_pool, big_id, "c@big.test", ["hr"])
    await _seed_section(admin_pool, big_id, "hr", big_admin)
    await _seed_section(admin_pool, big_id, "finance", big_admin)

    token = _set_session(tenant_id=system_tenant_id, user=f"counts-{SUPERADMIN_EMAIL}", system_roles=["superadmin"])
    try:
        async with _simulate_request_connection():
            companies = await list_companies()
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    by_id = {c.tenant_id: c for c in companies}
    assert (by_id[str(small_id)].user_count, by_id[str(small_id)].section_count) == (1, 1)
    assert (by_id[str(big_id)].user_count, by_id[str(big_id)].section_count) == (3, 2)
    assert by_id[str(small_id)].is_active is True


# ---------------------------------------------------------------------------
# Nhóm 2 — đường phục hồi không mở thêm cửa nào
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("caller_roles", [["admin"], ["hr"], []])
async def test_non_superadmin_cannot_reach_any_company_route(admin_pool: Pool, caller_roles: list[str]) -> None:
    """4 route đều nhận `tenant_id` TRÊN URL — ngoại lệ có chủ đích với INV-1, và chỉ hợp lệ CHỪNG
    NÀO `require_superadmin` còn đứng đó. Bài này chạy cho cả 4 route: bỏ sót `require_superadmin`
    ở đúng 1 route thôi thì company-admin đọc/sửa được MỌI công ty khác chỉ bằng cách đoán 1 UUID.

    Company-admin (`["admin"]`) là ca nguy hiểm nhất và dễ lọt nhất: `require_admin` (dùng ở các
    route quản user cũ) CHẤP NHẬN họ, nên gõ nhầm `require_admin` thay vì `require_superadmin` ở
    đây sẽ xanh mọi bài test khác."""
    company_id = await _seed_tenant(admin_pool, f"ankor-fence-{'-'.join(caller_roles) or 'norole'}")
    victim_id = await _seed_tenant(admin_pool, f"borea-fence-{'-'.join(caller_roles) or 'norole'}")
    caller_email = f"ke-goi-{'-'.join(caller_roles) or 'norole'}@ankor.test"
    await _seed_user(admin_pool, company_id, caller_email, caller_roles)
    victim_user_id = await _seed_user(admin_pool, victim_id, f"nan-nhan-{caller_email}", ["admin"])

    token = _set_session(tenant_id=company_id, user=caller_email, system_roles=caller_roles)
    try:
        for call in (
            lambda: list_company_users(str(victim_id)),
            lambda: add_company_admin(
                str(victim_id), AddCompanyAdminRequest(email="ke-cuop@x.test", password="mat-khau-du-dai")
            ),
            lambda: reset_company_user_password(
                str(victim_id), str(victim_user_id), ResetPasswordRequest(new_password="mat-khau-du-dai")
            ),
            lambda: update_company(str(victim_id), UpdateCompanyRequest(is_active=False)),
        ):
            with pytest.raises(HTTPException) as exc_info:
                async with _simulate_request_connection():
                    await call()
            assert exc_info.value.status_code == 403, f"route này để lọt {caller_roles}"
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_suspending_a_company_kills_jwt_issued_before_the_suspension(admin_pool: Pool) -> None:
    """Quyết định D2, và là bài quan trọng nhất file này.

    Chặn ở `routes/auth.py::login` KHÔNG đủ — nó chỉ chặn lần đăng nhập TIẾP THEO, còn mọi JWT cấp
    TRƯỚC lúc tạm khoá vẫn gọi lọt mọi route tới khi tự hết hạn (`jwt_expire_minutes`, mặc định 480
    phút). Đúng lỗ đã phải vá riêng 1 lần cho `core.users.is_active` (xem docstring
    `deactivate_user`). Bài này vì thế đi qua `fetch_fresh_identity` — cửa mà mọi route admin/
    sections/agents/runs/publish thật sự đi qua — chứ KHÔNG qua `login`."""
    system_tenant_id = await _seed_superadmin(admin_pool, "suspend")
    company_id = await _seed_tenant(admin_pool, "ankor-suspend")
    await _seed_user(admin_pool, company_id, "boss@ankor-suspend.test", ["admin"])

    # Trước khi tạm khoá: JWT của admin công ty đi qua bình thường (đối chứng — không có nó, bài
    # này xanh cả khi `fetch_fresh_identity` từ chối MỌI người).
    async with admin_pool.connection() as conn:
        identity = await fetch_fresh_identity(conn, "boss@ankor-suspend.test")
    assert identity.system_roles == ["admin"]

    token = _set_session(tenant_id=system_tenant_id, user=f"suspend-{SUPERADMIN_EMAIL}", system_roles=["superadmin"])
    try:
        async with _simulate_request_connection():
            updated = await update_company(str(company_id), UpdateCompanyRequest(is_active=False))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]
    assert updated.is_active is False

    # JWT CŨ (cấp trước lúc tạm khoá) — không đăng nhập lại, không đổi gì phía client.
    async with admin_pool.connection() as conn:
        with pytest.raises(HTTPException) as exc_info:
            await fetch_fresh_identity(conn, "boss@ankor-suspend.test")
    assert exc_info.value.status_code == 403


async def test_suspending_a_company_also_blocks_a_fresh_login(admin_pool: Pool) -> None:
    """Nửa thứ hai của D2 — người của công ty bị tạm khoá cũng không đăng nhập MỚI được. Không có
    nửa này, họ vẫn lấy được JWT rồi mới bị 403 ở request kế tiếp: đúng về mặt an toàn nhưng UI
    hiện ra là "đăng nhập thành công rồi mọi thứ đều lỗi", không đọc được."""
    company_id = await _seed_tenant(admin_pool, "ankor-suspend-login", is_active=False)
    await _seed_user(
        admin_pool, company_id, "boss@ankor-suspend-login.test", ["admin"], password=hash_password("mat-khau-dung")
    )

    async with _simulate_request_connection():
        with pytest.raises(HTTPException) as exc_info:
            await login(LoginRequest(email="boss@ankor-suspend-login.test", password="mat-khau-dung"), _fake_request())
    # Cùng 401 + cùng thông điệp với "sai mật khẩu"/"không tồn tại" — không mở oracle mới cho phép
    # dò xem 1 email có tồn tại ở công ty đang bị tạm khoá hay không.
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Email hoặc mật khẩu không đúng."


async def test_reactivating_a_company_restores_access(admin_pool: Pool) -> None:
    """Đối chứng bắt buộc cho 2 bài trên — tạm khoá phải ĐẢO NGƯỢC được. Thiếu bài này, một bản vá
    quá tay (vd `fetch_fresh_identity` từ chối mọi tenant, hay `update_company` chỉ ghi được
    `false`) vẫn xanh hết."""
    system_tenant_id = await _seed_superadmin(admin_pool, "restore")
    company_id = await _seed_tenant(admin_pool, "ankor-restore", is_active=False)
    await _seed_user(admin_pool, company_id, "boss@ankor-restore.test", ["admin"])

    async with admin_pool.connection() as conn:
        with pytest.raises(HTTPException):
            await fetch_fresh_identity(conn, "boss@ankor-restore.test")

    token = _set_session(tenant_id=system_tenant_id, user=f"restore-{SUPERADMIN_EMAIL}", system_roles=["superadmin"])
    try:
        async with _simulate_request_connection():
            await update_company(str(company_id), UpdateCompanyRequest(is_active=True))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    async with admin_pool.connection() as conn:
        identity = await fetch_fresh_identity(conn, "boss@ankor-restore.test")
    assert identity.system_roles == ["admin"]


async def test_reset_password_kills_jwt_issued_before_the_reset(admin_pool: Pool) -> None:
    """Reset mật khẩu xảy ra ĐÚNG lúc nghi tài khoản bị chiếm — nếu phiên cũ vẫn sống tới 480 phút
    thì thao tác này gần như vô nghĩa. Route phải ghi `password_changed_at = now()` cùng lúc ghi
    hash mới, để `fetch_fresh_identity` loại được JWT ký trước đó.

    Mô phỏng "JWT cũ" bằng ContextVar `_request_token_issued_at` thay vì ký token thật/chờ đồng hồ
    trôi — cùng cách `test_routes_auth.py` đã dùng cho `change_own_password`."""
    system_tenant_id = await _seed_superadmin(admin_pool, "kill")
    company_id = await _seed_tenant(admin_pool, "ankor-kill")
    user_id = await _seed_user(
        admin_pool, company_id, "bi-chiem@ankor-kill.test", ["admin"], password=hash_password("mat-khau-cu")
    )

    token = _set_session(tenant_id=system_tenant_id, user=f"kill-{SUPERADMIN_EMAIL}", system_roles=["superadmin"])
    try:
        async with _simulate_request_connection():
            await reset_company_user_password(
                str(company_id), str(user_id), ResetPasswordRequest(new_password="mat-khau-moi")
            )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    stale_iat = datetime.now(UTC) - timedelta(hours=1)
    iat_token = middleware._request_token_issued_at.set(stale_iat)
    try:
        async with admin_pool.connection() as conn:
            with pytest.raises(HTTPException) as exc_info:
                await fetch_fresh_identity(conn, "bi-chiem@ankor-kill.test")
        assert exc_info.value.status_code == 401
    finally:
        middleware._request_token_issued_at.reset(iat_token)


async def test_superadmin_cannot_touch_the_system_tenant(admin_pool: Pool) -> None:
    """`_resolve_company` loại `__system__`. Không loại, superadmin tự tạm khoá được tenant của
    CHÍNH MÌNH — và vì `fetch_fresh_identity` giờ kiểm `core.tenants.is_active`, cú bấm đó khoá
    cứng MỌI superadmin ra khỏi hệ thống ngay lập tức, không route nào mở lại được."""
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES ('__system__') RETURNING id")
        row = await cur.fetchone()
    assert row is not None
    system_tenant_id = UUID(str(row[0]))
    await _seed_user(admin_pool, system_tenant_id, f"system-{SUPERADMIN_EMAIL}", ["superadmin"])

    token = _set_session(tenant_id=system_tenant_id, user=f"system-{SUPERADMIN_EMAIL}", system_roles=["superadmin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await update_company(str(system_tenant_id), UpdateCompanyRequest(is_active=False))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 404


async def test_malformed_tenant_id_returns_400_not_500(admin_pool: Pool) -> None:
    """Chuỗi không phải UUID đi thẳng vào `WHERE id = %s` (cột UUID) làm psycopg raise lỗi cú pháp
    chưa bắt ⇒ 500 thay vì 400 — bug đã sửa 1 lần ở `routes/documents.py` (review app#27 finding
    #2) và `routes/sections.py` đã theo khuôn. Route mới không được lặp lại lần thứ ba."""
    system_tenant_id = await _seed_superadmin(admin_pool, "badid")

    token = _set_session(tenant_id=system_tenant_id, user=f"badid-{SUPERADMIN_EMAIL}", system_roles=["superadmin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await list_company_users("khong-phai-uuid")
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 400


async def test_unknown_tenant_id_returns_404(admin_pool: Pool) -> None:
    """UUID hợp lệ nhưng không có công ty nào — 404, không phải 500 hay 200-với-danh-sách-rỗng
    (rỗng sẽ đọc như "công ty này chưa có ai", sai hoàn toàn so với "công ty này không tồn tại")."""
    system_tenant_id = await _seed_superadmin(admin_pool, "notfound")

    token = _set_session(tenant_id=system_tenant_id, user=f"notfound-{SUPERADMIN_EMAIL}", system_roles=["superadmin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await list_company_users(str(uuid4()))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 404


async def test_rename_company_to_an_existing_name_returns_409_not_500(admin_pool: Pool) -> None:
    """`core.tenants.name` UNIQUE — đổi tên trùng phải là 409 đọc được."""
    system_tenant_id = await _seed_superadmin(admin_pool, "conflict")
    await _seed_tenant(admin_pool, "Ten Da Co")
    company_id = await _seed_tenant(admin_pool, "Ten Sap Doi")

    token = _set_session(tenant_id=system_tenant_id, user=f"conflict-{SUPERADMIN_EMAIL}", system_roles=["superadmin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await update_company(str(company_id), UpdateCompanyRequest(name="Ten Da Co"))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 409


async def test_patch_company_with_no_field_returns_400(admin_pool: Pool) -> None:
    """`PATCH {}` gần như luôn là bug phía client (quên map field) — trả 400 để nó vỡ ra ngay thay
    vì "200 nhưng chẳng đổi gì"."""
    system_tenant_id = await _seed_superadmin(admin_pool, "empty")
    company_id = await _seed_tenant(admin_pool, "ankor-empty-patch")

    token = _set_session(tenant_id=system_tenant_id, user=f"empty-{SUPERADMIN_EMAIL}", system_roles=["superadmin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await update_company(str(company_id), UpdateCompanyRequest())
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 400


async def test_reset_password_cannot_target_a_user_of_another_company(admin_pool: Pool) -> None:
    """`tenant_id` và `user_id` đều do client khai trên URL — cặp không khớp phải 404. Thiếu kiểm
    này, superadmin (hoặc bug phía UI) đặt lại mật khẩu nhầm người ở công ty khác mà không ai biết."""
    system_tenant_id = await _seed_superadmin(admin_pool, "cross")
    company_id = await _seed_tenant(admin_pool, "ankor-cross")
    other_id = await _seed_tenant(admin_pool, "borea-cross")
    outsider_id = await _seed_user(admin_pool, other_id, "nguoi-ngoai@borea-cross.test", ["admin"])

    token = _set_session(tenant_id=system_tenant_id, user=f"cross-{SUPERADMIN_EMAIL}", system_roles=["superadmin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await reset_company_user_password(
                    str(company_id), str(outsider_id), ResetPasswordRequest(new_password="mat-khau-du-dai")
                )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 404


async def test_superadmin_deactivates_a_user_of_a_company_they_do_not_belong_to(admin_pool: Pool) -> None:
    """`deactivate_user` (route cũ) scope tenant NGƯỜI GỌI nên superadmin nhận 404 cho mọi user của
    công ty thật. Ca dùng: admin công ty nghỉ việc, công ty không còn ai thu tài khoản của họ."""
    system_tenant_id = await _seed_superadmin(admin_pool, "deact")
    company_id = await _seed_tenant(admin_pool, "ankor-deact")
    await _seed_user(admin_pool, company_id, "boss@ankor-deact.test", ["admin"])
    leaver_id = await _seed_user(admin_pool, company_id, "nghi-viec@ankor-deact.test", ["hr"])

    token = _set_session(tenant_id=system_tenant_id, user=f"deact-{SUPERADMIN_EMAIL}", system_roles=["superadmin"])
    try:
        async with _simulate_request_connection():
            result = await deactivate_company_user(str(company_id), str(leaver_id))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert result.is_active is False

    # Phiên đang mở phải chết ngay, không đợi JWT hết hạn — `fetch_fresh_identity` là cửa mọi route
    # admin/sections/agents/runs/publish đi qua.
    async with admin_pool.connection() as conn:
        with pytest.raises(HTTPException) as exc_info:
            await fetch_fresh_identity(conn, "nghi-viec@ankor-deact.test")
    assert exc_info.value.status_code == 403


async def test_deactivate_then_reactivate_restores_the_account(admin_pool: Pool) -> None:
    """Đối chứng bắt buộc — thiếu nó, một bản cài đặt chỉ-ghi-`false` vẫn xanh bài trên."""
    system_tenant_id = await _seed_superadmin(admin_pool, "react")
    company_id = await _seed_tenant(admin_pool, "ankor-react")
    await _seed_user(admin_pool, company_id, "boss@ankor-react.test", ["admin"])
    user_id = await _seed_user(admin_pool, company_id, "nv@ankor-react.test", ["hr"])

    token = _set_session(tenant_id=system_tenant_id, user=f"react-{SUPERADMIN_EMAIL}", system_roles=["superadmin"])
    try:
        async with _simulate_request_connection():
            await deactivate_company_user(str(company_id), str(user_id))
        async with _simulate_request_connection():
            result = await reactivate_company_user(str(company_id), str(user_id))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert result.is_active is True
    async with admin_pool.connection() as conn:
        identity = await fetch_fresh_identity(conn, "nv@ankor-react.test")
    assert identity.system_roles == ["hr"]


async def test_cannot_deactivate_the_LAST_active_admin_of_a_company(admin_pool: Pool) -> None:
    """Chốt quan trọng nhất của cặp route này.

    Không có nó, chính nhóm route dựng ra để gỡ ca "công ty mất admin" lại trở thành cách nhanh
    nhất tạo ra nó — và ca đó không tự gỡ được: `add_company_admin` cần superadmin nhớ ra mà gọi."""
    system_tenant_id = await _seed_superadmin(admin_pool, "last")
    company_id = await _seed_tenant(admin_pool, "ankor-last-admin")
    sole_admin = await _seed_user(admin_pool, company_id, "admin-duy-nhat@ankor-last.test", ["admin"])
    await _seed_user(admin_pool, company_id, "nhan-vien@ankor-last.test", ["hr"])

    token = _set_session(tenant_id=system_tenant_id, user=f"last-{SUPERADMIN_EMAIL}", system_roles=["superadmin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await deactivate_company_user(str(company_id), str(sole_admin))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 409

    async with admin_pool.connection() as conn:
        cur = await conn.execute("SELECT is_active FROM core.users WHERE id = %s", (str(sole_admin),))
        row = await cur.fetchone()
    assert row is not None and row[0] is True, "admin cuối cùng bị vô hiệu hoá dù route đã ném 409"


async def test_can_deactivate_an_admin_when_another_active_admin_remains(admin_pool: Pool) -> None:
    """Vế đối chứng của bài trên: chốt chỉ chặn admin **cuối cùng**, không chặn mọi admin. Thiếu
    bài này, một bản vá quá tay (cấm vô hiệu hoá mọi admin) vẫn xanh."""
    system_tenant_id = await _seed_superadmin(admin_pool, "two")
    company_id = await _seed_tenant(admin_pool, "ankor-two-admins")
    first_admin = await _seed_user(admin_pool, company_id, "admin1@ankor-two.test", ["admin"])
    await _seed_user(admin_pool, company_id, "admin2@ankor-two.test", ["admin"])

    token = _set_session(tenant_id=system_tenant_id, user=f"two-{SUPERADMIN_EMAIL}", system_roles=["superadmin"])
    try:
        async with _simulate_request_connection():
            result = await deactivate_company_user(str(company_id), str(first_admin))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert result.is_active is False


async def test_an_already_inactive_admin_does_not_count_as_the_last_one(admin_pool: Pool) -> None:
    """Phép đếm phải tính admin **đang hoạt động**, không phải mọi admin.

    Công ty có 2 admin nhưng 1 đã bị vô hiệu hoá ⇒ người còn lại LÀ người cuối cùng, phải chặn.
    Đếm nhầm sang `count(*)` không lọc `is_active` sẽ cho qua và khoá cứng công ty."""
    system_tenant_id = await _seed_superadmin(admin_pool, "ghost")
    company_id = await _seed_tenant(admin_pool, "ankor-ghost-admin")
    working_admin = await _seed_user(admin_pool, company_id, "dang-lam@ankor-ghost.test", ["admin"])
    retired_admin = await _seed_user(admin_pool, company_id, "da-nghi@ankor-ghost.test", ["admin"])
    async with admin_pool.connection() as conn:
        await conn.execute("UPDATE core.users SET is_active = false WHERE id = %s", (str(retired_admin),))

    token = _set_session(tenant_id=system_tenant_id, user=f"ghost-{SUPERADMIN_EMAIL}", system_roles=["superadmin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await deactivate_company_user(str(company_id), str(working_admin))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 409
