"""`core.*` DDL (idempotent) + `ensure_all_schemas()` aggregator + `grant_app_privileges()` (F6).

Composition-root boot order: `core.ddl()` runs first (jobs/outbox live here — no cross-schema
FK, Decision #4), then each quadrant's own `ddl()` (direct-import, no entry-point discovery —
"KISS: chọn direct-import thay entry-point-discovery" per plan.md). `obs.ddl()` (P4, this app's
own `studio_app.obs.schema`) joins the tuple below; `kb`/`wb`/`eval` are still stub-empty
(`ddl() -> ""`) until P5/P7/P8 fill their body — running an empty string is a no-op, so this
stays idempotent and safe to call twice from day one.

Both functions MUST run against `get_admin_pool()` (studio_owner) — never `get_pool()` (F1/F2):
studio_owner OWNS every table it creates, which is what lets `FORCE ROW LEVEL SECURITY` (added
by the owning quadrant, P5) bite the owner too instead of silently bypassing the fence.
"""

from __future__ import annotations

from types import ModuleType

import studio_evalhub.schema as _evalhub_schema
import studio_kb.schema as _kb_schema
import studio_workbench.schema as _workbench_schema
from psycopg import sql

from studio_app.core._db import Pool
from studio_app.obs import schema as _obs_schema

_CORE_DDL = """
CREATE SCHEMA IF NOT EXISTS core;

-- `obs` schema SHELL only (plan.md Decision #4: "obs.*(DE, shell ship ở composition)") — its
-- tables (obs.trace_events/costs/golden_sets) land at Phase 4's `obs/schema.py:ddl()`. The shell
-- ships here so `grant_app_privileges` below can GRANT on all 5 schemas starting at P3, instead
-- of erroring on a schema that does not exist yet. Re-running `CREATE SCHEMA IF NOT EXISTS obs`
-- at P4 is a no-op — safe, idempotent, no conflict.
CREATE SCHEMA IF NOT EXISTS obs;

CREATE TABLE IF NOT EXISTS core.tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS core.jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    idempotency_key TEXT NOT NULL,
    job_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 5,
    leased_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS core.outbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    dispatched_at TIMESTAMPTZ
);
"""

# Direct-import seam (antichain, plan.md "Dependency matrix + file-ownership"): P5/P7/P8 fill in
# their own module's `ddl()` body without ever touching this file. `obs` (P4, composition-owned)
# joins this tuple here, at the phase that adds `studio_app.obs.schema`.
_QUADRANT_SCHEMA_MODULES: tuple[ModuleType, ...] = (
    _evalhub_schema,
    _kb_schema,
    _obs_schema,
    _workbench_schema,
)

# All 5 schemas this kit's composition root is responsible for granting `studio_app` access to
# (F6) — fixed closed set, not user input, but quoted via sql.Identifier anyway (correctness, not
# defense against untrusted input).
ALL_SCHEMAS: tuple[str, ...] = ("kb", "wb", "obs", "eval", "core")


def ddl() -> str:
    """This app's own (`core.*`) idempotent DDL."""
    return _CORE_DDL


async def ensure_all_schemas(admin_pool: Pool) -> None:
    """Run `core.ddl()` then every quadrant's `ddl()` (sorted by module name), via the ADMIN
    (studio_owner) pool. Idempotent: safe to call twice (CREATE ... IF NOT EXISTS throughout)."""
    async with admin_pool.connection() as conn:
        await conn.execute(ddl())
        for module in sorted(_QUADRANT_SCHEMA_MODULES, key=lambda m: m.__name__):
            quadrant_ddl = module.ddl()
            if quadrant_ddl:
                await conn.execute(quadrant_ddl)


async def grant_app_privileges(admin_pool: Pool) -> None:
    """Centralized cross-schema GRANT (F6) — call ONCE, right after `ensure_all_schemas`.

    Deliberately NOT scattered per-owner: R-DI §2/§7 documents the pain that led document-intake
    to abandon schema-per-module in favor of one consolidated schema. This kit keeps
    schema-per-quadrant (Decision #4) but avoids that same pain by centralizing the GRANT step
    here instead of asking each of the 4 quadrant owners to manage their own grants.

    Only grants on schemas that already EXIST: `kb`/`wb`/`eval` are still stub-empty `ddl()`
    (P5/P7/P8 fill them in later) so their schema does not exist yet at P3 — granting on a
    missing schema would error. This function is idempotent and re-run at every boot (and every
    `admin_pool` test fixture), so it naturally starts covering each quadrant's schema the moment
    that quadrant's `ddl()` creates it — no further change needed here when that happens.
    """
    async with admin_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT nspname FROM pg_catalog.pg_namespace WHERE nspname = ANY(%s::text[])",
            (list(ALL_SCHEMAS),),
        )
        existing_schemas = {row[0] for row in await cur.fetchall()}
        for schema in ALL_SCHEMAS:
            if schema not in existing_schemas:
                continue
            schema_id = sql.Identifier(schema)
            await conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO studio_app").format(schema_id))
            await conn.execute(
                sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO studio_app").format(
                    schema_id
                )
            )
            # Tables created AFTER this GRANT runs (later phases add tables to a schema that
            # already exists) still need privileges — ALTER DEFAULT PRIVILEGES covers that.
            await conn.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE studio_owner IN SCHEMA {} "
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO studio_app"
                ).format(schema_id)
            )
