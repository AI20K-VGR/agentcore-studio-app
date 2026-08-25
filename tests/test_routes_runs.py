"""`routes/runs.py::create_run` — app#44, mục D (`PROJECT-SCOPE-DEMO-DAY30.md`): "Test" đổi hẳn ý
nghĩa, từ "chạy 1 lượt hội thoại qua interpreter" (DAG cũ, dựng recipe từ `nodes`/`edges` client
gửi) thành **connectivity-check tĩnh**: với mỗi tool trong `tool_whitelist`, xác nhận có
executor/dispatcher THẬT hay không (`kb_search: OK`, `calculator: OK`, tool lạ:
`NOT_IMPLEMENTED`) — KHÔNG gọi LLM, KHÔNG tạo trace hội thoại, KHÔNG chạm `recipe.dag`/`nodes`/
`edges` nào cả.

`RunRequest` mới chỉ còn `agent_id` + `tool_whitelist` — mọi field DAG-shaped cũ (`nodes`/`edges`/
`kb_id`/`scope`/`golden_set_ref`/threshold) đã dời sang `routes/publish.py::PublishRequest` (route
`/evaluate`/`/publish` vẫn cần chúng để `create_dynamic_recipe(...)` dựng `recipe.dag`, xem
docstring `PublishRequest`). **T1 IDOR không còn áp dụng được cho endpoint này** — không còn recipe/
tenant-scoped operation nào trong `POST /api/runs` nữa (không DB, không KB, không LLM), nên không
có kênh rò rỉ tenant nào để bài IDOR cũ đo — xoá hẳn, không phải quên.

2 lớp test: (A) `_check_tool_connectivity` — hàm THUẦN (không DB/session), test trực tiếp không cần
`admin_pool`. (B) route `create_run` đầy đủ (admin-gate + response wiring) — vẫn cần
`admin_pool`/session giả lập, cùng khuôn `test_routes_chat_as_roles.py` (skip nếu thiếu DSN)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import Token
from uuid import UUID

import pytest
import pytest_asyncio
from studio_app import middleware
from studio_app.core._db import Pool, close_pools, get_pool
from studio_app.routes.runs import RunRequest, _check_tool_connectivity, create_run
from studio_workbench.tenant_wall import ResolvedContext

# ---------------------------------------------------------------------------
# Lớp A — `_check_tool_connectivity`, hàm thuần
# ---------------------------------------------------------------------------


def test_kb_search_always_ok() -> None:
    """`kb_search` luôn có executor thật (`KbRetrieveExecutor`, không đi qua `ToolDispatch`) — OK
    bất kể whitelist chứa gì khác, đúng bất biến A4 của `run_agent_loop`."""
    results = _check_tool_connectivity(["kb_search"])
    assert results == [{"tool": "kb_search", "status": "OK"}]


def test_supported_real_tools_ok() -> None:
    results = _check_tool_connectivity(["calculator", "current_datetime"])
    assert results == [
        {"tool": "calculator", "status": "OK"},
        {"tool": "current_datetime", "status": "OK"},
    ]


def test_unsupported_tool_not_implemented() -> None:
    results = _check_tool_connectivity(["rogue_tool"])
    assert results == [{"tool": "rogue_tool", "status": "NOT_IMPLEMENTED"}]


def test_mixed_whitelist_preserves_order_and_per_tool_status() -> None:
    results = _check_tool_connectivity(["kb_search", "calculator", "rogue_tool", "current_datetime"])
    assert results == [
        {"tool": "kb_search", "status": "OK"},
        {"tool": "calculator", "status": "OK"},
        {"tool": "rogue_tool", "status": "NOT_IMPLEMENTED"},
        {"tool": "current_datetime", "status": "OK"},
    ]


def test_empty_whitelist_yields_empty_results() -> None:
    assert _check_tool_connectivity([]) == []


def test_run_request_no_longer_has_dag_fields() -> None:
    """Kiểm cấu trúc: `RunRequest` không còn field DAG-shaped nào (`nodes`/`edges`/`kb_id`/`scope`/
    `golden_set_ref`/threshold) — những field đó giờ sống ở `routes/publish.py::PublishRequest`."""
    body = RunRequest(agent_id="a1", tool_whitelist=["kb_search"])
    for dead_field in ("nodes", "edges", "kb_id", "scope", "golden_set_ref", "success_threshold"):
        assert not hasattr(body, dead_field), f"RunRequest không nên còn field {dead_field!r}"


# ---------------------------------------------------------------------------
# Lớp B — route `create_run` đầy đủ (admin-gate + response), cần DB
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


def _set_session(tenant_id: UUID, user: str = "victim@ankor.vn") -> Token[ResolvedContext | None]:
    session = ResolvedContext(tenant_id=tenant_id, user=user, system_roles=["admin", "public"])
    return middleware._request_session.set(session)


async def test_create_run_checks_connectivity_for_whitelist(admin_pool: Pool) -> None:
    tenant = await _seed_tenant(admin_pool, "runs-connectivity-ok")
    await _seed_admin_user(admin_pool, tenant, "victim@ankor.vn")

    token = _set_session(tenant)
    try:
        async with _simulate_request_connection():
            response = await create_run(
                RunRequest(agent_id="agent-runs-probe", tool_whitelist=["kb_search", "calculator", "rogue_tool"])
            )
    finally:
        middleware._request_session.reset(token)

    assert response.agent_id == "agent-runs-probe"
    assert response.results == [
        {"tool": "kb_search", "status": "OK"},
        {"tool": "calculator", "status": "OK"},
        {"tool": "rogue_tool", "status": "NOT_IMPLEMENTED"},
    ]


async def test_create_run_requires_admin(admin_pool: Pool) -> None:
    """Gate role-gap giữ nguyên (đúng bản gốc trước app#44) — không phải mọi tài khoản đăng nhập
    đều gọi được, dù endpoint giờ không chạm KB/LLM/DB nào ngoài chính identity-check."""
    from fastapi import HTTPException

    tenant = await _seed_tenant(admin_pool, "runs-requires-admin")
    await _seed_employee_user(admin_pool, tenant, "employee@acme.com")
    session = ResolvedContext(tenant_id=tenant, user="employee@acme.com", system_roles=["public"])
    token = middleware._request_session.set(session)

    try:
        async with _simulate_request_connection():
            with pytest.raises(HTTPException) as exc_info:
                await create_run(RunRequest(agent_id="a", tool_whitelist=["kb_search"]))
    finally:
        middleware._request_session.reset(token)

    assert exc_info.value.status_code == 403


async def test_create_run_without_session_raises_401(admin_pool: Pool) -> None:
    del admin_pool
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await create_run(RunRequest(agent_id="a", tool_whitelist=["kb_search"]))
    assert exc_info.value.status_code == 401
