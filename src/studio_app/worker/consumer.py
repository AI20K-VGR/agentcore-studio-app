"""Queue consumer skeleton: `claim()` -> handler (`NotImplementedError` by default — each
quadrant supplies its own via `run_once(handler=...)`, this file never grows business logic) ->
`complete()`/`fail()`. Wires the reaper into the same module so a caller has one place to run
both halves of the loop.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from loguru import logger

from studio_app.core._db import Pool
from studio_app.core.queue import Job, ReaperRaceError, claim, complete, fail, reaper

Handler = Callable[[Job], Awaitable[None]]


async def _default_handler(job: Job) -> None:
    """Skeleton handler — P5-P8 each supply their own via `run_once(handler=...)`. Deliberately
    unimplemented: this file stays plumbing, never a quadrant's business logic (antichain)."""
    raise NotImplementedError(f"no handler wired for job_type={job.job_type!r}")


async def run_once(pool: Pool, *, handler: Handler = _default_handler, lease_seconds: int = 60) -> bool:
    """Claim at most one job and dispatch it to `handler`. Returns False when the queue was
    empty (nothing to claim); True after a claim + dispatch attempt, whether it completed or
    failed. A handler exception marks the job failed (requeue) rather than crashing the loop."""
    job = await claim(pool, lease_seconds=lease_seconds)
    if job is None:
        return False
    try:
        await handler(job)
    except ReaperRaceError:
        raise  # the reaper already reclaimed this job — nothing left for us to mark
    except Exception as exc:  # noqa: BLE001 — any handler failure fails the job, never crashes the loop
        logger.warning(f"[worker] job {job.id} handler failed: {exc}")
        await fail(pool, job.id)
        return True
    await complete(pool, job.id)
    return True


async def run_reaper_once(pool: Pool) -> int:
    """Wire the reaper into the same consumer loop — requeues jobs whose lease expired even
    while `claim()` otherwise finds nothing pending."""
    return await reaper(pool)
