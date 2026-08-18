"""Roles TƯƠI tra từ `core.users` mỗi request — KHÔNG BAO GIỜ tin `session.roles` (claim JWT) để
phân quyền. JWT là ảnh chụp lúc đăng nhập, sống tới `settings.jwt_expire_minutes` (mặc định 480
phút = 8 tiếng): 1 tài khoản bị thu hồi quyền hoặc chuyển tenant giữa chừng vẫn còn quyền cũ trong
JWT cũ tới lúc hết hạn nếu route tin thẳng `session.roles` (review `app#17`, Important #1, đợt 8).

Tách khỏi `middleware.py` có chủ đích — module đó tự giới hạn phạm vi ở việc giữ 1 connection +
danh tính JWT-derived cho request (F9), không phải nơi quyết "danh tính đó được làm gì". `authz.py`
là tầng NGAY TRÊN đó: biến 1 `ResolvedContext` (JWT) thành 1 `FreshIdentity` (DB) rồi mới cho phép
route quyết định.

Ban đầu (`app#17`) `FreshRoles`/`require_admin`/`require_superadmin` chỉ sống trong
`routes/admin.py`. Sau khi thêm `routes/sections.py`/`routes/agents.py` + gate lại
`routes/runs.py`/`routes/publish.py`, cùng 1 round-trip `SELECT id, roles, tenant_id FROM
core.users WHERE email=%s` lặp lại ở 7+ route handler — gom về đây 1 lần."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import NewType
from uuid import UUID

from fastapi import HTTPException
from psycopg import AsyncConnection

from studio_app.middleware import get_request_token_issued_at

FreshRoles = NewType("FreshRoles", list[str])
"""Type riêng cho "roles vừa tra từ `core.users` trong request này" — KHÁC `list[str]` thường (vd
`session.roles`, claim JWT). Không có type riêng, `require_admin(session.roles)` type-check sạch
như `require_admin(fresh_roles)` — mypy không phân biệt được, 1 lần review-miss là bug leo quyền
sống lại lặng lẽ (review `app#17` đợt 9, Type-safety gap). `NewType` không chặn được ai CỐ TÌNH viết
`require_admin(FreshRoles(session.roles))` để né mypy — nhưng chặn được ca ngộ nhận, việc thực tế
hay xảy ra hơn nhiều: gõ nhầm `session.roles` thay vì roles vừa tra sẽ ĐỎ NGAY ở mypy, không đợi
tới lúc chạy production."""


@dataclass(frozen=True, slots=True)
class FreshIdentity:
    """Danh tính TƯƠI của người gọi, tra lại từ `core.users` trong CHÍNH request này — `id` +
    `tenant_id` dùng khi route cần ghi dữ liệu THUỘC ĐÚNG tenant người gọi (thay vì tin
    `session.tenant_id` từ JWT), `roles` dùng cho `require_admin`/`require_superadmin`."""

    id: UUID
    tenant_id: UUID
    roles: FreshRoles


async def fetch_fresh_identity(conn: AsyncConnection, email: str) -> FreshIdentity:
    """`SELECT id, roles, tenant_id, password_changed_at FROM core.users WHERE email = %s` —
    round-trip DUY NHẤT dùng chung cho mọi route cần roles/tenant tươi thay vì tin JWT.

    Raise `HTTPException(403)` nếu JWT hợp lệ (chữ ký đúng, chưa hết hạn) nhưng KHÔNG còn dòng nào
    trong `core.users` khớp email đó — phòng thủ theo chiều sâu cho ca tài khoản bị offboard (xoá
    khỏi `core.users`) nhưng JWT cũ (tới 480 phút) vẫn còn hạn dùng (review `app#17`, Chặn 1).

    Raise `HTTPException(401)` nếu JWT ký TRƯỚC lần đổi mật khẩu gần nhất (`core.users.
    password_changed_at`) — đổi mật khẩu là cách người dùng tự xử lý phiên bị đánh cắp, JWT ký từ
    trước đó phải hết hiệu lực ngay, không đợi tới hết `jwt_expire_minutes` (review `app#21` 🔶,
    xem `core/schema.py` ngay trên cột này). `password_changed_at IS NULL` (chưa từng đổi mật khẩu
    tự phục vụ) bỏ qua kiểm tra — không có mốc nào để so.

    LƯU Ý PHẠM VI: chỉ áp dụng cho route nào GỌI hàm này (admin/sections/agents/runs/publish, và
    nhánh `as_roles` của chat) — đường chat MẶC ĐỊNH của nhân viên (`routes/chat.py::chat`,
    `body.as_roles is None`) dùng thẳng `session` (JWT) làm `session_context` cho interpreter,
    KHÔNG đi qua hàm này, nên JWT nhân viên bị đánh cắp vẫn chat được tới khi hết hạn dù chủ tài
    khoản đã đổi mật khẩu. Vá trọn vẹn đòi kiểm tra này chạy ở `tenant_context_middleware` (mọi
    request, thêm 1 round-trip DB cho MỌI route) — ngoài phạm vi bản vá "rẻ nhất" mà review yêu
    cầu, cùng dạng nợ kỹ thuật đã ghi nhận công khai ở `routes/admin.py` (comment trên
    `fetch_fresh_identity` ở `create_user`) cho khoảng-hở tenant_context tương tự."""
    cur = await conn.execute(
        "SELECT id, roles, tenant_id, password_changed_at FROM core.users WHERE email = %s",
        (email,),
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(
            status_code=403,
            detail="Tài khoản gọi API không tồn tại trong core.users.",
        )
    user_id, roles_raw, tenant_id, password_changed_at = row
    # `- 1s`: `iat` là unix timestamp NGUYÊN GIÂY (JWT chuẩn), `password_changed_at` là
    # `TIMESTAMPTZ` micro-giây từ `now()` — 1 JWT ký CÙNG giây với lần đổi mật khẩu (login lại
    # ngay sau khi đổi) có thể có `iat` (đã cắt xuống giây) nhỏ hơn `password_changed_at` (còn
    # phần lẻ micro-giây) dù thực ra được ký SAU. Khoan dung 1 giây tránh false-positive khoá
    # nhầm chính phiên vừa đăng nhập lại, vẫn thừa đủ chặt để loại JWT thật sự cũ (phút/giờ trước).
    if password_changed_at is not None and get_request_token_issued_at() < password_changed_at - timedelta(seconds=1):
        raise HTTPException(
            status_code=401,
            detail="Mật khẩu đã đổi sau khi phiên này đăng nhập — đăng nhập lại.",
        )
    return FreshIdentity(id=user_id, tenant_id=tenant_id, roles=FreshRoles(list(roles_raw)))


def require_superadmin(roles: FreshRoles) -> None:
    """403 — đã đăng nhập rồi (qua `get_request_session()`, 401 xử lý ở lớp dưới), chỉ thiếu ĐÚNG
    quyền superadmin. `roles` PHẢI là `FreshRoles` (tra qua `fetch_fresh_identity`), không phải
    `session.roles` — xem docstring module."""
    if "superadmin" not in roles:
        raise HTTPException(status_code=403, detail="Cần quyền superadmin.")


def require_admin(roles: FreshRoles) -> None:
    """Cùng lý do `require_superadmin` ở trên — `roles` phải là roles TƯƠI từ DB, không phải JWT."""
    if "admin" not in roles and "superadmin" not in roles:
        raise HTTPException(status_code=403, detail="Cần quyền admin.")
