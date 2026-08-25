"""`routes/chat.py::chat` — `body.as_roles` (admin giả lập role để test chat, xem docstring
`ChatRequest.as_roles`). Chỉ test 2 nhánh CHẶN (403 không-admin, 400 role-giả-lập-không-tồn-tại) —
cả 2 raise TRƯỚC KHI chạm `run_agent_loop()` (app#44 — trước đó `interpreter.run()`), nên không cần
dựng đủ KB/LLM thật, chỉ cần 1 recipe published hợp lệ để qua được `_load_published_recipe()`
(`graph_lint()` đã bị bỏ khỏi route này ở app#44 — không còn là tiền điều kiện nữa)."""

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
from studio_contracts import Edge, Node, NodeType
from studio_workbench import create_recipe
from studio_workbench.tenant_wall import ResolvedContext


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    yield
    await close_pools()


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


async def _seed_user(admin_pool: Pool, tenant_id: UUID, email: str, system_roles: list[str]) -> None:
    async with admin_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, system_roles) VALUES (%s, %s, %s, %s)",
            (str(tenant_id), email, "not-a-real-hash", system_roles),
        )


async def _seed_published_recipe(admin_pool: Pool, tenant_id: UUID, agent_id: str) -> None:
    # workbench#41 — create_recipe_d4() đã bị xoá. Dựng thủ công cùng hình dạng DAG 3-node nó
    # từng tự sinh (KB_RETRIEVE -> LLM_STEP -> END); chỉ cần recipe published hợp lệ qua
    # _load_published_recipe(), không đụng chi tiết kb_binding/system_prompt.
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


async def test_chat_as_roles_requires_admin(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "chat-as-system_roles-probe-a")
    await _seed_user(admin_pool, tenant_id, "employee@acme.com", ["hr"])
    await _seed_published_recipe(admin_pool, tenant_id, "agent-as-system_roles-a")

    token = _set_session(tenant_id=tenant_id, user="employee@acme.com", system_roles=["hr"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection(tenant_id):
                await chat("agent-as-system_roles-a", ChatRequest(message="hi", as_roles=["hr"]))
        assert exc_info.value.status_code == 403
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_chat_as_roles_rejects_unknown_section(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "chat-as-system_roles-probe-b")
    await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])
    await _seed_published_recipe(admin_pool, tenant_id, "agent-as-system_roles-b")

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", system_roles=["admin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection(tenant_id):
                await chat("agent-as-system_roles-b", ChatRequest(message="hi", as_roles=["khong-ton-tai"]))
        assert exc_info.value.status_code == 400
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_chat_as_roles_rejects_reserved_role_name_even_if_section_exists(admin_pool: Pool) -> None:
    """review `app#21` (phát hiện qua review độc lập) — `fetch_tenant_section_names` (dùng ở đây
    qua `chat.py`) tự trừ `RESERVED_ROLE_NAMES`. Seed thẳng section "superadmin" bằng SQL (bỏ qua
    validator layer-1, mô phỏng đúng ca dòng cũ từ trước layer-1 tồn tại): `as_roles=["superadmin"]`
    vẫn phải bị 400 dù section đó CÓ THẬT trong `core.sections` — nếu không, giá trị đó đi thẳng
    vào `session_context.system_roles`, đầu vào của hàng rào nội dung KB ở `interpreter.run()`."""
    tenant_id = await _seed_tenant(admin_pool, "chat-as-system_roles-probe-c")
    await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])
    await _seed_published_recipe(admin_pool, tenant_id, "agent-as-system_roles-c")
    async with admin_pool.connection() as conn:
        cur = await conn.execute("SELECT id FROM core.users WHERE email = %s", ("admin@acme.com",))
        admin_row = await cur.fetchone()
        assert admin_row is not None
        await conn.execute(
            "INSERT INTO core.sections (tenant_id, name, created_by) VALUES (%s, %s, %s)",
            (str(tenant_id), "superadmin", admin_row[0]),
        )

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", system_roles=["admin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection(tenant_id):
                await chat("agent-as-system_roles-c", ChatRequest(message="hi", as_roles=["superadmin"]))
        assert exc_info.value.status_code == 400
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]
