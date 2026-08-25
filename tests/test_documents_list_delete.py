"""`routes/documents.py` — phần đọc/xoá của tab Tài liệu (`GET /api/admin/documents`,
`POST /api/admin/documents/delete`).

Bài ở đây cố ý CHỈ đo hai thứ thuần, chạy được không cần Postgres: cách suy tên hiển thị, và chốt
chặn embedding trên đường chỉ-đọc. Phần route đi qua DB theo đúng khuôn `test_documents_routes.py`
(skip khi thiếu `STUDIO_DATABASE_URL_ADMIN`) — không dựng khuôn thứ hai cho cùng một loại test.
"""

from __future__ import annotations

import pytest
from studio_app.providers.factory import ReadOnlyEmbedding
from studio_app.routes.documents import _display_name


def test_display_name_bo_dung_tien_to_phong_ban() -> None:
    assert _display_name("hr-chinh-sach-nghi-phep", "hr") == "chinh-sach-nghi-phep"


def test_display_name_khong_cat_nham_khi_slug_phong_ban_co_gach() -> None:
    """Vế đắt của hàm này. `doc_id = f"{slug(section_role)}-{slug(stem)}"`, mà slug của tên phòng
    ban thường CÓ gạch (`"Nhan su"` → `"nhan-su"`). Cắt bằng `split("-", 1)` sẽ ra `"su-chinh-sach"`
    — tên tài liệu dính một mảnh tên phòng ban, và nhìn vào không ai đọc ra là sai."""
    assert _display_name("nhan-su-chinh-sach", "Nhan su") == "chinh-sach"


def test_display_name_giu_nguyen_khi_khong_khop_tien_to() -> None:
    """Dòng cũ (ghi trước khi khoá `doc_id` mang `section_role`, app#58) không có tiền tố nào để
    cắt. Trả nguyên còn hơn trả rỗng: một dòng trống trên bảng KB là thứ người dùng không hành
    động được."""
    assert _display_name("chinh-sach", "hr") == "chinh-sach"


async def test_read_only_embedding_nem_neu_bi_goi() -> None:
    """Chốt chặn, không phải bài hình thức: `KbPipeline` đòi một embedding ở constructor nhưng
    đường đọc/xoá không embed gì. Nếu sau này ai thêm một lời gọi có embed vào hai route đó, bài
    này (và chính lần chạy thật) phải đỏ ngay, thay vì âm thầm ghi vector rác vào `kb.chunks`."""
    with pytest.raises(AssertionError, match="không được embed"):
        await ReadOnlyEmbedding().embed(["a", "b"])


# ── Route đi qua DB thật (khuôn `test_documents_routes.py`) ──────────────────────────────────
from collections.abc import AsyncIterator  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402
from io import BytesIO  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402

import pytest_asyncio  # noqa: E402
from fastapi import HTTPException, UploadFile  # noqa: E402
from studio_app import middleware  # noqa: E402
from studio_app.core._db import Pool, close_pools, get_pool  # noqa: E402
from studio_app.routes.documents import (  # noqa: E402
    DeleteDocumentsRequest,  # noqa: E402
    delete_documents,
    list_documents,
    upload_document,
)
from studio_workbench.tenant_wall import ResolvedContext  # noqa: E402

_MD = b"# Tai lieu\n\n## Muc mot\nNoi dung mot hai ba bon nam sau bay tam chin muoi.\n"


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    yield
    await close_pools()


def _set_session(*, tenant_id: UUID, user: str, system_roles: list[str]) -> object:
    return middleware._request_session.set(ResolvedContext(tenant_id=tenant_id, user=user, system_roles=system_roles))


@asynccontextmanager
async def _request_conn() -> AsyncIterator[None]:
    pool = await get_pool()
    async with pool.connection() as conn:
        token = middleware._request_conn.set(conn)
        try:
            yield
        finally:
            middleware._request_conn.reset(token)


async def _seed(admin_pool: Pool, tenant_name: str, sections: list[str]) -> tuple[UUID, str]:
    email = f"admin@{tenant_name}.test"
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", (tenant_name,))
        row = await cur.fetchone()
        assert row is not None
        tenant_id = UUID(str(row[0]))
        cur = await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, system_roles) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (tenant_id, email, "x", ["admin"]),
        )
        row = await cur.fetchone()
        assert row is not None
        for name in sections:
            await conn.execute(
                "INSERT INTO core.sections (tenant_id, name, created_by) VALUES (%s, %s, %s)",
                (tenant_id, name, UUID(str(row[0]))),
            )
    return tenant_id, email


def _upload_file(filename: str) -> UploadFile:
    return UploadFile(filename=filename, file=BytesIO(_MD))


async def _upload(filename: str, section: str) -> None:
    async with _request_conn():
        await upload_document(file=_upload_file(filename), section_role=section, tenant_id=None)


async def test_list_groups_by_document_with_chunk_counts(admin_pool: Pool) -> None:
    """Hai tài liệu ở hai phòng ban ⇒ hai dòng, mỗi dòng đếm đúng số đoạn của riêng nó, và
    `total_chunks` là tổng THẬT của tenant chứ không phải tổng của `documents`."""
    tenant_id, email = await _seed(admin_pool, f"list-{uuid4().hex[:8]}", ["hr", "finance"])
    token = _set_session(tenant_id=tenant_id, user=email, system_roles=["admin"])
    try:
        await _upload("Chinh sach.md", "hr")
        await _upload("Bao cao.md", "finance")
        async with _request_conn():
            result = await list_documents(tenant_id=None)
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    by_role = {d.section_role: d for d in result.documents}
    assert set(by_role) == {"hr", "finance"}
    assert result.total_chunks == sum(d.chunk_count for d in result.documents)
    assert all(d.chunk_count > 0 for d in result.documents)
    # Tên hiển thị KHÔNG được là `doc_id` — đó là toàn bộ điểm của cột `name`.
    assert by_role["hr"].name != by_role["hr"].id


async def test_delete_removes_only_selected_documents(admin_pool: Pool) -> None:
    """Vế đắt: tài liệu KHÔNG được chọn phải còn nguyên. Một lệnh xoá quét sạch cả tenant vẫn làm
    bài "đã xoá cái được chọn" xanh — nên phải kiểm cả chiều còn lại."""
    tenant_id, email = await _seed(admin_pool, f"del-{uuid4().hex[:8]}", ["hr", "finance"])
    token = _set_session(tenant_id=tenant_id, user=email, system_roles=["admin"])
    try:
        await _upload("Chinh sach.md", "hr")
        await _upload("Bao cao.md", "finance")
        async with _request_conn():
            before = await list_documents(tenant_id=None)
            target = next(d for d in before.documents if d.section_role == "hr")
            deleted = await delete_documents(DeleteDocumentsRequest(ids=[target.id]), tenant_id=None)
            after = await list_documents(tenant_id=None)
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert deleted.deleted_documents == [target.id]
    assert deleted.deleted_chunks == target.chunk_count
    assert deleted.not_found == []
    assert [d.section_role for d in after.documents] == ["finance"], "tài liệu KHÔNG chọn bị xoá lây"


async def test_delete_reports_not_found_instead_of_silent_success(admin_pool: Pool) -> None:
    """Id không xoá được đoạn nào phải vào `not_found`. Đây là ô mà giao diện dựa vào để KHÔNG báo
    "đã xoá" trong khi tài liệu còn nguyên — dòng ghi trước khi `kb.chunks` có cột `doc_id` rơi đúng
    vào ca này."""
    tenant_id, email = await _seed(admin_pool, f"nf-{uuid4().hex[:8]}", ["hr"])
    token = _set_session(tenant_id=tenant_id, user=email, system_roles=["admin"])
    try:
        await _upload("Chinh sach.md", "hr")
        async with _request_conn():
            result = await delete_documents(DeleteDocumentsRequest(ids=["hr-khong-ton-tai"]), tenant_id=None)
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert result.deleted_documents == []
    assert result.not_found == ["hr-khong-ton-tai"]
    assert result.deleted_chunks == 0


async def test_company_admin_cannot_see_or_delete_another_tenant(admin_pool: Pool) -> None:
    """Hàng rào tenant trên đường ĐỌC và đường XOÁ — không chỉ đường ghi.

    Một route đọc/xoá lỏng tay hơn route ghi là cách rò dữ liệu chéo tenant kinh điển, và đó là lý
    do `_resolve_target_tenant` được tách ra dùng chung cho cả ba route."""
    tenant_a, email_a = await _seed(admin_pool, f"ta-{uuid4().hex[:8]}", ["hr"])
    tenant_b, email_b = await _seed(admin_pool, f"tb-{uuid4().hex[:8]}", ["hr"])

    token = _set_session(tenant_id=tenant_a, user=email_a, system_roles=["admin"])
    try:
        await _upload("Chinh sach.md", "hr")
        async with _request_conn():
            a_docs = await list_documents(tenant_id=None)
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]
    a_id = a_docs.documents[0].id

    token = _set_session(tenant_id=tenant_b, user=email_b, system_roles=["admin"])
    try:
        async with _request_conn():
            b_docs = await list_documents(tenant_id=None)
            # B xoá bằng chính id của A: phải là `not_found`, KHÔNG được xoá được.
            attempt = await delete_documents(DeleteDocumentsRequest(ids=[a_id]), tenant_id=None)
            # B khai thẳng tenant của A ⇒ 403, không phải im lặng cho qua.
            with pytest.raises(HTTPException) as raised:
                await list_documents(tenant_id=str(tenant_a))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert b_docs.documents == [], "B thấy tài liệu của A"
    assert attempt.deleted_chunks == 0 and attempt.not_found == [a_id], "B xoá được tài liệu của A"
    assert raised.value.status_code == 403

    # Và tài liệu của A vẫn còn nguyên sau khi B thử.
    token = _set_session(tenant_id=tenant_a, user=email_a, system_roles=["admin"])
    try:
        async with _request_conn():
            still = await list_documents(tenant_id=None)
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]
    assert [d.id for d in still.documents] == [a_id]
