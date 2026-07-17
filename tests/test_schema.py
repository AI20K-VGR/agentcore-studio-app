"""Phase 3 gate tests — schema idempotency, pool-split roles, centralized grant (F1/F2/F6) +
app-boot smoke. Needs a live DB: `docker compose -f docker-compose.test.yml up -d` (port 5433,
db `studio_test`) with `STUDIO_DATABASE_URL`/`STUDIO_DATABASE_URL_ADMIN` pointed at it — the
`admin_pool`/`pool` fixtures (root conftest.py) skip individually when those env vars are unset.
"""

from __future__ import annotations

import os

import pytest
from studio_app.core._db import Pool
from studio_app.core.schema import ensure_all_schemas, grant_app_privileges


async def test_ensure_and_grant_idempotent(admin_pool: Pool) -> None:
    """KHÓA: ensure_all_schemas + grant_app_privileges chạy 2 lần liên tiếp không lỗi (F6)."""
    await ensure_all_schemas(admin_pool)
    await grant_app_privileges(admin_pool)
    await ensure_all_schemas(admin_pool)
    await grant_app_privileges(admin_pool)


async def test_pool_split_roles() -> None:
    """KHÓA: get_admin_pool() nối studio_owner, get_pool() nối studio_app — 2 role khác nhau
    (F1/F2 — bắt regression dùng 1 pool cho cả DDL lẫn runtime, khiến RLS không áp)."""
    if not os.environ.get("STUDIO_DATABASE_URL_ADMIN") or not os.environ.get("STUDIO_DATABASE_URL"):
        pytest.skip("STUDIO_DATABASE_URL(_ADMIN) not set — needs docker-compose.test.yml up")

    from studio_app.core._db import close_pools, get_admin_pool, get_pool

    try:
        admin = await get_admin_pool()
        app_pool = await get_pool()

        async with admin.connection() as conn:
            row = await (await conn.execute("SELECT current_user")).fetchone()
        assert row is not None
        assert row[0] == "studio_owner"

        async with app_pool.connection() as conn:
            row = await (await conn.execute("SELECT current_user")).fetchone()
        assert row is not None
        assert row[0] == "studio_app"
    finally:
        await close_pools()


async def test_app_role_can_dml_after_grant(admin_pool: Pool, pool: Pool) -> None:
    """KHÓA: sau grant_app_privileges, studio_app INSERT+SELECT được core.tenants (F6 — GRANT
    tập trung đủ quyền, không rải cho 4 owner)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO core.tenants (name) VALUES (%s) RETURNING id",
            ("acme-corp",),
        )
        inserted = await cur.fetchone()
        assert inserted is not None
        tenant_id = inserted[0]

        cur = await conn.execute("SELECT name FROM core.tenants WHERE id = %s", (tenant_id,))
        selected = await cur.fetchone()
    assert selected is not None
    assert selected[0] == "acme-corp"


async def test_app_boot_runs_ddl_and_grant_via_lifespan() -> None:
    """Tests-After app-boot smoke: create_app()'s lifespan runs ensure_all_schemas +
    grant_app_privileges through the ADMIN pool without raising, and closes pools cleanly on
    shutdown."""
    if not os.environ.get("STUDIO_DATABASE_URL_ADMIN") or not os.environ.get("STUDIO_DATABASE_URL"):
        pytest.skip("STUDIO_DATABASE_URL(_ADMIN) not set — needs docker-compose.test.yml up")

    from studio_app.app import create_app

    app = create_app()
    async with app.router.lifespan_context(app):
        pass
