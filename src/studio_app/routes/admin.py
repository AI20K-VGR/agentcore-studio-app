"""`POST /api/admin/companies` + `POST /api/admin/users` (Kế hoạch 3) — 2 bậc quản trị của auth
thật (xem `routes/auth.py`). `_DEMO_ACCOUNTS`/`demo-login` đã bị xoá hẳn — đây giờ là con đường
DUY NHẤT để có tài khoản đăng nhập được (mọi tài khoản, kể cả để dev/test, đều đi qua đây):

- **superadmin** (bootstrap NGOÀI luồng API, `scripts/seed_superadmin.py`) tạo công ty mới
  (`core.tenants` row) + tài khoản admin ĐẦU TIÊN của công ty đó.
- **company-admin** (do superadmin tạo) tự tạo tài khoản nhân viên cho ĐÚNG công ty mình — không
  có cách nào tạo cho tenant khác (`tenant_id` CỐ Ý KHÔNG có field trong `CreateUserRequest`, copy
  nguyên văn pattern `RunRequest`/INV-1, `routes/runs.py:49-52`).

`system_roles` client gửi lên LUÔN bị validate server-side `<= (core.sections của đúng tenant) ∪ {"admin"}`
— không tin riêng UI chặn (đúng bài học ngưỡng `[0,1]`, `kit#129` §3.1). `"superadmin"` KHÔNG nằm
trong tập cho phép ở `/users` — company-admin không tự phong được superadmin cho ai. Từ vựng
`SECTION_VOCAB` toàn cục cứng (`studio_kb.doc_factory`) đã bị thay bằng `core.sections` theo-tenant
(`routes/sections.py`, chỉ superadmin CRUD) — xem `create_user` bên dưới.

`email` UNIQUE TOÀN HỆ THỐNG (không theo tenant) — 409 dưới đây (trùng email) LỘ RA "email này tồn
tại ở đâu đó trong hệ thống", kể cả tenant KHÁC, kể cả email superadmin. Đây là oracle CHẤP NHẬN có
chủ đích, KHÔNG phải bỏ sót — lý do đầy đủ + điều kiện đổi hướng ở
`docs/decisions/real-auth-system.md` §"Hệ quả đã chấp nhận" (review `app#17` đợt 5, Chặn A).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg.errors
from fastapi import APIRouter, HTTPException
from psycopg import AsyncConnection
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from studio_app.authz import fetch_fresh_identity, require_admin, require_superadmin
from studio_app.jwt_auth import hash_password, normalize_email
from studio_app.middleware import get_request_connection, get_request_session
from studio_app.validators import RESERVED_ROLE_NAMES, reject_blank, reject_oversized_password

router = APIRouter(prefix="/api/admin", tags=["admin"])


class CreateCompanyRequest(BaseModel):
    company_name: str
    admin_email: str
    admin_password: str = Field(min_length=8)

    _validate_admin_password = field_validator("admin_password")(reject_oversized_password)
    _normalize_admin_email = field_validator("admin_email")(normalize_email)
    _validate_company_name = field_validator("company_name")(reject_blank)


class CreateCompanyResponse(BaseModel):
    tenant_id: str
    admin_email: str
    """CỐ Ý không trả `admin_password`/`password_hash` — xem `test_admin_routes.py`."""


@router.post("/companies", response_model=CreateCompanyResponse)
async def create_company(body: CreateCompanyRequest) -> CreateCompanyResponse:
    # `get_request_session()` ở đây CHỈ để lấy identity (`session.user`, tức email đã qua verify
    # chữ ký JWT) — KHÔNG dùng `session.system_roles` để phân quyền (xem SELECT ngay dưới). 401 (chưa
    # chứng minh được là ai) vẫn xử lý ở đây như cũ.
    session = get_request_session()

    # Dùng `get_request_connection()` (connection middleware đã mở sẵn cho request này), KHÔNG tự
    # mở thêm 1 connection riêng qua `get_pool()` — trước bản vá đợt 6, mỗi request tới route này
    # giữ ĐỒNG THỜI 2 connection trong pool `max_size=8` suốt đời request, y hệt vấn đề
    # `routes/auth.py::login` đã sửa ở đợt 3 (review `app#17` đợt 5, Chặn B).
    conn = get_request_connection()

    # Tra `id` + `system_roles` TƯƠI từ `core.users`, KHÔNG dùng `session.system_roles` (claim JWT) để phân
    # quyền — JWT là ảnh chụp lúc đăng nhập, sống tới `jwt_expire_minutes` (mặc định 480 phút).
    # 1 superadmin bị thu hồi quyền giữa chừng (sửa `system_roles` trong `core.users`) vẫn còn quyền cũ
    # trong JWT tới lúc hết hạn nếu route tin thẳng `session.system_roles` (review `app#17`, Important #1,
    # đợt 8) — route này ĐÃ SẴN 1 lượt round-trip để lấy `created_by`, nên tra thêm cột `system_roles`
    # ở CÙNG query không tốn thêm round-trip nào. Đặt TRƯỚC bcrypt (khác thứ tự cũ) — không còn lý
    # do giữ hash trước connection nữa: `get_request_connection()` không phải 1 lượt checkout pool
    # mới (khác `get_pool()` cũ), connection đã được middleware giữ suốt đời request bất kể gọi lúc
    # nào, nên đổi thứ tự không tốn thêm pool pressure, mà còn fail-fast đúng roles trước khi tốn
    # ~200-370ms băm mật khẩu cho 1 request sẽ bị 403 dù sao.
    # `fetch_fresh_identity` gom round-trip SELECT + lưới an toàn "tài khoản bị offboard nhưng
    # JWT cũ còn hạn" (Chặn 1, review `app#17`) — xem docstring `authz.py`.
    identity = await fetch_fresh_identity(conn, session.user)
    created_by = identity.id
    require_superadmin(identity.system_roles)

    # bcrypt là CPU-bound đồng bộ (~200-370ms) — chạy qua threadpool để không chặn event loop.
    admin_password_hash = await run_in_threadpool(hash_password, body.admin_password)

    # roles mặc định của admin công ty ĐẦU TIÊN: CHỈ "admin" (mở canvas) — KHÔNG còn seed sẵn
    # role nội dung nào nữa (khác bản cũ `["admin", *sorted(SECTION_VOCAB)]`). `core.sections` giờ
    # là bảng THEO TENANT, và tenant vừa tạo ở dòng dưới CHƯA CÓ section nào (chicken-and-egg:
    # section do superadmin tạo SAU khi tenant đã tồn tại, `routes/sections.py`) — không còn gì để
    # seed sẵn ngoài "admin". Admin công ty muốn có role nội dung để tự test chat/canvas thì tự
    # `POST /api/admin/users` gán role cho CHÍNH mình sau khi superadmin tạo xong section.
    admin_roles = ["admin"]

    # MỘT SAVEPOINT DUY NHẤT bọc CẢ HAI insert — KHÔNG phải 2 savepoint riêng (review `app#17`
    # đợt 6, phát hiện độc lập từ 4 lượt review sau đợt 5: bug thật, không phải nitpick). Lý do 2
    # savepoint riêng SAI: nếu insert tenant THÀNH CÔNG (savepoint 1 đã RELEASE — merge vào
    # transaction ngoài của middleware) rồi insert admin user THẤT BẠI (savepoint 2 rollback,
    # raise HTTPException(409)) — HTTPException bị FastAPI's ExceptionMiddleware bắt VÀ CHUYỂN
    # THÀNH RESPONSE ngay TRONG lời gọi `call_next()`, nên nó KHÔNG BAO GIỜ propagate lên tới
    # `tenant_context_middleware`'s `async with pool.connection()` (middleware.py:96-122, `try/
    # finally`, không phải `try/except` — response trả về BÌNH THƯỜNG). Middleware thoát khối
    # `async with` KHÔNG có exception -> COMMIT nguyên transaction ngoài -> tenant vừa insert
    # (savepoint đã release) được commit thật, dù client nhận 409. `core.tenants.name` UNIQUE nên
    # tên công ty đó kẹt vĩnh viễn — không endpoint nào xoá được qua API. Gộp 1 savepoint: insert
    # thứ 2 lỗi sẽ rollback NGUYÊN savepoint đó, tức rollback CẢ hai insert cùng lúc — không còn
    # tenant mồ côi nào được release trước khi biết insert thứ 2 có ổn không.
    try:
        async with conn.transaction():
            cur = await conn.execute(
                "INSERT INTO core.tenants (name) VALUES (%s) RETURNING id",
                (body.company_name,),
            )
            row = await cur.fetchone()
            assert row is not None
            tenant_id = row[0]
            await conn.execute(
                "INSERT INTO core.users (tenant_id, email, password_hash, system_roles, created_by) "
                "VALUES (%s, %s, %s, %s, %s)",
                (tenant_id, body.admin_email, admin_password_hash, admin_roles, created_by),
            )
    except psycopg.errors.UniqueViolation as exc:
        # Một constraint DUY NHẤT có thể vỡ ở đây — `exc.diag.constraint_name` (Postgres tự đặt
        # tên `<table>_<column>_key` cho UNIQUE cột đơn không đặt tên tay, xác nhận thật qua
        # `pg_constraint`) phân biệt 2 ca thay vì suy luận từ thứ tự câu lệnh nào vừa chạy.
        if exc.diag.constraint_name == "tenants_name_key":
            raise HTTPException(status_code=409, detail=f"công ty {body.company_name!r} đã tồn tại") from exc
        if exc.diag.constraint_name == "users_email_key":
            raise HTTPException(status_code=409, detail=f"email {body.admin_email!r} đã tồn tại") from exc
        raise  # constraint lạ chưa biết — fail loud, không đoán mò thông điệp

    return CreateCompanyResponse(tenant_id=str(tenant_id), admin_email=body.admin_email)


class CreateUserRequest(BaseModel):
    email: str
    password: str = Field(min_length=8)
    system_roles: list[str]
    # `tenant_id` CỐ Ý KHÔNG có field ở đây — client không có chỗ nào để tự khai tenant, server
    # LUÔN dùng tenant_id TƯƠI (tra lại từ `core.users` ngay trong request, xem `create_user`)
    # của người gọi (company-admin đang đăng nhập), KHÔNG phải claim JWT. Copy nguyên văn lý do
    # `RunRequest` (`routes/runs.py:49-52`, INV-1): "request THẬM CHÍ KHÔNG THỂ mang trường đó,
    # chứ không phải mang được nhưng bị bỏ qua".

    _validate_password = field_validator("password")(reject_oversized_password)
    _normalize_email = field_validator("email")(normalize_email)


class CreateUserResponse(BaseModel):
    user_id: str
    email: str
    tenant_id: str
    system_roles: list[str]


@router.post("/users", response_model=CreateUserResponse)
async def create_user(body: CreateUserRequest) -> CreateUserResponse:
    # `get_request_session()` ở đây CHỈ để lấy identity (`session.user`) — KHÔNG dùng
    # `session.system_roles`/`session.tenant_id` (claim JWT) để phân quyền/gán tenant, xem SELECT dưới.
    session = get_request_session()

    # Dùng connection của middleware (`get_request_connection()`) + SAVEPOINT qua `conn.transaction()`
    # cho rollback độc lập khi `UniqueViolation`, KHÔNG tự mở connection riêng — cùng lý do
    # `create_company` ở trên (comment đầy đủ tại đó, review `app#17` đợt 5, Chặn B).
    conn = get_request_connection()

    # Tra `id`/`system_roles`/`tenant_id` TƯƠI từ `core.users`, KHÔNG dùng `session.system_roles`/
    # `session.tenant_id` (claim JWT) — JWT là ảnh chụp lúc đăng nhập, sống tới
    # `jwt_expire_minutes` (mặc định 480 phút). 2 hệ quả nếu tin thẳng JWT (review `app#17`,
    # Important #1, đợt 8):
    # 1. Admin bị thu hồi quyền (`system_roles` đổi trong DB) vẫn còn "admin" trong JWT cũ tới lúc hết
    #    hạn — leo quyền tạm thời, không cần làm gì thêm ngoài chờ JWT hết hạn tự nhiên.
    # 2. Admin bị CHUYỂN CÔNG TY (`tenant_id` đổi trong DB, vd tách/sáp nhập tenant) vẫn ghi user
    #    mới vào tenant CŨ (từ JWT) thay vì tenant HIỆN TẠI — user mới lạc sang company sai.
    # Route này ĐÃ SẴN 1 lượt round-trip để lấy `created_by`, nên tra thêm 2 cột này ở CÙNG query
    # không tốn thêm round-trip nào.
    #
    # CÒN SÓT (review `app#17` đợt 9, KHÔNG sửa ở PR này — phạm vi rộng hơn 1 route, xem
    # `docs/decisions/real-auth-system.md` §"Hệ quả đã chấp nhận"): `tenant_context_middleware`
    # (`middleware.py:109`) vẫn `SET LOCAL app.tenant_id` từ `session.tenant_id` (claim JWT, KHÔNG
    # phải `creator_tenant_id` tra tươi ở trên) cho MỌI query trên connection này trong suốt đời
    # request — kể cả các query bên dưới. Với admin bị re-home, INSERT dưới đây ghi user mới vào
    # `creator_tenant_id` (tenant TƯƠI, đúng) nhưng RLS context của chính connection đó vẫn đang
    # đứng ở tenant CŨ (từ JWT) — không lỗi ở ĐÂY chỉ vì `core.users` không bật RLS (ADR §Quyết
    # định #2), nhưng `wb.recipes`/`kb.chunks`/mọi bảng CÓ RLS mà route khác trên CÙNG request này
    # lỡ đọc/ghi sẽ vẫn lọc theo tenant CŨ. Đây là 1 THỂ HIỆN của lỗ hổng rộng hơn: `routes/runs.py`,
    # `routes/chat.py`, `routes/publish.py`, content-section role fence ở `packages/engine` đều còn
    # phân quyền bằng `session.system_roles`/`session.tenant_id` (claim JWT) — vá trọn vẹn đòi middleware
    # tự tra lại `system_roles`/`tenant_id` MỖI request (round-trip DB thêm cho MỌI route, không riêng
    # route admin), ngoài phạm vi 1 PR sửa 2 route admin.
    # `fetch_fresh_identity` gom round-trip SELECT + lưới an toàn "tài khoản bị offboard nhưng JWT
    # cũ còn hạn" (Chặn 1, review `app#17`) — xem docstring `authz.py`. Cùng 2 hệ quả nếu tin thẳng
    # JWT thay vì tra tươi (Important #1, đợt 8) vẫn áp dụng y hệt: (1) admin bị thu hồi quyền vẫn
    # còn "admin" trong JWT cũ tới lúc hết hạn, (2) admin bị chuyển công ty vẫn ghi user mới vào
    # tenant CŨ nếu tin `session.tenant_id`.
    identity = await fetch_fresh_identity(conn, session.user)
    created_by = identity.id
    creator_tenant_id = identity.tenant_id
    creator_roles = identity.system_roles
    require_admin(creator_roles)

    if "admin" not in creator_roles:
        # Người gọi có "superadmin" nhưng KHÔNG có "admin" — tức chưa từng qua `create_company`,
        # không thuộc công ty nào (`creator_tenant_id` là tenant `__system__` bootstrap). Nếu cho
        # tạo tiếp, user mới sẽ rơi vào `__system__` một cách âm thầm — không phải lỗ hổng, nhưng
        # là footgun im lặng (review `app#17`, "nên sửa" #3). Superadmin phải tạo công ty trước.
        raise HTTPException(
            status_code=400,
            detail="Superadmin không thuộc công ty nào — dùng POST /api/admin/companies để tạo "
            "công ty (và admin đầu tiên) trước khi tạo thêm user.",
        )

    # Vocab roles hợp lệ giờ TRA ĐỘNG theo tenant (`core.sections`), KHÔNG còn `SECTION_VOCAB`
    # tĩnh dùng chung toàn hệ thống — mỗi công ty có tập phòng ban riêng, do superadmin quản qua
    # `routes/sections.py`. Cùng transaction đang mở, không tốn round-trip riêng nào ngoài kế hoạch.
    cur = await conn.execute(
        "SELECT name FROM core.sections WHERE tenant_id = %s",
        (str(creator_tenant_id),),
    )
    section_names = {row[0] for row in await cur.fetchall()}
    # Trừ `RESERVED_ROLE_NAMES` khỏi `section_names` TRƯỚC khi hợp với `{"admin"}` — tầng 2 của
    # bản vá app#21 ⛔ (tầng 1: `reject_reserved_section_name` chặn tạo/đổi tên section trùng role
    # hệ thống). Cần tầng này RIÊNG vì DB có thể đã có sẵn 1 dòng `core.sections` tên `"superadmin"`
    # từ TRƯỚC khi tầng 1 tồn tại — không trừ ở đây thì dòng cũ đó vẫn âm thầm cấp quyền tạo/gán
    # superadmin cho mọi company-admin của tenant đó, bất kể validator mới đã chặn được record MỚI.
    valid_role_vocab = (section_names - RESERVED_ROLE_NAMES) | {"admin"}

    invalid_roles = set(body.system_roles) - valid_role_vocab
    if invalid_roles:
        raise HTTPException(
            status_code=400,
            detail=f"role {sorted(invalid_roles)} không hợp lệ — chỉ chấp nhận {sorted(valid_role_vocab)}",
        )
    if not body.system_roles:
        # `system_roles: []` tạo được và đăng nhập được — không phải lỗ hổng (fence trả 0 chunk cho tập
        # role rỗng), nhưng không có lý do hợp lệ nào cho phép tài khoản không role nào tồn tại
        # (review `app#17`, "nên sửa" #5).
        raise HTTPException(status_code=400, detail="roles không được rỗng.")

    # bcrypt là CPU-bound đồng bộ (~200-370ms) — chạy qua threadpool để không chặn event loop.
    # Chạy SAU mọi kiểm tra ở trên (đổi thứ tự so với trước đợt 8): fail-fast cho request sẽ bị
    # 400/403 dù sao, không tốn ~200-370ms băm mật khẩu vô ích.
    password_hash = await run_in_threadpool(hash_password, body.password)

    try:
        async with conn.transaction():
            cur = await conn.execute(
                "INSERT INTO core.users (tenant_id, email, password_hash, system_roles, created_by) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (str(creator_tenant_id), body.email, password_hash, body.system_roles, created_by),
            )
    except psycopg.errors.UniqueViolation as exc:  # email đã tồn tại — 409, không 500
        raise HTTPException(status_code=409, detail=f"email {body.email!r} đã tồn tại") from exc
    row = await cur.fetchone()
    assert row is not None
    user_id = row[0]

    return CreateUserResponse(
        user_id=str(user_id), email=body.email, tenant_id=str(creator_tenant_id), system_roles=body.system_roles
    )


class CompanySummary(BaseModel):
    tenant_id: str
    name: str
    created_at: str
    is_active: bool
    user_count: int
    section_count: int
    """`user_count`/`section_count` trả KÈM ở đây thay vì tách endpoint thống kê riêng (quyết định
    D4, app#75): danh sách công ty của UI superadmin cần đúng 2 con số này trên MỖI dòng, nên tách
    ra sẽ thành N+1 round-trip cho đúng dữ liệu 1 màn hình. Hai subquery tương quan dưới đây chạy
    trên `core.users.tenant_id`/`core.sections.tenant_id` (đều là FK có index)."""


@router.get("/companies", response_model=list[CompanySummary])
async def list_companies() -> list[CompanySummary]:
    """Superadmin-only — liệt kê mọi company (tenant thật, KHÔNG gồm `__system__`). Dùng cho UI
    superadmin chọn tenant để quản `core.sections`, xem `routes/sections.py`."""
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_superadmin(identity.system_roles)

    cur = await conn.execute(
        "SELECT t.id, t.name, t.created_at, t.is_active, "
        "(SELECT count(*) FROM core.users u WHERE u.tenant_id = t.id), "
        "(SELECT count(*) FROM core.sections sec WHERE sec.tenant_id = t.id) "
        "FROM core.tenants t WHERE t.name != '__system__' ORDER BY t.created_at DESC"
    )
    rows = await cur.fetchall()
    return [
        CompanySummary(
            tenant_id=str(row[0]),
            name=row[1],
            created_at=row[2].isoformat(),
            is_active=row[3],
            user_count=row[4],
            section_count=row[5],
        )
        for row in rows
    ]


class UserSummary(BaseModel):
    user_id: str
    email: str
    system_roles: list[str]
    is_active: bool
    created_at: str
    """CỐ Ý không có `password`/`password_hash` — xem `test_admin_routes.py`."""


@router.get("/users", response_model=list[UserSummary])
async def list_users() -> list[UserSummary]:
    """Admin-only, scoped tenant TƯƠI của người gọi — liệt kê nhân viên trong CHÍNH công ty mình,
    không có tham số tenant nào để tự khai (đúng nguyên tắc INV-1 — session luôn thắng client tự
    khai — `routes/runs.py:49-52`)."""
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_admin(identity.system_roles)

    cur = await conn.execute(
        "SELECT id, email, system_roles, is_active, created_at FROM core.users "
        "WHERE tenant_id = %s ORDER BY created_at DESC",
        (str(identity.tenant_id),),
    )
    rows = await cur.fetchall()
    return [
        UserSummary(
            user_id=str(row[0]),
            email=row[1],
            system_roles=list(row[2]),
            is_active=row[3],
            created_at=row[4].isoformat(),
        )
        for row in rows
    ]


async def _fetch_user_in_own_tenant(conn: AsyncConnection[Any], user_id: str, tenant_id: UUID) -> None:
    """404 (không phải 403) nếu `user_id` không thuộc tenant của người gọi — không xác nhận/phủ
    nhận `user_id` đó có tồn tại ở tenant KHÁC hay không (IDOR probe), khác ca `sections.py`'s
    `list_sections` (ở đó công ty admin TỰ khai tenant_id sai của CHÍNH họ, không phải dò ID lạ)."""
    cur = await conn.execute(
        "SELECT 1 FROM core.users WHERE id = %s AND tenant_id = %s",
        (user_id, str(tenant_id)),
    )
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail=f"user_id {user_id!r} không tồn tại trong công ty bạn")


class UpdateUserRolesRequest(BaseModel):
    system_roles: list[str]


@router.patch("/users/{user_id}", response_model=UserSummary)
async def update_user_roles(user_id: str, body: UpdateUserRolesRequest) -> UserSummary:
    """Admin-only, đổi `system_roles` của 1 nhân viên TRONG CHÍNH tenant mình — cùng validate vocab động
    theo `core.sections` như `create_user` (không tin riêng UI chặn).

    KHÔNG cho tự sửa role của CHÍNH tài khoản đang đăng nhập (cùng lý do `deactivate_user` chặn tự
    vô hiệu hoá chính mình) — admin duy nhất của công ty lỡ tay bỏ `"admin"` khỏi chính roles của
    họ sẽ tự khoá quyền quản trị công ty vĩnh viễn, không route nào (kể cả superadmin) sửa lại
    được (superadmin không có quyền sửa role user TRONG 1 tenant cụ thể, chỉ quản `core.sections`)."""
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_admin(identity.system_roles)
    await _fetch_user_in_own_tenant(conn, user_id, identity.tenant_id)

    if user_id == str(identity.id):
        raise HTTPException(status_code=400, detail="Không thể tự sửa role của chính tài khoản đang đăng nhập.")

    cur = await conn.execute(
        "SELECT name FROM core.sections WHERE tenant_id = %s",
        (str(identity.tenant_id),),
    )
    section_names = {row[0] for row in await cur.fetchall()}
    # Trừ `RESERVED_ROLE_NAMES` khỏi `section_names` TRƯỚC khi hợp với `{"admin"}` — tầng 2 của
    # bản vá app#21 ⛔ (tầng 1: `reject_reserved_section_name` chặn tạo/đổi tên section trùng role
    # hệ thống). Cần tầng này RIÊNG vì DB có thể đã có sẵn 1 dòng `core.sections` tên `"superadmin"`
    # từ TRƯỚC khi tầng 1 tồn tại — không trừ ở đây thì dòng cũ đó vẫn âm thầm cấp quyền tạo/gán
    # superadmin cho mọi company-admin của tenant đó, bất kể validator mới đã chặn được record MỚI.
    valid_role_vocab = (section_names - RESERVED_ROLE_NAMES) | {"admin"}

    invalid_roles = set(body.system_roles) - valid_role_vocab
    if invalid_roles:
        raise HTTPException(
            status_code=400,
            detail=f"role {sorted(invalid_roles)} không hợp lệ — chỉ chấp nhận {sorted(valid_role_vocab)}",
        )
    if not body.system_roles:
        raise HTTPException(status_code=400, detail="roles không được rỗng.")

    cur = await conn.execute(
        "UPDATE core.users SET system_roles = %s WHERE id = %s "
        "RETURNING id, email, system_roles, is_active, created_at",
        (body.system_roles, user_id),
    )
    row = await cur.fetchone()
    assert row is not None
    return UserSummary(
        user_id=str(row[0]), email=row[1], system_roles=list(row[2]), is_active=row[3], created_at=row[4].isoformat()
    )


@router.delete("/users/{user_id}", status_code=204)
async def deactivate_user(user_id: str) -> None:
    """Vô hiệu hoá (KHÔNG xoá cứng) — `core.users.id` được `core.users.created_by`/`core.sections.
    created_by` tham chiếu ngược (`REFERENCES core.users(id)`, không `ON DELETE CASCADE`); xoá cứng
    1 admin đã từng tạo nhân viên/section khác sẽ vỡ FK. Vô hiệu hoá giữ nguyên audit trail, và
    `routes/auth.py::login` chặn đăng nhập cho tài khoản `is_active=false` (coi như sai mật khẩu,
    không lộ khác biệt qua status code — cùng nguyên tắc chống oracle `login()` đã dùng).

    **Chặn cả JWT CŨ, không chỉ đăng nhập mới** (sửa lại đợt review `app#21` — bản trước chỉ nói
    đúng nửa sự thật ở câu trên, để hở nửa còn lại): `authz.fetch_fresh_identity` (mọi route admin/
    sections/agents/runs/publish gọi qua) VÀ `routes/auth.py::change_own_password` giờ cũng kiểm
    `is_active`. Trước bản vá đó, 1 admin vừa bị vô hiệu hoá ở ĐÂY vẫn gọi lọt mọi route đó bằng
    JWT cũ tới khi JWT tự hết hạn (`jwt_expire_minutes`, mặc định 480 phút) — endpoint này tưởng
    như cắt quyền ngay lập tức nhưng thực ra chỉ chặn được lần đăng nhập TIẾP THEO."""
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_admin(identity.system_roles)
    await _fetch_user_in_own_tenant(conn, user_id, identity.tenant_id)

    if user_id == str(identity.id):
        # Tự vô hiệu hoá chính mình có thể khoá cứng cả công ty nếu đây là admin duy nhất — không
        # có route nào khác để tự kích hoạt lại (chỉ admin KHÁC mới gọi được `reactivate` bên dưới).
        raise HTTPException(status_code=400, detail="Không thể tự vô hiệu hoá chính tài khoản đang đăng nhập.")

    await conn.execute("UPDATE core.users SET is_active = false WHERE id = %s", (user_id,))


@router.post("/users/{user_id}/reactivate", response_model=UserSummary)
async def reactivate_user(user_id: str) -> UserSummary:
    """Đối xứng với `deactivate_user` — admin đổi ý/xoá nhầm thì kích hoạt lại được, không cần chạm
    DB tay."""
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_admin(identity.system_roles)
    await _fetch_user_in_own_tenant(conn, user_id, identity.tenant_id)

    cur = await conn.execute(
        "UPDATE core.users SET is_active = true WHERE id = %s RETURNING id, email, system_roles, is_active, created_at",
        (user_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    return UserSummary(
        user_id=str(row[0]), email=row[1], system_roles=list(row[2]), is_active=row[3], created_at=row[4].isoformat()
    )


# ---------------------------------------------------------------------------
# app#75 — 4 endpoint superadmin thao tác VÀO TRONG 1 công ty đã tạo.
#
# Trước bản này, superadmin chỉ có đường ĐI VÀO (`POST /companies`) mà không có đường ĐI RA: mọi
# route quản user (`list_users`/`update_user_roles`/`deactivate_user`/`reactivate_user`) đều scope
# theo `identity.tenant_id` của NGƯỜI GỌI, mà superadmin đứng ở tenant `__system__` — nên họ không
# xem nổi danh sách tài khoản của công ty nào, `create_user` lại chặn thẳng họ bằng 400 ("Superadmin
# không thuộc công ty nào"). Hệ quả: 1 công ty mất tài khoản admin (quên mật khẩu / nghỉ việc) là
# hỏng vĩnh viễn, chỉ chữa được bằng SQL tay — `core.tenants.name` UNIQUE nên tạo lại cũng 409.
#
# 4 route dưới đây nhận `tenant_id` TRÊN URL. Đây là ngoại lệ CÓ CHỦ ĐÍCH với INV-1 ("không tin
# tenant_id client tự khai", `routes/runs.py:49-52`), đúng cùng lý do đã ghi cho
# `CreateSectionRequest.tenant_id` (`routes/sections.py`): người gọi là superadmin, họ KHÔNG có
# "tenant của session" nào để mặc định. Chỉ hợp lệ vì cả 4 đều `require_superadmin`, không phải
# "bất kỳ ai đã đăng nhập".
# ---------------------------------------------------------------------------


async def _resolve_company(conn: AsyncConnection[Any], tenant_id: str) -> UUID:
    """Parse UUID rồi khẳng định tenant đó tồn tại VÀ không phải `__system__` — dùng chung cho cả 4
    route dưới.

    Parse UUID TRƯỚC khi query: chuỗi sai định dạng đi thẳng vào `WHERE id = %s` (cột UUID) làm
    psycopg raise lỗi cú pháp CHƯA BẮT ⇒ 500 thay vì 400 rõ ràng (bug đã sửa 1 lần ở
    `routes/documents.py`, review app#27 finding #2, và `routes/sections.py` đã theo khuôn này).

    Loại `__system__` ở ĐÂY, không phải ở từng route: tenant hệ thống là chỗ superadmin tự đứng
    (`scripts/seed_superadmin.py`), cho phép thao tác vào nó nghĩa là superadmin tự đổi tên/tự tạm
    khoá chính tenant của mình — tạm khoá `__system__` sẽ khoá cứng MỌI superadmin ra khỏi hệ thống
    ngay lập tức (`authz.fetch_fresh_identity` giờ kiểm cả `core.tenants.is_active`), không route
    nào mở lại được."""
    try:
        UUID(tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"tenant_id không phải UUID hợp lệ: {tenant_id!r}") from exc

    cur = await conn.execute(
        "SELECT id FROM core.tenants WHERE id = %s AND name != '__system__'",
        (tenant_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"công ty {tenant_id!r} không tồn tại")
    return UUID(str(row[0]))


async def _fetch_user_in_company(conn: AsyncConnection[Any], user_id: str, tenant_id: UUID) -> None:
    """Bản superadmin của `_fetch_user_in_own_tenant` — cùng lý do trả 404 (không phải 403) khi
    `user_id` không thuộc công ty đang thao tác: không xác nhận/phủ nhận id đó có tồn tại ở công ty
    KHÁC hay không."""
    try:
        UUID(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"user_id không phải UUID hợp lệ: {user_id!r}") from exc

    cur = await conn.execute(
        "SELECT 1 FROM core.users WHERE id = %s AND tenant_id = %s",
        (user_id, str(tenant_id)),
    )
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail=f"user_id {user_id!r} không thuộc công ty này")


@router.get("/companies/{tenant_id}/users", response_model=list[UserSummary])
async def list_company_users(tenant_id: str) -> list[UserSummary]:
    """Superadmin xem danh sách tài khoản của MỘT công ty bất kỳ — song song với `list_users`
    (company-admin xem công ty CHÍNH MÌNH). Cùng `UserSummary`, nên cùng đảm bảo không có
    `password`/`password_hash` trong response."""
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_superadmin(identity.system_roles)
    target_tenant_id = await _resolve_company(conn, tenant_id)

    cur = await conn.execute(
        "SELECT id, email, system_roles, is_active, created_at FROM core.users "
        "WHERE tenant_id = %s ORDER BY created_at DESC",
        (str(target_tenant_id),),
    )
    rows = await cur.fetchall()
    return [
        UserSummary(
            user_id=str(row[0]),
            email=row[1],
            system_roles=list(row[2]),
            is_active=row[3],
            created_at=row[4].isoformat(),
        )
        for row in rows
    ]


class AddCompanyAdminRequest(BaseModel):
    email: str
    password: str = Field(min_length=8)
    # KHÔNG có `system_roles`: route này chỉ tạo ADMIN, cố định `["admin"]` — đúng hình dạng
    # `create_company` seed admin đầu tiên. Gán role nội dung cho nhân viên vẫn là việc của
    # company-admin qua `POST /api/admin/users` (superadmin không quản taxonomy người của 1 công
    # ty, chỉ mở lại được cánh cửa admin khi công ty mất nó).

    _validate_password = field_validator("password")(reject_oversized_password)
    _normalize_email = field_validator("email")(normalize_email)


@router.post("/companies/{tenant_id}/admins", response_model=CreateUserResponse)
async def add_company_admin(tenant_id: str, body: AddCompanyAdminRequest) -> CreateUserResponse:
    """Thêm 1 admin cho công ty ĐÃ CÓ — đường phục hồi cho ca "công ty còn đúng 1 admin và admin đó
    nghỉ việc". Không có route nào khác làm được việc này: `create_company` 409 vì
    `core.tenants.name` UNIQUE, `create_user` 400 vì superadmin không thuộc công ty nào."""
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_superadmin(identity.system_roles)
    target_tenant_id = await _resolve_company(conn, tenant_id)

    # bcrypt (~200-370ms, CPU-bound đồng bộ) chạy SAU mọi kiểm tra quyền/tồn tại ở trên — fail-fast,
    # không tốn băm cho request sẽ bị 403/404 dù sao (cùng thứ tự `create_user` đã chốt ở đợt 8).
    password_hash = await run_in_threadpool(hash_password, body.password)

    try:
        async with conn.transaction():
            cur = await conn.execute(
                "INSERT INTO core.users (tenant_id, email, password_hash, system_roles, created_by) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (str(target_tenant_id), body.email, password_hash, ["admin"], identity.id),
            )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail=f"email {body.email!r} đã tồn tại") from exc
    row = await cur.fetchone()
    assert row is not None

    return CreateUserResponse(
        user_id=str(row[0]), email=body.email, tenant_id=str(target_tenant_id), system_roles=["admin"]
    )


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=8)
    # Superadmin GÕ TAY mật khẩu mới, hệ thống KHÔNG sinh mật khẩu tạm (quyết định D1, app#75):
    # mật khẩu tạm chỉ an toàn khi có cờ "bắt đổi ở lần đăng nhập đầu", mà `core.users` chưa có cột
    # đó — thêm cột cho 1 tính năng chưa dùng là phình phạm vi. Cờ đó tách thành việc riêng.

    _validate_password = field_validator("new_password")(reject_oversized_password)


@router.post("/companies/{tenant_id}/users/{user_id}/reset-password", status_code=204)
async def reset_company_user_password(tenant_id: str, user_id: str, body: ResetPasswordRequest) -> None:
    """Đặt lại mật khẩu cho 1 tài khoản trong công ty — đường phục hồi cho ca "admin công ty quên
    mật khẩu" (`routes/auth.py::change_own_password` bắt buộc `old_password`, nên chính người quên
    không dùng được nó).

    Ghi `password_changed_at = now()` CÙNG lúc ghi hash mới, y hệt `change_own_password` — không
    phải để tiện, mà vì `authz.fetch_fresh_identity` so cột này với `iat` của JWT đang gọi: thiếu
    nó thì mọi JWT cấp TRƯỚC lần reset vẫn sống nguyên tới hết `jwt_expire_minutes` (mặc định 480
    phút). Reset mật khẩu thường xảy ra ĐÚNG lúc nghi tài khoản bị chiếm — để hở nửa đó thì thao
    tác này gần như vô nghĩa."""
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_superadmin(identity.system_roles)
    target_tenant_id = await _resolve_company(conn, tenant_id)
    await _fetch_user_in_company(conn, user_id, target_tenant_id)

    password_hash = await run_in_threadpool(hash_password, body.new_password)
    await conn.execute(
        "UPDATE core.users SET password_hash = %s, password_changed_at = now() WHERE id = %s",
        (password_hash, user_id),
    )


class UpdateCompanyRequest(BaseModel):
    name: str | None = None
    is_active: bool | None = None

    _validate_name = field_validator("name")(reject_blank)


@router.patch("/companies/{tenant_id}", response_model=CompanySummary)
async def update_company(tenant_id: str, body: UpdateCompanyRequest) -> CompanySummary:
    """Đổi tên công ty và/hoặc tạm khoá — hai việc gộp 1 route vì cùng là "sửa 1 dòng
    `core.tenants`" và UI làm chúng từ cùng 1 màn hình chi tiết.

    KHÔNG có `DELETE` đối xứng (quyết định D3, app#75): `core.users.created_by` và
    `core.sections.created_by` cùng `REFERENCES core.users(id)` không `ON DELETE CASCADE`, xoá cứng
    1 tenant vỡ FK y ca đã gặp ở `deactivate_user`. Đổi tên + tạm khoá đủ để dọn 1 công ty tạo
    nhầm — và giữ nguyên audit trail.

    Tạm khoá chặn ĐƯỢC CẢ JWT CŨ, không chỉ đăng nhập mới (quyết định D2): `authz.
    fetch_fresh_identity` JOIN sang `core.tenants` và 403 khi `is_active = false`, `routes/auth.py::
    login` kiểm cùng cột. Chặn riêng ở `login` là lặp lại đúng lỗ đã trả giá 1 lần cho `is_active`
    của user (xem docstring `deactivate_user`)."""
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_superadmin(identity.system_roles)
    target_tenant_id = await _resolve_company(conn, tenant_id)

    if body.name is None and body.is_active is None:
        # `PATCH {}` không phải no-op im lặng — client gửi body rỗng gần như luôn là bug phía client
        # (quên map field), trả 400 để nó vỡ ra ngay thay vì "200 nhưng chẳng đổi gì".
        raise HTTPException(status_code=400, detail="Cần ít nhất 1 trong 2 trường: name, is_active.")

    try:
        if body.name is not None:
            await conn.execute(
                "UPDATE core.tenants SET name = %s WHERE id = %s",
                (body.name, str(target_tenant_id)),
            )
        if body.is_active is not None:
            await conn.execute(
                "UPDATE core.tenants SET is_active = %s WHERE id = %s",
                (body.is_active, str(target_tenant_id)),
            )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail=f"công ty {body.name!r} đã tồn tại") from exc

    cur = await conn.execute(
        "SELECT t.id, t.name, t.created_at, t.is_active, "
        "(SELECT count(*) FROM core.users u WHERE u.tenant_id = t.id), "
        "(SELECT count(*) FROM core.sections sec WHERE sec.tenant_id = t.id) "
        "FROM core.tenants t WHERE t.id = %s",
        (str(target_tenant_id),),
    )
    row = await cur.fetchone()
    assert row is not None
    return CompanySummary(
        tenant_id=str(row[0]),
        name=row[1],
        created_at=row[2].isoformat(),
        is_active=row[3],
        user_count=row[4],
        section_count=row[5],
    )
