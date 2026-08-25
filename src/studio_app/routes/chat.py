"""`POST /api/agents/{agent_id}/chat` (Kế hoạch 2, A5) — chat với agent ĐÃ PUBLISH.

**app#44 — nối qua `run_agent_loop()` (engine#33), bỏ hẳn DAG-walk cũ.** Trước app#44, route này
phải thay `params["query"]` của MỌI node `kb-retrieve` bằng tin nhắn user gửi (`with_query()`)
TRƯỚC KHI chạy `interpreter.run()`, vì `Recipe.dag`'s node `kb-retrieve` mang `query` CỐ ĐỊNH tại
thời điểm build recipe. Kiến trúc mới (`PROJECT-SCOPE-DEMO-DAY30.md`) không còn khái niệm đó:
`run_agent_loop()` nhận `question: str` trực tiếp qua kwarg, KHÔNG đọc `recipe.dag` chút nào — nên
`with_query()` không còn cần thiết ở đây nữa (recipe published dùng NGUYÊN VẸN).

Cũng bỏ `graph_lint()`/`find_unsupported_tool_call()`: cả hai thuộc lớp kiểm DAG-shaped (7 luật đồ
thị / quét node `tool-call` trong `recipe.dag`) — `PROJECT-SCOPE-DEMO-DAY30.md` mục C nói rõ
"Không còn trong demo: banner lỗi `graph_lint` 7 luật đồ thị cũ", và `run_agent_loop()` không bao
giờ đọc `recipe.dag` nên không có gì để 2 hàm đó kiểm trước nữa — 1 tool ngoài `tool_whitelist` giờ
chỉ có thể tới từ chính LLM tự phát `TOOL_CALL:` lúc CHẠY, bị chặn bởi `WhitelistGuardedDispatch`
(engine, belt 2) và raise `ValueError` — bắt ở khối `except` bên dưới, không còn preflight tĩnh.

3 exception MỚI `run_agent_loop()` có thể ném mà `interpreter.run()` không có (docstring
`agent_loop.py`, "Handoff to app#44") — `AgentLoopExhausted`/`ValueError`/`PermissionError` — đều
map **500** (quyết định chốt với user qua AskUserQuestion, cùng phiên app#44): cả 3 là lỗi hệ
thống/recipe đã published nhưng hỏng khi CHẠY, không phải lỗi INPUT của client gọi `/chat`, cùng
mức nghiêm trọng `graph_lint()` đỏ/tool không dispatch được mà route này từng dùng trước app#44.

Tenant fence: KHÔNG tự thêm `WHERE tenant_id = %s` — dựa hẳn vào RLS của `wb.recipes`
(`schema.py`, `USING (tenant_id = current_setting('app.tenant_id'))`) trên connection đã
`SET LOCAL` bởi `tenant_context_middleware`, đúng nguyên tắc "RLS là hàng rào, không phải lớp phụ"
đã dùng cho `kb.chunks`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from studio_contracts import Recipe
from studio_engine import agent_loop
from studio_engine.agent_loop import AgentLoopExhausted
from studio_kb.postgres import PgKbSearch
from studio_workbench.tenant_wall import ResolvedContext

from studio_app.authz import fetch_fresh_identity, fetch_tenant_section_names, require_admin
from studio_app.core._db import get_pool
from studio_app.eval_adapter import _llm_answer
from studio_app.middleware import get_request_connection, get_request_session
from studio_app.obs.trace_writer import PgTraceWriter
from studio_app.providers.factory import build_embedding, build_llm, build_tool_dispatch

router = APIRouter(prefix="/api/agents", tags=["chat"])


class ChatRequest(BaseModel):
    message: str
    # Admin-only (kiểm ở route, không tin client) — "giả lập" chat như 1 nhân viên chỉ có ĐÚNG tập
    # role này, để admin tự kiểm nội dung nhân viên phòng ban X thấy được gì TRƯỚC khi tin agent,
    # KHÔNG cần tạo tài khoản nhân viên thật để test. `None` (mặc định) = dùng nguyên roles thật
    # của người gọi (đã mở rộng đủ mọi section nếu là admin, xem `routes/auth.py::login`).
    as_roles: list[str] | None = None


class ChatResponse(BaseModel):
    answer: str
    citations: list[str]
    refused: bool
    run_id: str
    version: int


async def _load_published_recipe(agent_id: str) -> tuple[Recipe, int]:
    """RLS (`wb.recipes`) tự lọc theo `app.tenant_id` của connection hiện tại — không thêm
    `WHERE tenant_id` tay ở đây, tránh 2 nguồn sự thật lệch nhau về tenant.

    Trả kèm `version` — client cần biết ĐANG chat với bản nào (đổi sau publish/rollback), không
    chỉ nội dung recipe."""
    conn = get_request_connection()
    cursor = await conn.execute(
        "SELECT recipe, version FROM wb.recipes WHERE agent_id = %s AND status = 'published' "
        "ORDER BY version DESC LIMIT 1",
        (agent_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"agent_id {agent_id!r} chưa có bản published nào cho tenant hiện tại",
        )
    return Recipe.model_validate(row[0]), row[1]


@router.post("/{agent_id}/chat", response_model=ChatResponse)
async def chat(agent_id: str, body: ChatRequest) -> ChatResponse:
    session = get_request_session()

    recipe, version = await _load_published_recipe(agent_id)
    if recipe.tenant_id != session.tenant_id:
        # Không nên trúng được (RLS đã lọc ở `_load_published_recipe`) — giữ lại như hàng rào thứ
        # hai đúng tinh thần defense-in-depth toàn codebase này theo, không tin RLS là đủ một mình.
        raise HTTPException(status_code=403, detail="recipe published thuộc tenant khác phiên hiện tại")

    # `body.as_roles` — CHỈ admin/superadmin được giả lập role khác (tra TƯƠI từ DB, không tin
    # `session.system_roles`/JWT, cùng nguyên tắc mọi route nhạy cảm khác). Validate mỗi role giả lập phải
    # là 1 section THẬT của tenant hiện tại — chặn gõ nhầm/role rác, không phải mở rộng quyền (chỉ
    # THU HẸP xuống 1 tập con, không bao giờ tự thêm được role không tồn tại).
    session_context: ResolvedContext = session
    if body.as_roles is not None:
        conn = get_request_connection()
        identity = await fetch_fresh_identity(conn, session.user)
        require_admin(identity.system_roles)
        # `fetch_tenant_section_names` tự trừ `RESERVED_ROLE_NAMES` (review app#21 — tầng 2 lặp
        # lại, xem docstring hàm đó): trước bản vá, 1 dòng `core.sections` cũ tên "superadmin" sẽ
        # lọt vào `valid_section_names`, cho phép `as_roles=["superadmin"]` đi thẳng vào
        # `session_context.roles` — đầu vào của hàng rào nội dung KB ở `run_agent_loop()` (trước
        # app#44: `interpreter.run()` — cùng cơ chế fence, `fenced_kb_params`, dùng chung).
        valid_section_names = await fetch_tenant_section_names(conn, session.tenant_id)
        invalid = set(body.as_roles) - valid_section_names
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"role {sorted(invalid)} không hợp lệ để giả lập — chỉ chấp nhận {sorted(valid_section_names)}",
            )
        session_context = ResolvedContext(tenant_id=session.tenant_id, user=session.user, system_roles=body.as_roles)

    # `Pool` (không phải `get_request_connection()`) — rủi ro pool self-deadlock CHƯA được giải
    # quyết ở route này, xem giải thích đầy đủ + lý do ở `routes/runs.py` (review `app#17` đợt 4,
    # sửa lại 1 lập luận SAI ở đợt 3: `get_pool()` là connection THỨ HAI cộng thêm vào connection
    # middleware đã giữ suốt request, không phải "tiết kiệm" hơn).
    pool = await get_pool()
    embedding = build_embedding()
    try:
        result = await agent_loop.run_agent_loop(
            recipe,
            session_context=session_context,
            kb_search=PgKbSearch(pool, embedding),
            llm=build_llm(),
            embedding=embedding,
            trace_writer=PgTraceWriter(pool),
            question=body.message,
            tool_dispatch=build_tool_dispatch(recipe.agent_config.tool_whitelist),
        )
    except (AgentLoopExhausted, ValueError, PermissionError) as exc:
        # app#44 — 3 exception `run_agent_loop()` có thể ném mà `interpreter.run()` không có (xem
        # docstring module này + "Handoff to app#44" ở `agent_loop.py`): `AgentLoopExhausted` (hết
        # `max_turns` chưa trả lời cuối), `ValueError` (LLM tự phát `TOOL_CALL:` một tool NGOÀI
        # `tool_whitelist` — thay thế preflight tĩnh `find_unsupported_tool_call()` đã bỏ ở trên,
        # giờ bắt lúc CHẠY qua `WhitelistGuardedDispatch`), `PermissionError` (`tenant_id` hỏng sau
        # fence — phòng thủ, không nên trúng thật qua `session` thật). Cả 3 map 500 — quyết định
        # chốt với user (AskUserQuestion, cùng phiên app#44), cùng mức nghiêm trọng `graph_lint()`
        # đỏ/tool không dispatch được mà route này từng dùng (đã bỏ ở trên). Không bắt bằng
        # `except Exception` chung: giữ MỌI exception KHÁC (bug thật) lộ nguyên dạng.
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

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
        version=version,
    )
