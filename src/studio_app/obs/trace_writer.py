"""`TraceWriter` pg-impl (F15) — implements `studio_contracts.protocols.TraceWriter`.

`write()` is ONE plain INSERT into `obs.trace_events`. NO cost-aggregation, NO dedup, NO
upsert/ON-CONFLICT logic belongs here — that is DE's downstream cost-aggregation concern, never
this seam. `test_trace_writer.py::test_pg_trace_writer_single_insert_roundtrip` pins this: 2
writes of 2 distinct events must yield 2 separate rows, never a merge.
"""

from __future__ import annotations

from psycopg.types.json import Jsonb
from studio_contracts.trace import TraceEvent

from studio_app.core._db import Pool


class PgTraceWriter:
    """`TraceWriter` Protocol impl backed by Postgres. `pool` MUST be the `studio_app` runtime
    pool (`get_pool()`) — trace writes are ordinary request-path DML, never admin-pool DDL."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def write(self, event: TraceEvent) -> None:
        """ONE plain INSERT (F15) — no read-before-write, no ON CONFLICT, no aggregation."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO obs.trace_events "
                "(event_id, run_id, agent_id, tenant, node_id, node_type, ts, inputs_hash, "
                " outputs, tokens, cost, citations) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    event.event_id,
                    event.run_id,
                    event.agent_id,
                    event.tenant,
                    event.node_id,
                    event.node_type.value,
                    event.ts,
                    event.inputs_hash,
                    Jsonb(event.outputs),
                    Jsonb(event.tokens.model_dump()),
                    event.cost,
                    Jsonb(event.citations) if event.citations is not None else None,
                ),
            )
