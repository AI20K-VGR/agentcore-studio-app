"""Tenant-context middleware (F9): one `get_pool()` connection + transaction per HTTP request,
held via a `ContextVar` so request-scoped code (e.g. `kb.search`) can reach the SAME connection
without pool reuse ever leaking it to another request.

`SET LOCAL app.tenant_id` is transaction-scoped by design — it resets automatically when the
transaction commits/rolls back at the end of the request, so a connection returned to the pool
never carries a stale tenant setting into whichever request borrows it next.

Fail-closed default (Decision #3): if server-side tenant resolution finds no tenant, this
middleware does NOT set `app.tenant_id` at all. RLS policies (added at P5) key off
`current_setting('app.tenant_id', true)` returning NULL, which the closed fence must treat as
0 rows — never "everything" (P3 does not add the RLS policy itself, only the connection-holding
mechanism the policy will later rely on).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

from psycopg import AsyncConnection, sql
from starlette.requests import Request
from starlette.responses import Response

from studio_app.core._db import get_pool

_request_conn: ContextVar[AsyncConnection[Any] | None] = ContextVar("_request_conn", default=None)


def get_request_connection() -> AsyncConnection[Any]:
    """Return the current request's held connection. Call ONLY from within a request scope
    handled by `tenant_context_middleware` — raises otherwise, so a caller can never silently
    fall back to a fresh (untenanted) connection."""
    conn = _request_conn.get()
    if conn is None:
        raise RuntimeError("no request-scoped connection — tenant_context_middleware did not run")
    return conn


def _resolve_tenant_id(request: Request) -> str | None:
    """Server-side tenant resolution seam. P3 stub: header-based, for wiring/tests only — the
    real auth-derived resolution (session/JWT claim) is out of this phase's scope. Returns None
    when no tenant is resolvable; the caller MUST NOT set `app.tenant_id` in that case
    (fail-closed, Decision #3)."""
    tenant_id = request.headers.get("x-tenant-id")
    return tenant_id or None


async def tenant_context_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    pool = await get_pool()
    async with pool.connection() as conn:
        tenant_id = _resolve_tenant_id(request)
        if tenant_id is not None:
            # SET LOCAL does not accept bind parameters over the wire protocol (it is a utility
            # statement, not a DML query) — sql.Literal safely inline-quotes the value instead.
            await conn.execute(sql.SQL("SET LOCAL app.tenant_id = {}").format(sql.Literal(tenant_id)))
        token = _request_conn.set(conn)
        try:
            response = await call_next(request)
        finally:
            _request_conn.reset(token)
    return response
