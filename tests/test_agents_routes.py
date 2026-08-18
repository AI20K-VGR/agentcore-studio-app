"""`routes/agents.py` — `GET /api/agents` (list agent đã publish của tenant) +
`POST /api/agents/{agent_id}/rollback` (nối dây `studio_workbench.publish.rollback()` đã có sẵn)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import HTTPException
from studio_app import middleware
from studio_app.core._db import Pool, close_pools, get_pool
from studio_app.routes.agents import RollbackRequest, list_agents, rollback_agent
from studio_workbench.tenant_wall import ResolvedContext


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    yield
    await close_pools()


def _set_session(*, tenant_id: UUID, user: str, roles: list[str]) -> object:
    session = ResolvedContext(tenant_id=tenant_id, user=user, roles=roles)
    return middleware._request_session.set(session)


async def _bind_tenant(conn: object, tenant_id: UUID) -> None:
    """`wb.recipes`/`wb.recipe_versions` bật `FORCE ROW LEVEL SECURITY` (`schema.py`, kit#117 Q7)
    — cắn CẢ `studio_owner` (`admin_pool`), không chỉ `studio_app`. Mọi đọc/ghi 2 bảng này (kể cả
    seed dữ liệu test) phải tự bind `app.tenant_id` TRƯỚC, transaction-scoped, đúng cơ chế
    `tenant_context_middleware` dùng cho request thật (`middleware.py`) — cùng pattern
    `test_gate2_publish_money_shot.py::_bind_tenant`."""
    await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))  # type: ignore[attr-defined]


@asynccontextmanager
async def _simulate_request_connection(tenant_id: UUID) -> AsyncIterator[None]:
    """`tenant_id` bắt buộc truyền vào (khác `test_admin_routes.py`'s bản không cần, vì `core.*`
    không có RLS) — route dưới đây chạm `wb.recipes` (CÓ RLS), nên phải bind đúng tenant của
    session trước khi route chạy, y hệt việc `tenant_context_middleware` làm cho request thật."""
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


_RECIPE_JSON = '{"agent_id": "placeholder", "tenant_id": "placeholder"}'
"""Nội dung `wb.recipes.recipe`/`wb.recipe_versions.recipe` không quan trọng cho 2 route này —
`list_agents`/`rollback_agent` chỉ đọc `agent_id`/`version`/`status`, không đụng payload recipe."""


async def _seed_published_recipe(
    admin_pool: Pool, *, tenant_id: UUID, agent_id: str, version: int, status: str = "published"
) -> None:
    async with admin_pool.connection() as conn, conn.transaction():
        await _bind_tenant(conn, tenant_id)
        await conn.execute(
            "INSERT INTO wb.recipes (agent_id, tenant_id, recipe, version, status) VALUES (%s, %s, %s, %s, %s)",
            (agent_id, str(tenant_id), _RECIPE_JSON, version, status),
        )
        await conn.execute(
            "INSERT INTO wb.recipe_versions (recipe_id, agent_id, tenant_id, recipe, version, status) "
            "SELECT id, agent_id, tenant_id, recipe, version, status FROM wb.recipes "
            "WHERE agent_id = %s AND tenant_id = %s AND version = %s",
            (agent_id, str(tenant_id), version),
        )


async def _read_recipe_rows(admin_pool: Pool, tenant_id: UUID, agent_id: str) -> list[tuple[int, str]]:
    async with admin_pool.connection() as conn, conn.transaction():
        await _bind_tenant(conn, tenant_id)
        cur = await conn.execute(
            "SELECT version, status FROM wb.recipes WHERE agent_id = %s AND tenant_id = %s ORDER BY version",
            (agent_id, str(tenant_id)),
        )
        return [(int(v), str(s)) for v, s in await cur.fetchall()]


async def test_list_agents_scoped_to_own_tenant_via_rls(admin_pool: Pool) -> None:
    tenant_a = await _seed_tenant(admin_pool, "agents-probe-a")
    tenant_b = await _seed_tenant(admin_pool, "agents-probe-b")
    await _seed_user(admin_pool, tenant_a, "user-a@acme.com", ["public"])
    await _seed_published_recipe(admin_pool, tenant_id=tenant_a, agent_id="agent-a1", version=1)
    await _seed_published_recipe(admin_pool, tenant_id=tenant_b, agent_id="agent-b1", version=1)

    token = _set_session(tenant_id=tenant_a, user="user-a@acme.com", roles=["public"])
    try:
        async with _simulate_request_connection(tenant_a):
            result = await list_agents()
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    agent_ids = {a.agent_id for a in result}
    assert agent_ids == {"agent-a1"}, "RLS phải tự lọc — không thấy agent-b1 của tenant khác"


async def test_list_agents_returns_latest_published_version(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "agents-probe-c")
    await _seed_user(admin_pool, tenant_id, "user@acme.com", ["public"])
    await _seed_published_recipe(admin_pool, tenant_id=tenant_id, agent_id="agent-c1", version=1)
    await _seed_published_recipe(admin_pool, tenant_id=tenant_id, agent_id="agent-c1", version=2)

    token = _set_session(tenant_id=tenant_id, user="user@acme.com", roles=["public"])
    try:
        async with _simulate_request_connection(tenant_id):
            result = await list_agents()
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert len(result) == 1
    assert result[0].agent_id == "agent-c1"
    assert result[0].latest_published_version == 2


async def test_list_agents_excludes_unpublished_status(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "agents-probe-d")
    await _seed_user(admin_pool, tenant_id, "user@acme.com", ["public"])
    await _seed_published_recipe(admin_pool, tenant_id=tenant_id, agent_id="agent-draft", version=1, status="draft")

    token = _set_session(tenant_id=tenant_id, user="user@acme.com", roles=["public"])
    try:
        async with _simulate_request_connection(tenant_id):
            result = await list_agents()
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert result == []


async def test_rollback_requires_admin(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "agents-probe-e")
    await _seed_user(admin_pool, tenant_id, "user@acme.com", ["public"])
    await _seed_published_recipe(admin_pool, tenant_id=tenant_id, agent_id="agent-e1", version=1)

    token = _set_session(tenant_id=tenant_id, user="user@acme.com", roles=["public"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection(tenant_id):
                await rollback_agent("agent-e1", RollbackRequest(to_version=1))
        assert exc_info.value.status_code == 403
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_rollback_missing_version_returns_404(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "agents-probe-f")
    await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])
    await _seed_published_recipe(admin_pool, tenant_id=tenant_id, agent_id="agent-f1", version=1)

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection(tenant_id):
                await rollback_agent("agent-f1", RollbackRequest(to_version=99))
        assert exc_info.value.status_code == 404
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_rollback_restores_prior_version(admin_pool: Pool) -> None:
    """`rollback()` (`studio_workbench.publish`) không xoá row cũ — nó chỉ đổi `status` (row v1:
    draft->published, row v2: published->rolled_back), cả 2 row VẪN CÙNG TỒN TẠI trong `wb.recipes`
    (đúng `UNIQUE(agent_id,tenant_id,version)`, khác version nên không đụng nhau). Seed v1=`draft`
    (đúng trạng thái thật `publish()` để lại khi v2 xuất bản đè lên), v2=`published` (đang live) —
    verify sau rollback phải lọc `status='published'` tường minh, không suy đoán thứ tự trả về."""
    tenant_id = await _seed_tenant(admin_pool, "agents-probe-g")
    await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])
    await _seed_published_recipe(admin_pool, tenant_id=tenant_id, agent_id="agent-g1", version=1, status="draft")
    await _seed_published_recipe(admin_pool, tenant_id=tenant_id, agent_id="agent-g1", version=2, status="published")

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        async with _simulate_request_connection(tenant_id):
            result = await rollback_agent("agent-g1", RollbackRequest(to_version=1))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert result.status == "rolled_back"
    assert result.version == 1

    rows = await _read_recipe_rows(admin_pool, tenant_id, "agent-g1")
    assert dict(rows) == {1: "published", 2: "rolled_back"}, (
        f"v1 phải thành published, v2 phải thành rolled_back, cả 2 row vẫn tồn tại — thấy {rows}"
    )
