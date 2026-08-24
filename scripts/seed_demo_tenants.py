"""Seed `core.tenants` với 2 tenant demo (ankor/borea) + 1 tài khoản admin mỗi tenant — chạy tay
1 lần.

`core.tenants` (`core/schema.py`) không có seed data nào sau khi tạo — bảng trống hoàn toàn.
Không seed ở `packages/kb` (kb chỉ ingest NỘI DUNG tài liệu vào `kb.chunks`, không sở hữu
registry tenant — `docs/mini-rfc-tenant-schema-unify.md:107` của chính kb ghi rõ `core.tenants`
thuộc `apps/studio`).

**BẮT BUỘC dùng đúng UUID hardcode** đã dùng xuyên suốt hệ thống (không để cột `id` tự sinh
`gen_random_uuid()`):
- `ANKOR_ID`/`BOREA_ID` — `packages/workbench/src/studio_workbench/recipe.py`
- `TENANT_IDS["ankor"/"borea"]` — `packages/kb/src/studio_kb/doc_factory.py`

Nếu seed bằng UUID ngẫu nhiên, `core.tenants` sẽ có 2 hàng nhưng KHÔNG khớp với bất kỳ hằng số
nào các quadrant khác đang dùng — toàn bộ demo login → publish → chat sẽ vỡ ở khâu "tenant_id
tồn tại trong core.tenants" dù mọi thứ khác đúng.

**Tài khoản admin đi kèm mỗi tenant** (`admin@ankor.vn`/`admin@borea.vn`) thay cho việc phải tự
insert tay `core.users` theo README "Cách B" — gán thẳng `tenant_id` fix cứng ở trên (không qua
route `POST /api/admin/companies`, vốn luôn sinh `tenant_id` ngẫu nhiên) nên khớp `kb.chunks` mà
`ingest_callisto.py` đã gắn sẵn. Roles gán đủ cả 4 role nội dung (`SECTION_VOCAB`) + `admin` để
canvas/chat ra dữ liệu thật ngay, không cần superadmin tạo `core.sections` trước (insert thẳng
SQL, không qua route `POST /api/admin/users` nên không bị validate theo `core.sections`).

Mật khẩu đọc từ biến môi trường `STUDIO_DEMO_ADMIN_PASSWORD` — có default cho tiện chạy dev cục bộ
NGAY (khác `seed_superadmin.py`: rủi ro thấp hơn nhiều, tài khoản này chỉ có quyền trong 1 tenant
demo chứa dữ liệu fixture, không phải quyền toàn hệ thống như superadmin) — nhưng vẫn nên override
bằng biến môi trường nếu DB này lộ ra ngoài máy cá nhân.

Chạy:
    uv run python apps/studio/scripts/seed_demo_tenants.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from uuid import UUID

from studio_app.core._db import close_pools, get_admin_pool
from studio_app.core.schema import ensure_all_schemas
from studio_app.jwt_auth import hash_password

# Khớp 1:1 với `studio_workbench.ANKOR_ID`/`BOREA_ID` và
# `studio_kb.doc_factory.TENANT_IDS` — 3 nơi đều phải ra cùng giá trị, đây là SSOT thứ 3 (không
# import chéo được vì `apps/studio` không được import lúc build-time bởi 2 package kia, và bản
# thân script này đứng ngoài mọi quadrant nên không có `.importlinter` nào cấm nó import cả 2 để
# đối chiếu tại runtime — xem `_assert_matches_known_constants` bên dưới).
_DEMO_TENANTS: dict[str, UUID] = {
    "ankor": UUID("a0000000-0000-0000-0000-000000000001"),
    "borea": UUID("b0000000-0000-0000-0000-000000000001"),
}

# email theo đúng quy ước README "Cách B" đã dùng cho `ankor` (`admin@ankor.vn`) — mở rộng thêm
# `borea` theo cùng quy luật. `SECTION_VOCAB` (studio_kb.doc_factory_core) gán đủ để canvas/chat
# ra dữ liệu KB thật ngay sau khi ingest, không cần superadmin tạo `core.sections` trước.
_DEMO_ADMIN_ROLES: list[str] = ["admin", "public", "hr", "finance", "engineering"]
_DEMO_ADMIN_EMAILS: dict[str, str] = {
    "ankor": "admin@ankor.vn",
    "borea": "admin@borea.vn",
}
_DEFAULT_DEMO_ADMIN_PASSWORD = "doi-mat-khau-nay-truoc-khi-dung-that"


def _assert_matches_known_constants() -> None:
    """Đối chiếu với 2 SSOT thật (workbench, kb) trước khi ghi DB — phòng trường hợp ai đó đổi
    hằng số ở 1 trong 2 nơi mà quên đổi ở đây, script sẽ báo lỗi rõ ràng thay vì âm thầm seed sai
    UUID rồi để lỗi lộ ra rất xa, khó truy (đúng tinh thần fail-loud toàn codebase này theo)."""
    from studio_kb.doc_factory import TENANT_IDS
    from studio_workbench import ANKOR_ID, BOREA_ID

    expected = {"ankor": ANKOR_ID, "borea": BOREA_ID}
    if expected != _DEMO_TENANTS:
        raise AssertionError(f"seed_demo_tenants: UUID lệch với studio_workbench — có {_DEMO_TENANTS}, cần {expected}")
    if _DEMO_TENANTS != TENANT_IDS:
        raise AssertionError(
            f"seed_demo_tenants: UUID lệch với studio_kb.doc_factory.TENANT_IDS — có {_DEMO_TENANTS}, cần {TENANT_IDS}"
        )


async def seed_demo_tenants() -> None:
    _assert_matches_known_constants()
    password = os.environ.get("STUDIO_DEMO_ADMIN_PASSWORD", _DEFAULT_DEMO_ADMIN_PASSWORD)
    # bcrypt giới hạn 72 byte — fail loud thay vì để `hash_password` raise traceback khó hiểu nếu
    # ai đó override bằng 1 password quá dài (cùng guard `seed_superadmin.py` đã dùng).
    if len(password.encode("utf-8")) > 72:
        raise SystemExit("STUDIO_DEMO_ADMIN_PASSWORD tối đa 72 byte (giới hạn bcrypt).")
    password_hash = hash_password(password)

    admin = await get_admin_pool()
    await ensure_all_schemas(admin)  # đảm bảo core.tenants/core.users tồn tại trước khi INSERT
    created_admins: list[str] = []
    async with admin.connection() as conn:
        for name, tenant_id in _DEMO_TENANTS.items():
            await conn.execute(
                "INSERT INTO core.tenants (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                (tenant_id, name),
            )
            email = _DEMO_ADMIN_EMAILS[name]
            cur = await conn.execute(
                "INSERT INTO core.users (tenant_id, email, password_hash, roles) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (email) DO NOTHING RETURNING id",
                (tenant_id, email, password_hash, _DEMO_ADMIN_ROLES),
            )
            if await cur.fetchone() is not None:
                created_admins.append(email)

    print(f"Seeded {len(_DEMO_TENANTS)} tenant demo vào core.tenants: {list(_DEMO_TENANTS)}")
    if created_admins:
        print(f"Seeded admin cho {len(created_admins)} tenant: {created_admins} (mật khẩu: {password!r})")
    else:
        print("Admin của 2 tenant demo đã tồn tại từ trước — không đổi gì (mật khẩu giữ nguyên).")


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
