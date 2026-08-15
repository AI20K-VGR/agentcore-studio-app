"""`POST /api/agents/{agent_id}/chat` (Kế hoạch 2, A5) — chat với agent ĐÃ PUBLISH.

Rủi ro thiết kế lớn nhất trong Kế hoạch 2, nói thẳng trong chính docstring này: `Recipe.dag`'s
node `kb-retrieve` mang `query` CỐ ĐỊNH tại thời điểm build recipe (`builder.py`'s tham số
`query: str`) — contract hiện tại KHÔNG có khái niệm "hỏi câu khác mỗi lượt chat" trên cùng 1
recipe đã publish. Route này vì vậy KHÔNG thể chỉ đọc `wb.recipes.recipe` rồi chạy thẳng — nó phải
`model_copy` recipe đã lưu, thay `params["query"]` của MỌI node `kb-retrieve` bằng tin nhắn user
gửi, TRƯỚC KHI chạy `interpreter.run()`. Đây là biến đổi CỤC BỘ trong route này — không đổi
`wb.recipes` đã lưu, không đổi contract `Recipe`/`Node`.

Tenant fence: KHÔNG tự thêm `WHERE tenant_id = %s` — dựa hẳn vào RLS của `wb.recipes`
(`schema.py`, `USING (tenant_id = current_setting('app.tenant_id'))`) trên connection đã
`SET LOCAL` bởi `tenant_context_middleware`, đúng nguyên tắc "RLS là hàng rào, không phải lớp phụ"
đã dùng cho `kb.chunks`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from studio_contracts import NodeType, Recipe
from studio_engine import interpreter
from studio_kb.postgres import PgKbSearch
from studio_workbench.validator import graph_lint

from studio_app.core._db import get_pool
from studio_app.eval_adapter import _llm_answer
from studio_app.middleware import get_request_connection, get_request_session
from studio_app.obs.trace_writer import PgTraceWriter
from studio_app.providers.factory import CallistoEmbedding, build_llm

router = APIRouter(prefix="/api/agents", tags=["chat"])


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    answer: str
    citations: list[str]
    refused: bool
    run_id: str


async def _load_published_recipe(agent_id: str) -> Recipe:
    """RLS (`wb.recipes`) tự lọc theo `app.tenant_id` của connection hiện tại — không thêm
    `WHERE tenant_id` tay ở đây, tránh 2 nguồn sự thật lệch nhau về tenant."""
    conn = get_request_connection()
    cursor = await conn.execute(
        "SELECT recipe FROM wb.recipes WHERE agent_id = %s AND status = 'published' ORDER BY version DESC LIMIT 1",
        (agent_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"agent_id {agent_id!r} chưa có bản published nào cho tenant hiện tại",
        )
    return Recipe.model_validate(row[0])


def _with_query(recipe: Recipe, message: str) -> Recipe:
    """`model_copy` toàn bộ `dag.nodes`, thay `params['query']` của MỌI node `kb-retrieve` bằng
    `message` — KHÔNG đụng `wb.recipes` đã lưu (không ghi gì lại DB ở route này)."""
    new_nodes = [
        node.model_copy(update={"params": {**node.params, "query": message}})
        if node.type is NodeType.KB_RETRIEVE
        else node
        for node in recipe.dag.nodes
    ]
    return recipe.model_copy(update={"dag": recipe.dag.model_copy(update={"nodes": new_nodes})})


@router.post("/{agent_id}/chat", response_model=ChatResponse)
async def chat(agent_id: str, body: ChatRequest) -> ChatResponse:
    session = get_request_session()

    recipe = await _load_published_recipe(agent_id)
    if recipe.tenant_id != session.tenant_id:
        # Không nên trúng được (RLS đã lọc ở `_load_published_recipe`) — giữ lại như hàng rào thứ
        # hai đúng tinh thần defense-in-depth toàn codebase này theo, không tin RLS là đủ một mình.
        raise HTTPException(status_code=403, detail="recipe published thuộc tenant khác phiên hiện tại")

    recipe = _with_query(recipe, body.message)

    try:
        graph_lint(recipe)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=f"recipe đã published nhưng graph_lint() đỏ: {exc}") from exc

    # `Pool` (không phải `get_request_connection()`) — cùng lý do đã ghi ở `routes/runs.py` (review
    # `app#17` đợt 3): `interpreter.run()` bên dưới trải dài qua nhiều truy vấn KB + gọi LLM (độ trễ
    # giây), giữ 1 connection request cố định suốt đó tốn pool capacity hơn, không phải ít hơn.
    pool = await get_pool()
    embedding = CallistoEmbedding()
    result = await interpreter.run(
        recipe,
        session_context=session,
        kb_search=PgKbSearch(pool, embedding),
        llm=build_llm(),
        embedding=embedding,
        trace_writer=PgTraceWriter(pool),
    )

    try:
        llm_out = _llm_answer(result.final_state)
    except LookupError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    raw_citations = llm_out.get("citations")
    return ChatResponse(
        answer=str(llm_out.get("answer", "")),
        citations=[str(c) for c in raw_citations] if isinstance(raw_citations, list) else [],
        refused=bool(llm_out.get("refused", False)),
        run_id=result.run_id,
    )
