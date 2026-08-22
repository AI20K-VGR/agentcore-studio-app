"""`routes/chat.py::chat` — PA-4 (app#41 review finding, dholmes0207): recipe đã published mang
node `tool-call{tool:"kb_search"}` (kb_search có kind `kb_retrieve`, nợ PA-3 ở
`create_recipe_d4()`, packages/workbench — KHÔNG sửa ở đây) phải trả 500 SẠCH
(`HTTPException`, message chỉ đúng nguyên nhân), không phải để `ValueError` trần từ
`interpreter.run()` leak ra thành unhandled exception. Cùng khuôn `test_routes_chat_as_roles.py`
(gọi thẳng `chat()`, không dựng `TestClient`) — cả bài test này raise TRƯỚC KHI chạm
`interpreter.run()`, nên không cần KB/LLM thật."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import HTTPException
from studio_app import middleware
from studio_app.core._db import Pool, close_pools, get_pool
from studio_app.routes.chat import ChatRequest, chat
from studio_workbench import create_recipe_d4
from studio_workbench.tenant_wall import ResolvedContext


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    yield
    await close_pools()


def _set_session(*, tenant_id: UUID, user: str, roles: list[str]) -> object:
    session = ResolvedContext(tenant_id=tenant_id, user=user, roles=roles)
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


async def _seed_user(admin_pool: Pool, tenant_id: UUID, email: str, roles: list[str]) -> None:
    async with admin_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, roles) VALUES (%s, %s, %s, %s)",
            (str(tenant_id), email, "not-a-real-hash", roles),
        )


async def _seed_published_recipe_with_unsupported_tool_call(admin_pool: Pool, tenant_id: UUID, agent_id: str) -> None:
    """`create_recipe_d4()` mặc định (không truyền `tool_whitelist`) sinh đúng node
    `tool-call{tool:"kb_search"}` cần cho bài test này — không cần tự dựng recipe tay."""
    recipe = create_recipe_d4(agent_id=agent_id, tenant_id=tenant_id)
    async with admin_pool.connection() as conn, conn.transaction():
        await _bind_tenant(conn, tenant_id)
        await conn.execute(
            "INSERT INTO wb.recipes (agent_id, tenant_id, recipe, version, status) "
            "VALUES (%s, %s, %s::jsonb, 1, 'published')",
            (agent_id, str(tenant_id), recipe.model_dump_json()),
        )


async def test_chat_rejects_published_recipe_with_unsupported_tool_call_with_clean_500(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "chat-pa4-unsupported-tool-call")
    await _seed_user(admin_pool, tenant_id, "employee@acme.com", ["public"])
    await _seed_published_recipe_with_unsupported_tool_call(admin_pool, tenant_id, "agent-pa4-kbsearch")

    token = _set_session(tenant_id=tenant_id, user="employee@acme.com", roles=["public"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection(tenant_id):
                await chat("agent-pa4-kbsearch", ChatRequest(message="hi"))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 500, (
        f"đáng lẽ 500 sạch (preflight PA-4, cùng mức graph_lint() đỏ), thực tế status={exc_info.value.status_code}"
    )
    assert "kb_retrieve" in exc_info.value.detail
