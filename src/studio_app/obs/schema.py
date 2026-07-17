"""`obs.*` DDL shell (Phase 4) — `obs.trace_events`, `obs.costs`, `obs.golden_sets`.

The `obs` SCHEMA itself (`CREATE SCHEMA IF NOT EXISTS obs`) already ships at
`core/schema.py::ddl()` (P3) — that lets `grant_app_privileges()` GRANT on all 5 kit schemas
starting at P3 instead of erroring on a schema that does not exist yet. This module only adds
the TABLES, and joins `core.schema._QUADRANT_SCHEMA_MODULES` here at P4 (direct-import seam,
same antichain pattern as kb/engine/workbench/evalhub).

`obs.trace_events` columns match `TraceEvent` (studio_contracts.trace, R-SPEC A1#2): event_id,
run_id, agent_id, tenant (NOT NULL, INV-1), node_id, node_type, ts, inputs_hash, outputs jsonb,
tokens jsonb, cost numeric, citations jsonb. `obs.costs`/`obs.golden_sets` are shells only — DE
fills their real columns + cost-aggregation logic later; NONE of that logic belongs in
`obs/trace_writer.py::write()` (F15 — that stays a single plain INSERT).
"""

from __future__ import annotations

_OBS_DDL = """
CREATE TABLE IF NOT EXISTS obs.trace_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    tenant TEXT NOT NULL,
    node_id TEXT NOT NULL,
    node_type TEXT NOT NULL,
    ts TEXT NOT NULL,
    inputs_hash TEXT NOT NULL,
    outputs JSONB NOT NULL DEFAULT '{}'::jsonb,
    tokens JSONB NOT NULL DEFAULT '{}'::jsonb,
    cost NUMERIC NOT NULL DEFAULT 0,
    citations JSONB
);

-- Shell only (DE fills real columns + aggregation semantics later, out of P4 scope).
CREATE TABLE IF NOT EXISTS obs.costs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Shell only (DE fills real columns later, out of P4 scope).
CREATE TABLE IF NOT EXISTS obs.golden_sets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def ddl() -> str:
    """This quadrant's idempotent DDL — joins `core.schema._QUADRANT_SCHEMA_MODULES` at P4."""
    return _OBS_DDL
