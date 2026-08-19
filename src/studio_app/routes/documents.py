"""`/api/admin/documents/*` — upload/purge/reindex tài liệu KB thật, đi thẳng qua `KbPipeline`
(`packages/kb`). `KbPipeline` có 5 method (chunker/embed_invoke/index/consent_purge/re_index),
route này dùng cả 5 — trước route này chưa route nào trong `apps/studio` gọi tới, tab "Tài liệu"
chỉ là placeholder tĩnh (`apps/web/src/admin/DocumentsPlaceholderTab.tsx`).

`section_role` KHÔNG dùng `SECTION_VOCAB` (`studio_kb.doc_factory_core` — 4 giá trị cố định chỉ
dùng cho bộ tài liệu mẫu tĩnh `docs/callisto-2.0/`, xem `doc_factory_v2.py::load_corpus_v2`).
Cơ chế fence nội dung THẬT (`routes/chat.py` → `interpreter.run()` → `KbRetrieveExecutor` →
`kb_search.search`) so khớp `section_role` với TÊN PHÒNG BAN thật của tenant (`core.sections`), nên
route upload validate `section_role` qua `fetch_tenant_section_names` — đúng hàm `routes/chat.py`
dùng cho `as_roles` — thay vì 1 vocab cố định.

**Giới hạn đã biết:** `KbPipeline` chưa có `list_documents`/`delete_document` (xem kb#180, issue
gửi team DE), nên route upload KHÔNG kiểm tra được `doc_id` đã tồn tại trước khi ghi. Re-upload
cùng tên file + phòng ban sẽ `ON CONFLICT DO UPDATE` (`postgres.py::_UPSERT`) các `chunk_id` trùng,
nhưng nếu bản mới có ÍT section hơn bản cũ, các `chunk_id` dư sẽ mồ côi lại trong DB (không bị xoá,
`consent_purge`/xoá toàn bộ vẫn dọn sạch được) — sẽ vá khi `delete_document` có, bằng cách gọi nó
trước `index()` mỗi lần upload.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from psycopg import AsyncConnection
from pydantic import BaseModel
from studio_kb.pipeline import KbPipeline

from studio_app.authz import FreshIdentity, fetch_fresh_identity, fetch_tenant_section_names, require_admin
from studio_app.core._db import get_pool
from studio_app.middleware import get_request_connection, get_request_session
from studio_app.providers.factory import CallistoEmbedding

router = APIRouter(prefix="/api/admin/documents", tags=["documents"])

# Chưa có tiền lệ giới hạn kích thước upload nào trong repo — 1 MiB là giá trị mặc định hợp lý cho
# tài liệu markdown thuần văn bản, chỉnh sau nếu cần qua config thay vì hardcode nếu nhu cầu thật.
_MAX_UPLOAD_BYTES = 1 * 1024 * 1024

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return slug or "doc"


async def _resolve_target_tenant(conn: AsyncConnection, identity: FreshIdentity, tenant_id: str | None) -> str:
    """Dual-gate tenant dùng chung cho cả 3 route dưới (upload/purge/reindex) — y hệt
    `routes/sections.py::list_sections`: superadmin bắt buộc khai `tenant_id` (JWT của họ trỏ
    `__system__`, không có tenant mặc định); company-admin dùng tenant mình, 403 nếu cố khai tenant
    khác."""
    if "superadmin" in identity.roles:
        if tenant_id is None:
            raise HTTPException(status_code=400, detail="superadmin phải khai tenant_id cho công ty nào")
        cur = await conn.execute(
            "SELECT 1 FROM core.tenants WHERE id = %s AND name != '__system__'",
            (tenant_id,),
        )
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"tenant_id {tenant_id!r} không tồn tại")
        return tenant_id
    if tenant_id is not None and tenant_id != str(identity.tenant_id):
        raise HTTPException(status_code=403, detail="company-admin chỉ thao tác được tenant mình")
    return str(identity.tenant_id)


async def _resolve_target_tenant_uuid(conn: AsyncConnection, identity: FreshIdentity, tenant_id: str | None) -> UUID:
    """`_resolve_target_tenant` + parse UUID — dùng chung cho cả 3 route, tránh lặp lại khối
    try/except `UUID(...)` giống hệt nhau 3 lần."""
    target_tenant_id = await _resolve_target_tenant(conn, identity, tenant_id)
    try:
        return UUID(target_tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"tenant_id không phải UUID hợp lệ: {target_tenant_id!r}") from exc


class UploadDocumentResponse(BaseModel):
    doc_id: str
    section_role: str
    chunk_count: int


class PurgeDocumentsResponse(BaseModel):
    tenant_id: str
    deleted_count: int


class ReindexDocumentsResponse(BaseModel):
    tenant_id: str
    chunk_count: int


@router.post("", response_model=UploadDocumentResponse)
async def upload_document(
    file: UploadFile = File(...),  # noqa: B008 — FastAPI DI, gọi trong default là idiom bắt buộc
    section_role: str = Form(...),  # noqa: B008
    tenant_id: str | None = Form(None),  # noqa: B008
) -> UploadDocumentResponse:
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_admin(identity.roles)

    tenant_uuid = await _resolve_target_tenant_uuid(conn, identity, tenant_id)
    valid_section_names = await fetch_tenant_section_names(conn, str(tenant_uuid))
    if section_role not in valid_section_names:
        raise HTTPException(
            status_code=400,
            detail=f"section_role {section_role!r} không hợp lệ — chỉ chấp nhận {sorted(valid_section_names)}",
        )

    if not file.filename or not file.filename.lower().endswith(".md"):
        raise HTTPException(status_code=422, detail="chỉ chấp nhận file .md")

    raw = await file.read()
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=422, detail=f"file vượt quá giới hạn {_MAX_UPLOAD_BYTES} byte")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"file không phải UTF-8 hợp lệ: {exc}") from exc

    # Tiền tố `tenant_uuid.hex` bắt buộc: `chunk_id` (`{doc_id}#c{n}`) là PRIMARY KEY toàn bảng
    # `kb.chunks`, KHÔNG tenant-scoped — 2 tenant cùng `doc_id` sẽ `ON CONFLICT DO UPDATE` đè lẫn
    # nhau (`postgres.py::_UPSERT`). Không dùng tên hiển thị công ty (có thể trùng giữa 2 tenant).
    stem = Path(file.filename).stem
    doc_id = f"{tenant_uuid.hex}-{_slugify(section_role)}-{_slugify(stem)}"

    pipeline = KbPipeline(await get_pool(), CallistoEmbedding())
    try:
        chunks = await pipeline.chunker(text, doc_id=doc_id, tenant_id=tenant_uuid, section_role=section_role)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    embeddings = await pipeline.embed_invoke(chunks)
    await pipeline.index(chunks, embeddings)

    return UploadDocumentResponse(doc_id=doc_id, section_role=section_role, chunk_count=len(chunks))


@router.delete("", response_model=PurgeDocumentsResponse)
async def purge_documents(tenant_id: str | None = Query(None)) -> PurgeDocumentsResponse:
    """Xoá SẠCH toàn bộ `kb.chunks` của 1 tenant qua `KbPipeline.consent_purge` (quyền xoá dữ liệu
    kiểu GDPR/consent, ĐÃ implement sẵn — chỉ chưa route nào gọi tới trước route này). KHÁC xoá
    từng tài liệu (`delete_document`, chưa có — kb#180): route này xoá TOÀN BỘ, không phân biệt
    tài liệu nào. Không đòi token xác nhận phụ ở server — theo đúng tiền lệ
    `sections.py::delete_section` (server không thêm ma sát, UI tự hỏi lại người dùng qua
    `window.confirm` trước khi gọi, xem `DocumentsPlaceholderTab.tsx`)."""
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_admin(identity.roles)

    tenant_uuid = await _resolve_target_tenant_uuid(conn, identity, tenant_id)

    pipeline = KbPipeline(await get_pool(), CallistoEmbedding())
    deleted = await pipeline.consent_purge(tenant_uuid)
    return PurgeDocumentsResponse(tenant_id=str(tenant_uuid), deleted_count=deleted)


@router.post("/reindex", response_model=ReindexDocumentsResponse)
async def reindex_documents(tenant_id: str | None = Query(None)) -> ReindexDocumentsResponse:
    """Nhúng lại + ghi lại toàn bộ `kb.chunks` của 1 tenant qua `KbPipeline.re_index` (ĐÃ implement
    sẵn — chỉ chưa route nào gọi tới trước route này). Giữ nguyên `chunk_id`/`section_role`, chỉ
    tính lại vector — dùng khi đổi `EmbeddingService` (model mới) và cần nhúng lại dữ liệu cũ theo
    không gian vector mới. KHÔNG tự động chạy khi đổi model (xem thảo luận trong PR/commit) — luôn
    là thao tác chủ động do admin bấm, vì tốn kém (nhúng lại toàn bộ) và có thể đi kèm đổi
    `EMBEDDING_DIM` cần migration schema riêng, không chỉ chạy lại hàm này."""
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_admin(identity.roles)

    tenant_uuid = await _resolve_target_tenant_uuid(conn, identity, tenant_id)

    pipeline = KbPipeline(await get_pool(), CallistoEmbedding())
    chunk_count = await pipeline.re_index(tenant_uuid)
    return ReindexDocumentsResponse(tenant_id=str(tenant_uuid), chunk_count=chunk_count)
