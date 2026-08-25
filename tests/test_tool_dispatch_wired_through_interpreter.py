"""Smoke test engine#32: `build_tool_dispatch()` đi hết đường qua `interpreter.run()` THẬT
(không mock `ToolCallExecutor`) — khoá composition-root wiring, không chỉ `RealToolDispatch` đứng
riêng (`test_providers_tool_dispatch.py`). Không nhận fixture DB (`admin_pool`/`pool`) — cùng lý do
`test_eval_adapter.py` khai, chạy được ở submodule CI reconstruct chỉ pytest, không cần Postgres
thật (kb_search/llm/embedding/trace_writer đều fake — chỉ `tool_dispatch` là thật)."""

from __future__ import annotations

from uuid import UUID

from studio_app.providers.factory import build_tool_dispatch
from studio_app.providers.fakes import FakeEmbedding, FakeLLM
from studio_contracts import Edge, Node, NodeType, TraceEvent
from studio_contracts.kb import KbSearchResultItem
from studio_engine import interpreter as engine_interpreter
from studio_workbench import create_recipe
from studio_workbench.tenant_wall import resolve_session

_TENANT_ID = UUID("d0000000-0000-0000-0000-000000000004")


class _NullKbSearch:
    async def search(
        self, query: str, tenant_id: UUID, section_roles: list[str], top_k: int
    ) -> list[KbSearchResultItem]:
        del query, tenant_id, section_roles, top_k
        return []


class _CollectingTraceWriter:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    async def write(self, event: TraceEvent) -> None:
        self.events.append(event)


async def test_calculator_tool_call_returns_real_result_not_stub_marker() -> None:
    """Recipe 2-node `tool-call(calculator) -> end`, chạy qua `interpreter.run()` với
    `tool_dispatch=build_tool_dispatch(...)` — output KHÔNG còn là `stub-dispatched` cũ (engine#31
    xưa), phải là `{"expression", "result"}` thật."""
    recipe = create_recipe(
        agent_id="smoke-calc-agent",
        tenant_id=_TENANT_ID,
        system_prompt="x",
        tool_whitelist=["calculator"],
        nodes=[
            Node(id="n1", type=NodeType.TOOL_CALL, params={"tool": "calculator", "expression": "6 * 7"}),
            Node(id="n2", type=NodeType.END, params={}),
        ],
        edges=[Edge(from_="n1", to="n2")],
    )

    result = await engine_interpreter.run(
        recipe,
        session_context=resolve_session({"tenant_id": _TENANT_ID, "user": "smoke-test", "roles": []}),
        kb_search=_NullKbSearch(),
        llm=FakeLLM(),
        embedding=FakeEmbedding(),
        trace_writer=_CollectingTraceWriter(),
        tool_dispatch=build_tool_dispatch(recipe.agent_config.tool_whitelist),
    )

    assert result.final_state["n1"] == {"expression": "6 * 7", "result": 42}
