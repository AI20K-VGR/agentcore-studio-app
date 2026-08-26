"""`POST /api/agents/{agent_id}/test-chat` — chat THẬT trên recipe DRAFT (canvas, CHƯA publish).

Thay thế nút "Test" cũ (`routes/runs.py::create_run`, connectivity-check tĩnh OK/NOT_IMPLEMENTED,
đã bỏ cùng bản vá này) — admin giờ tự nghĩ 1 case, hỏi thật, nhận trả lời thật + xem trace, ngay
trong lúc kéo thả cấu hình trên canvas, TRƯỚC khi Chấm điểm/Publish. 1 lượt chat thật tự nói lên
tool có chạy được hay không, rõ hơn hẳn bảng OK/NOT_IMPLEMENTED tĩnh.

**Tách biệt tuyệt đối với `routes/chat.py`** (chat với agent ĐÃ publish, `wb.recipes`) — route này
KHÔNG đọc/ghi `wb.recipes`, dựng `Recipe` in-memory từ chính `nodes`/`edges` client đang chỉnh trên
canvas, cùng cơ chế `routes/publish.py::_evaluate` dùng để chấm golden-set (dựng recipe qua
`create_recipe(...)`, không ghi DB). Khác `_evaluate`: route này chạy ĐÚNG 1 câu hỏi tự do (không
phải nguyên golden-set), nên gọi thẳng `agent_loop.run_agent_loop()` — bỏ qua hẳn lớp
`EvalHarness`/`LLMJudge`/`EngineAgentRunner` (chỉ cần thiết cho batch case + judge).

Không có `golden_set_ref` trong request — route này không chấm golden-set, dùng default của
`create_recipe()`. Không có `as_roles` — luôn chạy full quyền admin đang gọi (quyết định chốt cùng
user: admin tự test agent đang edit không cần giả lập role nhân viên; việc đó thuộc về tab "Dùng
thử" sau khi publish, xem `routes/chat.py`).

`enforce_agent_shape(recipe)` + `enforce_agent_topology(recipe)` CÓ chạy trước khi cho chat (khác
`routes/chat.py`, nơi đã bỏ hẳn từ app#44 vì recipe published không cần kiểm lại) — chặn sớm lỗi
cấu trúc DAG cho canvas đang dở, tránh tốn 1 lượt gọi LLM trên 1 recipe sẽ bị 2 hàm này chặn lúc
publish sau này (app#78/workbench#48 — trước là 1 `graph_lint(recipe)`, nay tách 2 hàm).

Trace: `run_id` tự sinh trong `run_agent_loop()`, ghi qua `PgTraceWriter` vào `obs.trace_events` —
độc lập hoàn toàn với `wb.recipes` (không FK/JOIN), đọc lại được ngay bằng `GET /api/runs/{run_id}`
(`routes/runs.py::get_run`, không cần sửa gì thêm)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError
from studio_contracts import Edge, Node
from studio_engine import agent_loop
from studio_engine.agent_loop import AgentLoopExhausted
from studio_kb.postgres import PgKbSearch
from studio_workbench import create_recipe
from studio_workbench.tenant_wall import ResolvedContext
from studio_workbench.validator import enforce_agent_shape, enforce_agent_topology

from studio_app.authz import fetch_fresh_identity, require_admin
from studio_app.core._db import get_pool
from studio_app.eval_adapter import _llm_answer
from studio_app.middleware import get_request_connection, get_request_session
from studio_app.obs.trace_writer import PgTraceWriter
from studio_app.providers.factory import build_embedding, build_llm, build_tool_dispatch

router = APIRouter(prefix="/api/agents", tags=["test-chat"])


class TestChatRequest(BaseModel):
    """Cùng khuôn `routes/publish.py::PublishRequest`, bớt `golden_set_ref` (route này không chấm
    golden-set) và thêm `message` (câu hỏi tự do admin tự nghĩ). `tenant_id` CỐ Ý KHÔNG có field —
    cùng kỷ luật `RunRequest`/`PublishRequest`: tenant luôn tới từ `get_request_session()`."""

    agent_id: str
    system_prompt: str
    tool_whitelist: list[str] = Field(default_factory=list)
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    message: str


class TestChatResponse(BaseModel):
    answer: str
    citations: list[str]
    refused: bool
    run_id: str


async def _run_test_chat(agent_id: str, body: TestChatRequest, session: ResolvedContext) -> TestChatResponse:
    """Tách khỏi route handler (cùng khuôn `routes/publish.py::_evaluate` vs
    `evaluate_agent`/`publish_agent`) — hàm này không chạm `require_admin`/`fetch_fresh_identity`
    (không cần DB để tra identity), nên test map exception/wiring gọi thẳng hàm này mà không cần
    Postgres thật; gate quyền chỉ sống trong `test_chat()` (route) bên dưới."""
    if body.agent_id != agent_id:
        raise HTTPException(
            status_code=400,
            detail=f"agent_id trên path ({agent_id!r}) khác agent_id trong body ({body.agent_id!r})",
        )

    try:
        nodes = [Node.model_validate(n) for n in body.nodes]
        edges = [Edge.model_validate(e) for e in body.edges]
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"node/edge không hợp lệ: {exc.errors()!r}") from exc

    try:
        recipe = create_recipe(
            agent_id=body.agent_id,
            tenant_id=session.tenant_id,
            system_prompt=body.system_prompt,
            tool_whitelist=body.tool_whitelist,
            nodes=nodes,
            edges=edges,
            temperature=body.temperature,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        # app#78 (workbench#48 breaking rename) — xem cùng comment ở routes/publish.py::_evaluate.
        enforce_agent_shape(recipe)
        enforce_agent_topology(recipe)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"recipe không qua validator: {exc}") from exc

    # `Pool` (không phải `get_request_connection()`) — cùng lý do đã giải thích ở `routes/chat.py`/
    # `routes/publish.py` (review `app#17` đợt 4): `get_request_connection()` là connection MIDDLEWARE
    # đã giữ suốt request, `get_pool()` là 1 connection RIÊNG cho `PgKbSearch`/`PgTraceWriter`.
    pool = await get_pool()
    embedding = build_embedding()
    try:
        result = await agent_loop.run_agent_loop(
            recipe,
            session_context=session,
            kb_search=PgKbSearch(pool, embedding),
            llm=build_llm(),
            embedding=embedding,
            trace_writer=PgTraceWriter(pool),
            question=body.message,
            tool_dispatch=build_tool_dispatch(recipe.agent_config.tool_whitelist),
        )
    except (AgentLoopExhausted, ValueError, PermissionError) as exc:
        # Cùng 3 exception + cùng ánh xạ 500 đã chốt ở `routes/chat.py` (xem docstring module đó) —
        # lỗi hệ thống/recipe hỏng khi CHẠY, không phải lỗi INPUT của client gọi route này.
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

    try:
        llm_out = _llm_answer(result.final_state)
    except LookupError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    raw_citations = llm_out.get("citations")
    return TestChatResponse(
        answer=str(llm_out.get("answer", "")),
        citations=[str(c) for c in raw_citations] if isinstance(raw_citations, list) else [],
        refused=bool(llm_out.get("refused", False)),
        run_id=result.run_id,
    )


@router.post("/{agent_id}/test-chat", response_model=TestChatResponse)
async def test_chat(agent_id: str, body: TestChatRequest) -> TestChatResponse:
    session = get_request_session()

    # Cùng gate role-gap các route admin khác (`routes/publish.py::evaluate_agent`/`publish_agent`).
    identity = await fetch_fresh_identity(get_request_connection(), session.user)
    require_admin(identity.system_roles)

    return await _run_test_chat(agent_id, body, session)
