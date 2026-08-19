"""`POST /api/runs` + `GET /api/runs/{run_id}` (Kế hoạch 2, A3) — route Test THẬT, thay
`packages/workbench/dev_playground_server.py` (server tạm, `http.server` trần, ngoài mọi package).

Khác `dev_playground_server.py` ở 3 điểm, đúng ý nghĩa "luồng thật" của GATE-2 (Day 20, #127):
- `kb_search = PgKbSearch(...)` (Postgres/pgvector thật, fence RLS) thay `StaticKbSearch` (RAM).
- `session = get_request_session()` (từ `Authorization: Bearer <jwt>`, qua `jwt_auth.verify_token`
  + `tenant_wall.resolve_session`) thay
  session giả dựng từ `recipe.tenant_id` client tự khai.
- `trace_writer = PgTraceWriter(...)` (ghi Postgres thật) thay `InMemoryTraceWriter`.

Dùng `create_dynamic_recipe` (không phải `Recipe.model_validate(toàn bộ JSON client gửi)`) để
`tenant_id` KHÔNG CÓ ĐƯỜNG NÀO lọt vào Recipe từ body client — tách tường minh ở chữ ký hàm, thay
vì chỉ dựa vào `interpreter.run()` ghi đè sau (2 lớp phòng thủ thay vì 1).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError
from studio_contracts import Edge, EmbeddingService, Node
from studio_engine import interpreter
from studio_kb.postgres import PgKbSearch
from studio_kb.trace_reader import PgTraceReader, render_timeline, walk_from_dag
from studio_workbench.builder import create_dynamic_recipe
from studio_workbench.validator import graph_lint

from studio_app.authz import fetch_fresh_identity, require_admin
from studio_app.core._db import get_pool
from studio_app.middleware import get_request_connection, get_request_session
from studio_app.obs.trace_writer import PgTraceWriter
from studio_app.providers.factory import build_embedding, build_llm

router = APIRouter(prefix="/api/runs", tags=["runs"])


class RunRequest(BaseModel):
    agent_id: str
    instructions: str
    model: str
    tool_whitelist: list[str] = Field(default_factory=list)
    kb_id: str
    scope: str
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    golden_set_ref: str = "callisto-golden-30-v1"
    # `ge=0.0, le=1.0` (kit#129 §3.1, vấn đề A, VinSOC AV-203052) — trước bản vá: client gửi
    # `success_threshold: -999` được chấp nhận thẳng, mọi agent "đạt" bất kể chất lượng thật.
    # Ràng buộc CHÍNH nằm ở contract (`ScorecardThreshold`, `create_dynamic_recipe` sẽ raise khi
    # dựng recipe) — khai lại `Field` ở đây để FastAPI trả 422 SỚM (đúng lỗi input), thay vì để
    # request đi hết `_evaluate()` rồi mới vỡ ở bước dựng recipe.
    success_threshold: float = Field(default=0.9, ge=0.0, le=1.0)
    citation_accuracy_threshold: float = Field(default=0.95, ge=0.0, le=1.0)
    # `tenant_id` CỐ Ý KHÔNG có field ở đây — request body không có chỗ nào để client khai nó;
    # tenant luôn tới từ `get_request_session()`, không phải body (đúng nguyên tắc "session luôn
    # thắng client tự khai" — INV-1 — nhưng ở đây còn mạnh hơn: request THẬM CHÍ KHÔNG THỂ mang
    # trường đó, chứ không phải "mang được nhưng bị bỏ qua").


class RunResponse(BaseModel):
    run_id: str
    agent_id: str
    tenant_id: str
    events: list[dict[str, Any]]
    timeline_text: str


@router.post("", response_model=RunResponse)
async def create_run(body: RunRequest) -> RunResponse:
    session = get_request_session()

    # Gate role-gap đã phát hiện (trước bản vá này, BẤT KỲ tài khoản đã đăng nhập nào — kể cả
    # employee chỉ có role nội dung, không "admin" — gọi thẳng được route này qua API dù UI không
    # hiện nút Test cho họ). `require_admin` tra roles TƯƠI từ DB, không tin `session.roles` (JWT)
    # — cùng nguyên tắc `routes/admin.py` đã dùng xuyên suốt.
    identity = await fetch_fresh_identity(get_request_connection(), session.user)
    require_admin(identity.roles)

    try:
        nodes = [Node.model_validate(n) for n in body.nodes]
        edges = [Edge.model_validate(e) for e in body.edges]
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"node/edge không hợp lệ: {exc.errors()!r}") from exc

    try:
        recipe = create_dynamic_recipe(
            agent_id=body.agent_id,
            tenant_id=session.tenant_id,
            instructions=body.instructions,
            model=body.model,
            tool_whitelist=body.tool_whitelist,
            nodes=nodes,
            edges=edges,
            kb_id=body.kb_id,
            scope=body.scope,
            golden_set_ref=body.golden_set_ref,
            success_threshold=body.success_threshold,
            citation_accuracy_threshold=body.citation_accuracy_threshold,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        graph_lint(recipe)
    except ValueError as exc:
        # Belt-and-suspenders phía server, cùng lý do comment gốc ở `dev_playground_server.py`:
        # UI đã chặn export/test khi `graph_lint()` đỏ, nhưng client vẫn có thể gửi thẳng qua API.
        raise HTTPException(status_code=400, detail=f"recipe không qua graph_lint(): {exc}") from exc

    # `Pool` (`get_pool()`), KHÔNG dùng `get_request_connection()` — review `app#17` đợt 3, "mở
    # rộng connection-reuse pattern sang runs.py/publish.py/chat.py?". QUYẾT ĐỊNH: KHÔNG mở rộng
    # trong PR này, nhưng đây KHÔNG PHẢI vì get_pool() "tiết kiệm" connection hơn — bản comment đầu
    # ở đây (đợt 3) khẳng định vậy là SAI, đã tự phản chứng (đợt 4): `middleware.py`'s
    # `tenant_context_middleware` ĐÃ giữ 1 connection từ CHÍNH pool này (`await pool.connection()`)
    # bọc quanh TOÀN BỘ `await call_next(request)` — tức MỌI request (bất kể route) đã tốn 1
    # connection suốt đời request rồi, KHÔNG PHỤ THUỘC route này dùng `get_pool()` hay
    # `get_request_connection()`. `get_pool()` ở đây là connection THỨ HAI, checkout/trả lại riêng
    # theo từng query của `PgKbSearch`/`PgTraceWriter` bên trong `interpreter.run()` — CỘNG THÊM
    # vào connection middleware đã giữ, không phải thay thế nó. Với `max_size=8` (`core/_db.py`),
    # dù route dùng `get_request_connection()` (không cộng thêm) hay `get_pool()` (cộng thêm tạm
    # thời mỗi query) thì phần đáy — 1 connection/request suốt cả lượt `interpreter.run()` có thể
    # kéo dài nhiều giây vì gọi LLM — VẪN CÒN NGUYÊN, khoảng 8 request đồng thời (bất kỳ route nào,
    # không riêng route này) đã đủ ăn hết pool. Đây là rủi ro Important còn tồn tại từ đợt 3, CHƯA
    # được PR này giải quyết — sửa đúng cần 1 trong 2: (a) đổi `PgKbSearch`/`PgTraceWriter`
    # (`packages/kb`) nhận 1 connection thay vì cả `Pool`, để route này tái dùng
    # `get_request_connection()` (giảm về đúng 1 connection/request, không cộng thêm — nhưng đổi
    # chữ ký 1 package dùng chung, ngoài scope PR này), hoặc (b) tái cấu trúc middleware để không
    # giữ connection xuyên suốt `call_next()` cho các route không cần nó suốt cả lượt chạy. Không
    # làm ở đây — ghi lại tường minh làm follow-up, không phải để im như đợt 3 đã cảnh báo.
    pool = await get_pool()
    embedding: EmbeddingService = build_embedding()
    kb_search = PgKbSearch(pool, embedding)
    llm = build_llm()
    trace_writer = PgTraceWriter(pool)

    result = await interpreter.run(
        recipe,
        session_context=session,
        kb_search=kb_search,
        llm=llm,
        embedding=embedding,
        trace_writer=trace_writer,
    )

    timeline_text = render_timeline(result.events, expected=walk_from_dag(recipe.dag))
    return RunResponse(
        run_id=result.run_id,
        agent_id=recipe.agent_id,
        tenant_id=str(recipe.tenant_id),
        events=[e.model_dump(mode="json") for e in result.events],
        timeline_text=timeline_text,
    )


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(run_id: str) -> RunResponse:
    session = get_request_session()
    pool = await get_pool()
    events = await PgTraceReader(pool).read_run(run_id, session.tenant_id)
    if not events:
        raise HTTPException(status_code=404, detail=f"run_id {run_id!r} không tìm thấy cho tenant hiện tại")
    return RunResponse(
        run_id=run_id,
        agent_id=events[0].agent_id,
        tenant_id=str(session.tenant_id),
        events=[e.model_dump(mode="json") for e in events],
        # `render_timeline` cần `expected` từ `dag` gốc — GET không giữ recipe (chỉ đọc lại
        # trace), nên không tái tạo được cột "gap" chính xác ở đây; để trống có chủ đích, khác
        # `dev_playground_server.py` (nó cache `timeline_text` lúc POST). Theo dõi như 1 hạn chế
        # đã biết, không phải lỗi — client nên dùng `timeline_text` từ response POST gốc.
        timeline_text="",
    )
