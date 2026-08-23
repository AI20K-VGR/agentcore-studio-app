"""`routes/documents.py` — cùng convention gọi thẳng hàm route + set `ContextVar` tay đã dùng ở
`test_sections_routes.py` (chưa có tiền lệ `TestClient` trong repo cho loại test này)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from io import BytesIO
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException, UploadFile
from studio_app import middleware
from studio_app.core._db import Pool, close_pools, get_pool
from studio_app.routes.documents import upload_document
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


async def _seed_section(admin_pool: Pool, tenant_id: UUID, name: str, created_by: UUID) -> None:
    async with admin_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO core.sections (tenant_id, name, created_by) VALUES (%s, %s, %s)",
            (str(tenant_id), name, str(created_by)),
        )


def _md_upload_file(filename: str, content: bytes) -> UploadFile:
    return UploadFile(filename=filename, file=BytesIO(content))


_VALID_MD = b"## Nghi phep\nBao truoc 3 ngay lam viec.\n"


async def test_company_admin_uploads_document(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "documents-probe-a")
    admin_id = await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])
    await _seed_section(admin_pool, tenant_id, "hr", admin_id)

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        async with _simulate_request_connection():
            result = await upload_document(
                file=_md_upload_file("leave.md", _VALID_MD),
                section_role="hr",
                tenant_id=None,
            )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert result.section_role == "hr"
    assert result.chunk_count == 1
    assert result.doc_id.startswith(tenant_id.hex)

    async with admin_pool.connection() as conn:
        # `kb.chunks` có `FORCE ROW LEVEL SECURITY` — cắn cả owner nếu chưa `set_config`
        # `app.tenant_id` (xem `postgres.py::_bind_tenant`), nên phải bind trước khi verify.
        await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
        cur = await conn.execute("SELECT count(*) FROM kb.chunks WHERE tenant_id = %s", (str(tenant_id),))
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 1


async def test_upload_rejects_unknown_section_role(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "documents-probe-b")
    await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await upload_document(
                    file=_md_upload_file("leave.md", _VALID_MD),
                    section_role="not-a-real-department",
                    tenant_id=None,
                )
        assert exc_info.value.status_code == 400
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_upload_rejects_unsupported_extension(admin_pool: Pool) -> None:
    """`.pdf` (ngoài `extract.SUPPORTED_SUFFIXES`) vẫn bị chặn — khác `.txt` (đã CHO PHÉP từ khi
    route chuyển sang `chunk_window.cut_window`, xem `test_upload_accepts_txt_file` bên dưới)."""
    tenant_id = await _seed_tenant(admin_pool, "documents-probe-c")
    admin_id = await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])
    await _seed_section(admin_pool, tenant_id, "hr", admin_id)

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await upload_document(
                    file=_md_upload_file("leave.pdf", _VALID_MD),
                    section_role="hr",
                    tenant_id=None,
                )
        assert exc_info.value.status_code == 422
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_upload_accepts_txt_file(admin_pool: Pool) -> None:
    """`.txt` — trước đây bị 422 ngay ở kiểm đuôi file (khi route chỉ nhận `.md`); giờ đi qua
    `extract.extract_text` + `chunk_window.cut_window`, không đòi hỏi cấu trúc heading `##`."""
    tenant_id = await _seed_tenant(admin_pool, "documents-probe-h")
    admin_id = await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])
    await _seed_section(admin_pool, tenant_id, "hr", admin_id)

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        async with _simulate_request_connection():
            result = await upload_document(
                file=_md_upload_file("ghi-chu.txt", b"Ghi chu roi rac, khong co heading nao ca."),
                section_role="hr",
                tenant_id=None,
            )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert result.chunk_count == 1


async def test_upload_accepts_md_without_heading(admin_pool: Pool) -> None:
    """Trước đây (`_cut_document`, cắt theo heading `##`) file `.md` không heading bị 422. Giờ route
    dùng `chunk_window.cut_window` (không đòi cấu trúc), nên phải THÀNH CÔNG — đúng động lực chính
    của thay đổi này: ghi chú `.md` thật thường không có heading chuẩn."""
    tenant_id = await _seed_tenant(admin_pool, "documents-probe-d")
    admin_id = await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])
    await _seed_section(admin_pool, tenant_id, "hr", admin_id)

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        async with _simulate_request_connection():
            result = await upload_document(
                file=_md_upload_file("leave.md", b"khong co heading nao ca"),
                section_role="hr",
                tenant_id=None,
            )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert result.chunk_count == 1


async def test_upload_accepts_docx_file(admin_pool: Pool) -> None:
    """`.docx` thật (dựng bằng `python-docx`, không phải giả `.md` đổi tên) — đi qua
    `extract._extract_docx` rồi `chunk_window.cut_window`, ghi thật vào `kb.chunks`."""
    from io import BytesIO as _BytesIO

    from docx import Document

    doc = Document()
    doc.add_paragraph("Chinh sach nghi phep.")
    doc.add_paragraph("Bao truoc it nhat 3 ngay lam viec.")
    buf = _BytesIO()
    doc.save(buf)

    tenant_id = await _seed_tenant(admin_pool, "documents-probe-i")
    admin_id = await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])
    await _seed_section(admin_pool, tenant_id, "hr", admin_id)

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        async with _simulate_request_connection():
            result = await upload_document(
                file=_md_upload_file("chinh-sach.docx", buf.getvalue()),
                section_role="hr",
                tenant_id=None,
            )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert result.chunk_count == 1

    async with admin_pool.connection() as conn:
        await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
        cur = await conn.execute("SELECT text FROM kb.chunks WHERE tenant_id = %s", (str(tenant_id),))
        row = await cur.fetchone()
    assert row is not None
    assert "Bao truoc it nhat 3 ngay lam viec." in row[0]


async def test_company_admin_cannot_upload_for_other_tenant(admin_pool: Pool) -> None:
    tenant_a = await _seed_tenant(admin_pool, "documents-probe-e-a")
    tenant_b = await _seed_tenant(admin_pool, "documents-probe-e-b")
    admin_a_id = await _seed_user(admin_pool, tenant_a, "admin-a@acme.com", ["admin"])
    await _seed_section(admin_pool, tenant_a, "hr", admin_a_id)

    token = _set_session(tenant_id=tenant_a, user="admin-a@acme.com", roles=["admin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await upload_document(
                    file=_md_upload_file("leave.md", _VALID_MD),
                    section_role="hr",
                    tenant_id=str(tenant_b),
                )
        assert exc_info.value.status_code == 403
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_superadmin_must_declare_tenant_id(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "documents-probe-f")
    await _seed_user(admin_pool, tenant_id, "su@sys", ["superadmin"])

    token = _set_session(tenant_id=tenant_id, user="su@sys", roles=["superadmin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await upload_document(
                    file=_md_upload_file("leave.md", _VALID_MD),
                    section_role="hr",
                    tenant_id=None,
                )
        assert exc_info.value.status_code == 400
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_upload_rejects_unknown_tenant_for_superadmin(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "documents-probe-g")
    await _seed_user(admin_pool, tenant_id, "su@sys", ["superadmin"])

    token = _set_session(tenant_id=tenant_id, user="su@sys", roles=["superadmin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection():
                await upload_document(
                    file=_md_upload_file("leave.md", _VALID_MD),
                    section_role="hr",
                    tenant_id=str(uuid4()),
                )
        assert exc_info.value.status_code == 404
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]
