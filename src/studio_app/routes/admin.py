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
"""

from __future__ import annotations

import psycopg.errors
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool
from studio_kb.doc_factory import SECTION_VOCAB
from studio_workbench.tenant_wall import ResolvedContext

from studio_app.core._db import get_pool
from studio_app.jwt_auth import hash_password
from studio_app.middleware import get_request_session

router = APIRouter(prefix="/api/admin", tags=["admin"])

# `roles` hợp lệ khi COMPANY-ADMIN tạo user — KHÔNG có "superadmin" (chỉ superadmin mới phong
# được superadmin, và hiện tại không route nào cho phép — cố tình, xem module docstring).
_USER_ROLE_VOCAB: frozenset[str] = SECTION_VOCAB | {"admin"}


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

    # CỐ Ý tự mở connection riêng qua `get_pool()`, KHÔNG dùng `get_request_connection()` của
    # middleware (khác `routes/auth.py::login`, đã đổi sang dùng connection đó — review `app#17`
    # đợt 3, Important, pool exhaustion) — block này bắt `UniqueViolation` GIỮA transaction rồi
    # RAISE TIẾP (không phải return/tiếp tục query khác), nên Postgres đánh dấu transaction đó
    # "aborted"; thoát khỏi `async with pool.connection()` bằng exception khiến psycopg tự
    # ROLLBACK connection RIÊNG này khi trả về pool. Nếu dùng connection của middleware (giữ suốt
    # đời request, chỉ COMMIT 1 lần lúc middleware thoát), transaction "aborted" đó sẽ không được
    # rollback cho tới khi middleware's `async with` thoát — COMMIT trên transaction aborted tuy
    # Postgres không lỗi (tự hiểu là ROLLBACK) nhưng để lại 1 quả mìn cho bất kỳ query nào khác
    # lỡ chạy trên cùng connection đó trong phần đời còn lại của request.
    pool = await get_pool()
    async with pool.connection() as conn:
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

        try:
            cur = await conn.execute(
                "INSERT INTO core.tenants (name) VALUES (%s) RETURNING id",
                (body.company_name,),
            )
        except psycopg.errors.UniqueViolation as exc:  # trùng company_name — 409, không 500
            raise HTTPException(status_code=409, detail=f"công ty {body.company_name!r} đã tồn tại") from exc
        row = await cur.fetchone()
        assert row is not None
        tenant_id = row[0]

        # roles mặc định của admin công ty ĐẦU TIÊN: "admin" (mở canvas) + đủ 4 role nội dung —
        # admin công ty cần đọc được mọi tài liệu để cấu hình agent, khác nhân viên phòng ban chỉ
        # cần role của mình.
        admin_roles = ["admin", *sorted(SECTION_VOCAB)]
        try:
            await conn.execute(
                "INSERT INTO core.users (tenant_id, email, password_hash, roles, created_by) "
                "VALUES (%s, %s, %s, %s, %s)",
                (tenant_id, body.admin_email, admin_password_hash, admin_roles, created_by),
            )
        except psycopg.errors.UniqueViolation as exc:  # trùng admin_email — 409, không 500
            raise HTTPException(status_code=409, detail=f"email {body.admin_email!r} đã tồn tại") from exc

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

    # bcrypt là CPU-bound đồng bộ (~200-370ms) — chạy qua threadpool để không chặn event loop, và
    # tính TRƯỚC khi mở connection để không giữ 1 connection Postgres suốt thời gian băm (review
    # `app#17`, đợt 2, mục 4).
    password_hash = await run_in_threadpool(hash_password, body.password)

    # Connection riêng, cùng lý do `create_company` ở trên (comment đầy đủ tại đó) — bắt
    # `UniqueViolation` giữa transaction rồi raise tiếp, cần rollback độc lập, không phải
    # connection dùng chung suốt đời request của middleware.
    pool = await get_pool()
    async with pool.connection() as conn:
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
