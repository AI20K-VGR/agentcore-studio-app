"""`POST /api/auth/demo-login` — Kế hoạch 2, A2. Phát hành JWT ký THẬT (`jwt_auth.issue_token`),
không còn trả JSON thô để client tự gửi lại (bản cũ, đã bỏ theo yêu cầu).

**Client CHỈ gửi `user` (email) — KHÔNG còn `tenant`/`roles` trong request nữa.** Trước đây 2
field này do client tự khai (đúng cho `roles` thường, nhưng SAI cho `"admin"` — client tự tick
rồi server tin là tái tạo lỗi "tag vs fence"). Sửa triệt để: `tenant`/`roles` giờ đều tra từ
`_DEMO_ACCOUNTS` (registry cứng bên dưới, khoá theo email) — ĐÚNG tinh thần "role được quy định
sẵn lúc tạo tài khoản", không phải thứ người đăng nhập tự chọn mỗi lần. Client không có cách nào
tự đổi tenant/roles của mình qua request nữa, dù có cố tình gửi field lạ trong body (Pydantic
`model_config` không khai `extra="allow"` nên field lạ bị Pydantic tự bỏ qua, không lỗi to tiếng
nhưng cũng không có tác dụng gì).

Ranh giới cần nói rõ (xem thêm `jwt_auth.py`): chữ ký/verify JWT là THẬT — không ai giả mạo được
token nếu không có `settings.jwt_secret`. NHƯNG registry `_DEMO_ACCOUNTS` vẫn là danh sách CỨNG,
không phải bảng `users` thật có mật khẩu/OAuth đứng trước — ai gõ ĐÚNG 1 email có trong danh sách
là đăng nhập được, không cần chứng minh gì thêm. Đây KHÔNG phải "ai cũng đăng nhập được" theo
nghĩa lỗ hổng (client không tự thêm được tài khoản/quyền nào ngoài danh sách) — mà là "hệ thống
hiện chưa có identity provider thật", một hạng mục lớn hơn hẳn, để dành cho phần "C" (tài khoản
thật) đã thống nhất làm sau.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from studio_workbench.tenant_wall import resolve_session

from studio_app.core._db import get_pool
from studio_app.jwt_auth import DUMMY_PASSWORD_HASH, issue_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])

# !!! DEV-TIME STUB — NOT production-grade, CÙNG TINH THẦN các dev-stub khác trong codebase này
# (middleware.py's x-tenant-id, tenant_wall.py). Registry tài khoản demo CỨNG — email -> (tenant
# slug, roles đã gán sẵn) — không phải bảng `users` thật. Đây là điểm DUY NHẤT cần thay khi có hệ
# thống tài khoản thật (phần "C" đã thống nhất làm sau): tra 1 bảng `core.users`/`wb.users` thay
# vì dict hardcode này, phần còn lại (`demo_login()` bên dưới, `AppShell::isAdmin` phía FE) không
# cần đổi gì — cùng shape input/output.
# `app#13` (review DongAnh2704, D20): hr@/finance@ thiếu role `public` — mọi nhân viên phải đọc
# được tài liệu chung công ty (`public` ∈ SECTION_VOCAB), chỉ nội dung PHÒNG BAN mới bị giới hạn.
# `guest@` (0 role) thay bằng `intern@` (chỉ `public`) — thực tế hơn, vẫn giữ được ca "quyền tối
# thiểu" mà không phải tài khoản 0 mục đích. Không FE/test nào tham chiếu `guest@` trước khi đổi
# (đã grep xác nhận), nên đổi tên an toàn.
_DEMO_ACCOUNTS: dict[str, tuple[str, list[str]]] = {
    "admin@ankor.vn": ("ankor", ["admin", "public", "hr", "finance", "engineering"]),
    "hr@ankor.vn": ("ankor", ["public", "hr"]),
    "finance@ankor.vn": ("ankor", ["public", "finance"]),
    "intern@ankor.vn": ("ankor", ["public"]),
    "admin@borea.vn": ("borea", ["admin", "public", "hr", "finance", "engineering"]),
    "nhanvien@borea.vn": ("borea", ["public", "hr"]),
}


class DemoLoginRequest(BaseModel):
    user: str
    """Email — khoá TRA CỨU DUY NHẤT vào `_DEMO_ACCOUNTS`. Không còn field `tenant`/`roles` —
    client không có chỗ nào để tự khai 2 giá trị đó nữa."""


class DemoLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    tenant_id: str
    user: str
    roles: list[str]


async def _resolve_tenant_slug(raw: str) -> str | None:
    """Tra `core.tenants` bằng id::text hoặc name. Trước `kit#129` §3.2, cùng logic này còn sống ở
    `middleware.py::_resolve_tenant_id` (đã xoá hẳn — đường `x-tenant-id` header không còn tồn
    tại); hàm này KHÔNG bị đụng vì nó tra bằng chuỗi email-derived, không phải header client tự
    khai trực tiếp. Trả `None` khi không tìm thấy (fail-closed — không đoán, không tạo tenant mới
    ở đây: seed tenant là việc của `scripts/seed_demo_tenants.py`, không phải của route đăng nhập)."""
    pool = await get_pool()
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM core.tenants WHERE id::text = %s OR name = %s ORDER BY (id::text = %s) DESC LIMIT 1",
            (raw, raw, raw),
        )
        row = await cur.fetchone()
    return str(row[0]) if row is not None else None


@router.post("/demo-login", response_model=DemoLoginResponse)
async def demo_login(body: DemoLoginRequest) -> DemoLoginResponse:
    account = _DEMO_ACCOUNTS.get(body.user)
    if account is None:
        raise HTTPException(
            status_code=404,
            detail=f"{body.user!r} không nằm trong danh sách tài khoản demo "
            "(`routes/auth.py::_DEMO_ACCOUNTS`) — không tự tạo tài khoản mới ở đây.",
        )
    tenant_slug, roles = account

    tenant_id = await _resolve_tenant_slug(tenant_slug)
    if tenant_id is None:
        raise HTTPException(
            status_code=404,
            detail=f"tenant {tenant_slug!r} không tồn tại trong core.tenants — "
            "chạy `scripts/seed_demo_tenants.py` trước nếu đây là môi trường demo mới.",
        )

    session: dict[str, Any] = {"tenant_id": tenant_id, "user": body.user, "roles": roles}
    try:
        resolved = resolve_session(session)
    except PermissionError as exc:
        # fail-closed của chính resolve_session (thiếu user/blank...) — trả 400, không 500, vì
        # đây là lỗi INPUT của client, không phải lỗi hệ thống.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    token = issue_token(resolved)
    return DemoLoginResponse(
        access_token=token,
        tenant_id=str(resolved.tenant_id),
        user=resolved.user,
        roles=resolved.roles,
    )


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
    """Đăng nhập THẬT (Kế hoạch 3) — tra `core.users` bằng email + verify mật khẩu, khác hẳn
    `demo_login()` (tra `_DEMO_ACCOUNTS` hardcode, không mật khẩu). Cả hai route CÙNG TỒN TẠI
    song song — không thay thế nhau: `demo-login` tiện cho dev-stack test nhanh không cần seed
    user thật, `login` là đường thật cho tài khoản do superadmin/company-admin tạo qua
    `routes/admin.py`. Cùng `ResolvedContext`/JWT shape hệt `demo_login` — `middleware.py`/
    `get_request_session()` không cần biết/đổi gì để phân biệt 2 đường này.

    Không tiết lộ "email không tồn tại" vs "sai mật khẩu" qua status code khác nhau (cả hai đều
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
