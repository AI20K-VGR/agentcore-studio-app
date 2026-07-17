"""Phase 4 gate test — TraceWriter pg-impl round-trip (F15). Needs a live DB: see
apps/studio/tests/test_schema.py module docstring for the fixture-skip behavior.
"""

from __future__ import annotations

from studio_app.core._db import Pool
from studio_app.obs.trace_writer import PgTraceWriter
from studio_contracts.nodes import NodeType
from studio_contracts.trace import Tokens, TraceEvent


def _sample_event(event_id: str) -> TraceEvent:
    return TraceEvent(
        event_id=event_id,
        run_id="run-1",
        agent_id="agent-1",
        tenant="tenant-1",
        node_id="node-1",
        node_type=NodeType.LLM_STEP,
        ts="2026-07-17T00:00:00Z",
        inputs_hash="deadbeef",
        outputs={"answer": "42"},
        tokens=Tokens(prompt=10, completion=5),
        cost=0.0021,
        citations=["doc-1"],
    )


async def test_pg_trace_writer_single_insert_roundtrip(admin_pool: Pool, pool: Pool) -> None:
    """KHÓA F15: write() then read obs.trace_events back matches the event field-for-field, AND
    a 2nd write() of a DIFFERENT event produces a 2nd separate row — never merged/aggregated
    (write() is 1 plain INSERT, no cost-aggregation/dedup — that logic belongs to DE, not here)."""
    del admin_pool  # ordering only — ensures schema/grants exist before this app-role DML
    writer = PgTraceWriter(pool)
    event = _sample_event("evt-1")

    await writer.write(event)

    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT event_id, run_id, agent_id, tenant, node_id, node_type, ts, inputs_hash, "
            "outputs, tokens, cost, citations FROM obs.trace_events WHERE event_id = %s",
            (event.event_id,),
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == event.event_id
    assert row[1] == event.run_id
    assert row[2] == event.agent_id
    assert row[3] == event.tenant
    assert row[4] == event.node_id
    assert row[5] == event.node_type.value
    assert row[6] == event.ts
    assert row[7] == event.inputs_hash
    assert row[8] == event.outputs
    assert row[9] == event.tokens.model_dump()
    assert float(row[10]) == event.cost
    assert row[11] == event.citations

    # No aggregation/dedup (F15): writing a SECOND distinct event yields a SECOND row.
    await writer.write(_sample_event("evt-2"))
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM obs.trace_events")
        count_row = await cur.fetchone()
    assert count_row is not None
    assert count_row[0] == 2
