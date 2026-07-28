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
from studio_contracts import LLM, Recipe, TraceEvent
from studio_contracts.kb import KbSearchResultItem
from studio_engine import interpreter as engine_interpreter
from studio_engine.interpreter import RunResult
from studio_workbench import create_recipe_d4

# CỐ Ý khác `ANKOR_ID` default của workbench (a0…01) — nếu adapter "quên" thread tenant_id xuống
# create_recipe_d4 và rơi về default thì A2/B8a phải ĐỎ (mutation-check T-1).
TENANT_ID = UUID("b0000000-0000-0000-0000-000000000002")
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
    """LLM fake trả 1 chuỗi cố định (FakeLLM của providers là hash-seeded, không điều khiển nội dung được)."""

    def __init__(self, response: str) -> None:
        self._response = response

    async def complete(self, prompt: str, **kwargs: object) -> str:  # noqa: ARG002
        return self._response


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
    """KHÓA A1: final_state llm-step → AgentAnswer; citations = LLM-trích ∩ retrieved (grounding engine)."""
    kb = _RecordingKbSearch([_item("ankor-leave-001#c1"), _item("ankor-leave-001#c2")])
    writer = _CollectingTraceWriter()
    runner = _runner(kb, _ScriptedLLM("Cần báo trước 3 ngày làm việc [ankor-leave-001#c1]."), writer)

    case_run = await runner.run_case(
        agent_id="agent-callisto-d4", query="nghỉ phép?", tenant_id=TENANT_ID, section_roles=["public"]
    )

    assert case_run.answer.answer == "Cần báo trước 3 ngày làm việc [ankor-leave-001#c1]."
    assert case_run.answer.citations == ["ankor-leave-001#c1"]
    assert case_run.answer.refused is False
    assert [e.node_type.value for e in case_run.events] == ["kb-retrieve", "llm-step", "tool-call", "end"]


async def test_run_case_threads_tenant_uuid_and_roles() -> None:
    """KHÓA A2 (D-13): tenant_id UUID + section_roles đi tới kb.search; mọi event mang đúng UUID, 1 run_id."""
    kb = _RecordingKbSearch([_item("ankor-expense-001#c2")])
    writer = _CollectingTraceWriter()
    runner = _runner(kb, _ScriptedLLM("Tối đa 20 triệu đồng [ankor-expense-001#c2]."), writer)

    case_run = await runner.run_case(
        agent_id="agent-callisto-d4", query="duyệt chi?", tenant_id=TENANT_ID, section_roles=["finance"]
    )

    assert kb.calls[0] == ("duyệt chi?", TENANT_ID, ["finance"], 3)
    assert {e.tenant_id for e in case_run.events} == {TENANT_ID}
    assert len({e.run_id for e in case_run.events}) == 1


async def test_run_case_events_passthrough_from_trace() -> None:
    """KHÓA A3: CaseRun.events == events đã emit qua trace_writer, nguyên vẹn cùng thứ tự —
    đây là nguồn chấm citation của evalhub, adapter không được tự chế/lọc."""
    kb = _RecordingKbSearch([_item("ankor-leave-001#c1")])
    writer = _CollectingTraceWriter()
    runner = _runner(kb, _ScriptedLLM("ok [ankor-leave-001#c1]"), writer)

    case_run = await runner.run_case(
        agent_id="agent-callisto-d4", query="q?", tenant_id=TENANT_ID, section_roles=["public"]
    )

    assert case_run.events == writer.events
    assert len(case_run.events) == 4


@pytest.mark.parametrize(
    "llm_text",
    [
        "Cần báo trước 3 ngày làm việc [ankor-leave-001#c1].",  # trả lời có trích dẫn grounded
        _REFUSAL,  # tín hiệu từ chối của engine ở D5 (sentinel); D6 engine đổi luật — xem docstring
    ],
)
async def test_run_case_surfaces_engine_refusal_faithfully(llm_text: str) -> None:
    """KHÓA A4 (ENGINE-AGNOSTIC): adapter phải phản ánh ĐÚNG quyết định `refused`/`citations` mà
    engine đưa ra — bất kể engine quyết bằng luật nào (sentinel `[[REFUSED]]` ở D5 · grounding ở
    engine#10/D6 · luật khác sau này).

    Cách làm: chạy `interpreter.run` TRỰC TIẾP để lấy quyết định GỐC của engine trong `final_state`,
    rồi chạy adapter trên cùng input và so hai bên. Nhờ vậy test pin đúng hợp đồng của adapter (map
    trung thực, không tự chế/nuốt cờ) mà KHÔNG pin luật refusal của engine — luật đó thuộc engine và
    có test riêng bên đó. Bản trước của test này drive refusal qua sentinel nên sẽ vỡ oan khi
    engine#10 (#27) bỏ sentinel."""
    query, roles = "nghỉ phép?", ["public"]

    # (1) Sự thật gốc — engine tự quyết
    raw = await engine_interpreter.run(
        create_recipe_d4(agent_id="agent-callisto-d4", tenant_id=TENANT_ID, scope="t/public", query=query),
        kb_search=_RecordingKbSearch([_item("ankor-leave-001#c1")]),
        llm=_ScriptedLLM(llm_text),
        embedding=FakeEmbedding(),
        trace_writer=_CollectingTraceWriter(),
    )
    engine_out = _llm_answer(raw.final_state)

    # (2) Adapter đi cùng đường, cùng input
    case_run = await _runner(
        _RecordingKbSearch([_item("ankor-leave-001#c1")]),
        _ScriptedLLM(llm_text),
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


class _StubInterpreter:
    """Namespace fake thay `eval_adapter.interpreter`; ghi lại recipe để pin đường adapter→builder."""

    def __init__(self, result: RunResult) -> None:
        self._result = result
        self.recipes: list[Recipe] = []

    async def run(self, recipe: Recipe, **kwargs: object) -> RunResult:  # noqa: ARG002
        self.recipes.append(recipe)
        return self._result


def _patched(
    monkeypatch: pytest.MonkeyPatch, final_state: dict[str, object]
) -> tuple[EngineAgentRunner, _StubInterpreter]:
    stub = _StubInterpreter(RunResult(run_id="r-test", events=[], final_state=final_state))
    monkeypatch.setattr(eval_adapter, "interpreter", stub)
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
    """KHÓA B8a: builder THẬT (chỉ patch interpreter) — tenant_id vào recipe (D-13), agent_id ""
    → fallback, section_roles round-trip qua scope "t/a,b" của adapter."""
    runner, stub = _patched(monkeypatch, {"n2": {"answer": "x"}})
    await runner.run_case(agent_id="", query="q?", tenant_id=TENANT_ID, section_roles=["public", "finance"])

    recipe = stub.recipes[0]
    assert recipe.tenant_id == TENANT_ID
    assert recipe.agent_id == "agent-callisto-d4"
    kb_node = next(n for n in recipe.dag.nodes if n.type.value == "kb-retrieve")
    assert kb_node.params["section_roles"] == ["public", "finance"]
    assert kb_node.params["query"] == "q?"


async def test_recipe_construction_empty_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    """KHÓA B8b: section_roles=[] → scope "t/" → node kb-retrieve nhận roles RỖNG (fail-closed phía
    kb: không role nào được cấp ngầm)."""
    runner, stub = _patched(monkeypatch, {"n2": {"answer": "x"}})
    await runner.run_case(agent_id="a", query="q?", tenant_id=TENANT_ID, section_roles=[])

    assert stub.recipes[0].agent_id == "a"  # passthrough khi non-empty (mutation-check T-2)
    kb_node = next(n for n in stub.recipes[0].dag.nodes if n.type.value == "kb-retrieve")
    assert kb_node.params["section_roles"] == []


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

    assert len(llm.prompts_seen) == 1, "llm-step phải được gọi đúng 1 lần"
    prompt = llm.prompts_seen[0]
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

    assert len(llm.prompts_seen) == 1
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
