"""Transactional outbox — `emit(event)` writes into `core.outbox` in the SAME transaction as the
caller's own state-change write, so a crash between the two can never lose the event (pattern
copied from R-DI §3, `core/event/outbox.py:1-2`: "the event is written in the SAME txn as the
state change, so a crash never loses it").
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb


async def emit(
    conn: AsyncConnection[Any],
    *,
    tenant_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> str:
    """Insert one outbox row on the CALLER's connection — never opens its own connection, so it
    always shares whatever transaction the caller is already in (same-txn guarantee)."""
    cur = await conn.execute(
        "INSERT INTO core.outbox (tenant_id, event_type, payload) VALUES (%s, %s, %s) RETURNING id",
        (tenant_id, event_type, Jsonb(payload)),
    )
    row = await cur.fetchone()
    if row is None:  # pragma: no cover — RETURNING id always yields exactly 1 row on INSERT
        raise RuntimeError("outbox insert returned no row")
    return str(row[0])
