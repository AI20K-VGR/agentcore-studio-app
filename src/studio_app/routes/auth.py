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

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator
from starlette.concurrency import run_in_threadpool
from studio_workbench.tenant_wall import resolve_session

from studio_app import rate_limit
from studio_app.jwt_auth import DUMMY_PASSWORD_HASH, issue_token, normalize_email, verify_password
from studio_app.middleware import get_request_connection

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str

    # Cùng `normalize_email()` phía ghi (`routes/admin.py`) — email lưu trong `core.users` đã qua
    # `.strip().lower()`, tra bằng chuỗi chưa chuẩn hoá sẽ không khớp (review `app#17`, "nên sửa"
    # #1). Một email sai định dạng (rỗng/thiếu "@") luôn 422 bất kể có tồn tại trong DB hay không —
    # không phải oracle mới, vì `normalize_email()` phía ghi cũng chặn y hệt những giá trị đó, nên
    # không có dòng `core.users` nào mang email sai định dạng để so sánh.
    _normalize_email = field_validator("email")(normalize_email)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    tenant_id: str
    user: str
    roles: list[str]
    """CỐ Ý không có `password`/`password_hash` ở bất kỳ đâu trong response — xem
    `test_admin_routes.py::test_password_hash_never_leaks_in_any_admin_response`."""


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request) -> LoginResponse:
    """Rate-limit theo IP TRƯỚC MỌI VIỆC KHÁC (kể cả tra DB) — review `app#17`, Important #2, đợt
    8: `bcrypt` cost-12 chạy KHÔNG ĐIỀU KIỆN bên dưới, kể cả nhánh `DUMMY_PASSWORD_HASH`, trên
    AnyIO threadpool DÙNG CHUNG (mặc định 40 thread) với mọi request khác của process. Route này
    KHÔNG cần đăng nhập (chính nó là cửa đăng nhập) nên không có gì chặn trước nó — ~110 req/s từ
    1 client ẩn danh đủ giữ bận toàn bộ threadpool, treo NGUYÊN process (`rate_limit.py` giải thích
    đầy đủ). `request.client.host` — KHÔNG đọc `X-Forwarded-For` (header client tự khai, giả mạo
    được nếu không có reverse proxy đáng tin cấu hình strip/overwrite nó; dùng thẳng header đó ở
    đây sẽ MỞ LẠI đường né rate-limit bằng cách tự đổi header mỗi request).

    Không tiết lộ "email không tồn tại" vs "sai mật khẩu" qua status code khác nhau (cả hai đều
    401) — tránh cho kẻ tấn công dò được danh sách email tồn tại trong hệ thống bằng cách thử
    từng email (user enumeration qua timing/status code).

    **Cả hai nhánh (email tồn tại/không) LUÔN gọi `verify_password()` đúng 1 lần** — nếu để
    `row is None` short-circuit trước khi gọi (như `or` thường làm), nhánh "không tồn tại" trả về
    gần như tức thì trong khi nhánh "tồn tại nhưng sai mật khẩu" tốn ~1 lần bcrypt (~hàng trăm ms)
    — thời gian phản hồi tự nó là oracle, độc lập với status code đã đóng ở trên (review `app#17`,
    Chặn 2, nửa "timing"). Dùng `jwt_auth.DUMMY_PASSWORD_HASH` khi không tìm thấy email để giữ chi
    phí bcrypt như nhau ở cả hai nhánh.

    **`verify_password()` chạy qua `run_in_threadpool`, không gọi trực tiếp trong `async def`** —
    `bcrypt.checkpw` là CPU-bound đồng bộ, ~200-370ms mỗi lần; gọi thẳng trong route async sẽ chặn
    NGUYÊN event loop của worker trong suốt thời gian đó — MỌI request khác đang chạy cùng worker
    (kể cả SSE chat không liên quan) bị treo theo. Route này KHÔNG yêu cầu đăng nhập (chính nó là
    cửa đăng nhập), nên vài request/giây đã đủ biến nó thành DoS rẻ tiền nếu không đẩy bcrypt ra
    thread pool riêng (review `app#17`, đợt 2, mục 4).

    Dùng `get_request_connection()` (connection `tenant_context_middleware` đã mở sẵn cho request
    này), KHÔNG tự mở thêm 1 connection riêng qua `get_pool()` — route KHÔNG đăng nhập (chưa có
    `session`) nên middleware không `SET LOCAL app.tenant_id` gì trên connection đó, nhưng
    `core.users` vốn không bật RLS (xem ADR `docs/decisions/real-auth-system.md`) nên không cần
    tenant context để tra email. Trước bản vá này route tự mở connection thứ 2, nghĩa là MỖI lần
    gọi `/api/auth/login` giữ đồng thời 2 connection trong pool `max_size=8` suốt đời request (1
    của middleware, không dùng tới; 1 của route) — route đăng nhập không cần login vẫn là route
    lưu lượng cao nhất trong hệ thống, review `app#17` đợt 3, Important."""
    client_ip = request.client.host if request.client is not None else "unknown"
    if not rate_limit.check_and_consume(client_ip):
        raise HTTPException(status_code=429, detail="Quá nhiều yêu cầu đăng nhập — thử lại sau.")

    conn = get_request_connection()
    cur = await conn.execute(
        "SELECT tenant_id, password_hash, roles FROM core.users WHERE email = %s",
        (body.email,),
    )
    row = await cur.fetchone()

    password_hash = row[1] if row is not None else DUMMY_PASSWORD_HASH
    password_ok = await run_in_threadpool(verify_password, body.password, password_hash)
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
