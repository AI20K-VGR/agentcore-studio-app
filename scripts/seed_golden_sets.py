"""Nạp các bộ golden đóng gói sẵn (`studio_kb/golden/*.yaml`) vào `eval.golden_sets` — chạy tay.

Cần thiết từ cutover `evalhub#47`/`#48`: `routes/publish.py` không còn đọc bộ case từ đĩa mà đọc từ
`eval.golden_sets` theo `(tenant_id, golden_set_ref)`. Bảng rỗng ⇒ `/evaluate` và `/publish` trả 400
ngay request đầu tiên. Script này là đường nạp đóng vòng đó.

Nạp cho **2 tenant demo** (ankor/borea) mặc định, giống `seed_demo_tenants.py` — cùng bộ UUID
hardcode đang dùng xuyên hệ thống (`studio_kb.doc_factory.TENANT_IDS`). Truyền UUID trên dòng lệnh
để nạp cho một tenant khác:

    uv run python apps/studio/scripts/seed_golden_sets.py
    uv run python apps/studio/scripts/seed_golden_sets.py 7f3c1e6a-....-....-....-............

**Chạy lại được nhiều lần** — `write_golden_set` upsert theo `(tenant_id, golden_set_ref)`. Nhưng
đúng vì nó upsert, script này KHÔNG chạy lúc backend boot: một bộ case tenant tự nạp qua
`POST /api/admin/golden-sets` trùng ref sẽ bị đè mất sau mỗi lần restart. Nạp là việc có chủ đích.

Yêu cầu `STUDIO_DATABASE_URL_ADMIN` (vai `studio_owner`) — `eval.golden_sets` bật RLS `FORCE`, nên
mọi ghi đều phải bind `app.tenant_id` trước, kể cả bằng vai owner. Script tự bind trong transaction.
"""

from __future__ import annotations

import asyncio
import sys
from uuid import UUID

from psycopg import sql
from studio_app.core._db import close_pools, get_admin_pool
from studio_app.core.golden_seed import GOLDEN_SET_DIR, seed_golden_sets
from studio_app.core.schema import ensure_all_schemas
from studio_kb.doc_factory import TENANT_IDS


async def _seed(tenant_ids: dict[str, UUID]) -> None:
    admin = await get_admin_pool()
    # `eval.golden_sets` chưa tồn tại thì INSERT lỗi khó hiểu — cùng lý do `seed_demo_tenants.py`
    # gọi hàm này trước khi ghi `core.tenants`.
    await ensure_all_schemas(admin)

    for name, tenant_id in tenant_ids.items():
        async with admin.connection() as conn, conn.transaction():
            # `SET LOCAL` không nhận bind parameter qua wire protocol (nó là utility statement) —
            # cùng lý do `middleware.py` phải dựng câu qua `sql.Literal` thay vì truyền tham số.
            await conn.execute(sql.SQL("SET LOCAL app.tenant_id = {}").format(sql.Literal(str(tenant_id))))
            refs = await seed_golden_sets(conn, tenant_id)
        print(f"  {name} ({tenant_id}): {len(refs)} bộ — {', '.join(refs)}")

    print(f"Nguồn: {GOLDEN_SET_DIR}")


async def main() -> None:
    args = sys.argv[1:]
    if args:
        try:
            tenant_ids = {f"tenant-{i + 1}": UUID(a) for i, a in enumerate(args)}
        except ValueError as exc:
            raise SystemExit(f"Tham số phải là UUID tenant hợp lệ: {exc}") from exc
    else:
        tenant_ids = dict(TENANT_IDS)

    if not GOLDEN_SET_DIR.is_dir():
        raise SystemExit(f"Không thấy thư mục golden ở {GOLDEN_SET_DIR} — `packages/kb` đã cài chưa?")

    try:
        await _seed(tenant_ids)
    finally:
        # `close_pools()` phải chạy TRONG CÙNG event loop đã dựng pool. Bản đầu của script này gọi
        # `asyncio.run(close_pools())` ở một `finally` ngoài — loop thứ hai, và psycopg_pool ném
        # `ValueError: The future belongs to a different loop`. Cùng khuôn `seed_demo_tenants.py`/
        # `seed_superadmin.py`: một `asyncio.run` duy nhất, `finally` nằm bên trong nó.
        await close_pools()


if __name__ == "__main__":
    if sys.platform == "win32":
        # Windows mặc định ProactorEventLoop; psycopg async từ chối thẳng. Cùng workaround
        # `scripts/seed_demo_tenants.py`/`seed_superadmin.py` đã dùng.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
