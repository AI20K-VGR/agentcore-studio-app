"""`POST /api/admin/companies` + `POST /api/admin/users` (Kế hoạch 3) — 2 bậc quản trị của auth
thật (xem `routes/auth.py`). `_DEMO_ACCOUNTS`/`demo-login` đã bị xoá hẳn — đây giờ là con đường
DUY NHẤT để có tài khoản đăng nhập được (mọi tài khoản, kể cả để dev/test, đều đi qua đây):

- **superadmin** (bootstrap NGOÀI luồng API, `scripts/seed_superadmin.py`) tạo công ty mới
  (`core.tenants` row) + tài khoản admin ĐẦU TIÊN của công ty đó.
- **company-admin** (do superadmin tạo) tự tạo tài khoản nhân viên cho ĐÚNG công ty mình — không
  có cách nào tạo cho tenant khác (`tenant_id` CỐ Ý KHÔNG có field trong `CreateUserRequest`, copy
  nguyên văn pattern `RunRequest`/INV-1, `routes/runs.py:49-52`).

`roles` client gửi lên LUÔN bị validate server-side `<= SECTION_VOCAB ∪ {"admin"}` — không tin
riêng UI chặn (đúng bài học ngưỡng `[0,1]`, `kit#129` §3.1). `"superadmin"` KHÔNG nằm trong tập
cho phép ở `/users` — company-admin không tự phong được superadmin cho ai.

`email` UNIQUE TOÀN HỆ THỐNG (không theo tenant) — 409 dưới đây (trùng email) LỘ RA "email này tồn
tại ở đâu đó trong hệ thống", kể cả tenant KHÁC, kể cả email superadmin. Đây là oracle CHẤP NHẬN có
chủ đích, KHÔNG phải bỏ sót — lý do đầy đủ + điều kiện đổi hướng ở
`docs/decisions/real-auth-system.md` §"Hệ quả đã chấp nhận" (review `app#17` đợt 5, Chặn A).
"""

from __future__ import annotations

import psycopg.errors
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool
from studio_kb.doc_factory import SECTION_VOCAB
from studio_workbench.tenant_wall import ResolvedContext

from studio_app.jwt_auth import hash_password, normalize_email
from studio_app.middleware import get_request_connection, get_request_session

router = APIRouter(prefix="/api/admin", tags=["admin"])

# `roles` hợp lệ khi COMPANY-ADMIN tạo user — KHÔNG có "superadmin" (chỉ superadmin mới phong
# được superadmin, và hiện tại không route nào cho phép — cố tình, xem module docstring).
_USER_ROLE_VOCAB: frozenset[str] = SECTION_VOCAB | {"admin"}


def _reject_blank(v: str) -> str:
    """`.strip()` rồi chặn rỗng — `company_name=""`/`"   "` trước bản vá vẫn tạo được tenant
    (review `app#17`, "nên sửa" #1, phần `company_name`)."""
    stripped = v.strip()
    if not stripped:
        raise ValueError("không được để trống")
    return stripped


def _reject_oversized_password(v: str) -> str:
    """bcrypt chỉ băm được tối đa 72 BYTE (không phải 72 ký tự — 1 ký tự có dấu tiếng Việt có thể
    chiếm 2-3 byte UTF-8), vượt quá sẽ raise `ValueError` bên trong `hash_password` -> 500 không
    bắt được ở tầng route. Chặn ở Pydantic validator (422, không phải 500) — review `app#17`, nửa
    "create" của Chặn 2 (nửa "login" đã chặn riêng ở `jwt_auth.verify_password`)."""
    if len(v.encode("utf-8")) > 72:
        raise ValueError("mật khẩu tối đa 72 byte (giới hạn bcrypt)")
    return v


def require_superadmin(session: ResolvedContext) -> None:
    """403 — đã đăng nhập rồi (qua get_request_session(), 401 xử lý ở lớp dưới), chỉ thiếu ĐÚNG
    quyền superadmin. Khác `get_request_session()`'s 401 (chưa chứng minh được là ai)."""
    if "superadmin" not in session.roles:
        raise HTTPException(status_code=403, detail="Cần quyền superadmin.")


def require_admin(session: ResolvedContext) -> None:
    if "admin" not in session.roles and "superadmin" not in session.roles:
        raise HTTPException(status_code=403, detail="Cần quyền admin.")


class CreateCompanyRequest(BaseModel):
    company_name: str
    admin_email: str
    admin_password: str = Field(min_length=8)

    _validate_admin_password = field_validator("admin_password")(_reject_oversized_password)
    _normalize_admin_email = field_validator("admin_email")(normalize_email)
    _validate_company_name = field_validator("company_name")(_reject_blank)


class CreateCompanyResponse(BaseModel):
    tenant_id: str
    admin_email: str
    """CỐ Ý không trả `admin_password`/`password_hash` — xem `test_admin_routes.py`."""


@router.post("/companies", response_model=CreateCompanyResponse)
async def create_company(body: CreateCompanyRequest) -> CreateCompanyResponse:
    session = get_request_session()
    require_superadmin(session)

    # bcrypt là CPU-bound đồng bộ (~200-370ms) — chạy qua threadpool để không chặn event loop, và
    # tính TRƯỚC khi mở connection để không giữ 1 connection Postgres suốt thời gian băm (review
    # `app#17`, đợt 2, mục 4).
    admin_password_hash = await run_in_threadpool(hash_password, body.admin_password)

    # Dùng `get_request_connection()` (connection middleware đã mở sẵn cho request này), KHÔNG tự
    # mở thêm 1 connection riêng qua `get_pool()` — trước bản vá, mỗi request tới route này giữ
    # ĐỒNG THỜI 2 connection trong pool `max_size=8` (1 của middleware, không dùng tới; 1 của route)
    # suốt đời request, y hệt vấn đề `routes/auth.py::login` đã sửa ở đợt 3. Khác `login` (chỉ đọc),
    # route này còn cần rollback ĐỘC LẬP khi `UniqueViolation` — nhưng đó là việc của SAVEPOINT
    # (`conn.transaction()`, xem dưới), không phải của việc mở thêm connection (review `app#17`,
    # đợt 5, Chặn B: 8 request đồng thời gọi route admin làm cả pool deadlock — 4×500 sau 30s, kèm
    # login của tenant KHÁC bị treo theo cùng khoảng thời gian, vì pool cạn kiệt ảnh hưởng toàn bộ
    # worker, không riêng gì request đang gọi route này).
    conn = get_request_connection()
    cur = await conn.execute("SELECT id FROM core.users WHERE email = %s", (session.user,))
    creator_row = await cur.fetchone()
    if creator_row is None:
        # Cùng lưới an toàn với `create_user` bên dưới (Chặn 1, review `app#17`) — trước bản
        # vá đợt 2, route này KHÔNG có chặn này: 1 JWT superadmin còn hạn (mặc định 480 phút)
        # nhưng tài khoản đã bị xoá khỏi `core.users` (offboard) vẫn tạo được công ty + admin
        # mới — token hết hạn hay account bị xoá không đồng nghĩa nhau nếu không kiểm lại DB.
        raise HTTPException(
            status_code=403,
            detail="Tài khoản gọi API không tồn tại trong core.users.",
        )
    created_by = creator_row[0]

    # roles mặc định của admin công ty ĐẦU TIÊN: "admin" (mở canvas) + đủ 4 role nội dung —
    # admin công ty cần đọc được mọi tài liệu để cấu hình agent, khác nhân viên phòng ban chỉ
    # cần role của mình.
    admin_roles = ["admin", *sorted(SECTION_VOCAB)]

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
                "INSERT INTO core.users (tenant_id, email, password_hash, roles, created_by) "
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
    roles: list[str]
    # `tenant_id` CỐ Ý KHÔNG có field ở đây — client không có chỗ nào để tự khai tenant, server
    # LUÔN dùng session.tenant_id của người gọi (company-admin đang đăng nhập). Copy nguyên văn
    # lý do `RunRequest` (`routes/runs.py:49-52`, INV-1): "request THẬM CHÍ KHÔNG THỂ mang
    # trường đó, chứ không phải mang được nhưng bị bỏ qua".

    _validate_password = field_validator("password")(_reject_oversized_password)
    _normalize_email = field_validator("email")(normalize_email)


class CreateUserResponse(BaseModel):
    user_id: str
    email: str
    tenant_id: str
    roles: list[str]


@router.post("/users", response_model=CreateUserResponse)
async def create_user(body: CreateUserRequest) -> CreateUserResponse:
    session = get_request_session()
    require_admin(session)

    if "admin" not in session.roles:
        # Người gọi có "superadmin" nhưng KHÔNG có "admin" — tức chưa từng qua `create_company`,
        # không thuộc công ty nào (session.tenant_id là tenant `__system__` bootstrap). Nếu cho
        # tạo tiếp, user mới sẽ rơi vào `__system__` một cách âm thầm — không phải lỗ hổng, nhưng
        # là footgun im lặng (review `app#17`, "nên sửa" #3). Superadmin phải tạo công ty trước.
        raise HTTPException(
            status_code=400,
            detail="Superadmin không thuộc công ty nào — dùng POST /api/admin/companies để tạo "
            "công ty (và admin đầu tiên) trước khi tạo thêm user.",
        )

    invalid_roles = set(body.roles) - _USER_ROLE_VOCAB
    if invalid_roles:
        raise HTTPException(
            status_code=400,
            detail=f"role {sorted(invalid_roles)} không hợp lệ — chỉ chấp nhận {sorted(_USER_ROLE_VOCAB)}",
        )
    if not body.roles:
        # `roles: []` tạo được và đăng nhập được — không phải lỗ hổng (fence trả 0 chunk cho tập
        # role rỗng), nhưng không có lý do hợp lệ nào cho phép tài khoản không role nào tồn tại
        # (review `app#17`, "nên sửa" #5).
        raise HTTPException(status_code=400, detail="roles không được rỗng.")

    # bcrypt là CPU-bound đồng bộ (~200-370ms) — chạy qua threadpool để không chặn event loop, và
    # tính TRƯỚC khi mở connection để không giữ 1 connection Postgres suốt thời gian băm (review
    # `app#17`, đợt 2, mục 4).
    password_hash = await run_in_threadpool(hash_password, body.password)

    # Dùng connection của middleware (`get_request_connection()`) + SAVEPOINT qua `conn.transaction()`
    # cho rollback độc lập khi `UniqueViolation`, KHÔNG tự mở connection riêng — cùng lý do
    # `create_company` ở trên (comment đầy đủ tại đó, review `app#17` đợt 5, Chặn B).
    conn = get_request_connection()
    # session.tenant_id — KHÔNG đọc tenant_id từ body (body không có field đó).
    cur = await conn.execute(
        "SELECT id FROM core.users WHERE email = %s",
        (session.user,),
    )
    creator_row = await cur.fetchone()
    if creator_row is None:
        # Phòng thủ theo chiều sâu: người gọi có role "admin"/"superadmin" trong JWT hợp lệ
        # (chữ ký đúng) nhưng KHÔNG có dòng nào trong `core.users` — hiện tại `issue_token()`
        # chỉ được gọi từ `login()` SAU KHI verify mật khẩu khớp 1 dòng `core.users` thật, nên
        # nhánh này không còn đường nào tới được trong luồng bình thường (khác giai đoạn còn
        # `demo-login`, khi đây là lỗ hổng leo quyền thật — review `app#17` Chặn 1: JWT từ
        # `demo-login` không cần mật khẩu vẫn mint được tài khoản thật bền vững qua route này).
        # Giữ lại chặn này làm lưới an toàn cho mọi thay đổi tương lai ở `issue_token()`/`login()`.
        raise HTTPException(
            status_code=403,
            detail="Tài khoản gọi API không tồn tại trong core.users.",
        )
    created_by = creator_row[0]

    try:
        async with conn.transaction():
            cur = await conn.execute(
                "INSERT INTO core.users (tenant_id, email, password_hash, roles, created_by) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (str(session.tenant_id), body.email, password_hash, body.roles, created_by),
            )
    except psycopg.errors.UniqueViolation as exc:  # email đã tồn tại — 409, không 500
        raise HTTPException(status_code=409, detail=f"email {body.email!r} đã tồn tại") from exc
    row = await cur.fetchone()
    assert row is not None
    user_id = row[0]

    return CreateUserResponse(
        user_id=str(user_id), email=body.email, tenant_id=str(session.tenant_id), roles=body.roles
    )
