"""`routes/chat.py::chat` — nối qua `run_agent_loop()` (app#44, call site 1/3), bỏ hẳn
`with_query`/`graph_lint`/`find_unsupported_tool_call`.

Không dựng Postgres thật: monkeypatch `chat_module._load_published_recipe` (bỏ qua bước đọc
`wb.recipes` — hành vi CỦA nó có test riêng, `test_routes_chat_as_roles.py`/route DB thật khác) +
`chat_module.get_pool` (sentinel, cùng khuôn `test_publish_exception_mapping.py` — `PgKbSearch`/
`PgTraceWriter` chỉ lưu `pool`, không mở connection lúc dựng) + `chat_module.agent_loop` (double
ghi lại args thật `chat()` truyền, hoặc ném exception cần đo). Trọng tâm bài ở đây là HỢP ĐỒNG NỐI
DÂY của `chat()` (gọi đúng hàm, đúng kwarg, map đúng exception) — hành vi bản thân `run_agent_loop`
đã có test riêng đầy đủ ở `packages/engine`/`test_eval_adapter*.py`, không lặp lại ở đây."""

from __future__ import annotations

from uuid import UUID

import pytest
import studio_app.routes.chat as chat_module
from fastapi import HTTPException
from studio_app import middleware
from studio_app.routes.chat import ChatRequest, chat
from studio_contracts import Recipe
from studio_engine.agent_loop import AgentLoopExhausted
from studio_engine.interpreter import RunResult
from studio_workbench import create_recipe_d4
from studio_workbench.tenant_wall import ResolvedContext

TENANT_ID = UUID("a0000000-0000-0000-0000-000000000001")


class _SentinelPool:
    """Không mở connection thật — `PgKbSearch`/`PgTraceWriter` chỉ lưu tham chiếu lúc dựng, và
    `_RecordingAgentLoop`/`_RaisingAgentLoop` bên dưới không bao giờ gọi phương thức nào của nó."""


async def _fake_get_pool() -> _SentinelPool:
    return _SentinelPool()


class _RecordingAgentLoop:
    """Namespace fake thay `chat_module.agent_loop` — ghi lại kwargs thật `chat()` truyền, trả 1
    `RunResult` cố định mô phỏng câu trả lời có trích dẫn."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def run_agent_loop(self, recipe: Recipe, **kwargs: object) -> RunResult:
        self.calls.append({"recipe": recipe, **kwargs})
        return RunResult(
            run_id="r-chat-test",
            events=[],
            final_state={
                "t2-llm": {
                    "answer": "Cần báo trước 3 ngày [c1].",
                    "citations": ["c1"],
                    "refused": False,
                    "signal": "final-answer",
                }
            },
        )


class _RaisingAgentLoop:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def run_agent_loop(self, recipe: Recipe, **kwargs: object) -> RunResult:  # noqa: ARG002
        raise self._exc


def _recipe() -> Recipe:
    return create_recipe_d4(agent_id="agent-chat-test", tenant_id=TENANT_ID, scope="t/public")


async def _fake_load_recipe(agent_id: str) -> tuple[Recipe, int]:  # noqa: ARG001
    return _recipe(), 3


def _set_session() -> object:
    session = ResolvedContext(tenant_id=TENANT_ID, user="victim@ankor.vn", roles=["public"])
    return middleware._request_session.set(session)


def test_chat_module_no_longer_imports_dag_only_helpers() -> None:
    """Kiểm cấu trúc: `with_query`/`graph_lint`/`find_unsupported_tool_call` không còn là attribute
    của module này — 3 thứ này thuộc kiến trúc DAG cũ (`PROJECT-SCOPE-DEMO-DAY30.md` mục C: "Không
    còn trong demo: banner lỗi graph_lint 7 luật đồ thị cũ"). Bài này đỏ nếu ai đó import lại 1
    trong 3 tên đó vào module — đúng tinh thần contract-test mà `test_graph_lint_before_interpreter_run.py`
    (đã retire) từng giữ, viết lại cho kiến trúc mới."""
    assert not hasattr(chat_module, "with_query")
    assert not hasattr(chat_module, "graph_lint")
    assert not hasattr(chat_module, "find_unsupported_tool_call")


async def test_chat_calls_run_agent_loop_with_message_as_question(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_module, "_load_published_recipe", _fake_load_recipe)
    monkeypatch.setattr(chat_module, "get_pool", _fake_get_pool)
    stub = _RecordingAgentLoop()
    monkeypatch.setattr(chat_module, "agent_loop", stub)

    token = _set_session()
    try:
        response = await chat("agent-chat-test", ChatRequest(message="nghỉ phép cần báo trước bao lâu?"))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert response.answer == "Cần báo trước 3 ngày [c1]."
    assert response.citations == ["c1"]
    assert response.refused is False
    assert response.version == 3
    assert len(stub.calls) == 1
    assert stub.calls[0]["question"] == "nghỉ phép cần báo trước bao lâu?"
    # Recipe truyền đi phải NGUYÊN VẸN — không with_query/model_copy nào bơm message vào dag.
    assert stub.calls[0]["recipe"] == _recipe()


@pytest.mark.parametrize(
    "exc",
    [
        AgentLoopExhausted(
            "agent loop exhausted after 6 turn(s) without a final answer",
            partial=RunResult(run_id="r1", events=[], final_state={}),
            turns=6,
        ),
        ValueError("tool not in whitelist: rogue_tool"),
        PermissionError("tenant_id không phải UUID hợp lệ"),
    ],
    ids=["AgentLoopExhausted", "ValueError", "PermissionError"],
)
async def test_chat_maps_agent_loop_exceptions_to_clean_500(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    """3 exception `run_agent_loop()` có thể ném mà `interpreter.run()` không có (docstring
    `agent_loop.py`, "Handoff to app#44") đều phải thành 500 SẠCH — không rơi trần thành lỗi
    unhandled. Quyết định đã chốt với user (AskUserQuestion, cùng phiên app#44)."""
    monkeypatch.setattr(chat_module, "_load_published_recipe", _fake_load_recipe)
    monkeypatch.setattr(chat_module, "get_pool", _fake_get_pool)
    monkeypatch.setattr(chat_module, "agent_loop", _RaisingAgentLoop(exc))

    token = _set_session()
    try:
        with pytest.raises(HTTPException) as exc_info:
            await chat("agent-chat-test", ChatRequest(message="q?"))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 500, (
        f"{type(exc).__name__} phải map 500, thực tế status={exc_info.value.status_code}"
    )
