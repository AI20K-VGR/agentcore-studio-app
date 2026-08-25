"""`routes/test_chat.py::test_chat`/`_run_test_chat` — nút Test, chat THẬT trên recipe DRAFT.

Cùng khuôn `test_routes_chat_agent_loop.py` (double `agent_loop`, không dựng Postgres thật cho lớp
kiểm hợp đồng nối dây) — nhưng gọi `_run_test_chat()` trực tiếp (không qua route `test_chat()`) cho
mọi test map exception/wiring, vì `test_chat()` gọi `fetch_fresh_identity(get_request_connection(),
...)` TRƯỚC — cần Postgres thật để tra `core.users` (cùng lý do `test_publish_exception_mapping.py`
gọi thẳng `_evaluate()` thay vì `evaluate_agent()`/`publish_agent()`).

Gate quyền (`require_admin`) chỉ sống trong `test_chat()` (route) — kiểm riêng ở lớp cần DB thật,
cùng khuôn `test_routes_runs.py`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import Token
from uuid import UUID

import pytest
import pytest_asyncio
import studio_app.routes.test_chat as test_chat_module
from fastapi import HTTPException
from studio_app import middleware
from studio_app.core._db import Pool, close_pools, get_pool
from studio_app.routes.test_chat import TestChatRequest as _TestChatRequest
from studio_app.routes.test_chat import _run_test_chat
from studio_app.routes.test_chat import test_chat as _test_chat_route
from studio_contracts import Recipe
from studio_engine.agent_loop import AgentLoopExhausted
from studio_engine.interpreter import RunResult
from studio_workbench.tenant_wall import ResolvedContext

TENANT_ID = UUID("a0000000-0000-0000-0000-000000000001")


class _SentinelPool:
    """Không mở connection thật — `PgKbSearch`/`PgTraceWriter` chỉ lưu tham chiếu lúc dựng."""


async def _fake_get_pool() -> _SentinelPool:
    return _SentinelPool()


class _RecordingAgentLoop:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def run_agent_loop(self, recipe: Recipe, **kwargs: object) -> RunResult:
        self.calls.append({"recipe": recipe, **kwargs})
        return RunResult(
            run_id="r-test-chat-test",
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


def _body(**overrides: object) -> _TestChatRequest:
    base: dict[str, object] = {
        "agent_id": "agent-test-chat",
        "system_prompt": "Tra cứu quy trình và bảo mật Callisto.",
        "tool_whitelist": [],
        "nodes": [
            {"id": "n1", "type": "kb-retrieve", "params": {}},
            {"id": "n2", "type": "llm-step", "params": {"temperature": 0.0}},
            {"id": "n4", "type": "end", "params": {}},
        ],
        "edges": [{"from": "n1", "to": "n2"}, {"from": "n2", "to": "n4"}],
        "message": "nghỉ phép cần báo trước bao lâu?",
    }
    base.update(overrides)
    return _TestChatRequest.model_validate(base)


def _session() -> ResolvedContext:
    return ResolvedContext(tenant_id=TENANT_ID, user="admin@ankor.vn", system_roles=["admin"])


# ---------------------------------------------------------------------------
# Lớp A — `_run_test_chat`, không cần DB (khuôn `test_routes_chat_agent_loop.py`)
# ---------------------------------------------------------------------------


async def test_run_test_chat_rejects_agent_id_mismatch() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await _run_test_chat("agent-other", _body(), _session())
    assert exc_info.value.status_code == 400


async def test_run_test_chat_rejects_invalid_node(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(test_chat_module, "get_pool", _fake_get_pool)
    body = _body(nodes=[{"id": "n1", "type": "not-a-real-type", "params": {}}])
    with pytest.raises(HTTPException) as exc_info:
        await _run_test_chat("agent-test-chat", body, _session())
    assert exc_info.value.status_code == 400


async def test_run_test_chat_rejects_graph_lint_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(test_chat_module, "get_pool", _fake_get_pool)
    # 1 node kb-retrieve lơ lửng, không nối gì — graph_lint từ chối (thiếu edge/end reachability).
    body = _body(
        nodes=[{"id": "n1", "type": "kb-retrieve", "params": {}}],
        edges=[],
    )
    with pytest.raises(HTTPException) as exc_info:
        await _run_test_chat("agent-test-chat", body, _session())
    assert exc_info.value.status_code == 400


async def test_run_test_chat_calls_run_agent_loop_with_message_as_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(test_chat_module, "get_pool", _fake_get_pool)
    # `_RecordingAgentLoop.run_agent_loop` không bao giờ dùng `llm`/`embedding` thật — nhưng
    # `_run_test_chat()` vẫn gọi `build_llm()`/`build_embedding()` (đọc `Settings` thật qua env)
    # TRƯỚC khi tới đó, cùng vướng mắc `test_publish_exception_mapping.py` đã gặp.
    monkeypatch.setattr(test_chat_module, "build_llm", lambda: None)
    monkeypatch.setattr(test_chat_module, "build_embedding", lambda: None)
    stub = _RecordingAgentLoop()
    monkeypatch.setattr(test_chat_module, "agent_loop", stub)

    response = await _run_test_chat("agent-test-chat", _body(), _session())

    assert response.answer == "Cần báo trước 3 ngày [c1]."
    assert response.citations == ["c1"]
    assert response.refused is False
    assert response.run_id == "r-test-chat-test"
    assert len(stub.calls) == 1
    assert stub.calls[0]["question"] == "nghỉ phép cần báo trước bao lâu?"


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
async def test_run_test_chat_maps_agent_loop_exceptions_to_clean_500(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    monkeypatch.setattr(test_chat_module, "get_pool", _fake_get_pool)
    monkeypatch.setattr(test_chat_module, "build_llm", lambda: None)
    monkeypatch.setattr(test_chat_module, "build_embedding", lambda: None)
    monkeypatch.setattr(test_chat_module, "agent_loop", _RaisingAgentLoop(exc))

    with pytest.raises(HTTPException) as exc_info:
        await _run_test_chat("agent-test-chat", _body(), _session())

    assert exc_info.value.status_code == 500, (
        f"{type(exc).__name__} phải map 500, thực tế status={exc_info.value.status_code}"
    )


# ---------------------------------------------------------------------------
# Lớp B — route `test_chat` đầy đủ (admin-gate), cần DB — khuôn `test_routes_runs.py`
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    yield
    await close_pools()


async def _seed_tenant(admin_pool: Pool, name: str) -> UUID:
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", (name,))
        row = await cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


async def _seed_employee_user(admin_pool: Pool, tenant_id: UUID, email: str) -> None:
    async with admin_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, system_roles) VALUES (%s, %s, %s, %s)",
            (str(tenant_id), email, "not-a-real-hash", ["public"]),
        )


@asynccontextmanager
async def _simulate_request_connection() -> AsyncIterator[None]:
    pool = await get_pool()
    async with pool.connection() as conn:
        token = middleware._request_conn.set(conn)
        try:
            yield
        finally:
            middleware._request_conn.reset(token)


def _set_session(tenant_id: UUID, user: str, roles: list[str]) -> Token[ResolvedContext | None]:
    session = ResolvedContext(tenant_id=tenant_id, user=user, system_roles=roles)
    return middleware._request_session.set(session)


async def test_test_chat_route_requires_admin(admin_pool: Pool) -> None:
    tenant = await _seed_tenant(admin_pool, "test-chat-requires-admin")
    await _seed_employee_user(admin_pool, tenant, "employee@acme.com")
    token = _set_session(tenant, "employee@acme.com", ["public"])

    try:
        async with _simulate_request_connection():
            with pytest.raises(HTTPException) as exc_info:
                await _test_chat_route("agent-test-chat", _body())
    finally:
        middleware._request_session.reset(token)

    assert exc_info.value.status_code == 403


async def test_test_chat_route_without_session_raises_401(admin_pool: Pool) -> None:
    del admin_pool
    with pytest.raises(HTTPException) as exc_info:
        await _test_chat_route("agent-test-chat", _body())
    assert exc_info.value.status_code == 401
