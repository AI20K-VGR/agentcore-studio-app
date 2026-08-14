"""`routes/admin.py` — bất biến phân quyền của hệ thống auth thật (Kế hoạch 3, bước 4/4). Cùng
tinh thần `apps#11` (Duy) đã áp cho `_DEMO_ACCOUNTS`: mọi nơi PHÂN QUYỀN phải có test PIN cứng,
không được để "chưa ai chạm tới nên chưa ai biết sai" — bài học §6 Điều 3 VinSOC (`kit#129`):
"nguyên tắc này còn áp được cho trường nào nữa mà tôi đang bỏ sót?" áp lại đúng vào hệ thống
admin mới, không chỉ `_DEMO_ACCOUNTS` cũ.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from studio_app import middleware
from studio_app.core._db import Pool, close_pools
from studio_app.routes.admin import CreateCompanyRequest, CreateUserRequest, create_company, create_user
from studio_workbench.tenant_wall import ResolvedContext


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    yield
    await close_pools()


def _set_session(*, tenant_id: UUID, user: str, roles: list[str]) -> object:
    session = ResolvedContext(tenant_id=tenant_id, user=user, roles=roles)
    return middleware._request_session.set(session)


async def _seed_tenant(admin_pool: Pool, name: str) -> UUID:
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", (name,))
        row = await cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


async def _seed_admin_user(admin_pool: Pool, tenant_id: UUID, email: str) -> None:
    """Chèn 1 dòng `core.users` thật cho người gọi — bắt buộc từ khi vá Chặn 1 (review `app#17`):
    `create_user` giờ đòi `session.user` phải có dòng trong `core.users`, không còn chấp nhận
    `created_by = None` ngầm định như trước (đúng cửa mà JWT từ demo-login trước đây lách qua
    được). Nội dung `password_hash` không quan trọng ở đây — `create_user` không verify lại nó,
    chỉ cần dòng TỒN TẠI."""
    async with admin_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, roles) VALUES (%s, %s, %s, %s)",
            (str(tenant_id), email, "not-a-real-hash", ["admin"]),
        )


async def test_non_superadmin_cannot_create_company(admin_pool: Pool) -> None:
    """403 — chỉ superadmin mới tạo được công ty mới. Admin công ty thường (dù có role "admin")
    không được leo lên tạo tenant khác."""
    tenant_id = await _seed_tenant(admin_pool, "probe-non-superadmin")
    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            await create_company(
                CreateCompanyRequest(company_name="evil-co", admin_email="evil@evil.com", admin_password="password123")
            )
        assert exc_info.value.status_code == 403
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_company_admin_cannot_create_user_in_other_tenant(admin_pool: Pool) -> None:
    """T1 IDOR cho hệ thống admin mới: `CreateUserRequest` không có field `tenant_id` — dù
    company-admin của tenant A cố tạo user, user đó CHỈ có thể rơi vào tenant A (session), không
    có cách nào chỉ định tenant B dù có muốn."""
    tenant_a = await _seed_tenant(admin_pool, "probe-tenant-a")
    tenant_b = await _seed_tenant(admin_pool, "probe-tenant-b")
    await _seed_admin_user(admin_pool, tenant_a, "admin@acme.com")

    body = CreateUserRequest(email="new-hire@acme.com", password="password123", roles=["public"])
    assert not hasattr(body, "tenant_id")  # không có đường nào để khai — đúng như RunRequest

    token = _set_session(tenant_id=tenant_a, user="admin@acme.com", roles=["admin"])
    try:
        result = await create_user(body)
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert result.tenant_id == str(tenant_a)
    assert result.tenant_id != str(tenant_b)


async def test_company_admin_cannot_grant_superadmin_role(admin_pool: Pool) -> None:
    """Mutant leo quyền: company-admin cố tạo 1 user mang role "superadmin" — server phải chặn
    400, "superadmin" KHÔNG nằm trong _USER_ROLE_VOCAB dù người gọi có role "admin"."""
    tenant_id = await _seed_tenant(admin_pool, "probe-no-self-superadmin")
    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            await create_user(CreateUserRequest(email="wannabe@acme.com", password="password123", roles=["superadmin"]))
        assert exc_info.value.status_code == 400
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_create_user_rejects_role_outside_vocab(admin_pool: Pool) -> None:
    """Đối chứng với bài trên: không chỉ chặn riêng "superadmin", mà chặn MỌI chuỗi ngoài
    SECTION_VOCAB ∪ {"admin"} — vd lỗi gõ hoặc role tự chế không tồn tại."""
    tenant_id = await _seed_tenant(admin_pool, "probe-bad-role-string")
    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            await create_user(CreateUserRequest(email="typo@acme.com", password="password123", roles=["hrr"]))
        assert exc_info.value.status_code == 400
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_non_admin_cannot_create_user(admin_pool: Pool) -> None:
    """403 — nhân viên thường (không "admin"/"superadmin") không tự tạo được tài khoản khác,
    kể cả cho đúng tenant của mình."""
    tenant_id = await _seed_tenant(admin_pool, "probe-non-admin-create-user")
    token = _set_session(tenant_id=tenant_id, user="hr@acme.com", roles=["public", "hr"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            await create_user(CreateUserRequest(email="another@acme.com", password="password123", roles=["public"]))
        assert exc_info.value.status_code == 403
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_password_never_leaks_in_create_company_response(admin_pool: Pool) -> None:
    """`CreateCompanyResponse` không có field `admin_password`/`password_hash` — kiểm bằng
    model_dump() thay vì đọc mã bằng mắt (khoá cứng, không lệ thuộc ai đó nhớ không thêm lại)."""
    del admin_pool
    token = _set_session(tenant_id=UUID(int=0), user="su@sys", roles=["superadmin"])
    try:
        result = await create_company(
            CreateCompanyRequest(
                company_name="probe-no-leak-co", admin_email="admin@no-leak.com", admin_password="password123"
            )
        )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    dumped = result.model_dump()
    assert "admin_password" not in dumped
    assert "password_hash" not in dumped
    assert "password123" not in str(dumped)


async def test_demo_login_derived_session_cannot_mint_real_user(admin_pool: Pool) -> None:
    """Chặn 1, review `app#17`: JWT phát từ `demo-login` (registry `_DEMO_ACCOUNTS`, không mật
    khẩu) mang role "admin" y hệt `admin@ankor.vn`, nhưng KHÔNG có dòng nào trong `core.users` —
    khác hẳn admin thật (tạo qua `create_company`/`seed_superadmin.py`, LUÔN có dòng). Trước bản
    vá, `created_by` fallback về `None` và INSERT chạy tiếp — ai gọi `demo-login` xong gọi route
    này sẽ mint được tài khoản thật bền vững, mật khẩu tự chọn, kể cả role admin. Mô phỏng đúng
    session mà `demo-login` phát cho `admin@ankor.vn`, KHÔNG seed `core.users` cho email đó."""
    tenant_id = await _seed_tenant(admin_pool, "probe-demo-login-cannot-mint")
    token = _set_session(
        tenant_id=tenant_id,
        user="admin@ankor.vn",
        roles=["admin", "public", "hr", "finance", "engineering"],
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            await create_user(
                CreateUserRequest(email="minted-by-demo-login@ankor.vn", password="password123", roles=["public"])
            )
        assert exc_info.value.status_code == 403
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_superadmin_without_company_cannot_create_user() -> None:
    """Nên-sửa #3, review `app#17`: superadmin CHƯA tạo công ty nào (chỉ có role "superadmin",
    không có "admin") gọi thẳng `/api/admin/users` sẽ tạo user rơi vào tenant `__system__` một
    cách âm thầm (session.tenant_id lúc bootstrap) — chặn 400, không phải lỗ hổng nhưng là footgun
    im lặng nếu để lọt."""
    token = _set_session(tenant_id=UUID(int=0), user="su@sys", roles=["superadmin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            await create_user(CreateUserRequest(email="orphan@sys", password="password123", roles=["public"]))
        assert exc_info.value.status_code == 400
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


def test_create_user_rejects_password_over_72_bytes() -> None:
    """Chặn 2, nửa "create" — review `app#17`: bcrypt chỉ băm được tối đa 72 byte; không chặn ở
    Pydantic thì `hash_password()` raise `ValueError` không ai bắt -> 500. Phải chặn TRƯỚC route,
    ở tầng request (422), không phải 500."""
    with pytest.raises(ValidationError):
        CreateUserRequest(email="oversized@acme.com", password="a" * 73, roles=["public"])


def test_create_company_rejects_admin_password_over_72_bytes() -> None:
    with pytest.raises(ValidationError):
        CreateCompanyRequest(company_name="oversized-co", admin_email="oversized@co.com", admin_password="a" * 73)


async def test_create_company_duplicate_name_returns_409_not_500(admin_pool: Pool) -> None:
    """`core.tenants.name` có UNIQUE — trước bản vá, `create_company` không bắt `UniqueViolation`
    nên trùng tên công ty (hoặc trùng `admin_email`, `core.users.email` cũng UNIQUE) ra 500, bất
    đối xứng với `create_user` ngay cạnh đã bắt đúng 409 (review `app#17`, "nên sửa" #1)."""
    del admin_pool
    token = _set_session(tenant_id=UUID(int=0), user="su@sys", roles=["superadmin"])
    try:
        await create_company(
            CreateCompanyRequest(
                company_name="probe-dup-co", admin_email="admin1@dup.com", admin_password="password123"
            )
        )
        with pytest.raises(HTTPException) as exc_info:
            await create_company(
                CreateCompanyRequest(
                    company_name="probe-dup-co", admin_email="admin2@dup.com", admin_password="password123"
                )
            )
        assert exc_info.value.status_code == 409
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]
