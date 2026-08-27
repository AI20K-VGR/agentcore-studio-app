"""Composition root — FastAPI factory (Decision #1: direct composition, NO DI-framework).

`create_app()`'s lifespan runs schema DDL + the centralized cross-schema GRANT through the
ADMIN pool ONLY — never `get_pool()` (see `core/_db.py` and `core/schema.py` module docstrings
for why the pool split matters, F1/F2/F6). The tenant-context middleware wires the per-request
connection-holding mechanism (F9).

Deviation note (P3, reported to the plan lead): plan.md's Requirements text says this factory
should "import class stub quadrant (từ P1) để wiring". P1, as actually committed, shipped only
package-structure stubs (`__init__.py` docstrings) for `studio_kb`/`studio_engine`/
`studio_workbench`/`studio_evalhub` — no class definitions exist yet to import. Phase 3's own
Files/Implement/Success sections do not list a class-stub import as part of this phase's
concrete deliverable, so this factory wires DDL+grants+middleware only; quadrant class wiring is
left to the phase that actually introduces those classes (P4 shared-runtime providers, or
P5-P8 for the quadrant classes themselves).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from studio_app.core._db import close_pools, get_admin_pool
from studio_app.core.schema import ensure_all_schemas, grant_app_privileges, grant_scorer_privileges
from studio_app.middleware import tenant_context_middleware
from studio_app.providers.embeddings import EmbeddingGatewayError
from studio_app.routes import admin as admin_routes
from studio_app.routes import agents as agents_routes
from studio_app.routes import auth as auth_routes
from studio_app.routes import chat as chat_routes
from studio_app.routes import documents as documents_routes
from studio_app.routes import golden_sets as golden_sets_routes
from studio_app.routes import publish as publish_routes
from studio_app.routes import runs as runs_routes
from studio_app.routes import sections as sections_routes
from studio_app.routes import test_chat as test_chat_routes


# Windows mặc định `ProactorEventLoop`; psycopg async từ chối thẳng ("Psycopg cannot use the
# 'ProactorEventLoop' to run in async mode"). `apps/studio/scripts/seed_*.py` + kit-root
# `conftest.py` tự vá bằng `asyncio.set_event_loop_policy(...)` NGAY TRƯỚC `asyncio.run(main())`
# CỦA RIÊNG CHÚNG — cách đó KHÔNG áp dụng được cho `uvicorn`: `uvicorn.loops.asyncio.
# asyncio_loop_factory()` (Python 3.14, uvicorn 0.51 — kiểm chứng trực tiếp bằng repro cô lập, không
# suy đoán) trả THẲNG `asyncio.ProactorEventLoop` trên Windows, bỏ qua hoàn toàn policy toàn cục
# đang set — set-policy ở module-level (bản vá trước ở đây) chạy đúng lúc nhưng vô nghĩa, vì
# uvicorn không đọc policy đó khi tự chọn loop.
#
# Cách ĐÚNG (uvicorn hỗ trợ chính thức, 2 hình dạng khác nhau — dễ nhầm): `Config.loop` nhận 1
# chuỗi "module:function". `get_loop_factory()` (`uvicorn/config.py`) rẽ 2 nhánh: (a) tên có sẵn
# ("auto"/"asyncio"/"uvloop") -> gọi hàm đó VỚI `use_subprocess=` rồi mới trả kết quả (dạng
# "factory-của-factory", đúng hình `uvicorn.loops.asyncio.asyncio_loop_factory`); (b) chuỗi TUỲ Ý
# (như của chúng ta) -> `return import_from_string(self.loop)` NGAY, KHÔNG gọi thêm — nghĩa là hàm ở
# nhánh (b) phải là `Callable[[], AbstractEventLoop]` THẬT (0 tham số, trả THẲNG 1 instance loop),
# KHÔNG phải factory-của-factory như nhánh (a). Bản đầu của hàm này viết nhầm theo hình (a) (nhận
# `use_subprocess`, trả CLASS `SelectorEventLoop` chưa gọi) — `Runner` gọi nó như nhánh (b), tưởng
# giá trị trả về (chính là CLASS) là loop INSTANCE, gọi `create_task`/`close` như method KHÔNG bound
# -> "missing 1 required positional argument: self" (đã thấy khi chạy thật, không phải suy đoán).
def _win_loop_factory() -> asyncio.AbstractEventLoop:
    return asyncio.SelectorEventLoop()


# `CreateUserRequest.password`/`CreateCompanyRequest.admin_password` — tên field CÓ mật khẩu, ở
# BẤT KỲ route nào tương lai thêm field mật khẩu mới cũng nên đặt tên khớp 1 trong 2 chuỗi này
# (hoặc thêm vào tập) để được redact tự động, xem `_redact_sensitive_validation_errors` dưới đây.
_SENSITIVE_FIELD_NAMES = frozenset({"password", "admin_password"})

# Trần số node được duyệt trong `_value_contains_sensitive_key` — review `app#17` đợt 4, Critical:
# xem docstring hàm đó.
_MAX_SCAN_NODES = 2000


def _value_contains_sensitive_key(value: object) -> bool:
    """Quét LẶP (stack tường minh, KHÔNG đệ quy) qua `dict`/`list` lồng nhau ở BẤT KỲ độ sâu nào,
    tìm key trùng `_SENSITIVE_FIELD_NAMES` — review `app#17` đợt 3: bản đầu chỉ soi key ở top-level
    `input`, bỏ lọt ca field cha là object/list chứa 1 object con có key nhạy cảm bên trong.

    Review `app#17` đợt 4, Critical, do CHÍNH bản đệ quy (đợt 3) tạo ra: bản đệ quy bị
    `RecursionError` với body lồng đủ sâu (thực nghiệm reviewer xác nhận: ~600 tầng đã vỡ dưới
    limit đệ quy mặc định của Python, vì mỗi tầng tốn NHIỀU HƠN 1 frame — đi qua `any()`/genexpr).
    `RecursionError` đó KHÔNG được bắt ở đâu cả -> 500 -> traceback (chứa NGUYÊN payload lỗi, gồm
    cả field `password` thật) đi thẳng vào log server dưới uvicorn/gunicorn thật — mật khẩu lộ ra
    log mà KHÔNG CẦN đăng nhập/request hợp lệ, chỉ cần 1 field rác lồng đủ sâu, TỆ HƠN lỗ ban đầu
    PR này định đóng (lỗ cũ lộ qua RESPONSE, còn lộ này lộ vào LOG server, và không cần hợp lệ hoá
    request). Đồng thời đệ quy không giới hạn tự nó là 1 vector DoS nhẹ (CPU tỉ lệ độ sâu, ~1KB
    payload đủ tốn nhiều giây).

    Vá bằng vòng lặp `while` + `list` làm stack tường minh (không tốn Python call-frame nào theo
    độ sâu dữ liệu, chỉ tốn heap) VÀ chặn thêm `_MAX_SCAN_NODES` (đếm số node đã duyệt, không chỉ
    độ sâu — chặn cả body RỘNG lẫn SÂU). Vượt trần -> trả `True` (fail-closed, coi như CÓ thể nhạy
    cảm, redact), KHÔNG phải `False` — không chứng minh được phần chưa duyệt tới là an toàn thì
    không được kết luận an toàn, đúng nguyên tắc over-redact đã dùng xuyên suốt hàm này."""
    stack: list[object] = [value]
    visited = 0
    while stack:
        current = stack.pop()
        visited += 1
        if visited > _MAX_SCAN_NODES:
            return True
        if isinstance(current, dict):
            for key, v in current.items():
                if str(key) in _SENSITIVE_FIELD_NAMES:
                    return True
                stack.append(v)
        elif isinstance(current, (list, tuple)):
            stack.extend(current)
    return False


def _error_touches_sensitive_field(error_dict: dict[str, object]) -> bool:
    """True nếu error item này CÓ THỂ mang giá trị 1 field nhạy cảm — qua `loc` của chính nó (ca
    thường: field đó tự sai, VD mật khẩu quá dài), qua `input`/`ctx` (ca `"missing"`: Pydantic v2
    gắn NGUYÊN dict input gốc — mọi field ANH EM còn lại, kể cả field nhạy cảm đã gửi hợp lệ — vào
    `input` của error field bị THIẾU, không phải field nhạy cảm tự nó lỗi), HOẶC qua `loc` KHÔNG
    XÁC ĐỊNH ĐƯỢC TÊN FIELD NÀO (ca body sai hẳn HÌNH DẠNG, không parse được thành object — `input`
    khi đó là NGUYÊN payload thô, có thể chứa mật khẩu ở bất kỳ đâu trong đó mà không có key nào
    để soi).

    Review `app#17` đợt 3, Critical (3 lượt — lượt 2 verify LẦN ĐẦU bằng test HTTP thật qua ASGI
    mới lộ ra lượt 2 tự nó cũng SAI, không chỉ dừng ở đọc review):
    - Lượt 1: `LoginRequest(password=...)` thiếu `email` → error có `loc=("email",)` nhưng
      `input={"password": "<mật khẩu thật>"}` — bản đầu chỉ soi `loc` nên bỏ lọt. Vá bằng cách soi
      thêm `input`.
    - Lượt 2 (bản vá lượt 1 vẫn lọt): gửi THẲNG 1 JSON array làm body thay vì object. Gọi
      `LoginRequest.model_validate([...])` TRỰC TIẾP (ngoài FastAPI) cho `loc=[]` (rỗng hẳn) — ban
      đầu vá bằng `len(loc) == 0`. NHƯNG verify lại bằng `test_http_asgi.py` (request HTTP THẬT,
      qua FastAPI, không gọi thẳng Pydantic) cho kết quả KHÁC: FastAPI tự chèn `"body"` làm phần tử
      ĐẦU của `loc` cho MỌI lỗi validate body (xác nhận qua debug print thật:
      `loc=["body"]`, `input=["attacker@example.com", "<mật khẩu thật>"]`, error item này còn
      KHÔNG CÓ `ctx`) — `len(loc) == 0` không bao giờ đúng qua đường HTTP thật, bài test lượt 2 tự
      nó XANH GIẢ (assert sai chỗ, không phải code đúng). So `loc` với field lỗi thường (VD
      `admin_password` quá dài) qua CÙNG đường HTTP thật: `loc=["body", "admin_password"]` — 2
      phần tử: `"body"` (loại vị trí) + tên field thật. Vậy tín hiệu đúng là ĐỘ DÀI `loc` sau khi
      trừ marker vị trí (`"body"/"query"/"path"/...`) — `len(loc) <= 1` nghĩa là CHỈ có marker vị
      trí, không có tên field nào theo sau -> không xác định được field -> không thể chứng minh
      input đó an toàn -> redact toàn bộ, chấp nhận over-redact vài ca vô hại (route không có field
      nhạy cảm) để không bỏ lọt ca có."""
    loc = error_dict.get("loc", ())
    if isinstance(loc, (list, tuple)):
        if any(str(part) in _SENSITIVE_FIELD_NAMES for part in loc):
            return True
        if len(loc) <= 1:
            return True
    return _value_contains_sensitive_key(error_dict.get("input")) or _value_contains_sensitive_key(
        error_dict.get("ctx")
    )


async def _redact_sensitive_validation_errors(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Handler mặc định của FastAPI cho `RequestValidationError` echo lại NGUYÊN GIÁ TRỊ client
    gửi vào `detail[].input` (Pydantic v2 `errors()`, và `.ctx.error` cho validator tự viết như
    `_reject_oversized_password` ở `routes/admin.py`) — với field `password`/`admin_password`,
    nghĩa là response 422 chứa THẲNG mật khẩu client vừa gõ. Response body 422 rất dễ bị log lại
    (reverse-proxy access log, APM, devtools/HAR của bất kỳ ai bắt request) — đúng loại lỗ mà PR
    này đang cố đóng (Chặn 1/2, `app#17`), nhưng lại mở ra qua chính đường vá (review `app#17`,
    đợt 2, mục Critical #1). Chặn bằng cách bỏ `input`/`ctx` khỏi MỌI error item chạm field nhạy
    cảm — qua `loc` CỦA CHÍNH NÓ hoặc qua sibling-field lộ trong `input` (đợt 3, xem
    `_error_touches_sensitive_field`) — giữ nguyên cho field khác, người dùng vẫn biết field nào
    sai, chỉ không thấy lại giá trị mật khẩu."""
    del request
    redacted = []
    for error in exc.errors():
        error_dict = dict(error)
        if _error_touches_sensitive_field(error_dict):
            error_dict.pop("input", None)
            error_dict.pop("ctx", None)
        redacted.append(error_dict)
    return JSONResponse(status_code=422, content=jsonable_encoder({"detail": redacted}))


async def _embedding_gateway_error_to_503(request: Request, exc: EmbeddingGatewayError) -> JSONResponse:
    """app#30, QĐ-6/Q2: `providers/embeddings.py::EmbeddingGatewayError` là domain error (module đó
    KHÔNG import FastAPI) — ánh xạ HTTP ở ĐÂY, composition root, cùng khuôn
    `_redact_sensitive_validation_errors` ở trên. CỐ Ý khác precedent `build_llm()` (raise
    `HTTPException(500)` từ TRONG module provider) — issue §4.1 đòi fail-closed **503** cho
    embedding, không phải 500; gọi tên chỗ vênh ra trong PR body, không bắt chước im lặng."""
    del request
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    del app  # unused — required by the ASGI lifespan callable signature
    admin = await get_admin_pool()
    await ensure_all_schemas(admin)
    await grant_app_privileges(admin)
    await grant_scorer_privileges(admin)
    try:
        yield
    finally:
        await close_pools()


def create_app() -> FastAPI:
    """Build the FastAPI app: lifespan boots DDL+grants via the admin pool; middleware holds one
    tenant-scoped connection per request via contextvar (F9)."""
    app = FastAPI(title="AgentCore Studio", lifespan=_lifespan)
    # mypy: stub của Starlette khai `add_exception_handler` nhận handler cho `Exception` chung,
    # không suy được kiểu hẹp hơn `RequestValidationError` dù đây là cách dùng CHÍNH THỐNG của
    # FastAPI (`@app.exception_handler(RequestValidationError)` cũng cùng 1 signature) — cùng lớp
    # false-positive đã gặp ở `test_admin_routes.py`'s `# type: ignore[arg-type]` cho ContextVar.
    app.add_exception_handler(RequestValidationError, _redact_sensitive_validation_errors)  # type: ignore[arg-type]
    # app#30 QĐ-6/Q2 — cùng lý do type:ignore ở trên (Starlette stub không suy được kiểu exception
    # hẹp hơn `Exception` chung dù đây là cách dùng chính thống của FastAPI).
    app.add_exception_handler(EmbeddingGatewayError, _embedding_gateway_error_to_503)  # type: ignore[arg-type]
    # DEV-TIME — cho phép `apps/web` (Vite, cổng KHÁC — 5173 — nên trình duyệt coi là origin
    # khác) gọi được API này. `dev_playground_server.py` (server tạm cũ) cũng tự set
    # `Access-Control-Allow-Origin: *` cho đúng lý do này — không phải phát minh mới, chỉ là
    # FastAPI cần khai qua middleware thay vì set header tay mỗi response như code cũ.
    # `allow_credentials=False` (mặc định) vì luồng demo dùng `Authorization: Bearer <jwt>` tự
    # đính vào header, KHÔNG dùng cookie trình duyệt tự quản — CORS spec cấm
    # `allow_origins=["*"]` cùng `allow_credentials=True`, nên giữ mặc định là đúng, không phải
    # thiếu sót.
    # CORS thêm SAU tenant_context_middleware (review PR#5, DE, M2): Starlette xếp middleware
    # thêm SAU CÙNG ra NGOÀI CÙNG — thêm CORS trước như bản cũ làm nó nằm TRONG
    # tenant_context_middleware, nên response lỗi phát sinh ngay trong middleware đó (401/500)
    # không bao giờ đi qua CORS để được gắn header — trình duyệt báo "CORS error", che mất lỗi
    # 401/500 thật, ngược đúng mục đích thêm CORS (test được qua trình duyệt).
    app.middleware("http")(tenant_context_middleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Kế hoạch 2 (luồng demo login -> canvas -> publish -> chat) — mỗi router chỉ lắp ráp lời gọi
    # tới seam các quadrant đã có sẵn (resolve_session, interpreter.run, publish, EvalHarness.run),
    # không tự chứa business logic (F3, đúng nguyên tắc "composition root wires, không viết domain
    # logic" đã ghi ở docstring module này).
    app.include_router(auth_routes.router)
    app.include_router(admin_routes.router)
    app.include_router(sections_routes.router)
    app.include_router(documents_routes.router)
    app.include_router(golden_sets_routes.router)
    app.include_router(agents_routes.router)
    app.include_router(runs_routes.router)
    app.include_router(publish_routes.router)
    # `publish_routes` xuất HAI router, không phải một: `router` (`/api/agents/{id}/…`) và
    # `jobs_router` (`/api/eval-jobs/{job_id}`, đường đọc tiến độ lượt chấm nền). Quên dòng dưới thì
    # `POST /evaluate-async` vẫn trả 202 kèm `job_id` và task nền vẫn chạy trọn vẹn — chỉ có đường
    # TRA trạng thái là không tồn tại, nên giao diện nhận 404 mặc định của FastAPI và hiện "Not
    # Found" ngay khi vừa bấm Chấm điểm. Đã xảy ra thật một lần (app#91).
    app.include_router(publish_routes.jobs_router)
    app.include_router(chat_routes.router)
    app.include_router(test_chat_routes.router)
    return app
