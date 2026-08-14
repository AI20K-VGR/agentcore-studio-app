"""`POST /api/auth/login` — đăng nhập THẬT (Kế hoạch 3), tra `core.users` bằng email + verify
mật khẩu (bcrypt, `jwt_auth.verify_password`), phát JWT ký thật (`jwt_auth.issue_token`).

**Lịch sử — `POST /api/auth/demo-login` (Kế hoạch 2, A2) đã bị XOÁ** (không phải deprecate song
song): route đó tra 1 registry cứng `_DEMO_ACCOUNTS` (email -> tenant/roles gán sẵn), đăng nhập
được chỉ bằng cách gõ ĐÚNG 1 email trong danh sách, không cần mật khẩu. Bản thiết kế ban đầu định
giữ 2 đường song song ("demo tiện cho dev-stack, login là đường thật cho production") — huỷ quyết
định đó: hệ thống giờ CHỈ còn 1 đường đăng nhập, mọi tài khoản (kể cả tài khoản dùng để dev/test)
đều phải có dòng thật trong `core.users` (tạo qua `scripts/seed_superadmin.py` rồi
`POST /api/admin/companies`/`POST /api/admin/users`). `app#13`/`app#11` (2 issue nhắm vào
`_DEMO_ACCOUNTS`) coi như đóng theo hướng "registry không còn tồn tại" thay vì "đã vá đúng registry
đó" — xem comment đóng issue kèm SHA.

`tenant`/`roles` không còn field nào cho client tự khai trong request — luôn tra từ `core.users`
theo email đã xác thực mật khẩu, đúng tinh thần "role được quy định sẵn lúc tạo tài khoản".
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from studio_workbench.tenant_wall import resolve_session

from studio_app.core._db import get_pool
from studio_app.jwt_auth import DUMMY_PASSWORD_HASH, issue_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    tenant_id: str
    user: str
    roles: list[str]
    """CỐ Ý không có `password`/`password_hash` ở bất kỳ đâu trong response — xem
    `test_admin_routes.py::test_password_hash_never_leaks_in_any_admin_response`."""


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest) -> LoginResponse:
    """Không tiết lộ "email không tồn tại" vs "sai mật khẩu" qua status code khác nhau (cả hai đều
    401) — tránh cho kẻ tấn công dò được danh sách email tồn tại trong hệ thống bằng cách thử
    từng email (user enumeration qua timing/status code).

    **Cả hai nhánh (email tồn tại/không) LUÔN gọi `verify_password()` đúng 1 lần** — nếu để
    `row is None` short-circuit trước khi gọi (như `or` thường làm), nhánh "không tồn tại" trả về
    gần như tức thì trong khi nhánh "tồn tại nhưng sai mật khẩu" tốn ~1 lần bcrypt (~hàng trăm ms)
    — thời gian phản hồi tự nó là oracle, độc lập với status code đã đóng ở trên (review `app#17`,
    Chặn 2, nửa "timing"). Dùng `jwt_auth.DUMMY_PASSWORD_HASH` khi không tìm thấy email để giữ chi
    phí bcrypt như nhau ở cả hai nhánh."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT tenant_id, password_hash, roles FROM core.users WHERE email = %s",
            (body.email,),
        )
        row = await cur.fetchone()

    password_hash = row[1] if row is not None else DUMMY_PASSWORD_HASH
    password_ok = verify_password(body.password, password_hash)
    if row is None or not password_ok:
        raise HTTPException(status_code=401, detail="Email hoặc mật khẩu không đúng.")

    tenant_id, _password_hash, roles = row
    session: dict[str, Any] = {"tenant_id": str(tenant_id), "user": body.email, "roles": list(roles)}
    try:
        resolved = resolve_session(session)
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    token = issue_token(resolved)
    return LoginResponse(
        access_token=token,
        tenant_id=str(resolved.tenant_id),
        user=resolved.user,
        roles=resolved.roles,
    )
