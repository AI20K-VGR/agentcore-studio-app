"""`core.*` DDL (idempotent) + `ensure_all_schemas()` aggregator + `grant_app_privileges()` (F6).

Composition-root boot order: `core.ddl()` runs first (jobs/outbox live here — no cross-schema
FK, Decision #4), then each quadrant's own `ddl()` (direct-import, no entry-point discovery —
"KISS: chọn direct-import thay entry-point-discovery" per plan.md). `obs.ddl()` (P4, this app's
own `studio_app.obs.schema`) joins the tuple below; `kb`/`wb`/`eval` are still stub-empty
(`ddl() -> ""`) until P5/P7/P8 fill their body — running an empty string is a no-op, so this
stays idempotent and safe to call twice from day one.

Both functions MUST run against `get_admin_pool()` (studio_owner) — never `get_pool()` (F1/F2):
studio_owner OWNS every table it creates, which is what lets `FORCE ROW LEVEL SECURITY` (added
by the owning quadrant, P5) bite the owner too instead of silently bypassing the fence.
"""

from __future__ import annotations

from types import ModuleType

import studio_evalhub.schema as _evalhub_schema
import studio_kb.schema as _kb_schema
import studio_workbench.schema as _workbench_schema
from psycopg import sql

from studio_app.core._db import Pool
from studio_app.obs import schema as _obs_schema

_CORE_DDL = """
CREATE SCHEMA IF NOT EXISTS core;

-- `obs` schema SHELL only (plan.md Decision #4: "obs.*(DE, shell ship ở composition)") — its
-- tables (obs.trace_events/costs/golden_sets) land at Phase 4's `obs/schema.py:ddl()`. The shell
-- ships here so `grant_app_privileges` below can GRANT on all 5 schemas starting at P3, instead
-- of erroring on a schema that does not exist yet. Re-running `CREATE SCHEMA IF NOT EXISTS obs`
-- at P4 is a no-op — safe, idempotent, no conflict.
CREATE SCHEMA IF NOT EXISTS obs;

CREATE TABLE IF NOT EXISTS core.tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Auth thật (Kế hoạch 3, xem routes/auth.py) — đường đăng nhập DUY NHẤT (registry demo
-- _DEMO_ACCOUNTS/demo-login đã bị xoá hẳn, không còn song song với đường này). Không RLS theo
-- tenant_id (khác wb.recipes/kb.chunks): bước LOGIN tra email
-- xảy ra TRƯỚC KHI biết tenant nào (gà-trứng — chưa đăng nhập thì chưa có app.tenant_id để RLS
-- lọc theo). Ranh giới "admin công ty chỉ quản được user tenant mình" enforce ở tầng application
-- (routes/admin.py so session.tenant_id), không phải DB — cùng cách core.tenants đang xử lý.
-- password_hash KHÔNG BAO GIỜ được đọc lại vào response (routes/admin.py, jwt_auth.py).
--
-- tenant_id CỐ Ý NOT NULL, kể cả cho superadmin — không nullable. ResolvedContext/tenant_wall.py/
-- RLS ở MỌI bảng khác (wb.*, kb.*, eval.*) đều giả định tenant_id luôn có giá trị; nullable sẽ
-- kéo theo sửa dây chuyền ra ngoài apps/studio, rủi ro cao cho lợi ích nhỏ. Thay vào đó, superadmin
-- thuộc về 1 tenant HỆ THỐNG đặc biệt (core.tenants.name = '__system__', seed_superadmin.py tạo
-- nếu chưa có) — không phải công ty thật, không route nghiệp vụ nào đọc dữ liệu dưới tenant này,
-- nên `SET LOCAL app.tenant_id` = tenant hệ thống lúc superadmin đăng nhập là vô hại (RLS các bảng
-- nghiệp vụ trả 0 dòng cho tenant đó — đúng, vì không có dữ liệu thật nào gắn tenant này).
CREATE TABLE IF NOT EXISTS core.users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES core.tenants(id),
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    roles TEXT[] NOT NULL DEFAULT '{}',
    created_by UUID NULL REFERENCES core.users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active BOOLEAN NOT NULL DEFAULT true
);

-- Đường thứ hai cho DB đã tồn tại từ trước cột `is_active` (`routes/admin.py::delete_user` —
-- vô hiệu hoá thay vì DELETE cứng, xem docstring route đó) — `CREATE TABLE IF NOT EXISTS` ở trên
-- là no-op trên bảng đã có, nên thiếu dòng này thì máy đồng đội/DB cũ không bao giờ có cột.
-- `DEFAULT true` an toàn ngay cả trên bảng đã có dữ liệu — mọi user đã tồn tại coi như đang hoạt
-- động (đúng ngữ nghĩa, không phải giá trị bịa như cảnh báo tương tự ở `eval.golden_sets.tenant_id`).
ALTER TABLE core.users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT true;

-- `NULL` = "chưa từng đổi mật khẩu tự phục vụ kể từ khi cột này tồn tại" -> không JWT nào bị coi
-- là cũ (không có mốc nào để so `iat`). `routes/auth.py::change_own_password` ghi `now()` vào đây
-- CÙNG lúc ghi `password_hash` mới; `authz.fetch_fresh_identity` so cột này với `iat` của JWT đang
-- gọi (`jwt_auth.issued_at`) — JWT ký TRƯỚC lần đổi mật khẩu gần nhất bị coi là hết hiệu lực dù
-- chưa qua `exp` (review app#21 🔶: đổi mật khẩu là cách người dùng tự xử lý phiên bị đánh cắp,
-- nhưng trước bản vá này JWT cũ vẫn sống nguyên tới hết `jwt_expire_minutes`, mặc định 480 phút).
ALTER TABLE core.users ADD COLUMN IF NOT EXISTS password_changed_at TIMESTAMPTZ NULL;

-- Cache vector production cho `GatewayEmbedding` (app#30, QĐ-2 — Postgres, KHÔNG file-based:
-- settings.py:37-51 ghi lại ba hiện thân của cùng lớp bug đường-dẫn-file). Đường ĐỌC
-- (packages/kb/src/studio_kb/postgres.py:213 `embed([query])`) là 1 text/call, không batch được
-- từ trong apps/studio (đứng sau ranh giới package DE) — cache thay cho batching ở đó.
--
-- cache_key = sha256(f"{model}|{dim}|{text}") — khớp nguyên văn `kb#40`
-- (packages/kb/tests/embedding-tests/_vector_cache.py:56-63). Gộp model+dim vào khoá chứ không chỉ
-- băm text: cùng câu qua gemini-embedding-001 ở 2048 vs 1536 là HAI vector khác nhau.
--
-- KHÔNG RLS (cố ý, khác wb.recipes/kb.chunks): bảng này là một HÀM THUẦN của (model, dim, text) —
-- cùng input luôn cho cùng vector, bất kể tenant nào hỏi. Dùng chung hàng giữa các tenant là ĐÚNG
-- ngữ nghĩa, không phải rò rỉ — bảng không mang cột `tenant_id` nào để mà rò.
--
-- `embedding vector(2048)` cố ý viết hằng số, KHÔNG nội suy `EMBEDDING_DIM` từ Python vào chuỗi
-- DDL này: đây là schema `core` (apps/studio sở hữu), không phải `kb`. Không index HNSW
-- (packages/kb/src/studio_kb/schema.py:40-41 khoá cứng "2048 chỉ hợp lệ VÌ không còn index HNSW").
CREATE TABLE IF NOT EXISTS core.embedding_cache (
    cache_key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    embedding vector(2048) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- "Phòng ban"/section per tenant (thay SECTION_VOCAB toàn cục cứng của studio_kb.doc_factory) —
-- routes/sections.py: chỉ superadmin tạo/sửa/xoá, routes/admin.py::create_user tra động bảng này
-- để validate roles thay vì frozenset tĩnh.
--
-- KHÔNG RLS (khác wb.recipes/kb.chunks, GIỐNG core.users/core.tenants) — cố ý: superadmin quản
-- section của TENANT KHÁC tenant JWT của chính họ (JWT superadmin trỏ `__system__`,
-- scripts/seed_superadmin.py). RLS khoá theo current_setting('app.tenant_id') sẽ tự chặn nhầm
-- CHÍNH superadmin khỏi mọi tenant không phải __system__ — hỏng thẳng tính năng. Ranh giới tenant
-- enforce ở tầng application (routes/sections.py so tenant_id tường minh), đúng tiền lệ core.users
-- đã dùng (xem comment ngay trên CREATE TABLE core.users ở trên).
CREATE TABLE IF NOT EXISTS core.sections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES core.tenants(id),
    name TEXT NOT NULL,
    created_by UUID NOT NULL REFERENCES core.users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

-- `core.user_sections` (GAP-2, `G:\\My Drive\\ERD.drawio` — "1 user thuộc nhiều section, 1 section
-- nhiều user"). Shell — 0 reference nào tới `user_sections`/`UserSection` trong toàn bộ
-- `apps/studio/src` (đã verify bằng grep thật), nên đây là bảng SẠCH: chưa writer, chưa reader nào
-- có thể vỡ vì DDL này.
--
-- Khoá chính GHÉP `(user_id, section_id)`, không có cột `id` riêng — đúng ERD (cả 2 cột đều
-- 🔑, không cột nào khác). Không `tenant_id`: tenant của 1 hàng suy được qua JOIN `core.users`/
-- `core.sections` (cả hai đều đã có `tenant_id NOT NULL`), thêm cột trùng lặp ở đây chỉ tạo nguy cơ
-- lệch dữ liệu (2 nguồn cho cùng 1 sự thật) mà ERD cũng không vẽ.
--
-- KHÔNG RLS — cùng lý do `core.users`/`core.sections` (comment ngay phía trên 2 bảng đó): superadmin
-- cần gán/xem section CHÉO tenant (JWT của họ trỏ `__system__`), RLS theo `app.tenant_id` sẽ tự khoá
-- nhầm chính superadmin. Ranh giới tenant enforce ở tầng application, đúng tiền lệ.
--
-- `ON DELETE CASCADE` cả 2 chiều: xoá user hoặc xoá section thì gỡ luôn phần gán liên quan — không
-- để lại hàng mồ côi trỏ vào 1 user/section không còn tồn tại (cùng khuôn
-- `wb.recipe_versions.recipe_id` đã dùng).
CREATE TABLE IF NOT EXISTS core.user_sections (
    user_id UUID NOT NULL REFERENCES core.users (id) ON DELETE CASCADE,
    section_id UUID NOT NULL REFERENCES core.sections (id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, section_id)
);

CREATE TABLE IF NOT EXISTS core.jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    idempotency_key TEXT NOT NULL,
    job_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 5,
    leased_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS core.outbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    dispatched_at TIMESTAMPTZ
);
"""

# Direct-import seam (antichain, plan.md "Dependency matrix + file-ownership"): P5/P7/P8 fill in
# their own module's `ddl()` body without ever touching this file. `obs` (P4, composition-owned)
# joins this tuple here, at the phase that adds `studio_app.obs.schema`.
_QUADRANT_SCHEMA_MODULES: tuple[ModuleType, ...] = (
    _evalhub_schema,
    _kb_schema,
    _obs_schema,
    _workbench_schema,
)

# All 5 schemas this kit's composition root is responsible for granting `studio_app` access to
# (F6) — fixed closed set, not user input, but quoted via sql.Identifier anyway (correctness, not
# defense against untrusted input).
ALL_SCHEMAS: tuple[str, ...] = ("kb", "wb", "obs", "eval", "core")


def ddl() -> str:
    """This app's own (`core.*`) idempotent DDL."""
    return _CORE_DDL


async def ensure_all_schemas(admin_pool: Pool) -> None:
    """Run `core.ddl()` then every quadrant's `ddl()` (sorted by module name), via the ADMIN
    (studio_owner) pool. Idempotent: safe to call twice (CREATE ... IF NOT EXISTS throughout)."""
    async with admin_pool.connection() as conn:
        await conn.execute(ddl())
        for module in sorted(_QUADRANT_SCHEMA_MODULES, key=lambda m: m.__name__):
            quadrant_ddl = module.ddl()
            if quadrant_ddl:
                await conn.execute(quadrant_ddl)


async def grant_app_privileges(admin_pool: Pool) -> None:
    """Centralized cross-schema GRANT (F6) — call ONCE, right after `ensure_all_schemas`.

    Deliberately NOT scattered per-owner: R-DI §2/§7 documents the pain that led document-intake
    to abandon schema-per-module in favor of one consolidated schema. This kit keeps
    schema-per-quadrant (Decision #4) but avoids that same pain by centralizing the GRANT step
    here instead of asking each of the 4 quadrant owners to manage their own grants.

    Only grants on schemas that already EXIST: `kb`/`wb`/`eval` are still stub-empty `ddl()`
    (P5/P7/P8 fill them in later) so their schema does not exist yet at P3 — granting on a
    missing schema would error. This function is idempotent and re-run at every boot (and every
    `admin_pool` test fixture), so it naturally starts covering each quadrant's schema the moment
    that quadrant's `ddl()` creates it — no further change needed here when that happens.
    """
    async with admin_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT nspname FROM pg_catalog.pg_namespace WHERE nspname = ANY(%s::text[])",
            (list(ALL_SCHEMAS),),
        )
        existing_schemas = {row[0] for row in await cur.fetchall()}
        for schema in ALL_SCHEMAS:
            if schema not in existing_schemas:
                continue
            schema_id = sql.Identifier(schema)
            await conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO studio_app").format(schema_id))
            await conn.execute(
                sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO studio_app").format(
                    schema_id
                )
            )
            # Tables created AFTER this GRANT runs (later phases add tables to a schema that
            # already exists) still need privileges — ALTER DEFAULT PRIVILEGES covers that.
            await conn.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE studio_owner IN SCHEMA {} "
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO studio_app"
                ).format(schema_id)
            )


async def grant_scorer_privileges(admin_pool: Pool) -> None:
    """Least-privilege GRANT cho `studio_scorer` — ĐÚNG `SELECT`, ĐÚNG `obs.trace_events`.

    Vì sao role này tồn tại (`evalhub#37`, hệ quả GAP-1 của chính repo này). `obs/schema.py` bật
    `ENABLE`+`FORCE ROW LEVEL SECURITY` trên `obs.trace_events` với policy
    `tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid`. Hai hàm chấm điểm của
    evalhub — `read_run_unscoped`/`list_runs_all_tenants` — **cố ý** không set `app.tenant_id`: chúng
    phải nhìn thấy event của MỌI tenant thì `tenant_scope_ok` mới bắt được run có node đầu mang
    tenant A còn node sau mang tenant B. Dưới policy đó, role không `BYPASSRLS` khớp 0 dòng và nhận
    `[]` **im lặng**. Chính khối DDL bên `obs/schema.py` đã ghi trước hệ quả này và giao lại cho AIE-2.

    **Vì sao KHÔNG gộp vào `grant_app_privileges`.** Hàm kia cấp `SELECT, INSERT, UPDATE, DELETE`
    trên MỌI bảng của cả 5 schema, cộng `ALTER DEFAULT PRIVILEGES` để bảng tương lai tự có quyền.
    Đúng cho `studio_app` — role đó bị RLS chặn nên quyền rộng vẫn nằm trong hàng rào. **Sai hoàn
    toàn** cho `studio_scorer`: role này có `BYPASSRLS`, nên với nó KHÔNG có policy nào chặn, và
    quyền bảng là lưới **duy nhất** còn lại. Một `ALTER DEFAULT PRIVILEGES` ở đây sẽ lặng lẽ cấp
    quyền xuyên tenant trên mọi bảng ai đó tạo sau này. Vì thế hàm này liệt kê **tường minh** một
    bảng, không lặp `ALL_SCHEMAS`, và cố ý **không** có `ALTER DEFAULT PRIVILEGES`.

    Bảng chưa tồn tại ⇒ no-op im lặng, không raise: cùng khuôn `grant_app_privileges` (hàm này chạy
    ở mọi lần boot và mọi fixture `admin_pool`, nên nó tự bắt đầu có tác dụng đúng lúc `obs.ddl()`
    tạo bảng). Role chưa tồn tại ⇒ cũng no-op: `docker/postgres-init/00-roles.sql` chỉ chạy ở
    initdb, nên một volume có sẵn từ trước `evalhub#37` sẽ chưa có `studio_scorer` — boot không được
    phép chết vì chuyện đó, evalhub đã fail-closed sẵn ở phía nó (`UnscopedReadUnavailable`).
    """
    async with admin_pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'studio_scorer'")
        if await cur.fetchone() is None:
            return
        cur = await conn.execute("SELECT to_regclass('obs.trace_events')")
        row = await cur.fetchone()
        if row is None or row[0] is None:
            return
        await conn.execute("GRANT USAGE ON SCHEMA obs TO studio_scorer")
        await conn.execute("GRANT SELECT ON obs.trace_events TO studio_scorer")
