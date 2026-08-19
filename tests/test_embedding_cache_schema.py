"""`core.embedding_cache` — DDL idempotency + shape + pool-split roundtrip (app#30 Phase 2).

Needs a live DB: `docker compose -f docker-compose.test.yml up -d` (port 5433, db `studio_test`)
with `STUDIO_DATABASE_URL`/`STUDIO_DATABASE_URL_ADMIN` pointed at it — the `admin_pool`/`pool`
fixtures (root conftest.py) skip individually when those env vars are unset (INV-4: CI never
fails for a missing DSN, only skips).
"""

from __future__ import annotations

from studio_app.core._db import Pool
from studio_app.core.schema import ensure_all_schemas

_DIM = 2048
_VECTOR_LITERAL = str([0.0] * _DIM)

_COLUMNS_SQL = """
SELECT column_name, udt_name, is_nullable
  FROM information_schema.columns
 WHERE table_schema = 'core' AND table_name = 'embedding_cache'
 ORDER BY ordinal_position
"""

_EMBEDDING_TYPMOD_SQL = """
SELECT atttypmod FROM pg_attribute
 WHERE attrelid = 'core.embedding_cache'::regclass AND attname = 'embedding' AND NOT attisdropped
"""


async def test_embedding_cache_table_exists(admin_pool: Pool) -> None:
    """KHÓA: 5 cột đúng tên/kiểu, `embedding` là `vector(2048)` — chưa migrate thì bảng không
    tồn tại, query information_schema trả rỗng."""
    async with admin_pool.connection() as conn:
        cur = await conn.execute(_COLUMNS_SQL)
        rows = await cur.fetchall()
        columns = {name: (udt, nullable) for name, udt, nullable in rows}

        assert columns.keys() == {"cache_key", "model", "dim", "embedding", "created_at"}
        assert columns["cache_key"] == ("text", "NO")
        assert columns["model"] == ("text", "NO")
        assert columns["dim"] == ("int4", "NO")
        assert columns["embedding"] == ("vector", "NO")
        assert columns["created_at"][1] == "NO"

        typmod_cur = await conn.execute(_EMBEDDING_TYPMOD_SQL)
        typmod_row = await typmod_cur.fetchone()
        assert typmod_row is not None
        assert typmod_row[0] == _DIM


async def test_ddl_is_idempotent(admin_pool: Pool) -> None:
    """KHÓA: `ensure_all_schemas` chạy hai lần liên tiếp không raise (CREATE ... IF NOT EXISTS)."""
    await ensure_all_schemas(admin_pool)
    await ensure_all_schemas(admin_pool)


async def test_roundtrip_via_app_pool(admin_pool: Pool, pool: Pool) -> None:
    """KHÓA: bảng cache KHÔNG bị RLS fence (QĐ-2 cố ý không RLS) — ghi qua studio_owner, đọc lại
    được qua studio_app (đường runtime thật)."""
    async with admin_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO core.embedding_cache (cache_key, model, dim, embedding) VALUES (%s, %s, %s, %s)",
            ("roundtrip-key", "google/gemini-embedding-001", _DIM, _VECTOR_LITERAL),
        )

    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT model, dim FROM core.embedding_cache WHERE cache_key = %s",
            ("roundtrip-key",),
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "google/gemini-embedding-001"
    assert row[1] == _DIM


async def test_insert_on_conflict_do_nothing(admin_pool: Pool) -> None:
    """KHÓA: `ON CONFLICT (cache_key) DO NOTHING` — hành vi P3's GatewayEmbedding sẽ dựa vào để
    ghi cache mà không cần transaction round-trip kiểm tồn tại trước."""
    insert_sql = (
        "INSERT INTO core.embedding_cache (cache_key, model, dim, embedding) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (cache_key) DO NOTHING"
    )
    async with admin_pool.connection() as conn:
        await conn.execute(insert_sql, ("dup-key", "google/gemini-embedding-001", _DIM, _VECTOR_LITERAL))
        await conn.execute(insert_sql, ("dup-key", "google/gemini-embedding-001", _DIM, _VECTOR_LITERAL))

        cur = await conn.execute("SELECT count(*) FROM core.embedding_cache WHERE cache_key = %s", ("dup-key",))
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 1
