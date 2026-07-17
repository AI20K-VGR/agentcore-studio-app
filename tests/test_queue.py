"""Phase 3 gate tests — SKIP LOCKED claim, reaper lease-requeue, enqueue idempotency (R-DI §3).
Needs a live DB (see test_schema.py module docstring for the fixture-skip behavior)."""

from __future__ import annotations

import asyncio

from studio_app.core._db import Pool
from studio_app.core.queue import claim, enqueue, reaper


async def _seed_tenant(admin_pool: Pool, name: str) -> str:
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", (name,))
        row = await cur.fetchone()
    assert row is not None
    return str(row[0])


async def test_enqueue_idempotent(admin_pool: Pool) -> None:
    """KHÓA: enqueue() dùng ON CONFLICT DO NOTHING — 2 lần cùng idempotency_key chỉ tạo 1 job."""
    tenant_id = await _seed_tenant(admin_pool, "tenant-dedup")

    first = await enqueue(admin_pool, tenant_id=tenant_id, idempotency_key="dup-key", job_type="demo", payload={})
    second = await enqueue(admin_pool, tenant_id=tenant_id, idempotency_key="dup-key", job_type="demo", payload={})
    assert first is not None
    assert second is None

    async with admin_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM core.jobs WHERE tenant_id = %s AND idempotency_key = %s",
            (tenant_id, "dup-key"),
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 1


async def test_two_workers_no_double_claim(admin_pool: Pool) -> None:
    """KHÓA: SKIP LOCKED — 2 worker claim() đồng thời (2 conn thật) không nhận trùng 1 job."""
    tenant_id = await _seed_tenant(admin_pool, "tenant-claim")

    for i in range(2):
        job_id = await enqueue(
            admin_pool, tenant_id=tenant_id, idempotency_key=f"claim-{i}", job_type="demo", payload={}
        )
        assert job_id is not None

    job_a, job_b = await asyncio.gather(claim(admin_pool), claim(admin_pool))
    assert job_a is not None
    assert job_b is not None
    assert job_a.id != job_b.id
    assert job_a.status == "processing"
    assert job_b.status == "processing"


async def test_reaper_requeues_expired_lease(admin_pool: Pool) -> None:
    """KHÓA: reaper() requeue job có lease hết hạn về `pending` khi còn attempts (max_attempts=5
    mặc định, attempts=1 sau claim đầu tiên → vẫn còn attempts)."""
    tenant_id = await _seed_tenant(admin_pool, "tenant-reap")
    job_id = await enqueue(admin_pool, tenant_id=tenant_id, idempotency_key="reap-1", job_type="demo", payload={})
    assert job_id is not None

    claimed = await claim(admin_pool, lease_seconds=0)
    assert claimed is not None
    assert claimed.id == job_id

    # lease_seconds=0 → leased_until = now() at claim time; sleeping past that makes it expired.
    await asyncio.sleep(1.1)
    requeued_count = await reaper(admin_pool)
    assert requeued_count >= 1

    async with admin_pool.connection() as conn:
        cur = await conn.execute("SELECT status, attempts FROM core.jobs WHERE id = %s", (job_id,))
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "pending"
    assert row[1] == 1
