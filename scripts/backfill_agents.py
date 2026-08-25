"""Nạp `wb.agents` cho agent đã publish TRƯỚC khi bảng này tồn tại — chạy tay, MỘT LẦN sau khi
deploy Phần E (xem `docs/decisions.md`/plan liên quan).

Trước Phần E, "agent" chỉ là 1 chuỗi `agent_id` suy ra từ `wb.recipes` đã publish — không có row
`wb.agents` nào cho các agent đó, nên `display_name` sẽ rơi về fallback `agent_id` (xem
`routes/agents.py::list_agents`) cho tới khi admin tự đổi tên qua `PATCH /api/agents/{agent_id}`.
Script này KHÔNG bắt buộc để hệ thống hoạt động (fallback đã đủ an toàn) — chỉ để mọi agent cũ có
sẵn 1 row `wb.agents` thật, sẵn sàng cho tính năng phân quyền theo phòng ban (Phần F, cần FK tới
`wb.agents`).

**Chạy lại được nhiều lần** — `ON CONFLICT (agent_id, tenant_id) DO NOTHING`, không đè
`display_name` admin đã tự đổi.

`wb.recipes`/`wb.agents` bật RLS `FORCE` (bind cả vai owner) — script tự `SET LOCAL app.tenant_id`
cho TỪNG tenant lấy từ `core.tenants` (bảng đó KHÔNG RLS, an toàn liệt kê hết).

    uv run python apps/studio/scripts/backfill_agents.py
"""

from __future__ import annotations

import asyncio
import sys

from psycopg import sql
from studio_app.core._db import close_pools, get_admin_pool
from studio_app.core.schema import ensure_all_schemas


async def _backfill() -> None:
    admin = await get_admin_pool()
    await ensure_all_schemas(admin)

    async with admin.connection() as conn:
        cur = await conn.execute("SELECT id, name FROM core.tenants")
        tenants = await cur.fetchall()

    total = 0
    for tenant_id, tenant_name in tenants:
        async with admin.connection() as conn, conn.transaction():
            # `SET LOCAL` không nhận bind parameter qua wire protocol — cùng lý do
            # `seed_golden_sets.py` dựng câu qua `sql.Literal` thay vì truyền tham số.
            await conn.execute(sql.SQL("SET LOCAL app.tenant_id = {}").format(sql.Literal(str(tenant_id))))
            cur = await conn.execute(
                """
                INSERT INTO wb.agents (agent_id, tenant_id, display_name)
                SELECT DISTINCT agent_id, tenant_id, agent_id
                FROM wb.recipes
                WHERE status = 'published'
                ON CONFLICT (agent_id, tenant_id) DO NOTHING
                """
            )
            print(f"  {tenant_name} ({tenant_id}): +{cur.rowcount} agent")
            total += cur.rowcount

    print(f"Xong — {total} row `wb.agents` mới (display_name = agent_id, admin tự đổi sau).")


async def main() -> None:
    try:
        await _backfill()
    finally:
        await close_pools()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
