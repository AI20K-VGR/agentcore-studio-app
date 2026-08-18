"""`routes/sections.py` — CRUD `core.sections` theo tenant. Cùng convention gọi thẳng hàm route +
set ContextVar tay đã dùng ở `test_admin_routes.py` (chưa có tiền lệ `TestClient` trong repo cho
loại test này, xem docstring `test_http_asgi.py`)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from pydantic import ValidationError
from studio_app import middleware
from studio_app.core._db import Pool, close_pools, get_pool
from studio_app.routes.sections import (
    CreateSectionRequest,
    RenameSectionRequest,
    create_section,
    delete_section,
    list_sections,
    rename_section,
)
from studio_workbench.tenant_wall import ResolvedContext


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    yield
    await close_pools()


def _set_session(*, tenant_id: UUID, user: str, roles: list[str]) -> object:
    session = ResolvedContext(tenant_id=tenant_id, user=user, roles=roles)
    return middleware._request_session.set(session)


@asynccontextmanager
async def _simulate_request_connection() -> AsyncIterator[None]:
    pool = await get_pool()
    async with pool.connection() as conn:
        token = middleware._request_conn.set(conn)
        try:
            yield
        finally:
            middleware._request_conn.reset(token)


async def _seed_tenant(admin_pool: Pool, name: str) -> UUID:
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", (name,))
        row = await cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


async def _seed_user(admin_pool: Pool, tenant_id: UUID, email: str, roles: list[str]) -> UUID:
    async with admin_pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, roles) VALUES (%s, %s, %s, %s) RETURNING id",
            (str(tenant_id), email, "not-a-real-hash", roles),
        )
        row = await cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


async def test_only_superadmin_can_create_section(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "sections-probe-a")
    await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await create_section(CreateSectionRequest(tenant_id=str(tenant_id), name="hr"))
        assert exc_info.value.status_code == 403
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_superadmin_creates_section(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "sections-probe-b")
    await _seed_user(admin_pool, tenant_id, "su@sys", ["superadmin"])

    token = _set_session(tenant_id=tenant_id, user="su@sys", roles=["superadmin"])
    try:
        async with _simulate_request_connection():
            result = await create_section(CreateSectionRequest(tenant_id=str(tenant_id), name="finance"))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert result.name == "finance"
    assert result.tenant_id == str(tenant_id)


@pytest.mark.parametrize("reserved_name", ["admin", "superadmin"])
def test_create_section_rejects_reserved_name(reserved_name: str) -> None:
    """Tầng 1 của bản vá `app#21` ⛔ — `name` trùng 1 role hệ thống bị chặn ngay ở Pydantic
    (422 khi qua HTTP thật), TRƯỚC khi có cơ hội chèn vào `core.sections` rồi lọt vào `valid_role_
    vocab` (`routes/admin.py::create_user`). Test THUẦN model, không cần DB — cùng
    `test_create_section_rejects_reserved_name_on_rename` bên dưới cho `RenameSectionRequest`."""
    with pytest.raises(ValidationError):
        CreateSectionRequest(tenant_id=str(uuid4()), name=reserved_name)


@pytest.mark.parametrize("reserved_name", ["admin", "superadmin"])
def test_rename_section_rejects_reserved_name(reserved_name: str) -> None:
    with pytest.raises(ValidationError):
        RenameSectionRequest(name=reserved_name)


async def test_create_section_rejects_unknown_tenant(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "sections-probe-c")
    await _seed_user(admin_pool, tenant_id, "su@sys", ["superadmin"])

    token = _set_session(tenant_id=tenant_id, user="su@sys", roles=["superadmin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await create_section(CreateSectionRequest(tenant_id=str(uuid4()), name="hr"))
        assert exc_info.value.status_code == 404
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_create_section_rejects_duplicate_name_same_tenant(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "sections-probe-d")
    await _seed_user(admin_pool, tenant_id, "su@sys", ["superadmin"])

    token = _set_session(tenant_id=tenant_id, user="su@sys", roles=["superadmin"])
    try:
        async with _simulate_request_connection():
            await create_section(CreateSectionRequest(tenant_id=str(tenant_id), name="hr"))
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await create_section(CreateSectionRequest(tenant_id=str(tenant_id), name="hr"))
        assert exc_info.value.status_code == 409
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_two_tenants_can_share_section_name(admin_pool: Pool) -> None:
    """`UNIQUE(tenant_id, name)` — trùng tên GIỮA 2 tenant khác nhau phải hợp lệ, chỉ trùng
    TRONG CÙNG 1 tenant mới bị chặn."""
    tenant_a = await _seed_tenant(admin_pool, "sections-probe-e-a")
    tenant_b = await _seed_tenant(admin_pool, "sections-probe-e-b")
    await _seed_user(admin_pool, tenant_a, "su@sys", ["superadmin"])

    token = _set_session(tenant_id=tenant_a, user="su@sys", roles=["superadmin"])
    try:
        async with _simulate_request_connection():
            await create_section(CreateSectionRequest(tenant_id=str(tenant_a), name="hr"))
        async with _simulate_request_connection():
            result = await create_section(CreateSectionRequest(tenant_id=str(tenant_b), name="hr"))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert result.name == "hr"
    assert result.tenant_id == str(tenant_b)


async def test_company_admin_cannot_see_other_tenant_sections(admin_pool: Pool) -> None:
    tenant_a = await _seed_tenant(admin_pool, "sections-probe-f-a")
    tenant_b = await _seed_tenant(admin_pool, "sections-probe-f-b")
    admin_a_id = await _seed_user(admin_pool, tenant_a, "admin-a@acme.com", ["admin"])
    async with admin_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO core.sections (tenant_id, name, created_by) VALUES (%s, %s, %s)",
            (str(tenant_b), "hr", str(admin_a_id)),
        )

    token = _set_session(tenant_id=tenant_a, user="admin-a@acme.com", roles=["admin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await list_sections(tenant_id=str(tenant_b))
        assert exc_info.value.status_code == 403
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_company_admin_lists_own_tenant_sections(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "sections-probe-g")
    admin_id = await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])
    async with admin_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO core.sections (tenant_id, name, created_by) VALUES (%s, %s, %s)",
            (str(tenant_id), "engineering", str(admin_id)),
        )

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        async with _simulate_request_connection():
            result = await list_sections(tenant_id=None)
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert [s.name for s in result] == ["engineering"]


async def test_rename_section_cascades_to_user_roles(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "sections-probe-h")
    su_id = await _seed_user(admin_pool, tenant_id, "su@sys", ["superadmin"])
    await _seed_user(admin_pool, tenant_id, "hr-user@acme.com", ["hr"])
    async with admin_pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO core.sections (tenant_id, name, created_by) VALUES (%s, %s, %s) RETURNING id",
            (str(tenant_id), "hr", str(su_id)),
        )
        row = await cur.fetchone()
    assert row is not None
    section_id = row[0]

    token = _set_session(tenant_id=tenant_id, user="su@sys", roles=["superadmin"])
    try:
        async with _simulate_request_connection():
            result = await rename_section(str(section_id), RenameSectionRequest(name="people"))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert result.name == "people"
    async with admin_pool.connection() as conn:
        cur = await conn.execute("SELECT roles FROM core.users WHERE email = %s", ("hr-user@acme.com",))
        row = await cur.fetchone()
    assert row is not None
    assert list(row[0]) == ["people"], "role cũ 'hr' phải được thay bằng 'people' sau rename"


async def test_delete_section_blocked_when_user_still_assigned(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "sections-probe-i")
    su_id = await _seed_user(admin_pool, tenant_id, "su@sys", ["superadmin"])
    await _seed_user(admin_pool, tenant_id, "finance-user@acme.com", ["finance"])
    async with admin_pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO core.sections (tenant_id, name, created_by) VALUES (%s, %s, %s) RETURNING id",
            (str(tenant_id), "finance", str(su_id)),
        )
        row = await cur.fetchone()
    assert row is not None
    section_id = row[0]

    token = _set_session(tenant_id=tenant_id, user="su@sys", roles=["superadmin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await delete_section(str(section_id))
        assert exc_info.value.status_code == 409
        detail = exc_info.value.detail
        # `HTTPException.detail` khai `str | None` ở type stub — thu hẹp kiểu bằng `isinstance`
        # thật (mypy hiểu được, không cần `type: ignore`) thay vì ép kiểu mù — đúng thứ route
        # `delete_section` trả thật lúc 409 (dict, không phải chuỗi).
        assert isinstance(detail, dict)
        assert detail["user_count"] == 1
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_delete_section_succeeds_when_unused(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "sections-probe-j")
    su_id = await _seed_user(admin_pool, tenant_id, "su@sys", ["superadmin"])
    async with admin_pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO core.sections (tenant_id, name, created_by) VALUES (%s, %s, %s) RETURNING id",
            (str(tenant_id), "unused-section", str(su_id)),
        )
        row = await cur.fetchone()
    assert row is not None
    section_id = row[0]

    token = _set_session(tenant_id=tenant_id, user="su@sys", roles=["superadmin"])
    try:
        async with _simulate_request_connection():
            await delete_section(str(section_id))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    async with admin_pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM core.sections WHERE id = %s", (str(section_id),))
        row = await cur.fetchone()
    assert row is None
