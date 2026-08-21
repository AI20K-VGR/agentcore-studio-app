"""`obs.*` DDL shell (Phase 4) — `obs.trace_events`, `obs.costs`, `obs.golden_sets`.

The `obs` SCHEMA itself (`CREATE SCHEMA IF NOT EXISTS obs`) already ships at
`core/schema.py::ddl()` (P3) — that lets `grant_app_privileges()` GRANT on all 5 kit schemas
starting at P3 instead of erroring on a schema that does not exist yet. This module only adds
the TABLES, and joins `core.schema._QUADRANT_SCHEMA_MODULES` here at P4 (direct-import seam,
same antichain pattern as kb/engine/workbench/evalhub).

`obs.trace_events` columns match `TraceEvent` (studio_contracts.trace, R-SPEC A1#2): event_id,
run_id, agent_id, tenant_id (UUID NOT NULL, INV-1 / D-13), node_id, node_type, ts, inputs_hash, outputs jsonb,
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
    tenant_id UUID NOT NULL,
    node_id TEXT NOT NULL,
    node_type TEXT NOT NULL,
    ts TEXT NOT NULL,
    inputs_hash TEXT NOT NULL,
    outputs JSONB NOT NULL DEFAULT '{}'::jsonb,
    tokens JSONB NOT NULL DEFAULT '{}'::jsonb,
    cost NUMERIC NOT NULL DEFAULT 0,
    citations JSONB
);

CREATE INDEX IF NOT EXISTS obs_trace_events_tenant_id_idx ON obs.trace_events (tenant_id);

-- GAP-1 — RLS production trên `obs.trace_events` (mini-RFC
-- `packages/kb/docs/mini-rfc-tenant-schema-unify.md`, phần B: đủ chữ ký 4/4 DE·SWE·AIE-1·AIE-2,
-- 2026-08-12). Điều kiện ký của AIE-1 ("KHÔNG ký B tới khi đường GHI tự bind app.tenant_id") đã tự
-- đóng từ trước (`obs/trace_writer.py::write()` đã `SET LOCAL app.tenant_id` trên chính transaction
-- nó mở, PR `apps/studio#4`) — phần còn thiếu chỉ là chính 3 dòng DDL này, cùng khuôn
-- `kb.chunks`/`wb.recipes`/`eval.golden_sets` đã dùng.
--
-- ⚠️ Bật dòng này ĐÒI HỎI mọi reader của `obs.trace_events` phải tự `SET LOCAL app.tenant_id` trên
-- connection nó mở, không chỉ lọc `WHERE tenant_id` — nếu không, `USING`/`WITH CHECK` thấy session
-- chưa set gì → 0 dòng câm lặng, không phải lỗi to tiếng. Đã vá 2/3 reader đã biết
-- (`packages/kb/src/studio_kb/trace_reader.py::PgTraceReader.read_run`,
-- `packages/kb/src/studio_kb/cost.py::PgCostReader.list_run_ids` — 2 hàm còn lại của `PgCostReader`
-- mượn `read_run` nên được vá miễn phí). **CHƯA vá được `packages/evalhub/src/studio_evalhub/
-- run_report.py::read_run_unscoped`/`list_runs_all_tenants`** — hai hàm đó CỐ Ý đọc xuyên mọi tenant
-- (công cụ bộ chấm AIE-2, đã qua thẩm định bảo mật VinSOC `kit#129` + `test_unscoped_reader_naming.py`
-- khoá tên hàm), không tương thích với policy dưới đây bằng `SET LOCAL` đơn thuần — sau khi dòng này
-- chạy, cả hai hàm đó trả rỗng cho tới khi AIE-2 tự vá (cần role Postgres riêng có `BYPASSRLS`, hoặc
-- một cơ chế khác — quyết định của AIE-2, ngoài phạm vi PR này).
ALTER TABLE obs.trace_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE obs.trace_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS obs_trace_events_tenant_isolation ON obs.trace_events;
CREATE POLICY obs_trace_events_tenant_isolation ON obs.trace_events
    USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
    WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);

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
