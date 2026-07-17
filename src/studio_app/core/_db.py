"""Postgres pool split (F1/F2, Decision #3): two lazily-built singleton pools.

`get_admin_pool()` — DSN `STUDIO_DATABASE_URL_ADMIN`, role `studio_owner`. Used ONLY for boot
DDL (`ensure_all_schemas`) and grants (`grant_app_privileges`) — see core/schema.py.

`get_pool()` — DSN `STUDIO_DATABASE_URL`, role `studio_app`. The runtime pool the request path
(middleware.py, quadrant `*.search`/DML) actually uses — this is the ONLY pool RLS applies
against, because `studio_app` is a non-owner of every table (docker/postgres-init/00-roles.sql).

Never call `ensure_all_schemas`/`grant_app_privileges` against `get_pool()`, and never run
request-path queries against `get_admin_pool()` — that is exactly the "1 pool for everything"
regression this split exists to prevent (an owner-connection silently bypasses RLS regardless of
policy, because table ownership bypasses RLS unless `FORCE ROW LEVEL SECURITY` is set AND the
connecting role is not the owner).

Pattern: R-DI §5 (`core/common/_db.py:1` — "one lazily-built pool per process").
"""

from __future__ import annotations

import asyncio
from typing import Any

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from studio_app.settings import get_settings

# Row type parametrized `Any` (not `object`): callers index rows positionally (`row[0]`) or by
# dict key (`row["col"]`, via `row_factory=dict_row`) — `Any` is what lets both through mypy
# strict without a cast at every call site.
Pool = AsyncConnectionPool[AsyncConnection[Any]]

_admin_pool: Pool | None = None
_app_pool: Pool | None = None
_lock = asyncio.Lock()


async def get_admin_pool() -> Pool:
    """Lazy singleton pool authenticated as `studio_owner` (DDL + grants at boot only)."""
    global _admin_pool
    if _admin_pool is None:
        async with _lock:
            if _admin_pool is None:
                settings = get_settings()
                pool: Pool = AsyncConnectionPool(settings.database_url_admin, min_size=1, max_size=4, open=False)
                await pool.open(wait=True, timeout=10)
                _admin_pool = pool
    return _admin_pool


async def get_pool() -> Pool:
    """Lazy singleton pool authenticated as `studio_app` (runtime DML — RLS applies)."""
    global _app_pool
    if _app_pool is None:
        async with _lock:
            if _app_pool is None:
                settings = get_settings()
                pool: Pool = AsyncConnectionPool(settings.database_url, min_size=1, max_size=8, open=False)
                await pool.open(wait=True, timeout=10)
                _app_pool = pool
    return _app_pool


async def close_pools() -> None:
    """Close both pools and clear the singletons — app shutdown / test teardown."""
    global _admin_pool, _app_pool
    if _admin_pool is not None:
        await _admin_pool.close()
        _admin_pool = None
    if _app_pool is not None:
        await _app_pool.close()
        _app_pool = None
