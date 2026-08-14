"""`POST /api/admin/companies` + `POST /api/admin/users` (Kế hoạch 3) — 2 bậc quản trị của auth
thật, kế bên `_DEMO_ACCOUNTS`/`demo-login` (không thay thế, xem `routes/auth.py`):

- **superadmin** (bootstrap NGOÀI luồng API, `scripts/seed_superadmin.py`) tạo công ty mới
  (`core.tenants` row) + tài khoản admin ĐẦU TIÊN của công ty đó.
- **company-admin** (do superadmin tạo, hoặc admin@ankor.vn/borea.vn kiểu cũ) tự tạo tài khoản
  nhân viên cho ĐÚNG công ty mình — không có cách nào tạo cho tenant khác (`tenant_id` CỐ Ý
  KHÔNG có field trong `CreateUserRequest`, copy nguyên văn pattern `RunRequest`/INV-1,
  `routes/runs.py:49-52`).

`roles` client gửi lên LUÔN bị validate server-side `<= SECTION_VOCAB ∪ {"admin"}` — không tin
riêng UI chặn (đúng bài học ngưỡng `[0,1]`, `kit#129` §3.1). `"superadmin"` KHÔNG nằm trong tập
cho phép ở `/users` — company-admin không tự phong được superadmin cho ai.
"""

from __future__ import annotations

import psycopg.errors
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from studio_kb.doc_factory import SECTION_VOCAB
from studio_workbench.tenant_wall import ResolvedContext

from studio_app.core._db import get_pool
from studio_app.jwt_auth import hash_password
from studio_app.middleware import get_request_session

router = APIRouter(prefix="/api/admin", tags=["admin"])

# `roles` hợp lệ khi COMPANY-ADMIN tạo user — KHÔNG có "superadmin" (chỉ superadmin mới phong
# được superadmin, và hiện tại không route nào cho phép — cố tình, xem module docstring).
_USER_ROLE_VOCAB: frozenset[str] = SECTION_VOCAB | {"admin"}


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


class CreateCompanyResponse(BaseModel):
    tenant_id: str
    admin_email: str
    """CỐ Ý không trả `admin_password`/`password_hash` — xem `test_admin_routes.py`."""


@router.post("/companies", response_model=CreateCompanyResponse)
async def create_company(body: CreateCompanyRequest) -> CreateCompanyResponse:
    session = get_request_session()
    require_superadmin(session)

    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO core.tenants (name) VALUES (%s) RETURNING id",
            (body.company_name,),
        )
        row = await cur.fetchone()
        assert row is not None
        tenant_id = row[0]

        # roles mặc định của admin công ty ĐẦU TIÊN: "admin" (mở canvas) + đủ 4 role nội dung
        # (đúng mẫu admin@ankor.vn/borea.vn hiện có ở _DEMO_ACCOUNTS — admin công ty cần đọc
        # được mọi tài liệu để cấu hình agent, khác nhân viên phòng ban chỉ cần role của mình).
        admin_roles = ["admin", *sorted(SECTION_VOCAB)]
        await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, roles, created_by) VALUES (%s, %s, %s, %s, %s)",
            (tenant_id, body.admin_email, hash_password(body.admin_password), admin_roles, None),
        )

    return CreateCompanyResponse(tenant_id=str(tenant_id), admin_email=body.admin_email)


class CreateUserRequest(BaseModel):
    email: str
    password: str = Field(min_length=8)
    roles: list[str]
    # `tenant_id` CỐ Ý KHÔNG có field ở đây — client không có chỗ nào để tự khai tenant, server
    # LUÔN dùng session.tenant_id của người gọi (company-admin đang đăng nhập). Copy nguyên văn
    # lý do `RunRequest` (`routes/runs.py:49-52`, INV-1): "request THẬM CHÍ KHÔNG THỂ mang
    # trường đó, chứ không phải mang được nhưng bị bỏ qua".


class CreateUserResponse(BaseModel):
    user_id: str
    email: str
    tenant_id: str
    roles: list[str]


@router.post("/users", response_model=CreateUserResponse)
async def create_user(body: CreateUserRequest) -> CreateUserResponse:
    session = get_request_session()
    require_admin(session)

    invalid_roles = set(body.roles) - _USER_ROLE_VOCAB
    if invalid_roles:
        raise HTTPException(
            status_code=400,
            detail=f"role {sorted(invalid_roles)} không hợp lệ — chỉ chấp nhận {sorted(_USER_ROLE_VOCAB)}",
        )

    pool = await get_pool()
    async with pool.connection() as conn:
        # session.tenant_id — KHÔNG đọc tenant_id từ body (body không có field đó).
        cur = await conn.execute(
            "SELECT id FROM core.users WHERE email = %s",
            (session.user,),
        )
        creator_row = await cur.fetchone()
        created_by = creator_row[0] if creator_row is not None else None

        try:
            cur = await conn.execute(
                "INSERT INTO core.users (tenant_id, email, password_hash, roles, created_by) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (str(session.tenant_id), body.email, hash_password(body.password), body.roles, created_by),
            )
        except psycopg.errors.UniqueViolation as exc:  # email đã tồn tại — 409, không 500
            raise HTTPException(status_code=409, detail=f"email {body.email!r} đã tồn tại") from exc
        row = await cur.fetchone()
        assert row is not None
        user_id = row[0]

    return CreateUserResponse(
        user_id=str(user_id), email=body.email, tenant_id=str(session.tenant_id), roles=body.roles
    )
