"""`POST /api/admin/documents` — upload 1 file `.md`/`.txt`/`.docx` thật, chunk/embed/index vào
`kb.chunks`. Trước route này, `KbPipeline` đã implement đủ 5 method (chunker/embed_invoke/index/
consent_purge/re_index) nhưng chưa route nào trong `apps/studio` gọi tới — tab "Tài liệu" chỉ là
placeholder tĩnh (`apps/web/src/admin/DocumentsPlaceholderTab.tsx`).

Phạm vi route này CỐ Ý chỉ có upload — không xoá/reindex/list. Nút "Xoá toàn bộ"/"Re-index toàn
bộ" ở UI hiện là khung hiển thị (không gọi API), giữ chỗ cho lúc nào tính năng đó thật sự cần.

**Cắt bằng `chunk_window.cut_window` (cửa sổ trượt 850 từ/overlap 170), KHÔNG dùng
`KbPipeline.chunker`/`_cut_document` (bộ cắt theo heading `##`).** Lý do: nội dung upload tự do
không có gì đảm bảo có cấu trúc `## title` như corpus Callisto 2.0 curate tay — `_cut_document`
raise ngay khi thiếu heading, đúng thứ chặn `.txt`/`.docx` (gần như không bao giờ có `##`) và cả
nhiều `.md` thật (ghi chú rời rạc, không heading). `cut_window` không quan tâm cấu trúc, chỉ cần có
chữ. Xem `packages/kb/src/studio_kb/chunk_window.py` cho số liệu đo (2000 token trần
`gemini-embedding-001` → 850 từ/chunk, chốt bằng A/B/C benchmark thật trên 100 câu hỏi, không phải
suy luận — 850/170 thắng cả benchmark chính lẫn stress test ngữ cảnh dài, xem
`packages/kb/chunking/report.md`).

`section_role` KHÔNG dùng `SECTION_VOCAB` (`studio_kb.doc_factory_core` — 4 giá trị cố định chỉ
dùng cho bộ tài liệu mẫu tĩnh `docs/callisto-2.0/`, xem `doc_factory_v2.py::load_corpus_v2`).
Cơ chế fence nội dung THẬT (`routes/chat.py` → `interpreter.run()` → `KbRetrieveExecutor` →
`kb_search.search`) so khớp `section_role` với TÊN PHÒNG BAN thật của tenant (`core.sections`), nên
route này validate `section_role` qua `fetch_tenant_section_names` — đúng hàm `routes/chat.py`
dùng cho `as_roles` — thay vì 1 vocab cố định.

**Giới hạn đã biết:** chưa kiểm tra được `doc_id` đã tồn tại trước khi ghi. Re-upload cùng tên
file + phòng ban sẽ `ON CONFLICT DO UPDATE` (`postgres.py::_UPSERT`) các `chunk_id` trùng, nhưng
nếu bản mới có ÍT chunk hơn bản cũ, các `chunk_id` dư sẽ mồ côi lại trong DB (không bị xoá).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from studio_kb.chunk_window import cut_window
from studio_kb.extract import SUPPORTED_SUFFIXES, UnsupportedFormatError, extract_text
from studio_kb.pipeline import KbPipeline

from studio_app.authz import fetch_fresh_identity, fetch_tenant_section_names, require_admin
from studio_app.core._db import get_pool
from studio_app.middleware import get_request_connection, get_request_session
from studio_app.providers.factory import build_embedding

router = APIRouter(prefix="/api/admin/documents", tags=["documents"])

# Chưa có tiền lệ giới hạn kích thước upload nào trong repo — 1 MiB là giá trị mặc định hợp lý cho
# tài liệu văn bản thuần (`.md`/`.txt`/`.docx`), chỉnh sau nếu cần qua config thay vì hardcode nếu
# nhu cầu thật.
_MAX_UPLOAD_BYTES = 1 * 1024 * 1024

# Hạn mức thứ HAI, theo SỐ TỪ sau khi trích. Cần nó vì `_MAX_UPLOAD_BYTES` chỉ chặn được KHỐI
# LƯỢNG CHỮ chừng nào byte còn tỉ lệ với chữ — đúng với `.md`/`.txt`, SAI với `.docx`: `.docx` là
# một file ZIP, nội dung được NÉN chứ không phình vì "overhead XML". Đo thật trên cùng hạn mức
# 1 MiB: `.txt` thuần ~168.000 từ (~247 chunk) · `.docx` văn xuôi tiếng Việt ~606.000 từ (~891
# chunk) · `.docx` nội dung lặp ~14.400.000 từ (~21.000 chunk).
#
# Số chunk mới là thứ sinh ra chi phí, không phải số byte: mỗi chunk là một mục trong lô embedding
# ≤90 (`providers/embeddings.py::_BATCH`), mỗi lô một lời gọi API trả tiền với timeout 120s. 21.000
# chunk là ~234 lời gọi TUẦN TỰ nằm trong ĐÚNG MỘT HTTP request, rồi `KbPipeline.index` ghi cả
# 21.000 dòng trong MỘT transaction. Nên chặn theo đúng đơn vị đó.
#
# 200.000 nằm TRÊN mức văn xuôi thật của một file 1 MiB (~168.000 từ) để không siết cửa nào đang
# mở — `.md`/`.txt` hợp lệ hôm nay upload được thì sau bản vá vẫn upload được.
_MAX_WORDS = 200_000

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return slug or "doc"


class UploadDocumentResponse(BaseModel):
    doc_id: str
    section_role: str
    chunk_count: int


@router.post("", response_model=UploadDocumentResponse)
async def upload_document(
    file: UploadFile = File(...),  # noqa: B008 — FastAPI DI, gọi trong default là idiom bắt buộc
    section_role: str = Form(...),  # noqa: B008
    tenant_id: str | None = Form(None),  # noqa: B008
) -> UploadDocumentResponse:
    """Dual-gate tenant y hệt `routes/sections.py::list_sections`: superadmin bắt buộc khai
    `tenant_id` (JWT của họ trỏ `__system__`, không có tenant mặc định); company-admin dùng tenant
    mình, 403 nếu cố khai tenant khác."""
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_admin(identity.system_roles)

    if "superadmin" in identity.system_roles:
        if tenant_id is None:
            raise HTTPException(
                status_code=400, detail="superadmin phải khai tenant_id để upload tài liệu cho công ty nào"
            )
        # Parse UUID TRƯỚC khi query — review app#27 (dholmes0207): parse SAU query để chuỗi thô
        # (vd "abc") đi thẳng vào `WHERE id = %s` trên cột UUID, psycopg raise lỗi cú pháp CHƯA BẮT
        # ⇒ 500 thay vì 400 mà nhánh except bên dưới chuẩn bị sẵn.
        try:
            tenant_uuid = UUID(tenant_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"tenant_id không phải UUID hợp lệ: {tenant_id!r}") from exc
        target_tenant_id = tenant_id
        cur = await conn.execute(
            "SELECT 1 FROM core.tenants WHERE id = %s AND name != '__system__'",
            (target_tenant_id,),
        )
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"tenant_id {target_tenant_id!r} không tồn tại")
    else:
        if tenant_id is not None and tenant_id != str(identity.tenant_id):
            raise HTTPException(status_code=403, detail="company-admin chỉ upload được cho tenant mình")
        target_tenant_id = str(identity.tenant_id)
        tenant_uuid = identity.tenant_id

    valid_section_names = await fetch_tenant_section_names(conn, target_tenant_id)
    if section_role not in valid_section_names:
        raise HTTPException(
            status_code=400,
            detail=f"section_role {section_role!r} không hợp lệ — chỉ chấp nhận {sorted(valid_section_names)}",
        )

    if not file.filename or not any(file.filename.lower().endswith(suf) for suf in SUPPORTED_SUFFIXES):
        raise HTTPException(
            status_code=422,
            detail=f"đuôi file không hỗ trợ — chỉ chấp nhận {sorted(SUPPORTED_SUFFIXES)}",
        )

    # Đọc theo chunk có chặn, huỷ NGAY khi vượt hạn mức — review app#27 (dholmes0207): đọc hết
    # `file.read()` vào bộ đệm rồi mới so `len(raw)` khiến `_MAX_UPLOAD_BYTES` chỉ là một phép
    # validate SAU khi đã nhận trọn (starlette spool ra đĩa khi vượt ngưỡng), không phải hàng rào —
    # body vài GB vẫn được nhận hết trước khi bị 422. Không dựa vào `file.size`/`Content-Length`
    # (client có thể không gửi, hoặc sai với chunked transfer encoding) — đọc dần là hàng rào thật.
    pieces: list[bytes] = []
    total = 0
    while True:
        piece = await file.read(64 * 1024)
        if not piece:
            break
        total += len(piece)
        if total > _MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=422, detail=f"file vượt quá giới hạn {_MAX_UPLOAD_BYTES} byte")
        pieces.append(piece)
    raw = b"".join(pieces)
    try:
        text = extract_text(file.filename, raw)
    except UnsupportedFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Đếm SAU khi trích, không phải trên `raw`: với `.docx` thì `len(raw)` không nói gì về lượng
    # chữ (xem `_MAX_WORDS`). `cut_window` split lại lần nữa ở dưới — thừa một lần `split()` trên
    # tối đa 200k từ (~10ms), rẻ hơn nhiều so với đổi chữ ký `cut_window` ở packages/kb chỉ để
    # chuyền sẵn list từ qua.
    word_count = len(text.split())
    if word_count > _MAX_WORDS:
        raise HTTPException(
            status_code=422,
            detail=f"tài liệu quá dài: {word_count} từ, tối đa {_MAX_WORDS}",
        )

    # Tiền tố `tenant_uuid.hex` bắt buộc: `chunk_id` (`{doc_id}#c{n}`) là PRIMARY KEY toàn bảng
    # `kb.chunks`, KHÔNG tenant-scoped — 2 tenant cùng `doc_id` sẽ `ON CONFLICT DO UPDATE` đè lẫn
    # nhau (`postgres.py::_UPSERT`). Không dùng tên hiển thị công ty (có thể trùng giữa 2 tenant).
    # Hậu tố hash 8-hex của TÊN FILE GỐC (trước `_slugify`) — review app#27 (dholmes0207): 2 tên
    # file khác nhau (vd "HR-Policy.md" / "hr policy.md", hay tên tiếng Việt có dấu bị `_slugify`
    # gộp hết thành "-") có thể slugify ra CÙNG chuỗi, khiến tài liệu sau ghi đè im lặng tài liệu
    # trước qua `ON CONFLICT DO UPDATE` dù là 2 tài liệu khác nhau. Hash bám theo tên gốc nên cùng
    # 1 file re-upload nguyên tên vẫn ra đúng `doc_id` cũ (idempotent), chỉ tên khác mới tách riêng.
    stem = Path(file.filename).stem
    name_hash = hashlib.sha256(file.filename.encode("utf-8")).hexdigest()[:8]
    doc_id = f"{tenant_uuid.hex}-{_slugify(section_role)}-{_slugify(stem)}-{name_hash}"

    chunks = cut_window(text, doc_id, tenant_uuid, section_role)
    if not chunks:
        raise HTTPException(status_code=422, detail="tài liệu rỗng — không có chữ nào để cắt chunk")

    pipeline = KbPipeline(await get_pool(), build_embedding())
    embeddings = await pipeline.embed_invoke(chunks)
    await pipeline.index(chunks, embeddings)

    return UploadDocumentResponse(doc_id=doc_id, section_role=section_role, chunk_count=len(chunks))
