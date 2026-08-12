"""Gate test — `PgTraceWriter.write()` phải tự đặt `app.tenant_id` ĐÚNG bằng `event.tenant_id`
trên CHÍNH connection/transaction nó tự mở (F15 giữ nguyên: writer tự đứng, không đi qua
`get_request_connection()` — `write()` bị gọi trực tiếp ngoài request scope ở 3 test khác:
`test_trace_writer.py`, `test_kb_search_live_readiness.py`, `test_spine_scored_from_postgres.py`).

`obs.trace_events` CHƯA bật RLS production (`obs/schema.py` không có dòng RLS nào — mini-RFC
`packages/kb/docs/mini-rfc-tenant-schema-unify.md` mới chỉ LÊN KẾ HOẠCH bật, chưa land). File này
bật RLS **CỤC BỘ trong phạm vi test** (setup/teardown quanh mỗi test case), y hệt idiom đã có ở
`packages/kb/tests/test_rls_framework.py` cho `kb.chunks` (`FORCE ROW LEVEL SECURITY` + policy
`USING`/`WITH CHECK` cast `NULLIF(current_setting('app.tenant_id', true), '')::uuid`) — KHÔNG sửa
`obs/schema.py`, RLS thật cho production thuộc mini-RFC riêng (mentor+DE), ngoài phạm vi ở đây.

Vì sao đây là bằng chứng thật (không phải test tautological): nếu `write()` không tự
`SET LOCAL app.tenant_id` trước INSERT, `WITH CHECK` của policy dưới đây từ chối ngay —
`current_setting(..., true)` trả NULL trên một transaction chưa set gì → `tenant_id = NULL` không
bao giờ đúng → `psycopg.errors.InsufficientPrivilege`. Test RED đúng nghĩa: writer ném lỗi RLS,
không phải lỗi cú pháp/fixture.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from studio_app.core._db import Pool
from studio_app.obs.trace_writer import PgTraceWriter
from studio_contracts.nodes import NodeType
from studio_contracts.trace import Tokens, TraceEvent

_POLICY_NAME = "obs_trace_events_tenant_isolation_test_only"

TENANT_A = UUID("c0000000-0000-0000-0000-0000000000aa")
TENANT_B = UUID("c0000000-0000-0000-0000-0000000000bb")


def _sample_event(event_id: str, tenant_id: UUID) -> TraceEvent:
    return TraceEvent(
        event_id=event_id,
        run_id="run-rls-1",
        agent_id="agent-rls-1",
        tenant_id=tenant_id,
        node_id="node-1",
        node_type=NodeType.LLM_STEP,
        ts="2026-08-12T00:00:00Z",
        inputs_hash="deadbeef",
        outputs={"answer": "42"},
        tokens=Tokens(prompt=10, completion=5),
        cost=0.0021,
        citations=["doc-1"],
    )


@pytest_asyncio.fixture
async def obs_trace_events_rls(admin_pool: Pool) -> AsyncIterator[None]:
    """Bật `ENABLE`+`FORCE ROW LEVEL SECURITY` cho `obs.trace_events` CỤC BỘ, chỉ trong phạm vi
    test dùng fixture này — gỡ lại (DROP POLICY + DISABLE RLS) ở teardown để KHÔNG rò rỉ sang test
    khác trong cùng DB dùng chung container (vd `test_trace_writer.py` đọc `obs.trace_events` qua
    `pool` không set `app.tenant_id` — nếu RLS còn bật sau test này, SELECT đó sẽ tự nhiên về 0
    dòng và test đó sẽ đỏ vì lý do KHÔNG liên quan)."""
    async with admin_pool.connection() as conn:
        await conn.execute("ALTER TABLE obs.trace_events ENABLE ROW LEVEL SECURITY")
        await conn.execute("ALTER TABLE obs.trace_events FORCE ROW LEVEL SECURITY")
        await conn.execute(sql.SQL("DROP POLICY IF EXISTS {} ON obs.trace_events").format(sql.Identifier(_POLICY_NAME)))
        await conn.execute(
            sql.SQL(
                "CREATE POLICY {} ON obs.trace_events "
                "USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) "
                "WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
            ).format(sql.Identifier(_POLICY_NAME))
        )
    try:
        yield
    finally:
        async with admin_pool.connection() as conn:
            await conn.execute(sql.SQL("DROP POLICY IF EXISTS {} ON obs.trace_events").format(sql.Identifier(_POLICY_NAME)))
            await conn.execute("ALTER TABLE obs.trace_events DISABLE ROW LEVEL SECURITY")


async def _count_visible(pool: Pool, event_id: str, tenant_id: UUID | None) -> int:
    """Đọc lại qua `pool` (non-owner, `studio_app`) với `app.tenant_id` set thành `tenant_id`
    (hoặc KHÔNG set gì nếu `None`) — 2-conn dance giống `test_rls_framework.py::_seed_chunk` +
    `test_tenant_scoped_visibility`."""
    async with pool.connection() as conn:
        if tenant_id is not None:
            await conn.execute(sql.SQL("SET LOCAL app.tenant_id = {}").format(sql.Literal(str(tenant_id))))
        cur = await conn.execute("SELECT count(*) FROM obs.trace_events WHERE event_id = %s", (event_id,))
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def test_write_sets_local_tenant_matching_event_under_rls(
    obs_trace_events_rls: None, admin_pool: Pool, pool: Pool
) -> None:
    """KHÓA: `write()` tự `SET LOCAL app.tenant_id = event.tenant_id` trên chính transaction nó
    mở — dưới RLS CỤC BỘ (fixture trên), INSERT đó phải qua được `WITH CHECK` (không exception),
    và hàng ghi ra CHỈ hiện với đúng `app.tenant_id = TENANT_A` — 0 dòng khi đọc bằng tenant khác
    hoặc không set gì (fail-closed, không phải "mọi tenant đều thấy" do writer quên set)."""
    del admin_pool  # ordering only — fixture `obs_trace_events_rls` đã cần `admin_pool` để bật RLS
    writer = PgTraceWriter(pool)
    event = _sample_event("evt-rls-a", TENANT_A)

    await writer.write(event)  # RED trước khi sửa: raises psycopg.errors.InsufficientPrivilege

    assert await _count_visible(pool, event.event_id, TENANT_A) == 1
    assert await _count_visible(pool, event.event_id, TENANT_B) == 0
    assert await _count_visible(pool, event.event_id, None) == 0


async def test_write_local_tenant_isolated_between_two_events(
    obs_trace_events_rls: None, admin_pool: Pool, pool: Pool
) -> None:
    """KHÓA bổ sung: 2 lần `write()` liên tiếp với tenant KHÁC nhau — mỗi transaction riêng của
    `write()` phải tự set đúng tenant CỦA CHÍNH NÓ, không rò rỉ/giữ lại giá trị từ lần gọi trước
    (loại trừ khả năng fix chỉ set đúng lần đầu nhờ trạng thái để lại từ test trước)."""
    del admin_pool
    writer = PgTraceWriter(pool)
    await writer.write(_sample_event("evt-rls-b1", TENANT_A))
    await writer.write(_sample_event("evt-rls-b2", TENANT_B))

    async with pool.connection() as conn:
        await conn.execute(sql.SQL("SET LOCAL app.tenant_id = {}").format(sql.Literal(str(TENANT_A))))
        cur = await conn.execute("SELECT event_id FROM obs.trace_events WHERE event_id IN (%s, %s)", ("evt-rls-b1", "evt-rls-b2"))
        rows_a = await cur.fetchall()
    assert [row[0] for row in rows_a] == ["evt-rls-b1"]

    async with pool.connection() as conn:
        await conn.execute(sql.SQL("SET LOCAL app.tenant_id = {}").format(sql.Literal(str(TENANT_B))))
        cur = await conn.execute("SELECT event_id FROM obs.trace_events WHERE event_id IN (%s, %s)", ("evt-rls-b1", "evt-rls-b2"))
        rows_b = await cur.fetchall()
    assert [row[0] for row in rows_b] == ["evt-rls-b2"]


async def test_local_rls_setup_genuinely_enforces_with_check(admin_pool: Pool, obs_trace_events_rls: None) -> None:
    """Sanity KHÓA cho fixture trên chính nó (không phải cho `write()`): một INSERT thô đặt
    `app.tenant_id` = TENANT_A nhưng `tenant_id` cột = TENANT_B (cross-tenant WRITE) phải bị
    `WITH CHECK` chặn — chứng minh policy cục bộ ở đây LÀ policy thật đang enforce (RLS đúng bật,
    không phải fixture tạo policy vô hại/không hiệu lực), khớp tinh thần
    `test_rls_framework.py::test_force_rls_and_with_check`."""
    del obs_trace_events_rls
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with admin_pool.connection() as conn:
            await conn.execute(sql.SQL("SET LOCAL app.tenant_id = {}").format(sql.Literal(str(TENANT_A))))
            await conn.execute(
                "INSERT INTO obs.trace_events "
                "(event_id, run_id, agent_id, tenant_id, node_id, node_type, ts, inputs_hash) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                ("evt-cross-write", "run-x", "agent-x", TENANT_B, "node-1", "llm_step", "2026-08-12T00:00:00Z", "deadbeef"),
            )
