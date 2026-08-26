"""`EngineAgentRunner.run_case()` — nối qua `run_agent_loop()` (engine#33) thay `interpreter.run()`
(app#44, call site 3/3). Trước bản vá này `run_case()` gọi `with_query(base, query)` rồi
`interpreter.run()` — DAG-walk cố định 4 node. Sau bản vá: `query` đi thẳng qua kwarg `question=`
của `run_agent_loop`, KHÔNG còn chạm `recipe.dag` — số lượt/loại event giờ biến thiên theo kịch bản
LLM double, không còn cố định `["kb-retrieve","llm-step","tool-call","end"]`.

Double `_MultiTurnScriptedLLM` (sibling `_ScriptedLLM` của `test_eval_adapter.py`) mô phỏng đúng
`TOOL_CALL:` rồi final-answer — cần thiết vì `_ScriptedLLM` (1 câu cố định) không bao giờ trigger
`kb_search` dưới vòng lặp mới (không có DAG kéo nó chạy vô điều kiện nữa)."""

from __future__ import annotations

from uuid import UUID

import pytest
from studio_app.eval_adapter import EngineAgentRunner
from studio_app.providers.fakes import FakeEmbedding
from studio_contracts import LLM, TraceEvent
from studio_contracts.kb import KbSearchResultItem

TENANT_ID = UUID("b0000000-0000-0000-0000-000000000002")


def _item(chunk_id: str, text: str = "…") -> KbSearchResultItem:
    return KbSearchResultItem(chunk_id=chunk_id, text=text, score=0.9, tenant_id=TENANT_ID, section_role="public")


class _RecordingKbSearch:
    def __init__(self, items: list[KbSearchResultItem]) -> None:
        self._items = items
        self.calls: list[tuple[str, UUID, list[str], int]] = []

    async def search(
        self, query: str, tenant_id: UUID, section_roles: list[str], top_k: int
    ) -> list[KbSearchResultItem]:
        self.calls.append((query, tenant_id, list(section_roles), top_k))
        return self._items


class _MultiTurnScriptedLLM:
    """`LLM` double trả lần lượt từng phần tử `responses` — 1 phần tử = 1 lượt `complete()`. Không
    đọc prompt (khác `ExtractiveFakeLLM`) — dùng khi cần kịch bản CỐ ĐỊNH độc lập với nội dung KB
    thật trả về (đúng vai trò cũ của `_ScriptedLLM`, mở rộng cho nhiều lượt)."""

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


async def test_run_case_no_longer_reads_recipe_dag_for_the_question() -> None:
    """KHOÁ — `question` (`query`) đi qua kwarg `question=` của `run_agent_loop`, KHÔNG còn qua
    `with_query`/`recipe.dag`'s `kb-retrieve` node `params["query"]`. Recipe gốc (`certified_recipe`)
    không mang `query` trên bất kỳ node nào — nếu implementation quay lại `with_query`, node đó sẽ
    có `query`, và bài này đỏ."""
    kb = _RecordingKbSearch([_item("ankor-leave-001#c1")])
    writer = _CollectingTraceWriter()
    llm = _MultiTurnScriptedLLM(
        [
            'TOOL_CALL: {"tool": "kb_search", "params": {"query": "nghỉ phép?"}}',
            "Cần báo trước 3 ngày [ankor-leave-001#c1].",
            "CO",  # engine#43 faithfulness-verify
        ]
    )
    runner = _runner(kb, llm, writer)

    case_run = await runner.run_case(
        agent_id="agent-callisto-d4", query="nghỉ phép?", tenant_id=TENANT_ID, section_roles=["public"]
    )

    assert case_run.answer.answer == "Cần báo trước 3 ngày [ankor-leave-001#c1]."
    assert case_run.answer.citations == ["ankor-leave-001#c1"]
    assert case_run.answer.refused is False
    # kb.search nhận đúng query LLM tự phát trong TOOL_CALL, không phải node.params["query"] tĩnh.
    assert kb.calls[0][0] == "nghỉ phép?"
    base = runner.certified_recipe(agent_id="agent-callisto-d4", tenant_id=TENANT_ID, section_roles=["public"])
    for node in base.dag.nodes:
        assert "query" not in node.params, "recipe gốc không được mang query trên bất kỳ node nào"


async def test_run_case_answers_immediately_without_any_tool_call() -> None:
    """LLM có thể trả lời NGAY lượt 1 mà không gọi tool nào — hợp lệ với vòng lặp mới (khác DAG cũ,
    nơi kb-retrieve luôn chạy). Sự kiện trace chỉ có đúng 1 `llm-step`, không có `kb-retrieve`."""
    writer = _CollectingTraceWriter()
    llm = _MultiTurnScriptedLLM(["Xin chào, tôi không cần tra cứu gì cho câu này."])
    runner = _runner(_RecordingKbSearch([]), llm, writer)

    case_run = await runner.run_case(
        agent_id="agent-callisto-d4", query="chào bạn", tenant_id=TENANT_ID, section_roles=["public"]
    )

    assert [e.node_type.value for e in case_run.events] == ["llm-step"]
    assert case_run.answer.answer == "Xin chào, tôi không cần tra cứu gì cho câu này."


async def test_run_case_money_shot_zero_chunk_refuses_without_dag() -> None:
    """MONEY-SHOT qua adapter mới: `kb.search` trả `[]` (fence chặn sạch) → LLM tự phát TOOL_CALL rồi
    nhận observation rỗng → từ chối. Không còn node `end` cố định (khác DAG cũ, 4 event)."""
    writer = _CollectingTraceWriter()
    llm = _MultiTurnScriptedLLM(
        [
            'TOOL_CALL: {"tool": "kb_search", "params": {"query": "câu hỏi tenant khác?"}}',
            "Không có đoạn trích nào để trả lời.",
        ]
    )
    runner = _runner(_RecordingKbSearch([]), llm, writer)

    case_run = await runner.run_case(
        agent_id="agent-callisto-d4", query="câu hỏi tenant khác?", tenant_id=TENANT_ID, section_roles=["public"]
    )

    assert case_run.answer.refused is True
    assert case_run.answer.citations == []
    assert [e.node_type.value for e in case_run.events] == ["llm-step", "kb-retrieve", "llm-step"]


async def test_run_case_agent_loop_exhausted_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """`AgentLoopExhausted` (hết `max_turns` chưa có câu trả lời) phải PROPAGATE ra khỏi `run_case()`
    nguyên vẹn — không bị `run_case` nuốt/biến thành kết quả rỗng-lặng-lẽ. Composition root
    (`routes/publish.py::_evaluate`) là nơi quyết định map HTTP status, không phải adapter này."""
    from studio_engine.agent_loop import DEFAULT_MAX_TURNS

    del monkeypatch
    writer = _CollectingTraceWriter()
    # Luôn phát TOOL_CALL, không bao giờ trả lời cuối ⇒ hết max_turns.
    llm = _MultiTurnScriptedLLM(
        ['TOOL_CALL: {"tool": "kb_search", "params": {"query": "q"}}'] * (DEFAULT_MAX_TURNS + 1)
    )
    runner = _runner(_RecordingKbSearch([_item("c1")]), llm, writer)

    from studio_engine.agent_loop import AgentLoopExhausted

    with pytest.raises(AgentLoopExhausted):
        await runner.run_case(agent_id="agent-callisto-d4", query="q", tenant_id=TENANT_ID, section_roles=["public"])
