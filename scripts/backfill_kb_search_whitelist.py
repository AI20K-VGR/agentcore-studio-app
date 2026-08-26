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

## Thứ tự bump BẮT BUỘC — review dholmes0207 trên chính PR script này, mục F1
Chuỗi 4 repo mà `agentcore-studio-engine#50` liệt kê CÓ `agentcore-studio-web#52`
(`fromCanvas.ts::deriveToolWhitelist()` suy `kb_search` từ node `kb-retrieve` trên canvas) — bỏ sót
bước này thì backfill bị CANVAS âm thầm xoá lại, không phải rollback:

1. `agentcore-studio-workbench#57` + `#58` merge, bump con trỏ kit → `packages/workbench`.
2. **`agentcore-studio-web#52` merge, bump con trỏ kit → `apps/web`.** Ở con trỏ CHƯA có #52,
   `deriveToolWhitelist()` chỉ suy `tool_whitelist` từ node `tool-call` (bỏ qua `kb-retrieve` hoàn
   toàn) và `buildRecipe()` không đọc lại `header.tool_whitelist` cũ. Nếu bỏ qua bước này: admin mở
   1 agent ĐÃ backfill trên canvas (còn ở bản UI cũ), sửa gì đó, bấm Publish → `buildRecipe()` suy
   LẠI whitelist từ đầu → `kb_search` biến mất → luật `no_kb_search` đã bị #57 gỡ nên publish trót
   lọt, không lỗi gì nổi lên → agent mất tra KB lần nữa, và lần này không còn lượt backfill nào để
   vá lại (dòng vừa publish là dòng MỚI, không phải dòng đã backfill).
3. Chạy script này (`--execute`) trên môi trường thật.
4. **SAU CÙNG** mới bump con trỏ kit → `packages/engine` (bản có PR #50, đảo A4) ở môi trường đó.

## Cách chạy
Mặc định DRY-RUN — chỉ in ra SẼ đổi gì, KHÔNG ghi:

    uv run python apps/studio/scripts/backfill_kb_search_whitelist.py

Ghi thật (SAU KHI đã xem kỹ output dry-run, và đã bump `apps/web` theo mục trên):

    uv run python apps/studio/scripts/backfill_kb_search_whitelist.py --execute

Chỉ nhận đúng 1 cờ (`--execute`) — cờ lạ/gõ sai (`--exec`, `-e`, `--execute=true`...) làm script
DỪNG NGAY với `SystemExit` thay vì âm thầm rơi về dry-run (review F6).

Yêu cầu `STUDIO_DATABASE_URL_ADMIN` trỏ đúng môi trường cần vá — chạy lần lượt cho MỌI môi trường
có recipe published thật trước khi bump con trỏ engine ở môi trường đó (không có cách nào 1 lần
chạy phủ hết mọi môi trường — DSN chỉ trỏ 1 database tại 1 thời điểm).

## Phạm vi
Vá CẢ `wb.recipes` LẪN `wb.recipe_versions`, MỌI `status` (không chỉ `'published'`) —
`rollback()` (`packages/workbench/src/studio_workbench/publish.py:268-327`) đọc NỘI DUNG recipe từ
`wb.recipe_versions`, nhưng chỉ THỰC SỰ ghi đè `wb.recipes` bằng nội dung đó khi dòng `wb.recipes`
của version đích KHÔNG CÒN SỐNG (`existing is None`, dòng 316-326) — khi dòng đó còn sống,
`rollback()` (dòng 316-319) chỉ lật lại `status`, KHÔNG đụng `recipe`, nên bản đã vá vẫn giữ
nguyên (review F5, thu hẹp lại so với bản trước — "mọi lượt rollback đều kéo lại bản cũ" là quá
lời). Dù vậy vẫn vá cả 2 bảng: không có gì đảm bảo `wb.recipes` của mọi version luôn còn sống mãi
mãi, và vá cả 2 rẻ hơn hẳn việc phải chứng minh trường hợp `existing is None` không bao giờ xảy ra.

Chỉ đổi recipe có node `kb-retrieve` trong `dag` VÀ chưa có `"kb_search"` trong whitelist
(`with_kb_search_whitelisted` — idempotent, chạy lại nhiều lần an toàn, từ lần 2 trở đi không đổi
gì nữa vì mọi dòng cần vá đã vá xong). Dòng lịch sử KHÔNG parse được thành `Recipe` hợp lệ (schema
`wb.recipe_versions` là JSONB append-only, trải qua nhiều đời contract — vd `ScorecardThreshold`
siết `ge=0.0, le=1.0` ở kit#129, dòng trước đó có thể mang giá trị ngoài khoảng) bị BỎ QUA có ghi
log, không làm chết cả lượt vá của tenant đó (review F2).

## Tác dụng phụ ngoài `tool_whitelist` — review F4
`model_dump_json(by_alias=True)` viết lại NGUYÊN VẸN mọi field khác của recipe, không chỉ
`tool_whitelist`, nên 1 dòng bị vá còn nhận 2 tác dụng phụ:
- Dòng tiền-DEC-2 (trước khi `system_prompt` đổi tên từ `instructions`, đọc được cả 2 nhờ
  `validation_alias`) sẽ bị VIẾT LẠI khoá `"instructions"` → `"system_prompt"` — cùng chiều chuẩn
  hoá DEC-2 đã chốt, không phải hồi quy, nhưng chưa từng được khai trong tài liệu script này.
- `Recipe`/`AgentConfig` không khai `model_config = ConfigDict(extra=...)` tường minh — mặc định
  pydantic v2 `extra="ignore"` — nên bất kỳ key lạ nào ở tầng model bị ĐỌC BỎ QUA rồi KHÔNG GHI LẠI
  khi round-trip qua `model_validate`/`model_dump_json`. Nhiều khả năng vô hại (mọi consumer đọc
  qua contract, không đọc raw JSONB), nhưng là tác dụng phụ thật, ghi lại ở đây cho người vận hành
  biết trước khi chạy `--execute`.

## `recipe_hash` — CỐ Ý không tính lại
Giữ nguyên giá trị `recipe_hash` cũ trên các dòng bị vá — cùng đánh đổi đã CHẤP NHẬN cho lần đổi tên
`instructions`→`system_prompt` trước đó (`studio_workbench.recipe.AgentConfig` docstring, DEC-2):
`recipe_hash` của dòng cũ không còn re-verify được từ 1 `model_dump` MỚI (vì nội dung đã đổi), nhưng
`rollback()` mang `history_recipe_hash` đi NGUYÊN VẸN thay vì tính lại nên không vỡ. Bất cứ đường
nào TỰ tính lại rồi so với `recipe_hash` cũ trên dòng đã vá thì không còn khớp — chấp nhận, grep
toàn repo lúc viết script này không thấy đường CODE nào làm vậy ngoài chính lúc publish.

**Ngoại lệ — quy trình KIỂM TRA THỦ CÔNG bằng lời, không phải code** (review dholmes0207,
`packages/workbench/src/studio_workbench/schema.py`'s docstring `wb.recipes`): tài liệu ở đó dạy
người vận hành tự xác minh 1 dòng bằng cách tính lại
`publish.recipe_hash(Recipe.model_validate(row["recipe"]))` rồi so với `recipe_hash` đã lưu. Quy
trình đó THẤT BẠI cho MỌI dòng đã bị script này vá (đúng như phần trên đã nói — không phải bug
mới), nhưng tài liệu ở `schema.py` không nói nó ngừng đúng sau 1 lần backfill. Người chạy quy trình
kiểm tra đó trên 1 dòng đã vá sẽ thấy hash "không khớp" và có thể tưởng nhầm là dữ liệu hỏng.

## RLS
`wb.recipes`/`wb.recipe_versions` đều `FORCE ROW LEVEL SECURITY` — `studio_owner` (`get_admin_pool`)
KHÔNG bypass RLS trên các bảng này (chủ đích, xem `docker/postgres-init/00-roles.sql`: "studio_owner
CAN still bypass RLS via table ownership by default — that gap is closed by FORCE ROW LEVEL
SECURITY"). Script loop qua MỌI tenant trong `core.tenants` (bảng không RLS), `SET LOCAL
app.tenant_id` mỗi vòng trong 1 transaction riêng — cùng khuôn `seed_golden_sets.py`. Output cuối
in cả số tenant/dòng đã QUÉT (không chỉ số dòng đã VÁ, review F3) — `Tổng: 0` phải phân biệt được
"quét đủ, không có gì cần vá" với "quét thiếu" (vd `core.tenants` thiếu 1 tenant thật) chỉ bằng cách
đọc lại số dòng đã quét.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from psycopg import sql
from psycopg.sql import SQL, Composed
from pydantic import ValidationError
from studio_app.core._db import Pool, close_pools, get_admin_pool
from studio_contracts import Recipe
from studio_workbench.recipe_ops import with_kb_search_whitelisted

# (schema, table) — tách sẵn cho `sql.Identifier(schema, table)` bên dưới, không nối chuỗi tay.
_TABLES: list[tuple[str, str]] = [("wb", "recipes"), ("wb", "recipe_versions")]

_KNOWN_FLAGS = {"--execute"}


class _RowCursor(Protocol):
    """Đúng phần bề mặt `_backfill_table` chạm tới sau `execute()` — `fetchall()` trên kết quả
    `SELECT id, agent_id, version, recipe FROM ...` (4 cột/dòng)."""

    async def fetchall(self) -> Sequence[tuple[object, object, object, object]]: ...


class _RowSource(Protocol):
    """Review dholmes0207 (PR backfill script, sau khi 6 finding đầu đã xử) — `_backfill_table`
    chỉ gọi `execute()`, không cần cả bề mặt `psycopg.AsyncConnection` thật. Khai `Protocol` hẹp
    đúng thứ dùng thay vì annotate `conn: AsyncConnection`: mypy UNSCOPED (`uv run mypy packages
    apps`, job `lint` của kit gốc — khác job CI riêng của repo này, chạy SAU khi con trỏ kit đã bump
    đủ để `AsyncConnection`/`with_kb_search_whitelisted` resolve thật, không còn bị `Any` che) từ
    chối `_FakeConn` (double trong test) làm `AsyncConnection` thật — structural typing qua
    `Protocol` giải quyết đúng chỗ, không phải nới kiểu về `Any`/thêm `# type: ignore`.

    `query: SQL | Composed` khớp ĐÚNG (không chỉ "đủ rộng") kiểu `sql.SQL(...)`/
    `sql.SQL(...).format(...)` thật sự trả về ở dưới. Từng thử `Composable` (lớp cha chung của
    `SQL`/`Composed`/`Identifier`...) — mypy vẫn từ chối `AsyncConnection` thật thoả Protocol, vì
    `execute()` thật là 2 `@overload` khai `str | bytes | SQL | Composed` / `Template` cho `query`,
    không phải `Composable` trần — so khớp Protocol với method có overload đòi type CHÍNH XÁC
    nằm trong hợp union đó, `Composable` (rộng hơn) vẫn trượt dù về logic là superset hợp lệ."""

    async def execute(self, query: SQL | Composed, params: Sequence[object] | None = ..., /) -> _RowCursor: ...


async def _tenant_ids(admin: Pool) -> list[UUID]:
    """`core.tenants` KHÔNG bật RLS (đăng ký tenant toàn hệ thống, phải đọc được TRƯỚC khi có
    `app.tenant_id` để bind) — an toàn liệt kê thẳng qua admin pool, không cần `SET LOCAL`."""
    async with admin.connection() as conn:
        cursor = await conn.execute("SELECT id FROM core.tenants ORDER BY id")
        rows = await cursor.fetchall()
    return [row[0] for row in rows]


class _TableStats:
    """Kết quả 1 bảng, 1 tenant — tách khỏi kiểu trả `int` đơn lẻ cũ để mang đủ 3 con số cho báo
    cáo cuối (review F3): tổng dòng đọc được, dòng bị vá/sẽ vá, dòng bỏ qua vì không parse được."""

    __slots__ = ("scanned", "changed", "skipped")

    def __init__(self) -> None:
        self.scanned = 0
        self.changed = 0
        self.skipped = 0


async def _backfill_table(conn: _RowSource, schema: str, table: str, tenant_id: UUID, *, execute: bool) -> _TableStats:
    """1 bảng, 1 tenant đã bind `app.tenant_id` qua `SET LOCAL` trên `conn` (caller lo việc bind)."""
    ident = sql.Identifier(schema, table)
    cursor = await conn.execute(sql.SQL("SELECT id, agent_id, version, recipe FROM {}").format(ident))
    rows = await cursor.fetchall()

    stats = _TableStats()
    stats.scanned = len(rows)
    for row_id, agent_id, version, raw_recipe in rows:
        try:
            recipe = Recipe.model_validate(raw_recipe)
        except ValidationError as exc:
            # review F2 — 1 dòng lịch sử không parse được (schema cũ, giá trị ngoài khoảng contract
            # hiện tại đã siết, vd ScorecardThreshold ge=0.0/le=1.0 từ kit#129) không được làm chết
            # cả lượt vá của tenant này: ghi log rồi đi tiếp, KHÔNG re-raise.
            stats.skipped += 1
            print(
                f"  [BỎ QUA — không parse được] {schema}.{table} id={row_id} agent_id={agent_id!r} "
                f"tenant_id={tenant_id} version={version}: {exc}"
            )
            continue

        patched = with_kb_search_whitelisted(recipe)
        if patched.agent_config.tool_whitelist == recipe.agent_config.tool_whitelist:
            continue  # không có node kb-retrieve, hoặc kb_search đã có sẵn — no-op đúng ý.

        stats.changed += 1
        marker = "VÁ" if execute else "SẼ VÁ (dry-run)"
        print(
            f"  [{marker}] {schema}.{table} id={row_id} agent_id={agent_id!r} tenant_id={tenant_id} "
            f"version={version}: {recipe.agent_config.tool_whitelist!r} -> "
            f"{patched.agent_config.tool_whitelist!r}"
        )
        if execute:
            # `by_alias=True` bắt buộc — cùng lý do `publish.py:225` (Edge.from_/"from" alias),
            # khác đi thì ghi sai shape wire cho MỌI consumer đọc thẳng JSONB (apps/web canvas).
            # Tác dụng phụ đã khai ở docstring module (review F4): viết lại NGUYÊN VẸN mọi field
            # khác, không chỉ tool_whitelist (chuẩn hoá instructions->system_prompt, bỏ key lạ).
            recipe_json = patched.model_dump_json(by_alias=True)
            set_clause = sql.SQL("recipe = %s::jsonb")
            if table == "recipes":  # chỉ wb.recipes có cột updated_at (wb.recipe_versions append-only)
                set_clause = sql.SQL("recipe = %s::jsonb, updated_at = now()")
            await conn.execute(
                sql.SQL("UPDATE {} SET {} WHERE id = %s").format(ident, set_clause),
                (recipe_json, row_id),
            )
    return stats


async def _backfill(execute: bool) -> None:
    admin = await get_admin_pool()
    tenants = await _tenant_ids(admin)
    if not tenants:
        print("core.tenants rỗng — không có gì để vá.")
        return

    scanned = changed = skipped = 0
    for tenant_id in tenants:
        async with admin.connection() as conn, conn.transaction():
            # `SET LOCAL` không nhận bind parameter qua wire protocol — cùng lý do
            # `seed_golden_sets.py` dựng câu qua `sql.Literal` thay vì truyền tham số.
            await conn.execute(sql.SQL("SET LOCAL app.tenant_id = {}").format(sql.Literal(str(tenant_id))))
            for schema, table in _TABLES:
                stats = await _backfill_table(conn, schema, table, tenant_id, execute=execute)
                scanned += stats.scanned
                changed += stats.changed
                skipped += stats.skipped

    mode = "Đã ghi" if execute else "Dry-run — chưa ghi gì"
    # review F3 — luôn in số đã QUÉT, không chỉ số đã VÁ: "0 dòng cần vá" chỉ đáng tin khi đi kèm
    # "đã quét N dòng trên M tenant" — thiếu số quét thì "0" không phân biệt được "sạch thật" với
    # "core.tenants/RLS bỏ sót dữ liệu".
    print(
        f"\n{mode}. Đã quét {len(tenants)} tenant, {scanned} dòng (2 bảng cộng lại). "
        f"{'Đã vá' if execute else 'Sẽ vá'}: {changed}. Bỏ qua (không parse được): {skipped}."
    )
    if not execute and changed > 0:
        print("Chạy lại kèm --execute để ghi thật.")


def _parse_execute_flag(argv: list[str]) -> bool:
    """review F6 — cờ lạ/gõ sai (`--exec`, `-e`, `--execute=true`...) phải DỪNG script với thông
    báo rõ, không được âm thầm rơi về dry-run như trước (chỉ kiểm `"--execute" in argv`, mọi cờ
    khác bị bỏ qua vô hình)."""
    unknown = [arg for arg in argv if arg not in _KNOWN_FLAGS]
    if unknown:
        raise SystemExit(
            f"Cờ không nhận diện được: {unknown!r} — chỉ chấp nhận --execute (hoặc không cờ nào, dry-run)."
        )
    return "--execute" in argv


async def main() -> None:
    execute = _parse_execute_flag(sys.argv[1:])
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
