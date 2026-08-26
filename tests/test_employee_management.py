"""`routes/admin.py` — admin công ty vận hành đội ngũ của chính mình (app#76).

Trước nhóm route này, admin tạo được tài khoản nhưng gần như không sửa được gì sau đó. Hai ca đắt
nhất, và cũng là hai ca file này canh kỹ nhất:

- **Nhân viên quên mật khẩu** ⇒ phải nhờ superadmin, tức việc nội bộ của công ty leo ra ngoài.
- **Gõ sai email lúc tạo** ⇒ tài khoản hỏng vĩnh viễn, và vì `core.users.email` UNIQUE toàn hệ
  thống nên địa chỉ đúng cũng bị chiếm chỗ.

Nhóm bài thứ hai canh chiều ngược lại: các đường mới **không** được lỏng hơn đường cũ. Ba chốt
chống-tự-khoá (tự reset mật khẩu mình, tự sửa role mình, tự đổi email mình) và chốt admin-cuối-cùng
đều nằm ở đây — chúng là loại lỗi mà test không bắt thì chỉ người dùng thật phát hiện, lúc đã khoá.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import HTTPException
from starlette.requests import Request
from studio_app import middleware, rate_limit
from studio_app.authz import fetch_fresh_identity
from studio_app.core._db import Pool, close_pools, get_pool
from studio_app.jwt_auth import hash_password, verify_password
from studio_app.routes.admin import (
    ResetEmployeePasswordRequest,
    UpdateUserRolesRequest,
    grant_admin,
    list_users,
    reset_employee_password,
    revoke_admin,
    update_user_roles,
)
from studio_app.routes.auth import ChangePasswordRequest, LoginRequest, change_own_password, login
from studio_workbench.tenant_wall import ResolvedContext


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    yield
    await close_pools()


@pytest_asyncio.fixture(autouse=True)
async def _reset_rate_limiter() -> AsyncIterator[None]:
    rate_limit.reset_all()
    yield


def _fake_request() -> Request:
    return Request(scope={"type": "http", "client": ("test-client", 0), "headers": []})


def _set_session(*, tenant_id: UUID, user: str, system_roles: list[str]) -> object:
    session = ResolvedContext(tenant_id=tenant_id, user=user, system_roles=system_roles)
    return middleware._request_session.set(session)


@asynccontextmanager
async def _simulate_request_connection() -> AsyncIterator[None]:
    pool = await get_pool()
    async with pool.connection() as conn:
        token = middleware._request_conn.set(conn)
        try:
            yield
        finally:
            middleware._request_conn.reset(token)


async def _seed_tenant(admin_pool: Pool, name: str) -> UUID:
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", (name,))
        row = await cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


async def _seed_user(
    admin_pool: Pool,
    tenant_id: UUID,
    email: str,
    system_roles: list[str],
    *,
    password: str = "not-a-real-hash",
) -> UUID:
    async with admin_pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, system_roles) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
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


# ---------------------------------------------------------------------------
# Đường vận hành có thật
# ---------------------------------------------------------------------------


async def test_admin_resets_an_employee_password_and_that_password_logs_in(admin_pool: Pool) -> None:
    """Ca gốc của app#76. Đi qua `login()` THẬT chứ không chỉ so hash — hash đúng mà `login` vẫn từ
    chối (vd vì `is_active`, vì tenant) thì đường phục hồi vẫn gãy."""
    tenant_id = await _seed_tenant(admin_pool, "ankor-reset-emp")
    admin_id = await _seed_user(admin_pool, tenant_id, "boss@ankor-reset-emp.test", ["admin"])
    await _seed_section(admin_pool, tenant_id, "hr", admin_id)
    await _seed_user(admin_pool, tenant_id, "quen@ankor-reset-emp.test", ["hr"], password=hash_password("mat-khau-cu"))

    token = _set_session(tenant_id=tenant_id, user="boss@ankor-reset-emp.test", system_roles=["admin"])
    try:
        async with _simulate_request_connection():
            users = await list_users()
            target = next(u for u in users if u.email == "quen@ankor-reset-emp.test")
        async with _simulate_request_connection():
            await reset_employee_password(target.user_id, ResetEmployeePasswordRequest(new_password="mat-khau-moi"))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    async with _simulate_request_connection():
        result = await login(LoginRequest(email="quen@ankor-reset-emp.test", password="mat-khau-moi"), _fake_request())
    assert result.user == "quen@ankor-reset-emp.test"
    # Admin gõ mật khẩu hộ, nên chính chủ phải bị buộc đổi ở lần đăng nhập này (quyết định D1).
    assert result.must_change_password is True


async def test_self_service_password_change_clears_the_must_change_flag(admin_pool: Pool) -> None:
    """Cờ phải TẮT khi chính chủ tự đổi — nếu không, người dùng bị hỏi đổi mật khẩu mãi mãi và cờ
    trở thành phiền nhiễu thay vì hàng rào."""
    tenant_id = await _seed_tenant(admin_pool, "ankor-clear-flag")
    admin_id = await _seed_user(admin_pool, tenant_id, "boss@ankor-clear.test", ["admin"])
    await _seed_section(admin_pool, tenant_id, "hr", admin_id)
    await _seed_user(admin_pool, tenant_id, "nv@ankor-clear.test", ["hr"], password=hash_password("mat-khau-cu"))

    token = _set_session(tenant_id=tenant_id, user="boss@ankor-clear.test", system_roles=["admin"])
    try:
        async with _simulate_request_connection():
            users = await list_users()
            target = next(u for u in users if u.email == "nv@ankor-clear.test")
        async with _simulate_request_connection():
            await reset_employee_password(target.user_id, ResetEmployeePasswordRequest(new_password="admin-dat-ho"))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    token = _set_session(tenant_id=tenant_id, user="nv@ankor-clear.test", system_roles=["hr"])
    try:
        async with _simulate_request_connection():
            await change_own_password(
                ChangePasswordRequest(old_password="admin-dat-ho", new_password="mat-khau-rieng-cua-toi")
            )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    async with _simulate_request_connection():
        result = await login(
            LoginRequest(email="nv@ankor-clear.test", password="mat-khau-rieng-cua-toi"), _fake_request()
        )
    assert result.must_change_password is False


async def test_admin_fixes_a_mistyped_employee_email(admin_pool: Pool) -> None:
    """`core.users.email` UNIQUE toàn hệ thống ⇒ tài khoản mang email sai vừa vô dụng vừa chiếm mất
    địa chỉ đúng. Bài này ghim cả hai vế: sửa được, VÀ sau khi sửa thì đăng nhập bằng địa chỉ đúng
    chạy thật."""
    tenant_id = await _seed_tenant(admin_pool, "ankor-fix-email")
    admin_id = await _seed_user(admin_pool, tenant_id, "boss@ankor-fix.test", ["admin"])
    await _seed_section(admin_pool, tenant_id, "hr", admin_id)
    typo_id = await _seed_user(
        admin_pool, tenant_id, "nv.gosai@ankor-fix.test", ["hr"], password=hash_password("mat-khau-du-dai")
    )

    token = _set_session(tenant_id=tenant_id, user="boss@ankor-fix.test", system_roles=["admin"])
    try:
        async with _simulate_request_connection():
            updated = await update_user_roles(str(typo_id), UpdateUserRolesRequest(email="nv.dung@ankor-fix.test"))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert updated.email == "nv.dung@ankor-fix.test"
    async with _simulate_request_connection():
        result = await login(LoginRequest(email="nv.dung@ankor-fix.test", password="mat-khau-du-dai"), _fake_request())
    assert result.user == "nv.dung@ankor-fix.test"


async def test_display_name_can_be_set_and_cleared(admin_pool: Pool) -> None:
    """`display_name` tuỳ chọn (D3) — đặt được, và **xoá được** bằng chuỗi rỗng. Không có vế xoá
    thì một cái tên gõ nhầm là vĩnh viễn."""
    tenant_id = await _seed_tenant(admin_pool, "ankor-name")
    admin_id = await _seed_user(admin_pool, tenant_id, "boss@ankor-name.test", ["admin"])
    await _seed_section(admin_pool, tenant_id, "hr", admin_id)
    user_id = await _seed_user(admin_pool, tenant_id, "nv@ankor-name.test", ["hr"])

    token = _set_session(tenant_id=tenant_id, user="boss@ankor-name.test", system_roles=["admin"])
    try:
        async with _simulate_request_connection():
            named = await update_user_roles(str(user_id), UpdateUserRolesRequest(display_name="Nguyễn Thị Thu"))
        async with _simulate_request_connection():
            cleared = await update_user_roles(str(user_id), UpdateUserRolesRequest(display_name="  "))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert named.display_name == "Nguyễn Thị Thu"
    assert cleared.display_name is None, "khoảng trắng phải thành NULL, không phải một tên toàn dấu cách"


async def test_last_login_at_is_recorded_only_after_a_SUCCESSFUL_login(admin_pool: Pool) -> None:
    """`NULL` = chưa từng đăng nhập, khác hẳn "đăng nhập lâu rồi" — đó là lý do cột này tồn tại
    (rà tài khoản bỏ hoang lúc offboard).

    Vế thứ hai quan trọng hơn: đăng nhập SAI **không** được ghi. Ghi cho cả lần sai biến cột này
    thành oracle — kẻ dò chỉ cần so mốc trước/sau là biết email nào tồn tại, phá đúng nguyên tắc mà
    mọi nhánh 401 của `login` đang giữ."""
    tenant_id = await _seed_tenant(admin_pool, "ankor-lastlogin")
    admin_id = await _seed_user(admin_pool, tenant_id, "boss@ankor-lastlogin.test", ["admin"])
    await _seed_section(admin_pool, tenant_id, "hr", admin_id)
    await _seed_user(admin_pool, tenant_id, "nv@ankor-lastlogin.test", ["hr"], password=hash_password("dung-mat-khau"))

    token = _set_session(tenant_id=tenant_id, user="boss@ankor-lastlogin.test", system_roles=["admin"])
    try:
        async with _simulate_request_connection():
            before = next(u for u in await list_users() if u.email == "nv@ankor-lastlogin.test")
        assert before.last_login_at is None, "chưa đăng nhập lần nào mà đã có mốc"

        async with _simulate_request_connection():
            with pytest.raises(HTTPException):
                await login(LoginRequest(email="nv@ankor-lastlogin.test", password="sai-be-bét"), _fake_request())
        async with _simulate_request_connection():
            after_failure = next(u for u in await list_users() if u.email == "nv@ankor-lastlogin.test")
        assert after_failure.last_login_at is None, "đăng nhập SAI cũng ghi mốc — cột này thành oracle dò email"

        async with _simulate_request_connection():
            await login(LoginRequest(email="nv@ankor-lastlogin.test", password="dung-mat-khau"), _fake_request())
        async with _simulate_request_connection():
            after_success = next(u for u in await list_users() if u.email == "nv@ankor-lastlogin.test")
        assert after_success.last_login_at is not None
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_admin_grants_and_revokes_admin_rights(admin_pool: Pool) -> None:
    """Phong/thu quyền quản trị là hành động RIÊNG, không phải một ô tick lẫn giữa các phòng ban —
    người được phong quản toàn bộ tài khoản của công ty."""
    tenant_id = await _seed_tenant(admin_pool, "ankor-grant")
    admin_id = await _seed_user(admin_pool, tenant_id, "boss@ankor-grant.test", ["admin"])
    await _seed_section(admin_pool, tenant_id, "hr", admin_id)
    user_id = await _seed_user(admin_pool, tenant_id, "nv@ankor-grant.test", ["hr"])

    token = _set_session(tenant_id=tenant_id, user="boss@ankor-grant.test", system_roles=["admin"])
    try:
        async with _simulate_request_connection():
            granted = await grant_admin(str(user_id))
        assert "admin" in granted.system_roles
        # Gọi lần hai phải là no-op, không nhét `"admin"` hai lần vào mảng.
        async with _simulate_request_connection():
            again = await grant_admin(str(user_id))
        assert again.system_roles.count("admin") == 1

        async with _simulate_request_connection():
            revoked = await revoke_admin(str(user_id))
        assert "admin" not in revoked.system_roles
        assert "hr" in revoked.system_roles, "thu quyền quản trị không được xoá luôn phòng ban"
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Đường mới không được lỏng hơn đường cũ
# ---------------------------------------------------------------------------


async def test_admin_cannot_reset_their_OWN_password_through_this_route(admin_pool: Pool) -> None:
    """Chốt dễ bị coi là thừa nhất, và là chốt quan trọng nhất file.

    `change_own_password` đòi `old_password` chính là để một JWT bị đánh cắp KHÔNG đổi được mật
    khẩu (kẻ trộm có token nhưng không có mật khẩu cũ). Cho admin tự gọi route này là mở lại đúng
    cửa đó: token đánh cắp của một admin sẽ đổi được mật khẩu mà không cần biết gì thêm, và chủ tài
    khoản mất quyền vào chính công ty mình."""
    tenant_id = await _seed_tenant(admin_pool, "ankor-self-reset")
    admin_id = await _seed_user(admin_pool, tenant_id, "boss@ankor-self.test", ["admin"])

    token = _set_session(tenant_id=tenant_id, user="boss@ankor-self.test", system_roles=["admin"])
    try:
        with pytest.raises(HTTPException) as raised:
            async with _simulate_request_connection():
                await reset_employee_password(str(admin_id), ResetEmployeePasswordRequest(new_password="mat-khau-moi"))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert raised.value.status_code == 400


async def test_admin_cannot_change_their_OWN_email(admin_pool: Pool) -> None:
    """Email là định danh đăng nhập: gõ nhầm là tự khoá mình ra ngoài, và không ai TRONG công ty gỡ
    được — phải nhờ superadmin cấp tài khoản admin mới."""
    tenant_id = await _seed_tenant(admin_pool, "ankor-self-email")
    admin_id = await _seed_user(admin_pool, tenant_id, "boss@ankor-self-email.test", ["admin"])

    token = _set_session(tenant_id=tenant_id, user="boss@ankor-self-email.test", system_roles=["admin"])
    try:
        with pytest.raises(HTTPException) as raised:
            async with _simulate_request_connection():
                await update_user_roles(str(admin_id), UpdateUserRolesRequest(email="doi@ankor-self-email.test"))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert raised.value.status_code == 400


async def test_changing_an_email_to_one_that_exists_returns_409_not_500(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "ankor-dup-email")
    admin_id = await _seed_user(admin_pool, tenant_id, "boss@ankor-dup.test", ["admin"])
    await _seed_section(admin_pool, tenant_id, "hr", admin_id)
    await _seed_user(admin_pool, tenant_id, "da-co@ankor-dup.test", ["hr"])
    other_id = await _seed_user(admin_pool, tenant_id, "nguoi-khac@ankor-dup.test", ["hr"])

    token = _set_session(tenant_id=tenant_id, user="boss@ankor-dup.test", system_roles=["admin"])
    try:
        with pytest.raises(HTTPException) as raised:
            async with _simulate_request_connection():
                await update_user_roles(str(other_id), UpdateUserRolesRequest(email="da-co@ankor-dup.test"))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert raised.value.status_code == 409


async def test_the_company_can_never_be_left_with_zero_admins(admin_pool: Pool) -> None:
    """Bất biến: công ty luôn còn ít nhất một admin đang hoạt động.

    Giữ nó là **một** chốt duy nhất — cấm tự thu quyền của chính mình. Bản đầu tôi viết thêm một
    chốt 409 "không để còn 0 admin"; đo lại thì nhánh đó không thể chạm tới (người gọi luôn là một
    admin đang hoạt động của chính tenant, nên khi thu quyền người KHÁC thì luôn còn ít nhất người
    gọi). Mutant xoá hẳn nhánh đó sống sót toàn bộ 41 bài ⇒ đã gỡ.

    Bài này ghim bất biến bằng đúng hai bước có thể dẫn tới 0: thu quyền người khác, rồi thu quyền
    chính mình."""
    tenant_id = await _seed_tenant(admin_pool, "ankor-zero-admins")
    boss_id = await _seed_user(admin_pool, tenant_id, "boss@ankor-zero.test", ["admin"])
    await _seed_section(admin_pool, tenant_id, "hr", boss_id)
    other_admin = await _seed_user(admin_pool, tenant_id, "admin2@ankor-zero.test", ["admin", "hr"])

    token = _set_session(tenant_id=tenant_id, user="boss@ankor-zero.test", system_roles=["admin"])
    try:
        # Bước 1 — thu quyền admin CÒN LẠI: hợp lệ, công ty còn đúng `boss`.
        async with _simulate_request_connection():
            stripped = await revoke_admin(str(other_admin))
        assert "admin" not in stripped.system_roles

        # Bước 2 — `boss` giờ là admin duy nhất, và không có đường nào để chính họ bỏ quyền.
        async with _simulate_request_connection():
            with pytest.raises(HTTPException) as raised:
                await revoke_admin(str(boss_id))
        assert raised.value.status_code == 400
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    async with admin_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM core.users WHERE tenant_id = %s AND is_active AND 'admin' = ANY(system_roles)",
            (str(tenant_id),),
        )
        row = await cur.fetchone()
    assert row is not None and row[0] >= 1, "công ty còn 0 admin đang hoạt động"


async def test_cannot_revoke_admin_from_yourself(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "ankor-self-revoke")
    boss_id = await _seed_user(admin_pool, tenant_id, "boss@ankor-sr.test", ["admin"])
    await _seed_user(admin_pool, tenant_id, "admin2@ankor-sr.test", ["admin"])

    token = _set_session(tenant_id=tenant_id, user="boss@ankor-sr.test", system_roles=["admin"])
    try:
        with pytest.raises(HTTPException) as raised:
            async with _simulate_request_connection():
                await revoke_admin(str(boss_id))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert raised.value.status_code == 400


@pytest.mark.parametrize("caller_roles", [["hr"], []])
async def test_non_admin_cannot_reach_any_employee_route(admin_pool: Pool, caller_roles: list[str]) -> None:
    """Hàng rào role trên cả bốn đường mới. Bỏ sót `require_admin` ở đúng một route thôi thì một
    nhân viên thường đặt lại được mật khẩu của sếp mình."""
    label = "-".join(caller_roles) or "norole"
    tenant_id = await _seed_tenant(admin_pool, f"ankor-fence-emp-{label}")
    caller_email = f"nv-{label}@ankor-fence.test"
    await _seed_user(admin_pool, tenant_id, caller_email, caller_roles)
    victim_id = await _seed_user(admin_pool, tenant_id, f"nan-nhan-{label}@ankor-fence.test", ["admin"])

    token = _set_session(tenant_id=tenant_id, user=caller_email, system_roles=caller_roles)
    try:
        for call in (
            lambda: reset_employee_password(str(victim_id), ResetEmployeePasswordRequest(new_password="mat-khau-du")),
            lambda: update_user_roles(str(victim_id), UpdateUserRolesRequest(display_name="đổi trộm")),
            lambda: grant_admin(str(victim_id)),
            lambda: revoke_admin(str(victim_id)),
        ):
            with pytest.raises(HTTPException) as raised:
                async with _simulate_request_connection():
                    await call()
            assert raised.value.status_code == 403, f"route này để lọt {caller_roles}"
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_admin_cannot_touch_an_employee_of_another_company(admin_pool: Pool) -> None:
    """Hàng rào tenant. 404 chứ không 403 — không xác nhận/phủ nhận id đó tồn tại ở công ty khác."""
    tenant_a = await _seed_tenant(admin_pool, "ankor-cross-emp")
    tenant_b = await _seed_tenant(admin_pool, "borea-cross-emp")
    await _seed_user(admin_pool, tenant_a, "boss@ankor-cross.test", ["admin"])
    outsider = await _seed_user(admin_pool, tenant_b, "nguoi-ngoai@borea-cross.test", ["admin"])

    token = _set_session(tenant_id=tenant_a, user="boss@ankor-cross.test", system_roles=["admin"])
    try:
        with pytest.raises(HTTPException) as raised:
            async with _simulate_request_connection():
                await reset_employee_password(
                    str(outsider), ResetEmployeePasswordRequest(new_password="mat-khau-du-dai")
                )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert raised.value.status_code == 404


async def test_reset_password_kills_sessions_issued_before_it(admin_pool: Pool) -> None:
    """Admin đặt lại mật khẩu thường xảy ra đúng lúc nghi tài khoản bị chiếm — để hở phiên cũ tới
    480 phút thì thao tác gần như vô nghĩa. Ghim qua `fetch_fresh_identity`, cửa mọi route đi qua."""
    from datetime import UTC, datetime, timedelta

    tenant_id = await _seed_tenant(admin_pool, "ankor-kill-session")
    admin_id = await _seed_user(admin_pool, tenant_id, "boss@ankor-kill.test", ["admin"])
    await _seed_section(admin_pool, tenant_id, "hr", admin_id)
    victim_id = await _seed_user(
        admin_pool, tenant_id, "bi-chiem@ankor-kill.test", ["hr"], password=hash_password("mat-khau-cu")
    )

    token = _set_session(tenant_id=tenant_id, user="boss@ankor-kill.test", system_roles=["admin"])
    try:
        async with _simulate_request_connection():
            await reset_employee_password(str(victim_id), ResetEmployeePasswordRequest(new_password="mat-khau-moi"))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    stale_iat = datetime.now(UTC) - timedelta(hours=1)
    iat_token = middleware._request_token_issued_at.set(stale_iat)
    try:
        async with admin_pool.connection() as conn:
            with pytest.raises(HTTPException) as raised:
                await fetch_fresh_identity(conn, "bi-chiem@ankor-kill.test")
        assert raised.value.status_code == 401
    finally:
        middleware._request_token_issued_at.reset(iat_token)

    # Và mật khẩu mới phải là mật khẩu THẬT, không phải chuỗi thô.
    async with admin_pool.connection() as conn:
        cur = await conn.execute("SELECT password_hash FROM core.users WHERE id = %s", (str(victim_id),))
        row = await cur.fetchone()
    assert row is not None
    assert row[0] != "mat-khau-moi"
    assert verify_password("mat-khau-moi", row[0])
