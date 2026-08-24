"""Tests cho `EngineAgentRunner` (adapter #29) — Day 6, ĐK-1 của #59 ("kèm test").

Ba lớp:

- **Lớp A** — chạy qua `studio_engine.interpreter` THẬT với 4 collaborator fake (hợp lệ: `studio_app`
  là composition root, `.importlinter` cho import mọi quadrant). Pin đấu-nối thật: map final_state,
  thread tenant_id UUID (D-13), passthrough events, phản ánh trung thực quyết định `refused`.
- **Lớp B** — monkeypatch `eval_adapter.interpreter` (patch reference module-level của adapter) để
  pin hành vi FAIL-CLOSED của mapper trước drift của engine (shape mà engine hôm nay không phát ra).
- **Lớp C** — spine đầy đủ với `ExtractiveFakeLLM`: LLM double CHỈ đọc prompt. Pin đoạn nối mà lớp A
  không chạm được — `kb.search` → `build_prompt` → LLM → `citations`. Lớp A dùng LLM trả chuỗi cố
  định nên nó *bỏ qua* prompt, tức chứng minh được adapter map đúng nhưng KHÔNG chứng minh được
  chunk đã truy xuất thật sự tới được model.

Không nhận fixture DB (`admin_pool`/`pool`) — chạy được ở submodule CI reconstruct (chỉ pytest).

**Không pin luật refusal của engine.** Từ engine#10 (merge 28/07) `LlmStepExecutor` bỏ hẳn
`REFUSAL_SENTINEL` và suy `refused = not citations`. A4 vì thế chạy `interpreter.run` trực tiếp để
LẤY quyết định gốc của engine rồi so với `AgentAnswer` đã map — pin hợp đồng adapter (map trung
thực), không pin luật. Nhờ vậy file này xanh trên cả engine trước lẫn sau #10.
"""

from __future__ import annotations

from uuid import UUID

import pytest
import studio_app.eval_adapter as eval_adapter
from studio_app.eval_adapter import EngineAgentRunner, _llm_answer
from studio_app.providers.fakes import ExtractiveFakeLLM, FakeEmbedding, FakeLLM
from studio_contracts import LLM, Edge, Node, NodeType, Recipe, TraceEvent
from studio_contracts.kb import KbSearchResultItem
from studio_engine import agent_loop as engine_agent_loop
from studio_engine import interpreter as engine_interpreter
from studio_engine.interpreter import RunResult
from studio_workbench import create_dynamic_recipe, create_recipe_d4
from studio_workbench.tenant_wall import resolve_session

# CỐ Ý khác `ANKOR_ID` default của workbench (a0…01) — nếu adapter "quên" thread tenant_id xuống
# create_recipe_d4 và rơi về default thì A2/B8a phải ĐỎ (mutation-check T-1).
TENANT_ID = UUID("b0000000-0000-0000-0000-000000000002")
# Tenant mà "client khai" trong recipe ở bài INV-1 dưới. Cố ý khác `TENANT_ID` (tenant server resolve)
# để hai đường đi được phân biệt: nếu run rơi về giá trị này thì client đã tự chọn được phạm vi đọc.
CLIENT_CLAIMED_TENANT_ID = UUID("c0000000-0000-0000-0000-000000000003")
# Chuỗi KHÔNG mang `[chunk_id]` nào đã truy xuất → engine (mọi phiên bản tới nay) kết luận
# `refused=True`: trước #10 vì khớp `REFUSAL_SENTINEL`, sau #10 vì `not citations` (ngoặc `[REFUSED]`
# có parse ra nhưng bị lọc do không nằm trong `retrieved_chunks`). Đây CHỈ là cách *kích* nhánh từ
# chối để kiểm adapter map lại có trung thực không — không phải hằng số hợp đồng của engine.
_REFUSAL = "[[REFUSED]]"


def _item(chunk_id: str, text: str = "…") -> KbSearchResultItem:
    return KbSearchResultItem(chunk_id=chunk_id, text=text, score=0.9, tenant_id=TENANT_ID, section_role="public")


class _RecordingKbSearch:
    """KbSearch fake: trả preset + ghi lại args (executor gọi POSITIONAL 4 tham số)."""

    def __init__(self, items: list[KbSearchResultItem]) -> None:
        self._items = items
        self.calls: list[tuple[str, UUID, list[str], int]] = []

    async def search(
        self, query: str, tenant_id: UUID, section_roles: list[str], top_k: int
    ) -> list[KbSearchResultItem]:
        self.calls.append((query, tenant_id, list(section_roles), top_k))
        return self._items


class _ScriptedLLM:
    """LLM fake trả 1 chuỗi cố định (FakeLLM của providers là hash-seeded, không điều khiển nội dung được).

    app#44: dưới `run_agent_loop`, 1 chuỗi cố định luôn được đọc là `FinalAnswer` NGAY lượt 1 (không
    khớp `TOOL_CALL:`) — `kb_search` không bao giờ chạy qua double này. Vẫn hợp lệ cho case "trả lời
    ngay, không cần tool" (Lớp A biến thể "không tool"); case cần chunk thật để chấm citation phải
    dùng `_MultiTurnScriptedLLM` bên dưới thay vì cái này (khác DAG cũ, nơi `kb-retrieve` chạy vô
    điều kiện trước `llm-step` nên `_ScriptedLLM` một câu vẫn "thấy" được chunk qua trace dù không
    tự gọi tool)."""

    def __init__(self, response: str) -> None:
        self._response = response

    async def complete(self, prompt: str, **kwargs: object) -> str:  # noqa: ARG002
        return self._response


class _MultiTurnScriptedLLM:
    """LLM fake trả lần lượt từng phần tử `responses` — 1 phần tử = 1 lượt `complete()`. Không đọc
    prompt (khác `ExtractiveFakeLLM`) — dùng khi cần kịch bản CỐ ĐỊNH độc lập với nội dung KB thật
    trả về: mở rộng `_ScriptedLLM` cho vòng lặp mới (engine#33), nơi trigger `kb_search` đòi ít nhất
    2 lượt (`TOOL_CALL:` rồi final-answer) thay vì 1 lượt DAG cũ chạy `kb-retrieve` vô điều kiện.
    Gọi quá số phần tử đã script → `IndexError` thẳng (không lặp lại phần tử cuối, tránh che giấu
    lỗi kịch bản thiếu lượt)."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self._calls = 0

    async def complete(self, prompt: str, **kwargs: object) -> str:  # noqa: ARG002
        response = self._responses[self._calls]
        self._calls += 1
        return response


class _CollectingTraceWriter:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    async def write(self, event: TraceEvent) -> None:
        self.events.append(event)


def _runner(kb: _RecordingKbSearch, llm: LLM, writer: _CollectingTraceWriter) -> EngineAgentRunner:
    return EngineAgentRunner(kb_search=kb, llm=llm, embedding=FakeEmbedding(), trace_writer=writer)


# ---------------------------------------------------------------------------
# Lớp A — interpreter THẬT + 4 fake
# ---------------------------------------------------------------------------


async def test_run_case_maps_llm_output_to_agent_answer() -> None:
    """KHÓA A1: final_state llm-step → AgentAnswer; citations = LLM-trích ∩ retrieved (grounding engine).

    app#44: `kb_search` không còn chạy vô điều kiện (khác DAG cũ) — LLM double phải TỰ phát
    `TOOL_CALL: kb_search` (lượt 1) rồi trả lời trích dẫn (lượt 2), `_MultiTurnScriptedLLM` thay
    `_ScriptedLLM` một câu. Event sequence giờ `["llm-step","kb-retrieve","llm-step"]` — không còn
    `tool-call`/`end` cố định (đó là 2 trong 6 `NodeType` của DAG cũ, vòng lặp mới chỉ phát 3:
    `LLM_STEP`/`KB_RETRIEVE`/`TOOL_CALL`, và `TOOL_CALL` chỉ khi model gọi 1 tool KHÁC `kb_search`)."""
    kb = _RecordingKbSearch([_item("ankor-leave-001#c1"), _item("ankor-leave-001#c2")])
    writer = _CollectingTraceWriter()
    llm = _MultiTurnScriptedLLM(
        [
            'TOOL_CALL: {"tool": "kb_search", "params": {"query": "nghỉ phép?"}}',
            "Cần báo trước 3 ngày làm việc [ankor-leave-001#c1].",
        ]
    )
    runner = _runner(kb, llm, writer)

    case_run = await runner.run_case(
        agent_id="agent-callisto-d4", query="nghỉ phép?", tenant_id=TENANT_ID, section_roles=["public"]
    )

    assert case_run.answer.answer == "Cần báo trước 3 ngày làm việc [ankor-leave-001#c1]."
    assert case_run.answer.citations == ["ankor-leave-001#c1"]
    assert case_run.answer.refused is False
    assert [e.node_type.value for e in case_run.events] == ["llm-step", "kb-retrieve", "llm-step"]


async def test_run_case_threads_tenant_uuid_and_roles() -> None:
    """KHÓA A2 (D-13): tenant_id UUID + section_roles đi tới kb.search; mọi event mang đúng UUID, 1 run_id.

    app#44: `query` LLM tự phát trong `TOOL_CALL:` — không còn từ `node.params["query"]` tĩnh của DAG."""
    kb = _RecordingKbSearch([_item("ankor-expense-001#c2")])
    writer = _CollectingTraceWriter()
    llm = _MultiTurnScriptedLLM(
        [
            'TOOL_CALL: {"tool": "kb_search", "params": {"query": "duyệt chi?"}}',
            "Tối đa 20 triệu đồng [ankor-expense-001#c2].",
        ]
    )
    runner = _runner(kb, llm, writer)

    case_run = await runner.run_case(
        agent_id="agent-callisto-d4", query="duyệt chi?", tenant_id=TENANT_ID, section_roles=["finance"]
    )

    assert kb.calls[0] == ("duyệt chi?", TENANT_ID, ["finance"], 5)
    assert {e.tenant_id for e in case_run.events} == {TENANT_ID}
    assert len({e.run_id for e in case_run.events}) == 1


async def test_inv1_recipe_khai_tenant_khac_thi_session_thang() -> None:
    """KHÓA INV-1 Tenant-Wall tại COMPOSITION ROOT: recipe khai một tenant, session resolve tenant
    khác ⇒ run chạy dưới tenant của **session**, không của recipe.

    Vì sao bài này phải ở `studio_app` chứ không ở engine hay workbench: mỗi bên chỉ kiểm được nửa của
    mình. `studio_engine/session.py` khai Protocol `SessionContext` và test rằng interpreter đọc
    `session_context.tenant_id`; `studio_workbench/tenant_wall.py` test rằng `resolve_session` fail-closed.
    Chỗ *hai nửa ráp vào nhau* — `ResolvedContext` của SWE thoả Protocol của AIE-1, và interpreter thật
    thắng recipe thật — không thuộc quadrant nào (`.importlinter` cấm hai bên import nhau), nên chỉ
    composition root kiểm được. Trước bài này chỗ đó trống.

    Chạy `interpreter.run` TRỰC TIẾP, không qua `run_case`: adapter dựng recipe *từ* cùng `tenant_id` mà
    nó đưa vào `resolve_session`, nên qua đường đó hai giá trị luôn khớp và không tạo ra được thế lệch
    cần kiểm. Thế lệch là điều kiện thí nghiệm, không phải cách adapter được gọi thật.

    app#44: bài này CỐ Ý vẫn nhắm `interpreter.run()` (DAG-walk, không phải `run_agent_loop()`) — nó
    khoá fence ở TẦNG interpreter/node.params (`kb_node.params["tenant_id"]` bị ghi đè), một cơ chế
    chỉ tồn tại trên đường DAG-walk (fallback vẫn sống, K2 engine#33). Fence tương đương cho
    `run_agent_loop()` là `agent_loop.fenced_kb_params` (cùng helper `fence.py` cả 2 đường dùng
    chung) — không cần bài riêng ở đây vì logic override đã chung 1 hàm, xem `packages/engine`'s
    test riêng cho `fenced_kb_params`.

    Ba assert, và assert đầu là cầu chì:

    1. recipe **thật sự** khai `CLIENT_CLAIMED_TENANT_ID`. Không có vế này, bài sẽ xanh rỗng-nghĩa nếu
       `create_recipe_d4` lặng lẽ bỏ qua tham số `tenant_id` — lúc đó "session thắng" chỉ vì recipe chưa
       bao giờ tuyên bố gì để mà thua.
    2. `kb.search` nhận tenant của session. Đây là hàng rào có hệ quả: nó quyết định đọc được chunk của ai.
    3. mọi `TraceEvent` mang tenant của session. Nếu trace ghi nhãn client-khai thì bộ chấm của evalhub
       (`tenant_scope_ok`, `citations_from_trace`) sẽ chấm trên một danh tính sai."""
    kb = _RecordingKbSearch([_item("ankor-leave-001#c1")])
    writer = _CollectingTraceWriter()
    recipe = create_recipe_d4(
        agent_id="agent-callisto-d4",
        tenant_id=CLIENT_CLAIMED_TENANT_ID,  # ← client khai
        scope="t/public",
        query="nghỉ phép?",
    )

    assert recipe.tenant_id == CLIENT_CLAIMED_TENANT_ID, "recipe phải thật sự khai tenant kia"

    # Cầu chì thứ hai, ở tầng NODE (review AIE-2 trên workbench#23). Từ workbench#23
    # `create_recipe_d4` không còn ghi `tenant_id`/`section_roles` vào `node.params`, nên nếu chỉ
    # dựa vào builder thì bài này sẽ XANH-RỖNG-NGHĨA đúng kiểu docstring cảnh báo ở trên: interpreter
    # có thể bị đổi thành "bổ sung" thay vì "ghi đè" (client params thắng) mà bài vẫn xanh, vì không
    # còn params nào để mà thắng (đã đo bằng mutant M3 thật — xem review PR#23). Thế lệch là ĐIỀU
    # KIỆN THÍ NGHIỆM — dựng tại đây, không mượn của builder.
    kb_node = recipe.dag.nodes[0]
    spoofed = kb_node.model_copy(
        update={
            "params": {
                **kb_node.params,
                "tenant_id": CLIENT_CLAIMED_TENANT_ID,
                "section_roles": ["finance"],
            }
        }
    )
    recipe = recipe.model_copy(
        update={"dag": recipe.dag.model_copy(update={"nodes": [spoofed, *recipe.dag.nodes[1:]]})}
    )
    assert recipe.dag.nodes[0].params["tenant_id"] == CLIENT_CLAIMED_TENANT_ID
    assert recipe.dag.nodes[0].params["section_roles"] == ["finance"]

    raw = await engine_interpreter.run(
        recipe,
        kb_search=kb,
        llm=_ScriptedLLM("Cần báo trước 3 ngày [ankor-leave-001#c1]."),
        embedding=FakeEmbedding(),
        trace_writer=writer,
        session_context=resolve_session(  # ← server resolve, khác recipe
            {"tenant_id": TENANT_ID, "user": "eval-harness", "roles": ["public"]}
        ),
    )

    assert kb.calls[0][1] == TENANT_ID, "kb.search phải nhận tenant của SESSION"
    assert kb.calls[0][1] != CLIENT_CLAIMED_TENANT_ID, "tenant client khai không được tới kb.search"
    assert kb.calls[0][2] == ["public"], "section_roles phải là của SESSION, không phải node tự khai"
    assert {e.tenant_id for e in raw.events} == {TENANT_ID}, "mọi TraceEvent mang tenant của SESSION"


async def test_run_case_events_passthrough_from_trace() -> None:
    """KHÓA A3: CaseRun.events == events đã emit qua trace_writer, nguyên vẹn cùng thứ tự —
    đây là nguồn chấm citation của evalhub, adapter không được tự chế/lọc.

    app#44: 3 event (`llm-step`,`kb-retrieve`,`llm-step`) — không còn 4 node DAG cố định."""
    kb = _RecordingKbSearch([_item("ankor-leave-001#c1")])
    writer = _CollectingTraceWriter()
    llm = _MultiTurnScriptedLLM(
        ['TOOL_CALL: {"tool": "kb_search", "params": {"query": "q?"}}', "ok [ankor-leave-001#c1]"]
    )
    runner = _runner(kb, llm, writer)

    case_run = await runner.run_case(
        agent_id="agent-callisto-d4", query="q?", tenant_id=TENANT_ID, section_roles=["public"]
    )

    assert case_run.events == writer.events
    assert len(case_run.events) == 3


@pytest.mark.parametrize(
    "final_answer_text",
    [
        "Cần báo trước 3 ngày làm việc [ankor-leave-001#c1].",  # trả lời có trích dẫn grounded
        _REFUSAL,  # tín hiệu từ chối của engine ở D5 (sentinel); D6 engine đổi luật — xem docstring
    ],
)
async def test_run_case_surfaces_engine_refusal_faithfully(final_answer_text: str) -> None:
    """KHÓA A4 (ENGINE-AGNOSTIC): adapter phải phản ánh ĐÚNG quyết định `refused`/`citations` mà
    engine đưa ra — bất kể engine quyết bằng luật nào (sentinel `[[REFUSED]]` ở D5 · grounding ở
    engine#10/D6 · công thức `(not citations) and (not used_non_kb_tool)` của vòng lặp mới, A5/DEC-4
    ở engine#33 · luật khác sau này).

    Cách làm: chạy `run_agent_loop` TRỰC TIẾP (app#44 — trước đó `interpreter.run`) để lấy quyết
    định GỐC của engine trong `final_state`, rồi chạy adapter trên CÙNG input (cùng kịch bản
    `_MultiTurnScriptedLLM`: TOOL_CALL kb_search lượt 1, `final_answer_text` lượt 2 — cần 2 lượt vì
    vòng lặp mới không chạy kb_search vô điều kiện như DAG cũ) và so hai bên. Nhờ vậy test pin đúng
    hợp đồng của adapter (map trung thực, không tự chế/nuốt cờ) mà KHÔNG pin luật refusal của engine
    — luật đó thuộc engine và có test riêng bên đó."""
    query, roles = "nghỉ phép?", ["public"]
    scripted_responses = ['TOOL_CALL: {"tool": "kb_search", "params": {"query": "nghỉ phép?"}}', final_answer_text]

    # (1) Sự thật gốc — engine tự quyết.
    # `session_context` dựng bằng CHÍNH `resolve_session` mà adapter dùng, cùng dict đầu vào: so hai
    # bên chỉ có nghĩa khi danh tính hai lượt giống nhau. Tự dựng `ResolvedContext` tay ở đây sẽ mở ra
    # khả năng lượt (1) và lượt (2) chạy dưới hai danh tính khác nhau mà test vẫn xanh.
    raw = await engine_agent_loop.run_agent_loop(
        create_recipe_d4(agent_id="agent-callisto-d4", tenant_id=TENANT_ID, scope="t/public"),
        kb_search=_RecordingKbSearch([_item("ankor-leave-001#c1")]),
        llm=_MultiTurnScriptedLLM(scripted_responses),
        embedding=FakeEmbedding(),
        trace_writer=_CollectingTraceWriter(),
        session_context=resolve_session({"tenant_id": TENANT_ID, "user": "eval-harness", "roles": roles}),
        question=query,
    )
    engine_out = _llm_answer(raw.final_state)

    # (2) Adapter đi cùng đường, cùng input
    case_run = await _runner(
        _RecordingKbSearch([_item("ankor-leave-001#c1")]),
        _MultiTurnScriptedLLM(scripted_responses),
        _CollectingTraceWriter(),
    ).run_case(agent_id="agent-callisto-d4", query=query, tenant_id=TENANT_ID, section_roles=roles)

    assert case_run.answer.refused is bool(engine_out.get("refused", False))
    raw_cites = engine_out.get("citations")
    assert case_run.answer.citations == ([str(c) for c in raw_cites] if isinstance(raw_cites, list) else [])


async def test_refusal_must_be_expressible_somehow() -> None:
    """KHÓA A5 (cross-quadrant): phải TỒN TẠI ít nhất một cách để agent nói "tôi từ chối" và engine
    ghi nhận `refused=True`. Nếu không còn cách nào thì nhánh từ-chối của scorecard (SC-04/05, hai
    trục T1/T6) trở thành KHÔNG THỂ chấm — bộ chấm mất một nửa bài kiểm hàng rào mà CI vẫn xanh.

    Không hardcode tín hiệu nào: thử các ứng viên hợp lý, chỉ cần MỘT cái hoạt động. Cùng lý do với
    A4 — sống sót khi engine đổi luật (điểm gãy #14 daily-note D6 của DongAnh2704: đổi sang
    `refused = not citations` sinh dương-tính-giả; test này bắt chiều ngược lại: mất hẳn tín hiệu)."""
    candidates = [_REFUSAL, "", "Không có thông tin trong tài liệu được cấp."]
    refused_flags = []
    for text in candidates:
        case_run = await _runner(
            _RecordingKbSearch([_item("ankor-leave-001#c1")]),
            _ScriptedLLM(text),
            _CollectingTraceWriter(),
        ).run_case(
            agent_id="agent-callisto-d4", query="hỏi chéo tenant?", tenant_id=TENANT_ID, section_roles=["public"]
        )
        refused_flags.append(case_run.answer.refused)

    assert any(refused_flags), (
        "Không ứng viên nào làm engine đặt refused=True → nhánh từ-chối của scorecard không thể "
        f"chấm được. Đã thử: {candidates!r}. Cần chốt tín hiệu từ chối với AIE-1 (#27)."
    )


# ---------------------------------------------------------------------------
# Lớp B — monkeypatch eval_adapter.interpreter (mapper fail-closed, không phụ thuộc engine)
# ---------------------------------------------------------------------------


class _StubAgentLoop:
    """Namespace fake thay `eval_adapter.agent_loop` (app#44 — trước đó `eval_adapter.interpreter`);
    ghi lại recipe + `question` để pin đường adapter→builder."""

    def __init__(self, result: RunResult) -> None:
        self._result = result
        self.recipes: list[Recipe] = []
        self.questions: list[str] = []

    async def run_agent_loop(self, recipe: Recipe, **kwargs: object) -> RunResult:
        self.recipes.append(recipe)
        question = kwargs["question"]
        assert isinstance(question, str)
        self.questions.append(question)
        return self._result


def _patched(
    monkeypatch: pytest.MonkeyPatch, final_state: dict[str, object]
) -> tuple[EngineAgentRunner, _StubAgentLoop]:
    stub = _StubAgentLoop(RunResult(run_id="r-test", events=[], final_state=final_state))
    monkeypatch.setattr(eval_adapter, "agent_loop", stub)
    runner = _runner(_RecordingKbSearch([]), _ScriptedLLM("unused"), _CollectingTraceWriter())
    return runner, stub


async def test_run_case_fails_closed_without_llm_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """KHÓA B5: final_state không có dict mang key 'answer' → LookupError (không đoán rỗng)."""
    runner, _ = _patched(monkeypatch, {"n1": ["chunk"], "n4": {"terminated": True}})
    with pytest.raises(LookupError):
        await runner.run_case(agent_id="a", query="q?", tenant_id=TENANT_ID, section_roles=["public"])


def test_llm_answer_picks_first_dict_with_answer_key() -> None:
    """_llm_answer: bỏ qua value không phải dict; lấy dict ĐẦU TIÊN có 'answer' (insertion order)."""
    out = _llm_answer({"n1": ["kb-output"], "n2": {"answer": "a"}, "n5": {"answer": "b"}})
    assert out == {"answer": "a"}


def test_llm_answer_raises_when_absent() -> None:
    with pytest.raises(LookupError):
        _llm_answer({"n1": [], "n4": {"terminated": True}})


@pytest.mark.parametrize(
    ("llm_out", "expected"),
    [
        ({"answer": "x"}, []),  # thiếu key citations
        ({"answer": "x", "citations": "a#c1"}, []),  # non-list → [] (isinstance-guard)
        ({"answer": "x", "citations": [123, "a#c1"]}, ["123", "a#c1"]),  # item non-str → str()
    ],
)
async def test_citations_guard_absent_or_non_list(
    monkeypatch: pytest.MonkeyPatch, llm_out: dict[str, object], expected: list[str]
) -> None:
    """KHÓA B6: citations absent/non-list không được nổ — về [] fail-closed."""
    runner, _ = _patched(monkeypatch, {"n2": llm_out})
    case_run = await runner.run_case(agent_id="a", query="q?", tenant_id=TENANT_ID, section_roles=["public"])
    assert case_run.answer.citations == expected


@pytest.mark.parametrize(
    ("llm_out", "expected"),
    [
        ({"answer": "x"}, False),  # thiếu key → mặc định KHÔNG từ chối
        ({"answer": "x", "refused": 1}, True),
        ({"answer": "x", "refused": "yes"}, True),
    ],
)
async def test_refused_coercion(monkeypatch: pytest.MonkeyPatch, llm_out: dict[str, object], expected: bool) -> None:
    """KHÓA B7: `refused` coerce bool — truthy không-phải-bool vẫn thành True."""
    runner, _ = _patched(monkeypatch, {"n2": llm_out})
    case_run = await runner.run_case(agent_id="a", query="q?", tenant_id=TENANT_ID, section_roles=["public"])
    assert case_run.answer.refused is expected


async def test_recipe_construction_via_real_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    """KHÓA B8a: builder THẬT (chỉ patch agent_loop) — tenant_id vào recipe (D-13), agent_id ""
    → fallback, section_roles round-trip qua scope "t/a,b" của adapter.

    app#44: `query` giờ quan sát được qua `stub.questions` (kwarg `question=`), KHÔNG còn
    `node.params["query"]` — vòng lặp mới không đọc `recipe.dag` để lấy câu hỏi."""
    runner, stub = _patched(monkeypatch, {"n2": {"answer": "x"}})
    await runner.run_case(agent_id="", query="q?", tenant_id=TENANT_ID, section_roles=["public", "finance"])

    recipe = stub.recipes[0]
    assert recipe.tenant_id == TENANT_ID
    assert recipe.agent_id == "agent-callisto-d4"
    # `section_roles` round-trip giờ quan sát được qua `kb_binding.scope` (builder KHÔNG còn ghi
    # nó vào `node.params` — hardening #122, `interpreter.run()` luôn ghi đè bằng session).
    assert recipe.kb_binding.scope == "t/public,finance"
    kb_node = next(n for n in recipe.dag.nodes if n.type.value == "kb-retrieve")
    assert "section_roles" not in kb_node.params
    assert "query" not in kb_node.params, "app#44: query không còn bơm vào node.params"
    assert stub.questions[0] == "q?"


async def test_recipe_construction_empty_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    """KHÓA B8b: section_roles=[] → scope "t/" → node kb-retrieve nhận roles RỖNG (fail-closed phía
    kb: không role nào được cấp ngầm)."""
    runner, stub = _patched(monkeypatch, {"n2": {"answer": "x"}})
    await runner.run_case(agent_id="a", query="q?", tenant_id=TENANT_ID, section_roles=[])

    assert stub.recipes[0].agent_id == "a"  # passthrough khi non-empty (mutation-check T-2)
    assert stub.recipes[0].kb_binding.scope == "t/"
    kb_node = next(n for n in stub.recipes[0].dag.nodes if n.type.value == "kb-retrieve")
    assert "section_roles" not in kb_node.params


# ---------------------------------------------------------------------------
# Lớp C — spine đầy đủ với LLM CHỈ-ĐỌC-PROMPT (`ExtractiveFakeLLM`)
#
# Lớp A dùng `_ScriptedLLM` (trả chuỗi cố định, bỏ qua prompt) nên nó pin được "adapter map trung
# thực" mà KHÔNG pin được "chunk đã truy xuất có thật sự tới model không". Trước engine#10 chỗ đó
# không thể pin: `llm-step` gửi `node.params["prompt"]` (rỗng với recipe của SWE), nên chunk chưa
# bao giờ vào prompt — đúng điểm gãy `SpyLLM` của DE (@DongAnh2704) tìm ra. engine#10 thêm
# `build_prompt(query, chunks)` nên giờ pin được, và đây là các test pin nó.
# ---------------------------------------------------------------------------


async def test_prompt_carries_retrieved_chunk_ids() -> None:
    """KHÓA C1: chunk từ `kb.search` phải tới được model, đúng khuôn `[chunk_id]` trên dòng riêng.

    Đây là mắt nối trước nay KHÔNG có test nào ở bất kỳ quadrant nào phủ: kb trả chunk (kb test
    dừng ở đó), engine render prompt (engine test dùng recipe của riêng nó), evalhub chấm citation
    (chấm trên trace, không nhìn prompt). Đứt ở giữa thì `citation_accuracy` = 0 mà mọi suite vẫn
    xanh — chính là trạng thái tồn tại tới hết Day 5."""
    kb = _RecordingKbSearch([_item("ankor-leave-001#c1", text="Báo trước 3 ngày làm việc.")])
    llm = ExtractiveFakeLLM()
    await _runner(kb, llm, _CollectingTraceWriter()).run_case(
        agent_id="a", query="nghỉ phép báo trước bao lâu?", tenant_id=TENANT_ID, section_roles=["public"]
    )

    # app#44: 2 lượt — lượt 1 (chưa search) phát TOOL_CALL, lượt 2 (đã có observation) đọc chunk.
    # Khác DAG cũ (1 lượt, chunk đã có sẵn trước khi llm-step chạy).
    assert len(llm.prompts_seen) == 2, "llm.complete() phải được gọi đúng 2 lượt (tool-call rồi final-answer)"
    assert "nghỉ phép báo trước bao lâu?" in llm.prompts_seen[0], "lượt 1 phải mang câu hỏi (chưa có chunk)"
    prompt = llm.prompts_seen[1]
    assert "[ankor-leave-001#c1]\nBáo trước 3 ngày làm việc." in prompt
    assert "nghỉ phép báo trước bao lâu?" in prompt, "câu hỏi phải đi kèm đoạn trích"


async def test_citation_originates_from_kb_data_not_from_the_test() -> None:
    """KHÓA C2: citation trả về đến từ DỮ LIỆU KB, không từ người viết test.

    Chạy 2 lần với chunk_id khác nhau, cùng mọi thứ còn lại: citation phải đi theo KB. Đây là lý do
    `ExtractiveFakeLLM` đáng tin hơn câu trả lời recorded — recorded thì citation do người viết
    fixture gõ, nên nó khớp `expected` bất kể KB làm gì (phê bình của @DongAnh2704: *"luôn ra điểm
    tuyệt đối và chứng minh số không"*)."""
    seen: list[list[str]] = []
    for chunk_id in ("ankor-leave-001#c1", "borea-expense-001#c9"):
        kb = _RecordingKbSearch([_item(chunk_id, text="nội dung")])
        case_run = await _runner(kb, ExtractiveFakeLLM(), _CollectingTraceWriter()).run_case(
            agent_id="a", query="q?", tenant_id=TENANT_ID, section_roles=["public"]
        )
        seen.append(case_run.answer.citations)

    assert seen == [["ankor-leave-001#c1"], ["borea-expense-001#c9"]]


async def test_empty_retrieval_reaches_the_model_as_an_explicit_absence() -> None:
    """KHÓA C3: fence chặn sạch (`kb.search` → []) thì model vẫn PHẢI được gọi và được nói rõ là
    không có đoạn trích — không phải nhận prompt rỗng.

    Vì sao đáng pin: nhánh này là SC-05 (chéo-vai) của golden set. Nếu prompt rỗng thì model không
    phân biệt được "không có tài liệu" với "hỏng ở đâu đó", và `citations` rỗng vì sai lý do."""
    kb = _RecordingKbSearch([])
    llm = ExtractiveFakeLLM()
    case_run = await _runner(kb, llm, _CollectingTraceWriter()).run_case(
        agent_id="a", query="thang lương gồm những bậc nào?", tenant_id=TENANT_ID, section_roles=["engineering"]
    )

    # app#44: 2 lượt — lượt 1 phát TOOL_CALL (chưa biết retrieval sẽ rỗng), lượt 2 nhận observation
    # rỗng và từ chối ngay (không search lại — `ExtractiveFakeLLM` phân biệt được nhờ header block).
    assert len(llm.prompts_seen) == 2
    assert "thang lương gồm những bậc nào?" in llm.prompts_seen[0]
    assert case_run.answer.citations == [], "không chunk nào truy xuất ⇒ không citation nào grounded"


async def test_hash_fake_llm_can_never_ground_a_citation() -> None:
    """KHÓA C4: ghi lại BẰNG TEST vì sao `FakeLLM` không dùng để đo chất lượng được.

    `FakeLLM` trả `fake-completion:<sha256 hex>` — hex không bao giờ chứa `[...]`, nên không
    citation nào grounded được, nên nhánh chấm nào cũng lệch. Pin ở đây để lần sau có người định
    dùng `FakeLLM` cho smoke-eval thì thấy ngay lý do không được.

    Chỉ assert `citations == []` (tính chất của FakeLLM + luật grounding "citation phải nằm trong
    `retrieved_chunks`"), KHÔNG assert `refused` — `refused` là luật của engine và đã đổi một lần
    ở #10; pin nó ở đây sẽ tái lập đúng lỗi mà A4 vừa được sửa để tránh."""
    kb = _RecordingKbSearch([_item("ankor-leave-001#c1", text="Báo trước 3 ngày làm việc.")])
    case_run = await _runner(kb, FakeLLM(), _CollectingTraceWriter()).run_case(
        agent_id="a", query="q?", tenant_id=TENANT_ID, section_roles=["public"]
    )

    assert case_run.answer.answer.startswith("fake-completion:")
    assert case_run.answer.citations == []


async def test_extractive_fake_reads_only_the_top_chunk() -> None:
    """KHÓA C5: pin giới hạn ĐÃ KHAI của `ExtractiveFakeLLM` — chỉ đọc đoạn trích đầu.

    Không phải "bug được chấp nhận" mà là hợp đồng: điểm của bộ fixture này là CẬN DƯỚI của hệ
    thống với một model tệ nhất còn đọc được. Case có đáp án nằm ở hạng 2–3 sẽ đỏ, và đỏ đúng.
    Nếu ai đó "cải tiến" nó thành đọc mọi đoạn thì test này đỏ để buộc sửa cả tuyên bố cận-dưới,
    thay vì âm thầm đẩy điểm smoke-eval lên."""
    kb = _RecordingKbSearch([_item("doc-a#c1", text="Đáp án hạng 1."), _item("doc-b#c1", text="Đáp án hạng 2.")])
    case_run = await _runner(kb, ExtractiveFakeLLM(), _CollectingTraceWriter()).run_case(
        agent_id="a", query="q?", tenant_id=TENANT_ID, section_roles=["public"]
    )

    assert case_run.answer.citations == ["doc-a#c1"]
    assert "Đáp án hạng 1." in case_run.answer.answer
    assert "Đáp án hạng 2." not in case_run.answer.answer


# ---------------------------------------------------------------------------
# Lớp D — `tool_dispatch` wiring (engine#32 review finding, Critical)
#
# `run_case()` trước fix này KHÔNG truyền `tool_dispatch` cho `interpreter.run()` — mọi recipe
# eval-gate có node `tool-call` âm thầm chấm trên kết quả STUB thay vì `RealToolDispatch` thật.
# Fix chỉ tiêm ở branch (a) — `self._recipe` được caller đưa vào (đường `routes/publish.py::
# _evaluate` dùng thật) — CỐ Ý không đụng branch (b) (`create_recipe_d4` tự dựng, whitelist mặc
# định `["kb_search"]` không phải tool `RealToolDispatch` hỗ trợ; xem comment tại
# `eval_adapter.py::run_case`). Bài dưới khoá đúng branch (a).
# ---------------------------------------------------------------------------


def _tool_call_recipe(tenant_id: UUID, expression: str) -> Recipe:
    return create_dynamic_recipe(
        agent_id="agent-tool-dispatch-check",
        tenant_id=tenant_id,
        instructions="x",
        model="m",
        tool_whitelist=["calculator"],
        nodes=[
            Node(id="n1", type=NodeType.KB_RETRIEVE, params={"top_k": 3}),
            Node(id="n2", type=NodeType.LLM_STEP, params={"temperature": 0.0}),
            Node(id="n3", type=NodeType.TOOL_CALL, params={"tool": "calculator", "expression": expression}),
            Node(id="n4", type=NodeType.END, params={}),
        ],
        edges=[Edge(from_="n1", to="n2"), Edge(from_="n2", to="n3"), Edge(from_="n3", to="n4")],
        kb_id="kb-smoke",
        scope="public",
    )


async def test_run_case_dispatches_real_tool_when_recipe_is_injected() -> None:
    """KHÓA D1: recipe TIÊM (branch (a), như `routes/publish.py::_evaluate` dùng thật), LLM tự phát
    `TOOL_CALL: {"tool":"calculator",...}` → `TraceEvent.outputs` của event `tool-call` đó PHẢI là
    `{"expression", "result"}` thật (`RealToolDispatch`), KHÔNG PHẢI marker stub `{"tool", "status":
    "stub-dispatched"}` (`WhitelistToolDispatch`, hành vi trước fix engine#32).

    app#44: `_tool_call_recipe`'s `dag` (kể cả node `tool-call` sẵn trong đó) KHÔNG còn được đọc —
    `run_agent_loop` chỉ cần `agent_config.tool_whitelist` chứa `"calculator"`; tool call thật sự
    đến từ chính LLM double tự phát tín hiệu, không từ 1 node DAG cố định."""
    recipe = _tool_call_recipe(TENANT_ID, "6 * 7")
    llm = _MultiTurnScriptedLLM(
        ['TOOL_CALL: {"tool": "calculator", "params": {"expression": "6 * 7"}}', "Kết quả là 42."]
    )
    runner = EngineAgentRunner(
        kb_search=_RecordingKbSearch([_item("ankor-leave-001#c1")]),
        llm=llm,
        embedding=FakeEmbedding(),
        trace_writer=_CollectingTraceWriter(),
        recipe=recipe,
    )

    case_run = await runner.run_case(agent_id="a", query="q?", tenant_id=TENANT_ID, section_roles=["public"])

    tool_event = next(e for e in case_run.events if e.node_type == NodeType.TOOL_CALL)
    assert tool_event.outputs == {"expression": "6 * 7", "result": 42}
    assert tool_event.outputs.get("status") != "stub-dispatched"


async def test_run_case_kb_search_always_available_even_without_injected_recipe() -> None:
    """KHÓA D2 (app#44 — thay hẳn bài cũ, xem lý do dưới): branch (b) — `self._recipe is None`,
    `create_recipe_d4` tự dựng whitelist mặc định `["kb_search"]`. Vòng lặp mới KHÔNG BAO GIỜ đưa
    `kb_search` qua `ToolDispatch`/`WhitelistToolDispatch` nữa — nó có nhánh riêng
    (`agent_loop.KB_SEARCH_TOOL`, A4: "kb_search luôn khả dụng, không bị `tool_whitelist` chặn") —
    nên toàn bộ lớp bug bài D2 CŨ khoá ("kb_search lỡ đi qua stub tool-dispatch, ra
    `{'tool':'kb_search','status':'stub-dispatched'}`") giờ KHÔNG THỂ xảy ra được nữa CẤU TRÚC, kể
    cả khi caller không tiêm `recipe=`/`tool_dispatch=` gì cả. Bài này pin đúng bất biến MỚI thay
    cho bất biến cũ đã hết ý nghĩa: `kb_search` luôn ra event `kb-retrieve` thật (`{"chunks": [...]}`),
    không bao giờ ra event `tool-call` nào."""
    kb = _RecordingKbSearch([_item("ankor-leave-001#c1")])
    writer = _CollectingTraceWriter()
    llm = _MultiTurnScriptedLLM(
        ['TOOL_CALL: {"tool": "kb_search", "params": {"query": "q?"}}', "ok [ankor-leave-001#c1]"]
    )
    runner = _runner(kb, llm, writer)

    case_run = await runner.run_case(
        agent_id="agent-callisto-d4", query="q?", tenant_id=TENANT_ID, section_roles=["public"]
    )

    assert not any(e.node_type == NodeType.TOOL_CALL for e in case_run.events), (
        "kb_search không bao giờ được đi qua ToolDispatch/tool-call — nó có nhánh riêng ở engine"
    )
    kb_event = next(e for e in case_run.events if e.node_type == NodeType.KB_RETRIEVE)
    chunks = kb_event.outputs["chunks"]
    assert isinstance(chunks, list)
    first_chunk = chunks[0]
    assert isinstance(first_chunk, dict)
    assert first_chunk["chunk_id"] == "ankor-leave-001#c1"
