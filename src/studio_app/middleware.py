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

from fastapi import HTTPException
from psycopg import AsyncConnection, sql
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from studio_workbench.tenant_wall import ResolvedContext

from studio_app.core._db import get_pool
from studio_app.jwt_auth import InvalidTokenError, verify_token

_request_conn: ContextVar[AsyncConnection[Any] | None] = ContextVar("_request_conn", default=None)
_request_session: ContextVar[ResolvedContext | None] = ContextVar("_request_session", default=None)


def get_request_connection() -> AsyncConnection[Any]:
    """Return the current request's held connection. Call ONLY from within a request scope
    handled by `tenant_context_middleware` — raises otherwise, so a caller can never silently
    fall back to a fresh (untenanted) connection."""
    conn = _request_conn.get()
    if conn is None:
        raise RuntimeError("no request-scoped connection — tenant_context_middleware did not run")
    return conn


def get_request_session() -> ResolvedContext:
    """Return the current request's resolved identity (tenant_id/user/roles) — the value a valid
    `Authorization: Bearer <jwt>` header resolves to via `jwt_auth.verify_token` +
    `studio_workbench.tenant_wall.resolve_session` (see `_resolve_jwt_session` below). Call ONLY
    from within a request scope that actually carried a valid Bearer token — raises `HTTPException
    401` otherwise (fail-closed: a caller must never silently fall back to an unauthenticated/empty
    session), same contract as `get_request_connection()` above.

    401, not a bare `RuntimeError` (review PR#5, DE, M1): this condition means "the CLIENT didn't
    authenticate", not "the server is broken" — every route calling this needs the same status
    code, so it belongs here once, not re-implemented per route via a try/except.

    `tenant_context_middleware` sets `app.tenant_id` (for `get_request_connection()`'s RLS-scoped
    connection) from this SAME source — as of `kit#129` §3.2 remediation, JWT/login is the ONLY
    way either one gets populated (the older `x-tenant-id` header dev-stub path was removed
    entirely, not just deprioritized). Routes that need `user`/`roles`, not just `tenant_id` for
    `SET LOCAL`, must go through this function, never re-derive identity themselves."""
    session = _request_session.get()
    if session is None:
        raise HTTPException(
            status_code=401,
            detail="Thiếu hoặc sai `Authorization: Bearer <jwt>` — cần đăng nhập trước (`POST /api/auth/demo-login`).",
        )
    return session


def _resolve_jwt_session(request: Request) -> ResolvedContext | None:
    """`Authorization: Bearer <token>` seam (Kế hoạch 2, A1) — as of `kit#129` §3.2 remediation
    (VinSOC AV-203064/AV-203754, High cả 2 lượt quét), đây là NGUỒN DUY NHẤT xác định tenant của
    một request. Đường cũ `x-tenant-id` header (dev-stub, tin thẳng client, không verify) đã bị
    XOÁ HẲN — không phải deprioritize, không phải fallback còn giữ lại. Trước bản vá, đường đó vô
    hại chỉ nhờ mọi route đều nhớ gọi `get_request_session()` trước; mentor gọi đó là "súng đã
    lên đạn nằm trên bàn" — route mới quên gọi hàm đó sẽ làm lỗ hổng sống lại. Xoá hẳn thay vì tin
    vào kỷ luật nhớ gọi hàm đúng chỗ.

    Delegates to `jwt_auth.verify_token` (real signature check, HS256 against
    `settings.jwt_secret`) — no re-implementation of JWT verification here.

    No `Authorization` header → `None` (không đăng nhập — khác hẳn "có token nhưng SAI", xem
    dưới). `tenant_context_middleware` không set `app.tenant_id` trong trường hợp này (fail-closed,
    Decision #3) — route nào cần định danh phải tự gọi `get_request_session()` và nhận 401.
    A header that IS present but fails verification (bad signature, expired, malformed) raises
    `InvalidTokenError`, caught by `tenant_context_middleware` below and turned into a 401 —
    "present but broken" must be loud, never silently fall back to an empty identity."""
    raw = request.headers.get("authorization")
    if not raw or not raw.lower().startswith("bearer "):
        return None
    token = raw[len("bearer ") :].strip()
    return verify_token(token)


async def tenant_context_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    try:
        session = _resolve_jwt_session(request)
    except InvalidTokenError as exc:
        return JSONResponse(status_code=401, content={"detail": str(exc)})

    pool = await get_pool()
    async with pool.connection() as conn:
        # Không còn nhánh else gọi resolver header-based (kit#129 §3.2) — không có JWT hợp lệ =
        # không có tenant, hết. Fail-closed (Decision #3), không phải "còn 1 nguồn dự phòng".
        tenant_id: str | None = None
        session_token = None
        if session is not None:
            tenant_id = str(session.tenant_id)
            session_token = _request_session.set(session)
        if tenant_id is not None:
            # SET LOCAL does not accept bind parameters over the wire protocol (it is a utility
            # statement, not a DML query) — sql.Literal safely inline-quotes the value instead.
            await conn.execute(sql.SQL("SET LOCAL app.tenant_id = {}").format(sql.Literal(tenant_id)))
        token = _request_conn.set(conn)
        try:
            response = await call_next(request)
        finally:
            _request_conn.reset(token)
            if session_token is not None:
                _request_session.reset(session_token)
    return response
