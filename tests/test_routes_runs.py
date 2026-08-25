"""`routes/runs.py::get_run` — `GET /api/runs/{run_id}` đọc lại trace CHAT thật (`PgTraceReader`),
dùng chung cho cả tab "Dùng thử" (agent published) lẫn nút Test (draft, `routes/test_chat.py`).

`POST /api/runs` (connectivity-check tĩnh OK/NOT_IMPLEMENTED, nút Test bản cũ) đã BỎ HẲN — nút Test
giờ là 1 khung chat thật (`routes/test_chat.py`), không còn khái niệm "kiểm tool tĩnh" nữa. Test
cho `_check_tool_connectivity`/`create_run`/`RunRequest` xoá theo, không còn gì để kiểm.

`require_admin` MỚI thêm vào `get_run` (trước đây chỉ lọc theo tenant qua RLS, không gate role) —
nhân viên (tab Chat published) không được đọc trace nữa, chỉ admin mới xem được các bước hệ thống
chạy qua (quyết định chốt cùng user: ẩn trace khỏi nhân viên, chỉ FE ẩn nút là không đủ)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import Token
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import HTTPException
from studio_app import middleware
from studio_app.core._db import Pool, close_pools, get_pool
from studio_app.routes.runs import get_run
from studio_workbench.tenant_wall import ResolvedContext


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


async def _seed_admin_user(admin_pool: Pool, tenant_id: UUID, email: str) -> None:
    async with admin_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, system_roles) VALUES (%s, %s, %s, %s)",
            (str(tenant_id), email, "not-a-real-hash", ["admin"]),
        )


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


async def test_get_run_requires_admin(admin_pool: Pool) -> None:
    """Nhân viên gọi `GET /api/runs/{run_id}` → 403 — chặn TRƯỚC khi chạm `PgTraceReader`, không
    phụ thuộc `run_id` có thật hay không (đúng tinh thần fail-closed: quyền kiểm trước, dữ liệu
    kiểm sau)."""
    tenant = await _seed_tenant(admin_pool, "runs-get-requires-admin")
    await _seed_employee_user(admin_pool, tenant, "employee@acme.com")
    token = _set_session(tenant, "employee@acme.com", ["public"])

    try:
        async with _simulate_request_connection():
            with pytest.raises(HTTPException) as exc_info:
                await get_run("00000000-0000-0000-0000-000000000000")
    finally:
        middleware._request_session.reset(token)

    assert exc_info.value.status_code == 403


async def test_get_run_admin_not_found_still_404(admin_pool: Pool) -> None:
    """Admin qua được gate quyền — `run_id` không tồn tại vẫn 404 như hành vi cũ (gate MỚI không
    che mất nhánh lỗi CŨ)."""
    tenant = await _seed_tenant(admin_pool, "runs-get-admin-404")
    await _seed_admin_user(admin_pool, tenant, "admin@acme.com")
    token = _set_session(tenant, "admin@acme.com", ["admin", "public"])

    try:
        async with _simulate_request_connection():
            with pytest.raises(HTTPException) as exc_info:
                await get_run("00000000-0000-0000-0000-000000000000")
    finally:
        middleware._request_session.reset(token)

    assert exc_info.value.status_code == 404


async def test_get_run_without_session_raises_401(admin_pool: Pool) -> None:
    del admin_pool
    with pytest.raises(HTTPException) as exc_info:
        await get_run("00000000-0000-0000-0000-000000000000")
    assert exc_info.value.status_code == 401
