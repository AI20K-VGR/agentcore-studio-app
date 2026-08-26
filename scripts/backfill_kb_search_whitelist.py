"""Backfill `kb_search` vào `tool_whitelist` cho recipe published TRƯỚC `agentcore-studio-workbench#57`
(engine#49 review F1, dholmes0207 trên PR #50) — chạy tay, MỘT LẦN cho mỗi môi trường, TRƯỚC khi
bump con trỏ `packages/engine` lên bản có PR #50 (đảo A4) merge.

## Vì sao cần
`agent_shape_lint` (workbench, TRƯỚC PR #57) có rule `tool_whitelist.no_kb_search` — FAIL nếu
`tool_whitelist` chứa `"kb_search"`, enforce ở `publish.py:159` (và `test_chat.py:106`) trước MỌI
lần ghi `wb.recipes`. Nên mọi recipe published TRƯỚC PR #57 merge KHÔNG THỂ có `"kb_search"` trong
`tool_whitelist` theo cấu trúc — dù canvas của nó CÓ node `kb-retrieve`, và dù nó ĐANG chạy được
tra KB thật (nhờ A4 cũ: `run_agent_loop()` hard-code `kb_search` luôn khả dụng, bỏ qua whitelist).

Sau khi engine bump lên bản có PR #50 (đảo A4 — `kb_search` giờ gate qua whitelist như mọi tool),
những recipe đó MẤT khả năng tra KB — và mất ÂM THẦM: LLM không được báo có tool đó nên không gọi,
không lỗi gì nổi lên, `used_kb_search` luôn `False` nên `refused` cũng luôn `False` (agent trông như
trả lời thành công bình thường, chỉ là câu trả lời không còn căn cứ). Xem thảo luận đầy đủ ở
docstring `with_kb_search_whitelisted` (`packages/workbench/src/studio_workbench/recipe_ops.py`)
và review thread PR #50 (`agentcore-studio-engine`, mục F1).

## Cách chạy
Mặc định DRY-RUN — chỉ in ra SẼ đổi gì, KHÔNG ghi:

    uv run python apps/studio/scripts/backfill_kb_search_whitelist.py

Ghi thật (SAU KHI đã xem kỹ output dry-run):

    uv run python apps/studio/scripts/backfill_kb_search_whitelist.py --execute

Yêu cầu `STUDIO_DATABASE_URL_ADMIN` trỏ đúng môi trường cần vá — chạy lần lượt cho MỌI môi trường
có recipe published thật trước khi bump con trỏ engine ở môi trường đó (không có cách nào 1 lần
chạy phủ hết mọi môi trường — DSN chỉ trỏ 1 database tại 1 thời điểm).

## Phạm vi
Vá CẢ `wb.recipes` LẪN `wb.recipe_versions`, MỌI `status` (không chỉ `'published'`) — `rollback()`
(`packages/workbench/src/studio_workbench/publish.py:268-323`) đọc nội dung recipe từ
`wb.recipe_versions`, KHÔNG phải `wb.recipes`. Vá thiếu bảng này thì 1 lượt rollback sau khi backfill
sẽ tự kéo lại đúng bản CŨ (thiếu `kb_search`) từ lịch sử, âm thầm xoá tác dụng của lần chạy này.

Chỉ đổi recipe có node `kb-retrieve` trong `dag` VÀ chưa có `"kb_search"` trong whitelist
(`with_kb_search_whitelisted` — idempotent, chạy lại nhiều lần an toàn, từ lần 2 trở đi không đổi
gì nữa vì mọi dòng cần vá đã vá xong).

## `recipe_hash` — CỐ Ý không tính lại
Giữ nguyên giá trị `recipe_hash` cũ trên các dòng bị vá — cùng đánh đổi đã CHẤP NHẬN cho lần đổi tên
`instructions`→`system_prompt` trước đó (`studio_workbench.recipe.AgentConfig` docstring, DEC-2):
`recipe_hash` của dòng cũ không còn re-verify được từ 1 `model_dump` MỚI (vì nội dung đã đổi), nhưng
`rollback()` mang `history_recipe_hash` đi NGUYÊN VẸN thay vì tính lại nên không vỡ. Bất cứ đường
nào TỰ tính lại rồi so với `recipe_hash` cũ trên dòng đã vá thì không còn khớp — chấp nhận, grep
toàn repo lúc viết script này không thấy đường nào làm vậy ngoài chính lúc publish.

## RLS
`wb.recipes`/`wb.recipe_versions` đều `FORCE ROW LEVEL SECURITY` — `studio_owner` (`get_admin_pool`)
KHÔNG bypass RLS trên các bảng này (chủ đích, xem `docker/postgres-init/00-roles.sql`: "studio_owner
CAN still bypass RLS via table ownership by default — that gap is closed by FORCE ROW LEVEL
SECURITY"). Script loop qua MỌI tenant trong `core.tenants` (bảng không RLS), `SET LOCAL
app.tenant_id` mỗi vòng trong 1 transaction riêng — cùng khuôn `seed_golden_sets.py`.
"""

from __future__ import annotations

import asyncio
import sys
from uuid import UUID

from psycopg import AsyncConnection, sql
from studio_app.core._db import Pool, close_pools, get_admin_pool
from studio_contracts import Recipe
from studio_workbench.recipe_ops import with_kb_search_whitelisted

# (schema, table) — tách sẵn cho `sql.Identifier(schema, table)` bên dưới, không nối chuỗi tay.
_TABLES: list[tuple[str, str]] = [("wb", "recipes"), ("wb", "recipe_versions")]


async def _tenant_ids(admin: Pool) -> list[UUID]:
    """`core.tenants` KHÔNG bật RLS (đăng ký tenant toàn hệ thống, phải đọc được TRƯỚC khi có
    `app.tenant_id` để bind) — an toàn liệt kê thẳng qua admin pool, không cần `SET LOCAL`."""
    async with admin.connection() as conn:
        cursor = await conn.execute("SELECT id FROM core.tenants ORDER BY id")
        rows = await cursor.fetchall()
    return [row[0] for row in rows]


async def _backfill_table(conn: AsyncConnection, schema: str, table: str, tenant_id: UUID, *, execute: bool) -> int:
    """1 bảng, 1 tenant đã bind `app.tenant_id` qua `SET LOCAL` trên `conn` (caller lo việc bind).
    Trả số dòng ĐÃ đổi (`execute=True`) hoặc SẼ đổi (`execute=False`, dry-run — không ghi gì)."""
    ident = sql.Identifier(schema, table)
    cursor = await conn.execute(sql.SQL("SELECT id, agent_id, version, recipe FROM {}").format(ident))
    rows = await cursor.fetchall()

    changed = 0
    for row_id, agent_id, version, raw_recipe in rows:
        recipe = Recipe.model_validate(raw_recipe)
        patched = with_kb_search_whitelisted(recipe)
        if patched.agent_config.tool_whitelist == recipe.agent_config.tool_whitelist:
            continue  # không có node kb-retrieve, hoặc kb_search đã có sẵn — no-op đúng ý.

        changed += 1
        marker = "VÁ" if execute else "SẼ VÁ (dry-run)"
        print(
            f"  [{marker}] {schema}.{table} id={row_id} agent_id={agent_id!r} tenant_id={tenant_id} "
            f"version={version}: {recipe.agent_config.tool_whitelist!r} -> "
            f"{patched.agent_config.tool_whitelist!r}"
        )
        if execute:
            # `by_alias=True` bắt buộc — cùng lý do `publish.py:225` (Edge.from_/"from" alias),
            # khác đi thì ghi sai shape wire cho MỌI consumer đọc thẳng JSONB (apps/web canvas).
            recipe_json = patched.model_dump_json(by_alias=True)
            set_clause = sql.SQL("recipe = %s::jsonb")
            if table == "recipes":  # chỉ wb.recipes có cột updated_at (wb.recipe_versions append-only)
                set_clause = sql.SQL("recipe = %s::jsonb, updated_at = now()")
            await conn.execute(
                sql.SQL("UPDATE {} SET {} WHERE id = %s").format(ident, set_clause),
                (recipe_json, row_id),
            )
    return changed


async def _backfill(execute: bool) -> None:
    admin = await get_admin_pool()
    tenants = await _tenant_ids(admin)
    if not tenants:
        print("core.tenants rỗng — không có gì để vá.")
        return

    total = 0
    for tenant_id in tenants:
        async with admin.connection() as conn, conn.transaction():
            # `SET LOCAL` không nhận bind parameter qua wire protocol — cùng lý do
            # `seed_golden_sets.py` dựng câu qua `sql.Literal` thay vì truyền tham số.
            await conn.execute(sql.SQL("SET LOCAL app.tenant_id = {}").format(sql.Literal(str(tenant_id))))
            for schema, table in _TABLES:
                total += await _backfill_table(conn, schema, table, tenant_id, execute=execute)

    mode = "Đã ghi" if execute else "Dry-run — chưa ghi gì"
    print(f"\n{mode}. Tổng số dòng {'vá' if execute else 'sẽ vá'}: {total}.")
    if not execute and total > 0:
        print("Chạy lại kèm --execute để ghi thật.")


async def main() -> None:
    execute = "--execute" in sys.argv[1:]
    try:
        await _backfill(execute)
    finally:
        # `close_pools()` phải chạy TRONG CÙNG event loop đã dựng pool — cùng khuôn
        # `seed_golden_sets.py`/`seed_demo_tenants.py`/`seed_superadmin.py`.
        await close_pools()


if __name__ == "__main__":
    if sys.platform == "win32":
        # Windows mặc định ProactorEventLoop; psycopg async từ chối thẳng — cùng workaround các
        # script seed khác trong thư mục này đã dùng.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
