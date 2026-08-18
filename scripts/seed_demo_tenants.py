"""Seed `core.tenants` với 2 tenant demo (ankor/borea) — chạy tay 1 lần.

`core.tenants` (`core/schema.py`) không có seed data nào sau khi tạo — bảng trống hoàn toàn.
Không seed ở `packages/kb` (kb chỉ ingest NỘI DUNG tài liệu vào `kb.chunks`, không sở hữu
registry tenant — `docs/mini-rfc-tenant-schema-unify.md:107` của chính kb ghi rõ `core.tenants`
thuộc `apps/studio`).

**BẮT BUỘC dùng đúng UUID hardcode** đã dùng xuyên suốt hệ thống (không để cột `id` tự sinh
`gen_random_uuid()`):
- `ANKOR_ID`/`BOREA_ID` — `packages/workbench/src/studio_workbench/builder.py`
- `TENANT_IDS["ankor"/"borea"]` — `packages/kb/src/studio_kb/doc_factory.py`

Nếu seed bằng UUID ngẫu nhiên, `core.tenants` sẽ có 2 hàng nhưng KHÔNG khớp với bất kỳ hằng số
nào các quadrant khác đang dùng — toàn bộ demo login → publish → chat sẽ vỡ ở khâu "tenant_id
tồn tại trong core.tenants" dù mọi thứ khác đúng.

Chạy:
    uv run python apps/studio/scripts/seed_demo_tenants.py
"""

from __future__ import annotations

import asyncio
import sys
from uuid import UUID

from studio_app.core._db import close_pools, get_admin_pool
from studio_app.core.schema import ensure_all_schemas

# Khớp 1:1 với `studio_workbench.builder.ANKOR_ID`/`BOREA_ID` và
# `studio_kb.doc_factory.TENANT_IDS` — 3 nơi đều phải ra cùng giá trị, đây là SSOT thứ 3 (không
# import chéo được vì `apps/studio` không được import lúc build-time bởi 2 package kia, và bản
# thân script này đứng ngoài mọi quadrant nên không có `.importlinter` nào cấm nó import cả 2 để
# đối chiếu tại runtime — xem `_assert_matches_known_constants` bên dưới).
_DEMO_TENANTS: dict[str, UUID] = {
    "ankor": UUID("a0000000-0000-0000-0000-000000000001"),
    "borea": UUID("b0000000-0000-0000-0000-000000000001"),
}


def _assert_matches_known_constants() -> None:
    """Đối chiếu với 2 SSOT thật (workbench, kb) trước khi ghi DB — phòng trường hợp ai đó đổi
    hằng số ở 1 trong 2 nơi mà quên đổi ở đây, script sẽ báo lỗi rõ ràng thay vì âm thầm seed sai
    UUID rồi để lỗi lộ ra rất xa, khó truy (đúng tinh thần fail-loud toàn codebase này theo)."""
    from studio_kb.doc_factory import TENANT_IDS
    from studio_workbench.builder import ANKOR_ID, BOREA_ID

    expected = {"ankor": ANKOR_ID, "borea": BOREA_ID}
    if expected != _DEMO_TENANTS:
        raise AssertionError(
            f"seed_demo_tenants: UUID lệch với studio_workbench.builder — có {_DEMO_TENANTS}, cần {expected}"
        )
    if _DEMO_TENANTS != TENANT_IDS:
        raise AssertionError(
            f"seed_demo_tenants: UUID lệch với studio_kb.doc_factory.TENANT_IDS — có {_DEMO_TENANTS}, cần {TENANT_IDS}"
        )


async def seed_demo_tenants() -> None:
    _assert_matches_known_constants()
    admin = await get_admin_pool()
    await ensure_all_schemas(admin)  # đảm bảo core.tenants tồn tại trước khi INSERT
    async with admin.connection() as conn:
        for name, tenant_id in _DEMO_TENANTS.items():
            await conn.execute(
                "INSERT INTO core.tenants (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                (tenant_id, name),
            )
    print(f"Seeded {len(_DEMO_TENANTS)} tenant demo vào core.tenants: {list(_DEMO_TENANTS)}")


async def main() -> None:
    try:
        await seed_demo_tenants()
    finally:
        await close_pools()


if __name__ == "__main__":
    if sys.platform == "win32":
        # Windows mặc định ProactorEventLoop; psycopg async từ chối thẳng ("Psycopg cannot use
        # the 'ProactorEventLoop' to run in async mode"). Selector* là loop chính lỗi đó khuyến
        # nghị, có sẵn trong stdlib mọi hệ điều hành script này chạy — không thêm dependency nào.
        # Cùng workaround `scripts/seed_superadmin.py` + `tests/conftest.py` đã dùng.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
