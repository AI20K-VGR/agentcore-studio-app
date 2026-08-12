"""`TraceWriter` pg-impl (F15) — implements `studio_contracts.protocols.TraceWriter`.

`write()` is ONE plain INSERT into `obs.trace_events`. NO cost-aggregation, NO dedup, NO
upsert/ON-CONFLICT logic belongs here — that is DE's downstream cost-aggregation concern, never
this seam. `test_trace_writer.py::test_pg_trace_writer_single_insert_roundtrip` pins this: 2
writes of 2 distinct events must yield 2 separate rows, never a merge.
"""

from __future__ import annotations

from psycopg import sql
from psycopg.types.json import Jsonb
from studio_contracts.trace import TraceEvent

from studio_app.core._db import Pool


class PgTraceWriter:
    """`TraceWriter` Protocol impl backed by Postgres. `pool` MUST be the `studio_app` runtime
    pool (`get_pool()`) — trace writes are ordinary request-path DML, never admin-pool DDL."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def write(self, event: TraceEvent) -> None:
        """ONE plain INSERT (F15) — no read-before-write, no ON CONFLICT, no aggregation.

        `write()` is called DIRECTLY, outside request scope, at several call-sites (eval-runner
        composition roots, `test_trace_writer.py`) — it never goes through
        `middleware.py::get_request_connection()`. So this method must NOT rely on any
        request-scoped `app.tenant_id` — instead it sets `app.tenant_id` itself, from
        `event.tenant_id` (required, NOT NULL field), on the SAME connection/transaction it opens
        for the INSERT right below. Same `SET LOCAL` idiom as `middleware.py:77` (`SET LOCAL` does
        not accept a bind param over the wire protocol — `sql.Literal` inline-quotes it safely).
        `SET LOCAL` is transaction-scoped: `pool.connection()` runs this whole block as one
        transaction (psycopg `autocommit=False` by default) and commits it at block-exit, so the
        SET applies to the INSERT that follows in the same block, and never leaks to whichever
        request/call borrows this connection next once it is returned to the pool.

        This keeps `obs.trace_events` write-ready for the day RLS (`ENABLE`+`FORCE ROW LEVEL
        SECURITY`, mini-RFC `packages/kb/docs/mini-rfc-tenant-schema-unify.md`) actually lands on
        this table — today RLS is NOT enabled here (`obs/schema.py` has no RLS DDL), so this SET
        is currently a no-op fence, not yet load-bearing against a real policy."""
        async with self._pool.connection() as conn:
            await conn.execute(sql.SQL("SET LOCAL app.tenant_id = {}").format(sql.Literal(str(event.tenant_id))))
            await conn.execute(
                "INSERT INTO obs.trace_events "
                "(event_id, run_id, agent_id, tenant_id, node_id, node_type, ts, inputs_hash, "
                " outputs, tokens, cost, citations) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    event.event_id,
                    event.run_id,
                    event.agent_id,
                    event.tenant_id,
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
