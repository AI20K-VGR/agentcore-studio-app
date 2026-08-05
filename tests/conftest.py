"""Local test-infra override (AIE-1, `apps/studio` — WRITE), scoped to THIS directory only.

Windows defaults `asyncio` to `ProactorEventLoop`, which `psycopg` async refuses ("Psycopg cannot
use the 'ProactorEventLoop' to run in async mode"). The kit-root `conftest.py` (repo cha) is
READ-only for AIE-1 (D13 plan constraint) and must not carry this fix. pytest merges `conftest.py`
files hierarchically, so this file adds the missing `event_loop_policy` override without touching
the root one — Linux/CI is unaffected (`SelectorEventLoop` is already the default there; this only
changes behavior on `win32`).
"""

from __future__ import annotations

import asyncio
import sys

import pytest


@pytest.fixture(scope="session")
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()
