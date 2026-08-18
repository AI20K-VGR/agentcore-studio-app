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
from loguru import logger
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool
from studio_workbench.tenant_wall import resolve_session

from studio_app import rate_limit
from studio_app.jwt_auth import DUMMY_PASSWORD_HASH, hash_password, issue_token, normalize_email, verify_password
from studio_app.middleware import get_request_connection, get_request_session
from studio_app.settings import get_settings
from studio_app.validators import reject_oversized_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _resolve_rate_limit_key(request: Request) -> str:
    """Xác định "IP" dùng làm key rate-limit (review `app#17`, Important #2, đợt 8→9).

    Mặc định `request.client.host` — IP TCP thật của kết nối tới process NÀY. Sau reverse
    proxy/load balancer, đó là IP của PROXY (giống nhau cho MỌI người dùng thật) chứ không phải
    IP client gốc — gộp mọi người dùng vào 1 bucket, tự-DoS người dùng hợp lệ. `settings.
    trust_x_forwarded_for` (mặc định `False`) là escape hatch CÓ CHỦ Ý, KHÔNG bật ngầm định: chỉ
    bật khi triển khai THẬT có reverse proxy đáng tin cấu hình set cứng `X-Forwarded-For` (không
    cho client tự khai header đó lọt qua) — bật nhầm trong triển khai không có proxy như vậy sẽ MỞ
    LẠI đường né rate-limit (client tự gửi `X-Forwarded-For` giả trực tiếp, không qua proxy nào).
    Lấy hop ĐẦU TIÊN của `X-Forwarded-For` (client gốc, theo quy ước chuẩn — các hop sau là proxy
    trung gian) khi header có mặt và cờ bật; rỗng/không có header thì rơi về `request.client`.

    `request.client is None` (1 số cấu hình ASGI, vd Unix socket) log WARNING thay vì âm thầm gộp
    vào 1 bucket `"unknown"` chung cho MỌI request kiểu này — review đợt 9 chỉ ra bản đợt 8 không
    log gì ở nhánh này, khiến hiện tượng "mọi client rơi vào 1 bucket" (dù do proxy hay do
    `client is None`) không để lại dấu vết nào để vận hành phát hiện."""
    settings = get_settings()
    if settings.trust_x_forwarded_for:
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()

    if request.client is not None:
        return request.client.host

    logger.warning(
        "[AUTH] login: request.client là None — rate-limit rơi về 1 bucket 'unknown' dùng chung "
        "cho MỌI request kiểu này (có thể tự-DoS người dùng hợp lệ nếu xảy ra thường xuyên)"
    )
    return "unknown"


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
    tenant_name: str
    """Tên công ty thật (`core.tenants.name`) — thêm để UI hiện tên công ty thay vì UUID
    `tenant_id` (redesign UI: `tenant_id` không có ý nghĩa gì với người dùng thật). Với superadmin
    đây là `"__system__"` — UI tầng superadmin không hiện field này ở đâu cả nên không thành vấn
    đề."""
    user: str
    roles: list[str]
    """CỐ Ý không có `password`/`password_hash` ở bất kỳ đâu trong response — xem
    `test_admin_routes.py::test_password_hash_never_leaks_in_any_admin_response`."""


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request) -> LoginResponse:
    """Rate-limit theo IP TRƯỚC KHI CHẠM DB/BCRYPT (KHÔNG phải "trước mọi việc khác" — Pydantic đã
    validate `body: LoginRequest` xong trước khi vào tới đây, FastAPI tự làm việc đó trước khi gọi
    route; sửa lại cách nói ở đợt 8, review đợt 9 chỉ ra bản cũ khẳng định quá tay) — review `app#17`,
    Important #2, đợt 8: `bcrypt` cost-12 chạy KHÔNG ĐIỀU KIỆN bên dưới, kể cả nhánh
    `DUMMY_PASSWORD_HASH`, trên AnyIO threadpool DÙNG CHUNG (mặc định 40 thread) với mọi request
    khác của process. Route này KHÔNG cần đăng nhập (chính nó là cửa đăng nhập) nên không có gì
    chặn trước nó — ~110 req/s từ 1 client ẩn danh đủ giữ bận toàn bộ threadpool, treo NGUYÊN
    process (`rate_limit.py` giải thích đầy đủ). Key rate-limit qua `_resolve_rate_limit_key()`
    (xem định nghĩa — mặc định `request.client.host`, có escape hatch `settings.
    trust_x_forwarded_for` cho triển khai sau reverse proxy đáng tin).

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
    client_key = _resolve_rate_limit_key(request)
    if not rate_limit.check_and_consume(client_key):
        # `Retry-After` = `REFILL_SECONDS` (1 token mới sau đúng ngần đó giây) — cho client thật
        # (không phải kẻ tấn công) biết chính xác lúc nào thử lại thay vì đoán/poll liên tục.
        raise HTTPException(
            status_code=429,
            detail="Quá nhiều yêu cầu đăng nhập — thử lại sau.",
            headers={"Retry-After": str(int(rate_limit.REFILL_SECONDS))},
        )

    conn = get_request_connection()
    cur = await conn.execute(
        "SELECT u.tenant_id, u.password_hash, u.roles, u.is_active, t.name "
        "FROM core.users u JOIN core.tenants t ON t.id = u.tenant_id WHERE u.email = %s",
        (body.email,),
    )
    row = await cur.fetchone()

    password_hash = row[1] if row is not None else DUMMY_PASSWORD_HASH
    password_ok = await run_in_threadpool(verify_password, body.password, password_hash)
    # `is_active` kiểm SAU verify_password, không trước — cả 2 nhánh (active/inactive) đã tốn đúng
    # 1 lần bcrypt như nhau, không mở oracle mới (thời gian/status code) phân biệt "tài khoản bị vô
    # hiệu hoá" với "sai mật khẩu"; cả 3 lý do thất bại (không tồn tại/sai mật khẩu/bị vô hiệu hoá)
    # trả CÙNG 401, cùng thông điệp — đúng nguyên tắc chống oracle đã áp cho email/mật khẩu.
    if row is None or not password_ok or not row[3]:
        raise HTTPException(status_code=401, detail="Email hoặc mật khẩu không đúng.")

    tenant_id, _password_hash, roles, _is_active, tenant_name = row
    effective_roles = list(roles)
    if "admin" in effective_roles:
        # Admin công ty = quyền cao nhất TRONG tenant đó — mặc định thấy MỌI nội dung KB, không
        # phải tự tick từng phòng ban như nhân viên (quyết định qua AskUserQuestion). Filter nội
        # dung lúc chat nằm ở `packages/engine::interpreter.run()` (ngoài `apps/studio`, team
        # khác sở hữu), đọc thẳng `session.roles` của JWT đã verify — nên mở rộng NGAY TẠI ĐÂY,
        # lúc phát JWT, là chỗ đúng phạm vi apps/studio, không cần sửa package khác. `core.users.
        # roles` trong DB VẪN chỉ lưu đúng `["admin"]` (không ghi đè) — đây chỉ là ảnh chụp trong
        # JWT của phiên đăng nhập này, tự làm mới ở lần đăng nhập kế tiếp nếu công ty có thêm
        # section mới (cùng bản chất "role là ảnh chụp lúc login" đã áp dụng xuyên suốt).
        cur = await conn.execute("SELECT name FROM core.sections WHERE tenant_id = %s", (str(tenant_id),))
        section_names = [row[0] for row in await cur.fetchall()]
        # `dict.fromkeys` giữ ĐÚNG thứ tự chèn (khác `set`, thứ tự không xác định) — roles gốc từ
        # DB đứng trước, section mới bổ sung nối sau, không xáo trộn thứ tự đã seed/lưu.
        effective_roles = list(dict.fromkeys([*effective_roles, *section_names]))
    session: dict[str, Any] = {"tenant_id": str(tenant_id), "user": body.email, "roles": effective_roles}
    try:
        resolved = resolve_session(session)
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    token = issue_token(resolved)
    return LoginResponse(
        access_token=token,
        tenant_id=str(resolved.tenant_id),
        tenant_name=tenant_name,
        user=resolved.user,
        roles=resolved.roles,
    )


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8)

    _validate_new_password = field_validator("new_password")(reject_oversized_password)


@router.patch("/password")
async def change_own_password(body: ChangePasswordRequest) -> dict[str, str]:
    """Đổi mật khẩu CỦA CHÍNH MÌNH — dùng chung cho CẢ 3 tầng (superadmin/admin/employee), không
    đặc cách/ngoại lệ cho ai. Khác hẳn "tự tạo/tự phong superadmin" (vẫn cấm tuyệt đối, chỉ qua
    `scripts/seed_superadmin.py`) — đổi mật khẩu KHÔNG cấp thêm quyền nào, chỉ xoay credential của
    1 tài khoản ĐÃ xác thực (JWT hợp lệ) qua session hiện tại.

    Bắt buộc đúng `old_password` trước khi ghi — không chỉ tin JWT còn hạn. JWT chứng minh "đã từng
    đăng nhập", không chứng minh "vẫn đang cầm điện thoại/máy này" (phiên có thể bị đánh cắp/để mở
    quên) — bắt gõ lại mật khẩu cũ là bước xác thực-lại (step-up), không phải kiểm tra thừa. Không
    mở thêm bề mặt tấn công nào: kẻ tấn công có cả JWT LẪN mật khẩu cũ đã đủ sức tự đăng nhập lại,
    không cần qua route này."""
    session = get_request_session()
    conn = get_request_connection()

    cur = await conn.execute(
        "SELECT password_hash FROM core.users WHERE email = %s",
        (session.user,),
    )
    row = await cur.fetchone()
    if row is None:
        # JWT hợp lệ nhưng không còn dòng trong core.users (tài khoản bị offboard giữa chừng) —
        # cùng lưới an toàn `authz.fetch_fresh_identity` đã dùng ở các route khác.
        raise HTTPException(status_code=403, detail="Tài khoản gọi API không tồn tại trong core.users.")

    old_ok = await run_in_threadpool(verify_password, body.old_password, row[0])
    if not old_ok:
        raise HTTPException(status_code=401, detail="Mật khẩu hiện tại không đúng.")

    new_hash = await run_in_threadpool(hash_password, body.new_password)
    await conn.execute(
        "UPDATE core.users SET password_hash = %s WHERE email = %s",
        (new_hash, session.user),
    )
    return {"detail": "Đã đổi mật khẩu."}
