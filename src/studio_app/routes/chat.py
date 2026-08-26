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
đã dùng cho `kb.chunks`. `wb.conversations`/`wb.conversation_messages` (app#74, dưới đây) theo
đúng cùng nguyên tắc đó.

**app#74 — `conversation_id` + lịch sử đa lượt (`kit#240`, phụ thuộc `engine#47`/`workbench#49`,
cả 2 đã merge vào con trỏ kit đang pin).** `ChatRequest.conversation_id` (`None` = phiên chat mới,
1 dòng `wb.conversations` được tạo và trả về; có giá trị = tiếp tục 1 phiên đã có, đọc tối đa
`_CONVERSATION_HISTORY_CAP` lượt gần nhất từ `wb.conversation_messages` truyền vào
`run_agent_loop(history=...)`). `agent_id` phải khớp `wb.conversations.agent_id` — không chỉ tenant
fence (RLS đã lo) mà còn là fence GIỮA CÁC AGENT cùng tenant (system_prompt/tool_whitelist/role-fence
có thể khác nhau) — không khớp/không tồn tại (kể cả conversation thuộc tenant khác, RLS trả 0 dòng)
→ 404. Chặn ở bước ĐỌC là đủ: FK Postgres không bị RLS chặn (chạy như owner bảng), nên nếu không
chặn từ đây, 1 client đoán được `conversation_id` thật của tenant khác vẫn có thể INSERT thêm
message vào record của tenant đó.

`_CONVERSATION_HISTORY_CAP` (10) là hằng số CỤC BỘ, phải giữ khớp tay với
`studio_engine.agent_loop._MAX_HISTORY_TURNS` — không import thẳng tên private đó (quy ước
per-module, không phải seam liên-package) và không SELECT không giới hạn rồi để `run_agent_loop`
tự cắt (chính docstring `agent_loop.py` cảnh báo "pass everything, the loop will cap it" KHÔNG
free — vẫn tốn round-trip DB không cần thiết).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel
from studio_contracts import Recipe
from studio_engine import agent_loop
from studio_engine.agent_loop import AgentLoopExhausted
from studio_engine.agent_protocol import HistoryTurn
from studio_kb.postgres import PgKbSearch
from studio_workbench.tenant_wall import ResolvedContext

from studio_app.authz import fetch_fresh_identity, fetch_tenant_section_names, require_admin
from studio_app.core._db import get_pool
from studio_app.eval_adapter import _llm_answer
from studio_app.middleware import get_request_connection, get_request_session
from studio_app.obs.trace_writer import PgTraceWriter
from studio_app.providers.factory import build_embedding, build_llm, build_tool_dispatch

router = APIRouter(prefix="/api/agents", tags=["chat"])

# app#74 — phải giữ khớp tay với `studio_engine.agent_loop._MAX_HISTORY_TURNS` (xem docstring
# module trên). Không export public từ engine để import trực tiếp — đây là quy ước riêng của
# module đó, không phải hợp đồng liên-package.
_CONVERSATION_HISTORY_CAP = 10


class ChatRequest(BaseModel):
    message: str
    # Admin-only (kiểm ở route, không tin client) — "giả lập" chat như 1 nhân viên chỉ có ĐÚNG tập
    # role này, để admin tự kiểm nội dung nhân viên phòng ban X thấy được gì TRƯỚC khi tin agent,
    # KHÔNG cần tạo tài khoản nhân viên thật để test. `None` (mặc định) = dùng nguyên roles thật
    # của người gọi (đã mở rộng đủ mọi section nếu là admin, xem `routes/auth.py::login`).
    as_roles: list[str] | None = None
    # app#74 — `None` = bắt đầu 1 phiên chat mới (server tự tạo `wb.conversations`, id sinh ra trả
    # về trong response). Có giá trị = tiếp tục phiên đã có, đọc lịch sử để thread vào prompt.
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    answer: str
    citations: list[str]
    refused: bool
    run_id: str
    version: int
    conversation_id: str


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


def _parse_conversation_id(raw: str, *, label: str = "conversation_id") -> str:
    """app#74 — validate TƯỜNG MINH trước khi đưa vào SQL, cùng tinh thần
    `_normalise_top_k`/`ValidationError -> 400` (`test_chat.py`): `wb.conversations.id` là cột
    `UUID`, một chuỗi rác đưa thẳng vào `%s` sẽ rơi thành lỗi Postgres thô
    (`InvalidTextRepresentation`) không bắt được sạch — chặn ở đây cho ra 400 rõ ràng thay vì để
    lỗi DB lộ nguyên dạng thành 500."""
    try:
        UUID(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{label} {raw!r} không phải UUID hợp lệ") from exc
    return raw


async def _load_conversation_history(agent_id: str, conversation_id: str) -> list[HistoryTurn]:
    """app#74 — `conversation_id` đã qua `_parse_conversation_id`. `agent_id` khớp là fence THẬT
    (không phải lặp lại RLS) — xem docstring module. Không thấy (kể cả vì RLS lọc mất do khác
    tenant) → 404, một nhánh DUY NHẤT cho cả "không tồn tại" lẫn "thuộc tenant/agent khác", không
    rò tin tồn-tại-hay-không của record thuộc chủ khác."""
    conn = get_request_connection()
    cursor = await conn.execute(
        "SELECT id FROM wb.conversations WHERE id = %s AND agent_id = %s",
        (conversation_id, agent_id),
    )
    if await cursor.fetchone() is None:
        raise HTTPException(
            status_code=404,
            detail=f"conversation_id {conversation_id!r} không tồn tại cho agent_id {agent_id!r}",
        )
    cursor = await conn.execute(
        "SELECT question, answer FROM wb.conversation_messages WHERE conversation_id = %s "
        "ORDER BY turn_index DESC LIMIT %s",
        (conversation_id, _CONVERSATION_HISTORY_CAP),
    )
    rows = await cursor.fetchall()
    # DESC + LIMIT lấy đúng N lượt GẦN NHẤT; `reversed()` trả lại thứ tự cũ->mới mà
    # `HistoryTurn`/`build_agent_prompt` (`agent_protocol.py`) mong đợi.
    return [HistoryTurn(question=str(row[0]), answer=str(row[1])) for row in reversed(rows)]


async def _start_new_conversation(agent_id: str, tenant_id: object) -> str:
    """app#74 — `body.conversation_id is None`: mở 1 phiên chat mới, `id` sinh bởi DB
    (`gen_random_uuid()`, `schema.py`), không có lịch sử nào để thread vào lượt đầu tiên."""
    conn = get_request_connection()
    cursor = await conn.execute(
        "INSERT INTO wb.conversations (tenant_id, agent_id) VALUES (%s, %s) RETURNING id",
        (str(tenant_id), agent_id),
    )
    row = await cursor.fetchone()
    assert row is not None  # INSERT ... RETURNING luôn trả đúng 1 dòng khi không lỗi
    return str(row[0])


async def _record_conversation_turn(
    *,
    conversation_id: str,
    tenant_id: object,
    question: str,
    answer: str,
    citations: list[str],
    run_id: str,
) -> None:
    """app#74 — ghi lại 1 lượt Q/A THÀNH CÔNG (gọi sau khi `run_agent_loop` + `_llm_answer` đã
    xong sạch — 1 lượt lỗi/500 không để lại record nào). `turn_index` = MAX hiện có + 1, truy vấn
    RIÊNG (không tái dùng dòng đầu của SELECT lịch sử ở `_load_conversation_history` — 2 việc khác
    nhau, dễ vỡ nếu sau này ai đổi `ORDER BY`/`LIMIT` ở đó). Race 2 request cùng
    `conversation_id` ghi đồng thời: `UNIQUE (conversation_id, turn_index)` sẽ raise cho request
    thua — chấp nhận như rủi ro đã biết, issue không yêu cầu khoá/retry."""
    conn = get_request_connection()
    cursor = await conn.execute(
        "SELECT COALESCE(MAX(turn_index), 0) + 1 FROM wb.conversation_messages WHERE conversation_id = %s",
        (conversation_id,),
    )
    row = await cursor.fetchone()
    assert row is not None
    turn_index = row[0]
    await conn.execute(
        "INSERT INTO wb.conversation_messages "
        "(conversation_id, tenant_id, turn_index, question, answer, citations, run_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (conversation_id, str(tenant_id), turn_index, question, answer, Jsonb(citations), run_id),
    )


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

    # app#74 — resolve `conversation_id`/`history` TRƯỚC `run_agent_loop`, cùng connection request-
    # scope (RLS + fence agent_id, xem docstring module + 2 helper trên). Phiên đã có (`conversation_id`
    # có giá trị) thì đọc lịch sử ngay — không có gì để trì hoãn. Phiên MỚI thì CHƯA tạo
    # `wb.conversations` ở đây (review dholmes0207, PR app#85): `_start_new_conversation` dời xuống
    # sau khi `run_agent_loop`/`_llm_answer` đã thành công sạch, cùng nguyên tắc "không để lại record
    # cho 1 lượt lỗi" đã áp cho `_record_conversation_turn` — trước bản vá, 1 lượt raise
    # `AgentLoopExhausted`/`ValueError`/`PermissionError` (map 500) vẫn để lại 1 dòng `wb.conversations`
    # RỖNG đã COMMIT (HTTPException bị `ExceptionMiddleware` của FastAPI/Starlette nuốt TRƯỚC KHI nổi
    # lên `tenant_context_middleware`, nên khối `pool.connection()` ở đó thoát sạch và COMMIT — verify
    # thật bằng probe, không chỉ suy luận) — rác không ai chạm tới được vì client chỉ nhận 500, không
    # bao giờ biết `conversation_id` đó.
    conversation_id: str | None
    if body.conversation_id is not None:
        conversation_id = _parse_conversation_id(body.conversation_id)
        history = await _load_conversation_history(agent_id, conversation_id)
    else:
        conversation_id = None
        history = []

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
            history=history,
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
    answer = str(llm_out.get("answer", ""))
    citations = [str(c) for c in raw_citations] if isinstance(raw_citations, list) else []

    # app#74 — tạo `wb.conversations` (nếu là phiên mới) VÀ ghi lại lượt này CHỈ SAU KHI mọi thứ ở
    # trên đã thành công sạch (không để lại record nào — kể cả dòng conversation cha — cho 1 lượt
    # lỗi/500). Xem comment dài ở chỗ resolve `conversation_id`/`history` phía trên.
    if conversation_id is None:
        conversation_id = await _start_new_conversation(agent_id, session.tenant_id)

    await _record_conversation_turn(
        conversation_id=conversation_id,
        tenant_id=session.tenant_id,
        question=body.message,
        answer=answer,
        citations=citations,
        run_id=result.run_id,
    )

    return ChatResponse(
        answer=answer,
        citations=citations,
        refused=bool(llm_out.get("refused", False)),
        run_id=result.run_id,
        version=version,
        conversation_id=conversation_id,
    )


class ConversationTurn(BaseModel):
    turn_index: int
    question: str
    answer: str
    citations: list[str]
    run_id: str | None


class ConversationResponse(BaseModel):
    conversation_id: str
    agent_id: str
    turns: list[ConversationTurn]


@router.get("/{agent_id}/conversations/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(agent_id: str, conversation_id: str) -> ConversationResponse:
    """app#74 — đọc lại TOÀN BỘ 1 phiên chat theo `turn_index ASC`, cho UI (`apps/web#28`) hiển
    thị lại lịch sử đầy đủ khi mở lại trang. KHÔNG cắt theo `_CONVERSATION_HISTORY_CAP` — cái cap
    đó chỉ áp cho phần thread vào prompt LLM ở `POST /chat`, UI cần xem đủ.

    Không `require_admin`: khác `GET /api/runs/{run_id}` (`routes/runs.py`, canh admin vì đó là
    trace hệ thống nội bộ) — đây là chính nội dung Q/A người dùng đã thấy lúc chat, không phải dữ
    liệu nội bộ cần thêm 1 lớp quyền."""
    get_request_session()  # 401 nếu chưa đăng nhập — cùng kỷ luật mọi route khác trong file này.
    conversation_id = _parse_conversation_id(conversation_id)

    conn = get_request_connection()
    cursor = await conn.execute(
        "SELECT id FROM wb.conversations WHERE id = %s AND agent_id = %s",
        (conversation_id, agent_id),
    )
    if await cursor.fetchone() is None:
        raise HTTPException(
            status_code=404,
            detail=f"conversation_id {conversation_id!r} không tồn tại cho agent_id {agent_id!r}",
        )

    cursor = await conn.execute(
        "SELECT turn_index, question, answer, citations, run_id FROM wb.conversation_messages "
        "WHERE conversation_id = %s ORDER BY turn_index ASC",
        (conversation_id,),
    )
    rows = await cursor.fetchall()
    turns = [
        ConversationTurn(
            turn_index=row[0],
            question=row[1],
            answer=row[2],
            citations=list(row[3]) if row[3] else [],
            run_id=row[4],
        )
        for row in rows
    ]
    return ConversationResponse(conversation_id=conversation_id, agent_id=agent_id, turns=turns)
