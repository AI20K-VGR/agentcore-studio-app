"""Phase 5 gate test (F3) — the tenant-context middleware's resolution seam maps an inbound
`x-tenant-id` header (a human NAME or a raw UUID) to the immutable `core.tenants.id` UUID, and
fail-closes to None for anything it cannot resolve. This is what keeps `SET LOCAL app.tenant_id`
a valid UUID so the kb RLS policy's `::uuid` cast never crashes the request (D-13). Needs a live
DB (root conftest `admin_pool` fixture)."""

from __future__ import annotations

from typing import Any

from studio_app.core._db import Pool
from studio_app.middleware import _resolve_tenant_id


class _FakeHeaders:
    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    def get(self, key: str) -> str | None:
        return self._data.get(key)


class _FakeRequest:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = _FakeHeaders(headers)


async def test_resolve_tenant_header_name_or_uuid_to_immutable_uuid(admin_pool: Pool) -> None:
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", ("ankor",))
        row = await cur.fetchone()
        assert row is not None
        ankor_id = row[0]

        # Header carries the human NAME → resolves to the immutable UUID.
        by_name = await _resolve_tenant_id(_FakeRequest({"x-tenant-id": "ankor"}), conn)  # type: ignore[arg-type]
        assert by_name == str(ankor_id)

        # Header carries the UUID directly → passes through.
        by_uuid = await _resolve_tenant_id(_FakeRequest({"x-tenant-id": str(ankor_id)}), conn)  # type: ignore[arg-type]
        assert by_uuid == str(ankor_id)

        # Unknown name → None (fail-closed) — never returns a slug that would crash `::uuid`.
        unknown = await _resolve_tenant_id(_FakeRequest({"x-tenant-id": "nope"}), conn)  # type: ignore[arg-type]
        assert unknown is None

        # No header at all → None.
        absent: Any = await _resolve_tenant_id(_FakeRequest({}), conn)  # type: ignore[arg-type]
        assert absent is None


async def test_resolve_tenant_runs_on_non_owner_app_pool(admin_pool: Pool, pool: Pool) -> None:
    """Production resolves on the `studio_app` (non-owner) request connection, NOT the owner pool.
    This proves `grant_app_privileges` actually gave studio_app SELECT on `core.tenants` — the
    resolver would silently fail-closed (None) for every request if that grant were missing."""
    async with admin_pool.connection() as admin_conn:
        cur = await admin_conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", ("borea",))
        row = await cur.fetchone()
        assert row is not None
        borea_id = row[0]

    async with pool.connection() as app_conn:
        resolved = await _resolve_tenant_id(_FakeRequest({"x-tenant-id": "borea"}), app_conn)  # type: ignore[arg-type]
        assert resolved == str(borea_id)
