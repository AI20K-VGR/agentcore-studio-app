"""`routes/chat.py` — app#74: `conversation_id` (đọc+ghi lịch sử qua `wb.conversations`/
`wb.conversation_messages`) + `GET /{agent_id}/conversations/{conversation_id}`.

Postgres THẬT (khuôn `test_routes_chat_as_roles.py` — `admin_pool`/`_seed_*`/
`_simulate_request_connection`): phần cốt lõi ở đây là hành vi ĐỌC/GHI 2 bảng mới qua RLS + fence
`agent_id`, không mô phỏng được sạch bằng monkeypatch thuần như `test_routes_chat_agent_loop.py`.
`agent_loop.run_agent_loop` vẫn được double (`_RecordingAgentLoop`) — hành vi CỦA nó có test riêng
ở `packages/engine`, trọng tâm ở đây là dây nối `conversation_id`/`history`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast
from uuid import UUID

import pytest
import pytest_asyncio
import studio_app.routes.chat as chat_module
from fastapi import HTTPException
from studio_app import middleware
from studio_app.core._db import Pool, close_pools, get_pool
from studio_app.routes.chat import ChatRequest, chat, get_conversation
from studio_contracts import Edge, Node, NodeType, Recipe
from studio_engine.agent_protocol import HistoryTurn
from studio_engine.interpreter import RunResult
from studio_workbench import create_recipe
from studio_workbench.tenant_wall import ResolvedContext


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    yield
    await close_pools()


class _SentinelKbPool:
    """Cùng khuôn `_SentinelPool` ở `test_routes_chat_agent_loop.py` — `_RecordingAgentLoop` bên
    dưới không bao giờ chạm `PgKbSearch`/`PgTraceWriter` được dựng từ nó."""


async def _fake_get_pool() -> _SentinelKbPool:
    return _SentinelKbPool()


class _RecordingAgentLoop:
    """Ghi lại kwargs thật `chat()` truyền (đặc biệt `history`), trả lời khác nhau mỗi lần gọi
    (đếm theo `self.calls`) để phân biệt được lượt 1/lượt 2 trong bài round-trip."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def run_agent_loop(self, recipe: Recipe, **kwargs: object) -> RunResult:
        self.calls.append({"recipe": recipe, **kwargs})
        n = len(self.calls)
        return RunResult(
            run_id=f"r-conv-{n}",
            events=[],
            final_state={
                "t2-llm": {
                    "answer": f"trả lời lượt {n} [c{n}].",
                    "citations": [f"c{n}"],
                    "refused": False,
                    "signal": "final-answer",
                }
            },
        )


def _set_session(*, tenant_id: UUID, user: str, system_roles: list[str]) -> object:
    session = ResolvedContext(tenant_id=tenant_id, user=user, system_roles=system_roles)
    return middleware._request_session.set(session)


async def _bind_tenant(conn: object, tenant_id: UUID) -> None:
    await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))  # type: ignore[attr-defined]


@asynccontextmanager
async def _simulate_request_connection(tenant_id: UUID) -> AsyncIterator[None]:
    pool = await get_pool()
    async with pool.connection() as conn:
        await _bind_tenant(conn, tenant_id)
        token = middleware._request_conn.set(conn)
        try:
            yield
        finally:
            middleware._request_conn.reset(token)


async def _seed_tenant(admin_pool: Pool, name: str) -> UUID:
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", (name,))
        row = await cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


async def _seed_published_recipe(admin_pool: Pool, tenant_id: UUID, agent_id: str) -> None:
    # workbench#41 — create_recipe_d4() đã bị xoá. Dựng thủ công cùng hình dạng DAG 3-node nó
    # từng tự sinh; chat() không đọc recipe.dag (app#44), chỉ cần Recipe hợp lệ.
    nodes = [
        Node(id="n1", type=NodeType.KB_RETRIEVE, params={"top_k": 3}),
        Node(id="n2", type=NodeType.LLM_STEP, params={"temperature": 0.0}),
        Node(id="n4", type=NodeType.END, params={}),
    ]
    edges = [Edge(from_="n1", to="n2"), Edge(from_="n2", to="n4")]
    recipe = create_recipe(
        agent_id=agent_id,
        tenant_id=tenant_id,
        system_prompt="Tra cứu quy trình và bảo mật Callisto.",
        tool_whitelist=[],
        nodes=nodes,
        edges=edges,
    )
    async with admin_pool.connection() as conn, conn.transaction():
        await _bind_tenant(conn, tenant_id)
        await conn.execute(
            "INSERT INTO wb.recipes (agent_id, tenant_id, recipe, version, status) "
            "VALUES (%s, %s, %s::jsonb, 1, 'published')",
            (agent_id, str(tenant_id), recipe.model_dump_json()),
        )


async def _seed_conversation(admin_pool: Pool, tenant_id: UUID, agent_id: str) -> UUID:
    async with admin_pool.connection() as conn, conn.transaction():
        await _bind_tenant(conn, tenant_id)
        cur = await conn.execute(
            "INSERT INTO wb.conversations (tenant_id, agent_id) VALUES (%s, %s) RETURNING id",
            (str(tenant_id), agent_id),
        )
        row = await cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


async def _seed_message(
    admin_pool: Pool, tenant_id: UUID, conversation_id: UUID, turn_index: int, question: str, answer: str
) -> None:
    async with admin_pool.connection() as conn, conn.transaction():
        await _bind_tenant(conn, tenant_id)
        await conn.execute(
            "INSERT INTO wb.conversation_messages "
            "(conversation_id, tenant_id, turn_index, question, answer, citations, run_id) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)",
            (str(conversation_id), str(tenant_id), turn_index, question, answer, "[]", f"r-seed-{turn_index}"),
        )


async def test_conversation_round_trip_threads_history_and_persists_messages(
    admin_pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = await _seed_tenant(admin_pool, "chat-conv-roundtrip")
    agent_id = "agent-conv-roundtrip"
    await _seed_published_recipe(admin_pool, tenant_id, agent_id)

    stub = _RecordingAgentLoop()
    monkeypatch.setattr(chat_module, "agent_loop", stub)
    monkeypatch.setattr(chat_module, "get_pool", _fake_get_pool)

    token = _set_session(tenant_id=tenant_id, user="u@acme.com", system_roles=["public"])
    try:
        # Lượt 1 — conversation_id=None => tạo phiên mới, history rỗng.
        async with _simulate_request_connection(tenant_id):
            r1 = await chat(agent_id, ChatRequest(message="hỏi 1"))
        assert stub.calls[0]["history"] == []
        conversation_id = r1.conversation_id
        UUID(conversation_id)  # phải là UUID hợp lệ

        # Lượt 2 — cùng conversation_id, mô phỏng 1 REQUEST khác (connection mới).
        async with _simulate_request_connection(tenant_id):
            r2 = await chat(agent_id, ChatRequest(message="hỏi 2", conversation_id=conversation_id))
        assert r2.conversation_id == conversation_id
        history_2 = cast("list[HistoryTurn]", stub.calls[1]["history"])
        assert len(history_2) == 1
        assert history_2[0].question == "hỏi 1"
        assert history_2[0].answer == r1.answer
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    async with admin_pool.connection() as conn:
        await _bind_tenant(conn, tenant_id)
        cur = await conn.execute(
            "SELECT turn_index, question, answer FROM wb.conversation_messages "
            "WHERE conversation_id = %s ORDER BY turn_index ASC",
            (conversation_id,),
        )
        rows = await cur.fetchall()
    assert [row[0] for row in rows] == [1, 2]
    assert rows[0][1] == "hỏi 1"
    assert rows[1][1] == "hỏi 2"


async def test_conversation_id_from_other_tenant_is_404_not_a_leak(admin_pool: Pool) -> None:
    tenant_a = await _seed_tenant(admin_pool, "chat-conv-tenant-a")
    tenant_b = await _seed_tenant(admin_pool, "chat-conv-tenant-b")
    agent_id = "agent-conv-cross-tenant"
    await _seed_published_recipe(admin_pool, tenant_a, agent_id)
    await _seed_published_recipe(admin_pool, tenant_b, agent_id)
    conversation_id = await _seed_conversation(admin_pool, tenant_a, agent_id)
    await _seed_message(admin_pool, tenant_a, conversation_id, 1, "bí mật tenant A", "đáp bí mật")

    token = _set_session(tenant_id=tenant_b, user="u@other.com", system_roles=["public"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection(tenant_b):
                await chat(agent_id, ChatRequest(message="hi", conversation_id=str(conversation_id)))
        assert exc_info.value.status_code == 404

        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection(tenant_b):
                await get_conversation(agent_id, str(conversation_id))
        assert exc_info.value.status_code == 404
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_conversation_id_from_other_agent_same_tenant_is_404(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "chat-conv-cross-agent")
    await _seed_published_recipe(admin_pool, tenant_id, "agent-conv-owner")
    await _seed_published_recipe(admin_pool, tenant_id, "agent-conv-stranger")
    conversation_id = await _seed_conversation(admin_pool, tenant_id, "agent-conv-owner")

    token = _set_session(tenant_id=tenant_id, user="u@acme.com", system_roles=["public"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection(tenant_id):
                await chat("agent-conv-stranger", ChatRequest(message="hi", conversation_id=str(conversation_id)))
        assert exc_info.value.status_code == 404
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_get_conversation_returns_all_turns_uncapped_in_order(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "chat-conv-get-uncapped")
    agent_id = "agent-conv-get-uncapped"
    await _seed_published_recipe(admin_pool, tenant_id, agent_id)
    conversation_id = await _seed_conversation(admin_pool, tenant_id, agent_id)
    total_turns = chat_module._CONVERSATION_HISTORY_CAP + 2  # cố ý vượt cap lịch sử prompt
    for i in range(1, total_turns + 1):
        await _seed_message(admin_pool, tenant_id, conversation_id, i, f"q{i}", f"a{i}")

    token = _set_session(tenant_id=tenant_id, user="u@acme.com", system_roles=["public"])
    try:
        async with _simulate_request_connection(tenant_id):
            response = await get_conversation(agent_id, str(conversation_id))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert response.conversation_id == str(conversation_id)
    assert len(response.turns) == total_turns  # KHÔNG cắt theo _CONVERSATION_HISTORY_CAP
    assert [t.turn_index for t in response.turns] == list(range(1, total_turns + 1))
    assert response.turns[0].question == "q1"
    assert response.turns[-1].question == f"q{total_turns}"


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "12345", ""])
async def test_chat_rejects_malformed_conversation_id_with_400(admin_pool: Pool, bad_id: str) -> None:
    tenant_id = await _seed_tenant(admin_pool, "chat-conv-bad-uuid")
    agent_id = "agent-conv-bad-uuid"
    await _seed_published_recipe(admin_pool, tenant_id, agent_id)

    token = _set_session(tenant_id=tenant_id, user="u@acme.com", system_roles=["public"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection(tenant_id):
                await chat(agent_id, ChatRequest(message="hi", conversation_id=bad_id))
        assert exc_info.value.status_code == 400

        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection(tenant_id):
                await get_conversation(agent_id, bad_id)
        assert exc_info.value.status_code == 400
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]
