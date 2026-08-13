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

from studio_app.core._db import get_pool
from studio_app.middleware import get_request_session
from studio_app.obs.trace_writer import PgTraceWriter
from studio_app.providers.factory import CallistoEmbedding, build_llm

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
    success_threshold: float = 0.9
    citation_accuracy_threshold: float = 0.95
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

    pool = await get_pool()
    embedding: EmbeddingService = CallistoEmbedding()
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
