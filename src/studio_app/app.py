"""Composition root — FastAPI factory (Decision #1: direct composition, NO DI-framework).

`create_app()`'s lifespan runs schema DDL + the centralized cross-schema GRANT through the
ADMIN pool ONLY — never `get_pool()` (see `core/_db.py` and `core/schema.py` module docstrings
for why the pool split matters, F1/F2/F6). The tenant-context middleware wires the per-request
connection-holding mechanism (F9).

Deviation note (P3, reported to the plan lead): plan.md's Requirements text says this factory
should "import class stub quadrant (từ P1) để wiring". P1, as actually committed, shipped only
package-structure stubs (`__init__.py` docstrings) for `studio_kb`/`studio_engine`/
`studio_workbench`/`studio_evalhub` — no class definitions exist yet to import. Phase 3's own
Files/Implement/Success sections do not list a class-stub import as part of this phase's
concrete deliverable, so this factory wires DDL+grants+middleware only; quadrant class wiring is
left to the phase that actually introduces those classes (P4 shared-runtime providers, or
P5-P8 for the quadrant classes themselves).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from studio_app.core._db import close_pools, get_admin_pool
from studio_app.core.schema import ensure_all_schemas, grant_app_privileges
from studio_app.middleware import tenant_context_middleware


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    del app  # unused — required by the ASGI lifespan callable signature
    admin = await get_admin_pool()
    await ensure_all_schemas(admin)
    await grant_app_privileges(admin)
    try:
        yield
    finally:
        await close_pools()


def create_app() -> FastAPI:
    """Build the FastAPI app: lifespan boots DDL+grants via the admin pool; middleware holds one
    tenant-scoped connection per request via contextvar (F9)."""
    app = FastAPI(title="AgentCore Studio", lifespan=_lifespan)
    app.middleware("http")(tenant_context_middleware)
    return app
