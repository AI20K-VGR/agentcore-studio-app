"""`routes/admin.py` — bất biến phân quyền của hệ thống auth thật (Kế hoạch 3, bước 4/4). Cùng
tinh thần `apps#11` (Duy) đã áp cho `_DEMO_ACCOUNTS`: mọi nơi PHÂN QUYỀN phải có test PIN cứng,
không được để "chưa ai chạm tới nên chưa ai biết sai" — bài học §6 Điều 3 VinSOC (`kit#129`):
"nguyên tắc này còn áp được cho trường nào nữa mà tôi đang bỏ sót?" áp lại đúng vào hệ thống
admin mới, không chỉ `_DEMO_ACCOUNTS` cũ.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from studio_app import middleware
from studio_app.core._db import Pool, close_pools, get_pool
from studio_app.routes.admin import (
    CreateCompanyRequest,
    CreateUserRequest,
    UpdateUserRolesRequest,
    create_company,
    create_user,
    deactivate_user,
    reactivate_user,
    update_user_roles,
)
from studio_workbench.tenant_wall import ResolvedContext


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    yield
    await close_pools()


def _set_session(*, tenant_id: UUID, user: str, roles: list[str]) -> object:
    session = ResolvedContext(tenant_id=tenant_id, user=user, roles=roles)
    return middleware._request_session.set(session)


@asynccontextmanager
async def _simulate_request_connection() -> AsyncIterator[None]:
    """`create_company`/`create_user` giờ đọc/ghi `core.users`/`core.tenants` qua
    `get_request_connection()` thay vì tự mở connection riêng (review `app#17` đợt 5, Chặn B: pool
    deadlock khi 2 connection/request giẫm lên `max_size=8`) — test gọi thẳng hàm (không qua ASGI)
    phải tự set `_request_conn` contextvar trước khi gọi, cùng convention
    `test_routes_auth.py::_simulate_request_connection`. Một `async with` = một "request" thật:
    commit xảy ra lúc thoát khối `pool.connection()`, nên dữ liệu chỉ VISIBLE cho connection khác
    (vd. `admin_pool` dùng để seed/verify) SAU KHI khối này đã thoát — 2 lệnh gọi route liên tiếp
    trong 1 bài test phải bọc RIÊNG từng khối, không dùng chung 1 khối cho cả hai (khác với 1
    request HTTP thật, mỗi lệnh gọi route ở đây là 1 "request" độc lập)."""
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


async def _seed_superadmin_user(admin_pool: Pool, tenant_id: UUID, email: str) -> None:
    """Cùng lý do `_seed_admin_user` bên dưới — `create_company` giờ CŨNG đòi `session.user` có
    dòng thật trong `core.users` (review `app#17` đợt 2: route này trước đó KHÔNG có chặn này,
    nên 1 JWT superadmin còn hạn của tài khoản đã bị xoá khỏi `core.users` vẫn tạo được công ty
    mới). `tenant_id` ở đây chỉ cần THOẢ FK `core.users.tenant_id -> core.tenants(id)`, không cần
    khớp tenant `__system__` thật — bài test không kiểm tenant của superadmin."""
    async with admin_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, roles) VALUES (%s, %s, %s, %s)",
            (str(tenant_id), email, "not-a-real-hash", ["superadmin"]),
        )


async def _seed_admin_user(admin_pool: Pool, tenant_id: UUID, email: str) -> UUID:
    """Chèn 1 dòng `core.users` thật cho người gọi — bắt buộc từ khi vá Chặn 1 (review `app#17`):
    `create_user` giờ đòi `session.user` phải có dòng trong `core.users`, không còn chấp nhận
    `created_by = None` ngầm định như trước (đúng cửa mà JWT từ demo-login trước đây lách qua
    được). Nội dung `password_hash` không quan trọng ở đây — `create_user` không verify lại nó,
    chỉ cần dòng TỒN TẠI. Trả `id` — caller cũ không đọc giá trị trả về vẫn chạy đúng."""
    return await _seed_user_with_roles(admin_pool, tenant_id, email, ["admin"])


async def _seed_section(admin_pool: Pool, tenant_id: UUID, name: str) -> None:
    """Từ khi `create_user` tra vocab roles hợp lệ ĐỘNG theo `core.sections` (thay `SECTION_VOCAB`
    tĩnh cũ), mọi bài test gán role nội dung (vd `"public"`) cho user MỚI phải tự seed section đó
    trước — role không nằm trong `core.sections` của đúng tenant sẽ bị 400 ngay ở bước validate."""
    async with admin_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO core.sections (tenant_id, name, created_by) "
            "VALUES (%s, %s, (SELECT id FROM core.users WHERE tenant_id = %s LIMIT 1))",
            (str(tenant_id), name, str(tenant_id)),
        )


async def _seed_user_with_roles(admin_pool: Pool, tenant_id: UUID, email: str, roles: list[str]) -> UUID:
    """Cùng lý do `_seed_admin_user` ở trên nhưng cho roles TUỲ Ý — từ đợt 8, `create_company`/
    `create_user` tra roles TƯƠI từ `core.users` (review `app#17`, Important #1) thay vì tin
    `session.roles`, nên MỌI bài test gọi 2 hàm này (kể cả bài kỳ vọng bị chặn quyền) đều cần
    người gọi có dòng thật trong DB — không còn suy ra thẳng từ `session.roles`. Trả `id` (đợt
    PATCH/DELETE `/api/admin/users/{id}`) — caller cũ không đọc giá trị trả về vẫn chạy đúng."""
    async with admin_pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, roles) VALUES (%s, %s, %s, %s) RETURNING id",
            (str(tenant_id), email, "not-a-real-hash", roles),
        )
        row = await cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


async def test_non_superadmin_cannot_create_company(admin_pool: Pool) -> None:
    """403 — chỉ superadmin mới tạo được công ty mới. Admin công ty thường (dù có role "admin")
    không được leo lên tạo tenant khác."""
    tenant_id = await _seed_tenant(admin_pool, "probe-non-superadmin")
    await _seed_admin_user(admin_pool, tenant_id, "admin@acme.com")
    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await create_company(
                    CreateCompanyRequest(
                        company_name="evil-co", admin_email="evil@evil.com", admin_password="password123"
                    )
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
    await _seed_section(admin_pool, tenant_a, "public")

    body = CreateUserRequest(email="new-hire@acme.com", password="password123", roles=["public"])
    assert not hasattr(body, "tenant_id")  # không có đường nào để khai — đúng như RunRequest

    token = _set_session(tenant_id=tenant_a, user="admin@acme.com", roles=["admin"])
    try:
        async with _simulate_request_connection():
            result = await create_user(body)
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert result.tenant_id == str(tenant_a)
    assert result.tenant_id != str(tenant_b)


async def test_company_admin_cannot_grant_superadmin_role(admin_pool: Pool) -> None:
    """Mutant leo quyền: company-admin cố tạo 1 user mang role "superadmin" — server phải chặn
    400, "superadmin" KHÔNG nằm trong _USER_ROLE_VOCAB dù người gọi có role "admin"."""
    tenant_id = await _seed_tenant(admin_pool, "probe-no-self-superadmin")
    await _seed_admin_user(admin_pool, tenant_id, "admin@acme.com")
    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await create_user(
                    CreateUserRequest(email="wannabe@acme.com", password="password123", roles=["superadmin"])
                )
        assert exc_info.value.status_code == 400
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_create_user_rejects_role_outside_vocab(admin_pool: Pool) -> None:
    """Đối chứng với bài trên: không chỉ chặn riêng "superadmin", mà chặn MỌI chuỗi ngoài
    (core.sections của tenant) ∪ {"admin"} — vd lỗi gõ hoặc role tự chế không tồn tại. Tenant này
    CỐ Ý không seed section nào — "hrr" phải bị chặn dù có seed section thật hay không, vì bản
    thân "hrr" (lỗi gõ của "hr") không khớp bất kỳ tên section nào."""
    tenant_id = await _seed_tenant(admin_pool, "probe-bad-role-string")
    await _seed_admin_user(admin_pool, tenant_id, "admin@acme.com")
    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await create_user(CreateUserRequest(email="typo@acme.com", password="password123", roles=["hrr"]))
        assert exc_info.value.status_code == 400
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_non_admin_cannot_create_user(admin_pool: Pool) -> None:
    """403 — nhân viên thường (không "admin"/"superadmin") không tự tạo được tài khoản khác,
    kể cả cho đúng tenant của mình."""
    tenant_id = await _seed_tenant(admin_pool, "probe-non-admin-create-user")
    await _seed_user_with_roles(admin_pool, tenant_id, "hr@acme.com", ["public", "hr"])
    token = _set_session(tenant_id=tenant_id, user="hr@acme.com", roles=["public", "hr"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await create_user(CreateUserRequest(email="another@acme.com", password="password123", roles=["public"]))
        assert exc_info.value.status_code == 403
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_password_never_leaks_in_create_company_response(admin_pool: Pool) -> None:
    """`CreateCompanyResponse` không có field `admin_password`/`password_hash` — kiểm bằng
    model_dump() thay vì đọc mã bằng mắt (khoá cứng, không lệ thuộc ai đó nhớ không thêm lại)."""
    tenant_id = await _seed_tenant(admin_pool, "probe-no-leak-superadmin-tenant")
    await _seed_superadmin_user(admin_pool, tenant_id, "su@sys")
    token = _set_session(tenant_id=tenant_id, user="su@sys", roles=["superadmin"])
    try:
        async with _simulate_request_connection():
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


async def test_session_without_core_users_row_cannot_create_user(admin_pool: Pool) -> None:
    """Chặn 1, review `app#17` (lịch sử): lúc còn `demo-login` (đã xoá hẳn, xem `routes/auth.py`),
    JWT phát từ đó mang role "admin" nhưng KHÔNG có dòng nào trong `core.users` — khác hẳn admin
    thật (tạo qua `create_company`/`seed_superadmin.py`, LUÔN có dòng). Trước bản vá, `created_by`
    fallback về `None` và INSERT chạy tiếp — lỗ hổng leo quyền thật qua route này.

    Giờ `demo-login` không còn tồn tại nên luồng bình thường không tạo được session kiểu này nữa —
    bài test set thẳng contextvar (`_set_session`, bỏ qua hẳn bước phát JWT) để giữ nguyên bài test
    làm lưới an toàn cho mọi thay đổi tương lai ở `issue_token()`/`login()` (xem comment ở
    `routes/admin.py::create_user`)."""
    tenant_id = await _seed_tenant(admin_pool, "probe-session-without-core-users-row")
    token = _set_session(
        tenant_id=tenant_id,
        user="admin@ankor.vn",
        roles=["admin", "public", "hr", "finance", "engineering"],
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await create_user(
                    CreateUserRequest(
                        email="minted-without-core-users-row@ankor.vn", password="password123", roles=["public"]
                    )
                )
        assert exc_info.value.status_code == 403
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_session_without_core_users_row_cannot_create_company(admin_pool: Pool) -> None:
    """Important #2+#3, review `app#17` đợt 2: trước bản vá, `create_company` KHÔNG kiểm
    `session.user` có tồn tại trong `core.users` không (khác `create_user` đã có chặn này từ đợt
    1) — 1 JWT superadmin còn hạn (mặc định 480 phút, không có revocation) của tài khoản ĐÃ BỊ XOÁ
    khỏi `core.users` (offboard) vẫn mint được tenant + admin mới có mật khẩu tự chọn. Cùng lưới an
    toàn với `test_session_without_core_users_row_cannot_create_user` — set thẳng contextvar, bỏ
    qua bước phát JWT, tenant chỉ cần thoả FK, không cần khớp `__system__` thật."""
    tenant_id = await _seed_tenant(admin_pool, "probe-offboarded-superadmin-tenant")
    token = _set_session(tenant_id=tenant_id, user="offboarded-su@sys", roles=["superadmin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await create_company(
                    CreateCompanyRequest(
                        company_name="probe-offboarded-mint",
                        admin_email="minted@offboarded.com",
                        admin_password="password123",
                    )
                )
        assert exc_info.value.status_code == 403
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_create_company_sets_created_by_to_real_superadmin_id(admin_pool: Pool) -> None:
    """Important #3, review `app#17` đợt 2: `created_by` của admin đầu tiên KHÔNG còn hardcode
    `None` — phải khớp đúng `id` thật của superadmin đã gọi route, đóng provenance mà Chặn 1 muốn
    đảm bảo (trước bản vá, mọi admin đầu tiên đều có `created_by IS NULL`, không phân biệt được
    "bootstrap" với "superadmin thật tạo")."""
    tenant_id = await _seed_tenant(admin_pool, "probe-created-by-tenant")
    await _seed_superadmin_user(admin_pool, tenant_id, "su-created-by@sys")
    token = _set_session(tenant_id=tenant_id, user="su-created-by@sys", roles=["superadmin"])
    try:
        async with _simulate_request_connection():
            await create_company(
                CreateCompanyRequest(
                    company_name="probe-created-by-co",
                    admin_email="admin@created-by.com",
                    admin_password="password123",
                )
            )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    async with admin_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT u.created_by, su.id FROM core.users u, core.users su WHERE u.email = %s AND su.email = %s",
            ("admin@created-by.com", "su-created-by@sys"),
        )
        row = await cur.fetchone()
    assert row is not None
    created_by, superadmin_id = row
    assert created_by == superadmin_id


async def test_superadmin_without_company_cannot_create_user(admin_pool: Pool) -> None:
    """Nên-sửa #3, review `app#17`: superadmin CHƯA tạo công ty nào (chỉ có role "superadmin",
    không có "admin") gọi thẳng `/api/admin/users` sẽ tạo user rơi vào tenant `__system__` một
    cách âm thầm (tenant_id lúc bootstrap) — chặn 400, không phải lỗ hổng nhưng là footgun im lặng
    nếu để lọt.

    Cần `admin_pool` + `_simulate_request_connection()` (khác bản trước đợt 8): roles/tenant giờ
    tra TƯƠI từ `core.users` (review `app#17`, Important #1 — không còn tin thẳng `session.roles`
    claim JWT), nên "su@sys" PHẢI có dòng thật trong DB để check "admin" not in roles chạy tới
    được, không còn suy ra thẳng từ `session.roles` như trước."""
    tenant_id = await _seed_tenant(admin_pool, "probe-superadmin-no-company")
    await _seed_superadmin_user(admin_pool, tenant_id, "su@sys")
    token = _set_session(tenant_id=tenant_id, user="su@sys", roles=["superadmin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
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
    tenant_id = await _seed_tenant(admin_pool, "probe-dup-co-superadmin-tenant")
    await _seed_superadmin_user(admin_pool, tenant_id, "su@sys")
    token = _set_session(tenant_id=tenant_id, user="su@sys", roles=["superadmin"])
    try:
        async with _simulate_request_connection():
            await create_company(
                CreateCompanyRequest(
                    company_name="probe-dup-co", admin_email="admin1@dup.com", admin_password="password123"
                )
            )
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await create_company(
                    CreateCompanyRequest(
                        company_name="probe-dup-co", admin_email="admin2@dup.com", admin_password="password123"
                    )
                )
        assert exc_info.value.status_code == 409
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_create_company_duplicate_admin_email_returns_409_not_500(admin_pool: Pool) -> None:
    """Đối trọng bài trên: `company_name` KHÁC nhau nhưng `admin_email` TRÙNG (`core.users.email`
    cũng UNIQUE, độc lập với `core.tenants.name`) — review `app#17` đợt 2 chỉ ra bài trên chỉ phủ
    nhánh trùng tên công ty, chưa có bài nào phủ nhánh trùng email dù docstring có nhắc tới. INSERT
    tenant THÀNH CÔNG trước (tên khác), rồi mới vỡ ở INSERT user (email trùng) — phải rollback cả
    tenant vừa tạo, không để lại 1 tenant mồ côi không có admin nào.

    GIỚI HẠN của bài này (4 review độc lập chỉ ra sau đợt 6): gọi thẳng hàm, không qua ASGI — khi
    `HTTPException` propagate qua `async with _simulate_request_connection():` (khối `pool.
    connection()` TỰ DựNG cho test), psycopg tự ROLLBACK TOÀN BỘ connection đó, kể cả phần đã
    "release" của SAVEPOINT trước. Qua HTTP THẬT, `HTTPException` bị FastAPI's `ExceptionMiddleware`
    bắt và chuyển thành response NGAY trong `call_next()` — KHÔNG BAO GIỜ propagate tới
    `tenant_context_middleware`'s `pool.connection()`, nên middleware đó COMMIT bình thường. Nếu 2
    INSERT của `create_company` từng nằm ở 2 SAVEPOINT riêng (bug đã sửa ở đợt 6), bài test NÀY vẫn
    xanh dù bug còn sống, vì cơ chế rollback ở đây MẠNH HƠN cơ chế thật. Bài xác nhận ĐÚNG đường thật
    (qua `ASGITransport`, sống sót được cả class bug này) là
    `test_http_asgi.py::test_create_company_partial_failure_does_not_orphan_tenant_through_real_http`
    — bài đó mới là nguồn xác nhận chính, bài này giữ lại vì vẫn hữu ích cho việc test nhanh 2 nhánh
    UNIQUE riêng biệt (tên công ty vs email)."""
    tenant_id = await _seed_tenant(admin_pool, "probe-dup-email-superadmin-tenant")
    await _seed_superadmin_user(admin_pool, tenant_id, "su-dup-email@sys")
    token = _set_session(tenant_id=tenant_id, user="su-dup-email@sys", roles=["superadmin"])
    try:
        async with _simulate_request_connection():
            await create_company(
                CreateCompanyRequest(
                    company_name="probe-dup-email-co-1",
                    admin_email="shared-admin@dup.com",
                    admin_password="password123",
                )
            )
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await create_company(
                    CreateCompanyRequest(
                        company_name="probe-dup-email-co-2",
                        admin_email="shared-admin@dup.com",
                        admin_password="password123",
                    )
                )
        assert exc_info.value.status_code == 409
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    async with admin_pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM core.tenants WHERE name = %s", ("probe-dup-email-co-2",))
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 0, "tenant thứ 2 phải bị rollback hoàn toàn, không để lại mồ côi"


async def test_create_user_rejects_empty_roles(admin_pool: Pool) -> None:
    """`roles: []` trước bản vá vẫn tạo được tài khoản (không hại — fence trả 0 chunk — nhưng không
    có lý do hợp lệ nào cho tài khoản không role nào tồn tại, review `app#17`, "nên sửa" #5).

    Cần `_simulate_request_connection()` từ đợt 8: check `invalid_roles`/`roles: []` giờ chạy SAU
    khi tra roles TƯƠI từ DB (Important #1), không còn trước connection nào như bản đợt 6."""
    tenant_id = await _seed_tenant(admin_pool, "probe-empty-roles")
    await _seed_admin_user(admin_pool, tenant_id, "admin@acme.com")
    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await create_user(CreateUserRequest(email="no-roles@acme.com", password="password123", roles=[]))
        assert exc_info.value.status_code == 400
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


def test_create_company_rejects_blank_company_name() -> None:
    """`company_name=""`/`"   "` trước bản vá vẫn tạo được tenant (review `app#17`, "nên sửa" #1)."""
    with pytest.raises(ValidationError):
        CreateCompanyRequest(company_name="   ", admin_email="admin@blank-co.com", admin_password="password123")


def test_create_user_normalizes_email_whitespace_and_case() -> None:
    """`"Admin@Acme.com"`/`"  admin@acme.com  "` trước bản vá tạo được tài khoản KHÔNG đăng nhập
    nổi bằng dạng chuẩn hoá (`login()` khớp `WHERE email = %s` chính xác) — review `app#17`,
    "nên sửa" #1. `CreateUserRequest`/`CreateCompanyRequest` giờ chuẩn hoá `.strip().lower()` ngay
    ở tầng Pydantic, TRƯỚC khi chạm route/DB."""
    body = CreateUserRequest(email="  Admin@ACME.example  ", password="password123", roles=["public"])
    assert body.email == "admin@acme.example"


def test_create_company_admin_email_normalized() -> None:
    body = CreateCompanyRequest(
        company_name="normalize-co", admin_email="  Admin@ACME.example  ", admin_password="password123"
    )
    assert body.admin_email == "admin@acme.example"


async def test_admin_updates_employee_roles(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "probe-update-roles")
    await _seed_admin_user(admin_pool, tenant_id, "admin@acme.com")
    await _seed_section(admin_pool, tenant_id, "finance")
    employee_id = await _seed_user_with_roles(admin_pool, tenant_id, "employee@acme.com", ["public"])

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        async with _simulate_request_connection():
            result = await update_user_roles(str(employee_id), UpdateUserRolesRequest(roles=["finance"]))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert result.roles == ["finance"]


async def test_admin_cannot_update_own_roles(admin_pool: Pool) -> None:
    """Cùng lý do `test_admin_cannot_deactivate_own_account` — lỡ bỏ `"admin"` khỏi chính roles
    của mình sẽ tự khoá quyền quản trị công ty vĩnh viễn, không route nào sửa lại được."""
    tenant_id = await _seed_tenant(admin_pool, "probe-self-update-roles")
    admin_id = await _seed_admin_user(admin_pool, tenant_id, "admin@acme.com")
    await _seed_section(admin_pool, tenant_id, "finance")

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await update_user_roles(str(admin_id), UpdateUserRolesRequest(roles=["finance"]))
        assert exc_info.value.status_code == 400
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_admin_cannot_update_roles_of_user_in_other_tenant(admin_pool: Pool) -> None:
    tenant_a = await _seed_tenant(admin_pool, "probe-update-roles-a")
    tenant_b = await _seed_tenant(admin_pool, "probe-update-roles-b")
    await _seed_admin_user(admin_pool, tenant_a, "admin-a@acme.com")
    victim_id = await _seed_user_with_roles(admin_pool, tenant_b, "victim@acme.com", ["public"])

    token = _set_session(tenant_id=tenant_a, user="admin-a@acme.com", roles=["admin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await update_user_roles(str(victim_id), UpdateUserRolesRequest(roles=["public"]))
        assert exc_info.value.status_code == 404
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_admin_deactivates_and_reactivates_employee(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "probe-deactivate")
    await _seed_admin_user(admin_pool, tenant_id, "admin@acme.com")
    employee_id = await _seed_user_with_roles(admin_pool, tenant_id, "employee@acme.com", ["public"])

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        async with _simulate_request_connection():
            await deactivate_user(str(employee_id))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    async with admin_pool.connection() as conn:
        cur = await conn.execute("SELECT is_active FROM core.users WHERE id = %s", (str(employee_id),))
        row = await cur.fetchone()
    assert row is not None
    assert row[0] is False

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        async with _simulate_request_connection():
            result = await reactivate_user(str(employee_id))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]
    assert result.is_active is True


async def test_admin_cannot_deactivate_own_account(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "probe-self-deactivate")
    admin_id = await _seed_user_with_roles(admin_pool, tenant_id, "admin@acme.com", ["admin"])

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await deactivate_user(str(admin_id))
        assert exc_info.value.status_code == 400
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


def test_create_user_rejects_blank_email() -> None:
    with pytest.raises(ValidationError):
        CreateUserRequest(email="   ", password="password123", roles=["public"])
