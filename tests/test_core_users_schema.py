"""`core.users` (Kế hoạch 3, auth thật) — DDL idempotent, quyền `studio_app` DML sau grant, và
`scripts/seed_superadmin.py` (bootstrap ngoài luồng API, không endpoint nào tạo được superadmin).
Cùng khuôn `test_schema.py::test_app_role_can_dml_after_grant`."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from studio_app.core._db import Pool, close_pools
from studio_app.core.schema import ensure_all_schemas
from studio_app.jwt_auth import hash_password, verify_password


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    yield
    await close_pools()


async def test_core_users_ddl_idempotent(admin_pool: Pool) -> None:
    """KHÓA: ensure_all_schemas chạy 2 lần liên tiếp không lỗi (cùng bất biến F6 đã áp cho
    core.tenants/jobs/outbox, giờ áp thêm cho core.users)."""
    await ensure_all_schemas(admin_pool)
    await ensure_all_schemas(admin_pool)


async def test_app_role_can_dml_core_users_after_grant(admin_pool: Pool, pool: Pool) -> None:
    """KHÓA: sau grant_app_privileges, studio_app INSERT+SELECT được core.users — `core` đã nằm
    trong ALL_SCHEMAS nên bảng mới tự động được GRANT, không cần sửa gì ở schema.py ngoài DDL."""
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", ("dml-probe-co",))
        row = await cur.fetchone()
        assert row is not None
        tenant_id = row[0]

    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, system_roles) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (tenant_id, "dml-probe@example.com", hash_password("whatever"), ["admin"]),
        )
        inserted = await cur.fetchone()
        assert inserted is not None

        cur = await conn.execute("SELECT email FROM core.users WHERE id = %s", (inserted[0],))
        selected = await cur.fetchone()
    assert selected is not None
    assert selected[0] == "dml-probe@example.com"


def test_hash_password_roundtrips_and_rejects_wrong_password() -> None:
    hashed = hash_password("correct-horse-battery-staple")
    assert verify_password("correct-horse-battery-staple", hashed) is True
    assert verify_password("wrong-password", hashed) is False


def test_hash_password_never_stores_plaintext() -> None:
    """Mutant thật cần bắt: nếu ai đó lỡ sửa hash_password() thành trả về nguyên plaintext (bug
    dễ mắc khi refactor vội), bài này phải đỏ."""
    plain = "correct-horse-battery-staple"
    assert hash_password(plain) != plain


async def test_core_users_email_insert_is_idempotent(admin_pool: Pool) -> None:
    """`scripts/seed_superadmin.py` KHÔNG import được vào test (repo này cố tình giữ `scripts/`
    đứng ngoài mọi import — trùng lặp nhỏ có chủ đích, cùng lý do `_CallistoEmbedding` trùng giữa
    `scripts/e2e_smoke_eval.py` và `routes/*.py`, xem `test_middleware_jwt.py:4`). Test thẳng bất
    biến DB mà script đó dựa vào: `ON CONFLICT (email) DO NOTHING` trên `core.users` — chạy 2 lần
    liên tiếp cùng email chỉ tạo đúng 1 dòng, verify bằng COUNT thật."""
    async with admin_pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO core.tenants (name) VALUES (%s) "
            "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name RETURNING id",
            ("idempotent-probe-tenant",),
        )
        row = await cur.fetchone()
        assert row is not None
        tenant_id = row[0]

        insert_sql = (
            "INSERT INTO core.users (tenant_id, email, password_hash, system_roles) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (email) DO NOTHING"
        )
        params = (tenant_id, "idempotent-probe@agentcore.internal", hash_password("whatever"), ["superadmin"])
        await conn.execute(insert_sql, params)
        await conn.execute(insert_sql, params)  # lần 2 — phải là no-op

        cur = await conn.execute(
            "SELECT count(*) FROM core.users WHERE email = %s", ("idempotent-probe@agentcore.internal",)
        )
        count_row = await cur.fetchone()
    assert count_row is not None
    assert count_row[0] == 1
