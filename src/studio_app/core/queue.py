"""Postgres queue — `SELECT ... FOR UPDATE SKIP LOCKED` + lease pattern, copied from R-DI §3
(`queue/__init__.py:148-157,196-242,288-304,122-126`), adapted to `core.jobs`.

Short committed txns only: `claim()` commits immediately after marking a row `processing` so a
slow downstream call never holds a row lock open — the anti-pattern this exists to avoid (a
worker doing an ~8s LLM call while holding the lock would starve connections and risk a
double-charge if it dies mid-call, per R-DI's comment).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from studio_app.core._db import Pool


class ReaperRaceError(RuntimeError):
    """Raised when complete()/fail() finds its job no longer `processing` — the reaper already
    requeued it out from under this worker (lease expired mid-flight)."""


@dataclass(frozen=True)
class Job:
    id: str
    tenant_id: str
    job_type: str
    payload: dict[str, Any]
    attempts: int
    status: str
    max_attempts: int


async def enqueue(
    pool: Pool,
    *,
    tenant_id: str,
    idempotency_key: str,
    job_type: str,
    payload: dict[str, Any],
) -> str | None:
    """Insert a new job. `ON CONFLICT (tenant_id, idempotency_key) DO NOTHING` — a duplicate
    enqueue is a dedup no-op, never an error. Returns the new job id, or None if it already
    existed."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO core.jobs (tenant_id, idempotency_key, job_type, payload) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (tenant_id, idempotency_key) DO NOTHING "
            "RETURNING id",
            (tenant_id, idempotency_key, job_type, Jsonb(payload)),
        )
        row = await cur.fetchone()
    return str(row[0]) if row else None


async def claim(pool: Pool, *, lease_seconds: int = 60) -> Job | None:
    """Claim one pending job via SKIP LOCKED — a short committed txn that does no work itself,
    so 2 concurrent workers never receive the same job (R-DI §3)."""
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "UPDATE core.jobs SET status = 'processing', attempts = attempts + 1, "
            "leased_until = now() + (%(lease)s || ' seconds')::interval, updated_at = now() "
            "WHERE id = ("
            "  SELECT id FROM core.jobs WHERE status = 'pending' "
            "  ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"
            ") "
            "RETURNING id, tenant_id, job_type, payload, attempts, status, max_attempts",
            {"lease": lease_seconds},
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return Job(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        job_type=row["job_type"],
        payload=row["payload"],
        attempts=row["attempts"],
        status=row["status"],
        max_attempts=row["max_attempts"],
    )


async def complete(pool: Pool, job_id: str) -> None:
    """Mark a claimed job `done`. Guarded by `WHERE status='processing'` — if the reaper already
    requeued this job (lease expired), 0 rows match and we raise instead of silently overwriting
    whatever state it is in now (R-DI §3 `queue/__init__.py:196-242`)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE core.jobs SET status = 'done', updated_at = now() "
            "WHERE id = %s AND status = 'processing' RETURNING id",
            (job_id,),
        )
        row = await cur.fetchone()
    if row is None:
        raise ReaperRaceError(f"job {job_id} is no longer 'processing' (reaper likely requeued it)")


async def fail(pool: Pool, job_id: str, *, requeue: bool = True) -> None:
    """Mark a claimed job `pending` (requeue) or `dead_letter`. Same `WHERE status='processing'`
    guard as `complete()`."""
    async with pool.connection() as conn:
        next_status = "pending" if requeue else "dead_letter"
        cur = await conn.execute(
            "UPDATE core.jobs SET status = %s, updated_at = now() "
            "WHERE id = %s AND status = 'processing' RETURNING id",
            (next_status, job_id),
        )
        row = await cur.fetchone()
    if row is None:
        raise ReaperRaceError(f"job {job_id} is no longer 'processing' (reaper likely requeued it)")


async def reaper(pool: Pool) -> int:
    """Requeue jobs whose lease expired: back to `pending` if attempts remain, else
    `dead_letter`. Returns the number of jobs touched (R-DI §3 `queue/__init__.py:288-304`)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE core.jobs SET "
            "  status = CASE WHEN attempts < max_attempts THEN 'pending' ELSE 'dead_letter' END, "
            "  leased_until = NULL, updated_at = now() "
            "WHERE status = 'processing' AND leased_until < now() "
            "RETURNING id"
        )
        rows = await cur.fetchall()
    return len(rows)
