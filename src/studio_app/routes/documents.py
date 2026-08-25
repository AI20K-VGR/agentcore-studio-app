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

**`doc_id` (cột `kb.chunks.doc_id`, tách khỏi vai trò PK của `chunk_id`) = `{slug(section_role)}-
{slug(stem)}`** — vd `("hr", "Bao Cao Q1.docx")` → `"hr-bao-cao-q1"`. Route xoá TOÀN BỘ chunk cũ
cùng `doc_id` (`KbPipeline.delete_by_doc_id`) TRƯỚC khi ghi chunk mới, nên re-upload không còn để
lại `chunk_id` mồ côi (đóng giới hạn cũ từng ghi ở đây — bản có ÍT chunk hơn bản trước không còn
sót dòng thừa).

`section_role` BẮT BUỘC nằm trong khoá — review app#58 (dholmes0207): bản trước chỉ dùng
`slug(stem)`, nên 2 tài liệu CÙNG TÊN FILE ở 2 phòng ban khác nhau (khác `section_role`, tức khác
ranh giới phân quyền đọc — `fetch_tenant_section_names`/`as_roles` ở `routes/chat.py`) đụng cùng
`doc_id`, và lệnh xoá orphan (không lọc theo phòng ban) xoá LUÔN tài liệu phòng ban kia. Tái hiện
được: upload `leave.md` vào `hr` rồi `finance` xoá mất bản `hr` — không lỗi, không cảnh báo.

**Rủi ro đã biết, chấp nhận CÓ CHỦ ĐÍCH (không chặn):** 2 file GỐC khác nhau, CÙNG `section_role`,
có thể slugify tên ra CÙNG `doc_id` (vd `"Doc 123.md"` và `"doc-123.md"` đều ra `"doc-123"`) — route
không phân biệt được với một re-upload hợp lệ, nên file sau **ghi đè êm** file trước (không có
UNIQUE constraint, không 409). Không dựng bảng đăng ký tên gốc để chặn cứng — `kb.chunks` là
1-dòng-1-chunk nên nhiều dòng của cùng 1 doc BẮT BUỘC chia sẻ 1 `doc_id`; UNIQUE(tenant_id, doc_id)
trực tiếp trên bảng này là sai kỹ thuật, và một bảng đăng ký riêng nằm ngoài phạm vi yêu cầu hiện
tại. Khác ca chéo phòng ban ở trên: đây LÀ cùng một ranh giới phân quyền, nên đánh đổi hợp lý.

**`doc_name` (cột `kb.chunks.doc_name`) = `stem` NGUYÊN VĂN, KHÔNG qua `_slugify`** — vd
`("hr", "Bao Cao Q1.docx")` → `"Bao Cao Q1"`. Tách riêng khỏi `doc_id` vì luật hiển thị: dữ liệu
nội bộ (`doc_id`/`chunk_id` — đã slugify/hash, mất dấu/rút gọn) không được đưa thẳng lên UI —
`doc_name` là seam DUY NHẤT cho việc đó, không dùng để xoá/so khớp (đó vẫn là việc của `doc_id`).
"""

from __future__ import annotations

import dataclasses
import hashlib
import re
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from studio_kb.chunk_window import cut_window
from studio_kb.extract import SUPPORTED_SUFFIXES, UnsupportedFormatError, extract_text
from studio_kb.pipeline import KbPipeline

from studio_app.authz import fetch_fresh_identity, fetch_tenant_section_names, require_admin
from studio_app.core._db import get_pool
from studio_app.core.golden_autogen import regenerate_for_section
from studio_app.middleware import borrowed_tenant_scope, get_request_connection, get_request_session
from studio_app.providers.factory import ReadOnlyEmbedding, build_embedding

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


async def _tenant_slug(conn: Any, tenant_id: UUID) -> str:
    """Slug của tenant từ `core.tenants.name` — thứ `GoldenCase.tenant` mang.

    `GoldenCase.tenant` là **slug**, không phải UUID (xem `SourceChunk` ở `golden_from_kb`), và các
    case bẫy chéo-tenant so sánh slug với slug. Đọc từ `core.tenants` chứ không suy từ
    `studio_kb.doc_factory.TENANT_IDS`: bảng đó chỉ có 2 tenant demo, còn route này chạy cho mọi
    tenant thật.
    """
    cur = await conn.execute("SELECT name FROM core.tenants WHERE id = %s", (tenant_id,))
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"tenant {tenant_id} không có trong core.tenants")
    return str(row[0])


class DocumentSummary(BaseModel):
    """Một tài liệu đã nạp, **theo góc nhìn người dùng**.

    `id` cố ý không tên là `doc_id`: nó là khoá để gọi xoá, không phải thứ để hiển thị. Giao diện
    vẽ `name`/`section_role`/`chunk_count` và **không bao giờ vẽ `id`** — người quản trị công ty
    không có lý do gì phải đọc một giá trị cột trong `kb.chunks`, còn mỗi giá trị kỹ thuật lọt ra
    màn hình là một thứ họ sẽ chép vào ticket rồi hỏi nó nghĩa là gì.

    `name` là phần tên tài liệu đã slug hoá (bỏ tiền tố phòng ban khỏi `doc_id`). **Không phải tên
    file gốc** — tên gốc hiện không được lưu ở đâu (`kb.chunks` chỉ có `doc_id`), nên đây là thứ
    trung thực nhất server biết. Muốn hiện đúng `"Báo Cáo Q1.docx"` phải thêm cột ở `packages/kb`;
    đã ghi issue riêng, không nhét vào đây.
    """

    id: str
    name: str
    section_role: str
    chunk_count: int


class DocumentListResponse(BaseModel):
    documents: list[DocumentSummary]
    total_chunks: int
    """Tổng số đoạn của CẢ tenant. Khai riêng thay vì để giao diện tự cộng `documents`: dòng ghi
    trước khi `kb.chunks` có cột `doc_id` không gom được vào tài liệu nào, nên hai số có thể lệch —
    và một giao diện tự cộng ra số khác số thật là cách làm người dùng tưởng mất dữ liệu."""


class DeleteDocumentsRequest(BaseModel):
    ids: list[str]
    """`DocumentSummary.id` của các tài liệu cần xoá. Nhận nhiều id một lần vì giao diện cho tích
    chọn nhiều dòng — để client gọi tuần tự thì một lần lỗi giữa chừng sẽ để lại trạng thái nửa vời
    mà người dùng không nhìn thấy được."""


class DeleteDocumentsResponse(BaseModel):
    deleted_chunks: int
    deleted_documents: list[str]
    not_found: list[str]
    """Id gửi lên mà xoá được 0 đoạn. Khai riêng thay vì im lặng bỏ qua: dòng ghi TRƯỚC khi cột
    `doc_id` tồn tại mang `NULL`, và `delete_by_doc_id` không đụng tới được (xem `schema.py` — chú
    thích ở đó nói `re_index` phục hồi được, thực tế nó đọc lại `doc_id` từ DB rồi ghi y nguyên,
    nên không phục hồi gì). Không có ô này thì người dùng bấm xoá, thấy báo thành công, tài liệu
    vẫn còn nguyên."""


_UNGROUPED_NAME = "(chưa gắn tài liệu)"
"""Nhãn cho chunk có `doc_id` rỗng. Gom thành MỘT mục thay vì giấu đi: chúng chiếm chỗ thật trong
`kb.chunks`, và một bảng đếm thiếu chúng sẽ khiến người dùng tưởng hệ thống làm mất dữ liệu."""


async def _resolve_target_tenant(conn: Any, identity: Any, tenant_id: str | None, action: str) -> tuple[str, UUID]:
    """Dual-gate tenant dùng chung cho cả ba route của tab Tài liệu — trước đây nằm riêng trong
    `upload_document`. Tách ra vì `list_documents`/`delete_documents` phải áp **đúng** luật đó: một
    route đọc hoặc xoá lỏng tay hơn route ghi là cách rò dữ liệu chéo tenant kinh điển.

    Parse UUID TRƯỚC khi query giữ nguyên từ bản cũ (review app#27): parse sau thì chuỗi thô đi
    thẳng vào `WHERE id = %s` trên cột UUID, psycopg raise lỗi cú pháp chưa bắt ⇒ 500 thay vì 400.
    """
    if "superadmin" in identity.system_roles:
        if tenant_id is None:
            raise HTTPException(status_code=400, detail=f"superadmin phải khai tenant_id để {action} cho công ty nào")
        try:
            tenant_uuid = UUID(tenant_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"tenant_id không phải UUID hợp lệ: {tenant_id!r}") from exc
        cur = await conn.execute(
            "SELECT 1 FROM core.tenants WHERE id = %s AND name != '__system__'",
            (tenant_id,),
        )
        if await cur.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"tenant_id {tenant_id!r} không tồn tại")
        return tenant_id, tenant_uuid
    if tenant_id is not None and tenant_id != str(identity.tenant_id):
        raise HTTPException(status_code=403, detail=f"company-admin chỉ {action} được cho tenant mình")
    return str(identity.tenant_id), identity.tenant_id


class UploadDocumentResponse(BaseModel):
    doc_id: str
    doc_name: str
    """Tên NGƯỜI ĐỌC ĐƯỢC để hiển thị UI — tên file gốc, bỏ đuôi, giữ nguyên hoa/thường/dấu. Khác
    `doc_id` (khoá kỹ thuật, slugify) — luật hiển thị: không được đưa thẳng dữ liệu nội bộ
    (`doc_id`/`chunk_id`) lên UI, xem `kb.chunks.doc_name` (`packages/kb/src/studio_kb/schema.py`)."""
    section_role: str
    chunk_count: int
    golden_set_ref: str
    """Bộ golden của phòng ban này, vừa được sinh lại từ chunk đang có."""
    golden_n_cases: int
    golden_n_ai: int
    golden_n_human: int
    """Số case `source="human"` được GIỮ NGUYÊN qua lần sinh lại này — tách khỏi tổng vì đó là câu
    người vừa sửa tay sẽ hỏi."""


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

    target_tenant_id, tenant_uuid = await _resolve_target_tenant(conn, identity, tenant_id, "upload tài liệu")

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

    # `chunk_id_prefix` — tiền tố `tenant_uuid.hex` bắt buộc: `chunk_id` (`{chunk_id_prefix}#c{n}`)
    # là PRIMARY KEY toàn bảng `kb.chunks`, KHÔNG tenant-scoped — 2 tenant cùng tên file sẽ
    # `ON CONFLICT DO UPDATE` đè lẫn nhau (`postgres.py::_UPSERT`) nếu thiếu tiền tố này. Không
    # dùng tên hiển thị công ty (có thể trùng giữa 2 tenant).
    # Hậu tố hash 8-hex của TÊN FILE GỐC (trước `_slugify`) — review app#27 (dholmes0207): 2 tên
    # file khác nhau (vd "HR-Policy.md" / "hr policy.md", hay tên tiếng Việt có dấu bị `_slugify`
    # gộp hết thành "-") có thể slugify ra CÙNG chuỗi — hash giữ `chunk_id_prefix` (PK) tách biệt
    # cho 2 upload khác nhau dù trùng slug. Hash bám theo tên gốc nên cùng 1 file re-upload nguyên
    # tên vẫn ra đúng `chunk_id_prefix` cũ (idempotent), chỉ tên khác mới tách riêng.
    #
    # `doc_id` (cột riêng, KHÔNG phải PK) = `{slug(section_role)}-{slug(stem)}` — khoá con người
    # dùng để xoá theo tài liệu (`KbPipeline.delete_by_doc_id`). PHẢI mang `section_role` — review
    # app#58 (dholmes0207): thiếu nó, lệnh xoá orphan (chỉ lọc `tenant_id`+`doc_id`, KHÔNG lọc
    # phòng ban) xoá NHẦM tài liệu cùng tên ở phòng ban khác (2 tài liệu khác nhau, khác ranh giới
    # phân quyền đọc, chỉ vì trùng tên file). Cố ý KHÔNG mang hash/tenant-hex: 2 file gốc khác
    # nhau, CÙNG phòng ban, trùng slug sẽ chia sẻ `doc_id` và ghi đè lẫn nhau (đã chấp nhận, xem
    # docstring đầu file — đây LÀ cùng ranh giới phân quyền nên đánh đổi hợp lý) — đổi lại, đây là
    # giá trị caller có thể tự tính lại được (biết cả tên file lẫn phòng ban mình vừa upload vào)
    # để gọi xoá sau này, không cần tra lại `chunk_id_prefix` đã hash.
    # `doc_name` (cột riêng, KHÔNG phải khoá) = `stem` NGUYÊN VĂN, KHÔNG qua `_slugify` — đây là
    # điểm khác biệt cố ý với `doc_id`: `doc_id` phải là khoá kỹ thuật ổn định (xoá/so khớp) nên
    # slugify là bắt buộc, còn `doc_name` chỉ để HIỂN THỊ nên giữ nguyên hoa/thường/dấu tiếng Việt
    # đúng như người upload đặt tên — luật hiển thị cấm đưa thẳng `doc_id`/`chunk_id` (dữ liệu nội
    # bộ, đã slugify/hash) lên UI.
    stem = Path(file.filename).stem
    name_hash = hashlib.sha256(file.filename.encode("utf-8")).hexdigest()[:8]
    chunk_id_prefix = f"{tenant_uuid.hex}-{_slugify(section_role)}-{_slugify(stem)}-{name_hash}"
    doc_id = f"{_slugify(section_role)}-{_slugify(stem)}"

    chunks = cut_window(text, chunk_id_prefix, tenant_uuid, section_role)
    if not chunks:
        raise HTTPException(status_code=422, detail="tài liệu rỗng — không có chữ nào để cắt chunk")
    chunks = [dataclasses.replace(c, doc_id=doc_id, doc_name=stem) for c in chunks]

    pipeline = KbPipeline(await get_pool(), build_embedding())
    embeddings = await pipeline.embed_invoke(chunks)
    # Xoá chunk cũ của CÙNG `doc_id` trước khi ghi chunk mới — đóng giới hạn orphan-chunk cũ (xem
    # docstring đầu file): re-upload bản NGẮN HƠN không còn sót `chunk_id` mồ côi. Đặt SAU
    # `embed_invoke` (không chạm DB) để lỗi embedding không xoá mất dữ liệu cũ trước khi có dữ liệu
    # mới sẵn sàng ghi — nhưng KHÔNG atomic với `index` bên dưới (2 giao dịch riêng, đúng seam
    # 5-method của `KbPipeline`): xoá thành công rồi `index` lỗi giữa chừng vẫn có thể để tenant
    # tạm thời mất doc này, biết và chấp nhận cho phạm vi hiện tại.
    await pipeline.delete_by_doc_id(tenant_uuid, doc_id)
    await pipeline.index(chunks, embeddings)

    # Golden set SINH RA Ở ĐÂY, không còn phải có sẵn. Sinh lại cho CẢ phòng ban (không chỉ tài
    # liệu vừa nạp) và giữ nguyên mọi case `source="human"` — xem `core/golden_autogen.py`.
    #
    # Bind `app.tenant_id` tường minh trong đúng transaction ghi: `eval.golden_sets` bật RLS
    # `FORCE`, và connection của middleware đã bind sẵn tenant của PHIÊN — nhưng route này cho
    # superadmin nạp hộ tenant khác (`tenant_id=` tham số), nên phải bind về tenant ĐÍCH. Cùng
    # khuôn `routes/golden_sets.py`.
    #
    # KHÔNG atomic với `index` ở trên (hai giao dịch riêng, đúng seam của `KbPipeline`): index
    # xong mà sinh golden vỡ thì tài liệu đã nằm trong `kb.chunks` còn bộ case chưa cập nhật.
    # Để lỗi NỔI LÊN chứ không nuốt: upload lại cùng file là idempotent (`delete_by_doc_id` +
    # sinh lại toàn bộ), nên đường phục hồi là thử lại, và một 500 nói rõ hơn một 200 nửa vời.
    conn = get_request_connection()
    # `borrowed_tenant_scope` chứ không tự `SET LOCAL` tại chỗ: khối này là SAVEPOINT lồng trong
    # transaction của middleware, và RELEASE SAVEPOINT **không** revert `SET LOCAL` — không trả lại
    # thì phần còn lại của request chạy dưới RLS context của công ty khác (review app#71 đợt 2,
    # mục 2; đo thật, xem docstring hàm đó).
    async with borrowed_tenant_scope(conn, tenant_uuid):
        golden = await regenerate_for_section(
            conn,
            pipeline,
            tenant_id=tenant_uuid,
            tenant_slug=await _tenant_slug(conn, tenant_uuid),
            section_role=section_role,
        )

    return UploadDocumentResponse(
        doc_id=doc_id,
        doc_name=stem,
        section_role=section_role,
        chunk_count=len(chunks),
        golden_set_ref=golden.golden_set_ref,
        golden_n_cases=golden.n_cases,
        golden_n_ai=golden.n_ai,
        golden_n_human=golden.n_human,
    )


def _display_name(doc_id: str, section_role: str) -> str:
    """Bỏ tiền tố phòng ban khỏi `doc_id` để ra tên tài liệu hiển thị.

    `doc_id = f"{slug(section_role)}-{slug(stem)}"` (xem chỗ dựng ở `upload_document`), nên cắt
    đúng tiền tố đó là ra phần tên. Dùng `removeprefix` chứ không `split("-", 1)`: tên phòng ban
    có thể chứa dấu gạch sau khi slug hoá (`"nhan-su"`), và `split` sẽ cắt nhầm giữa tên phòng ban.
    """
    return doc_id.removeprefix(f"{_slugify(section_role)}-") or doc_id


@router.get("", response_model=DocumentListResponse)
async def list_documents(tenant_id: str | None = None) -> DocumentListResponse:
    """Tài liệu đang có trong KB của tenant, kèm số đoạn mỗi tài liệu.

    Gom trong Python từ `KbPipeline.chunks_for_tenant` thay vì viết `GROUP BY` thẳng vào
    `kb.chunks`: bảng đó thuộc `packages/kb`, và một câu SQL của `apps/studio` đọc chéo vào schema
    quadrant khác là chỗ mà lần đổi cấu trúc kế tiếp bên đó sẽ làm vỡ mà không ai thấy trước. Cùng
    lý do (và cùng seam) `golden_autogen.regenerate_for_section` đang dùng.
    """
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_admin(identity.system_roles)
    _, tenant_uuid = await _resolve_target_tenant(conn, identity, tenant_id, "xem tài liệu")

    pipeline = KbPipeline(await get_pool(), ReadOnlyEmbedding())
    chunks = await pipeline.chunks_for_tenant(tenant_uuid)

    # Gom kèm `doc_name`: cột hiển thị kb#64 vừa thêm, giữ nguyên hoa/thường/dấu tiếng Việt. Lấy
    # tên của chunk ĐẦU TIÊN trong nhóm — mọi chunk cùng `doc_id` đến từ cùng một lần upload nên
    # mang cùng tên; nếu lệch thì đó là dấu hiệu hai tài liệu đụng `doc_id` (xem issue app#70), và
    # chọn cái đầu là chọn tất định thay vì chọn ngẫu nhiên theo thứ tự trả về.
    grouped: dict[tuple[str, str], int] = {}
    names: dict[tuple[str, str], str] = {}
    for chunk in chunks:
        key = (chunk.doc_id, chunk.section_role)
        grouped[key] = grouped.get(key, 0) + 1
        if chunk.doc_name and key not in names:
            names[key] = chunk.doc_name

    documents = [
        DocumentSummary(
            id=doc_id,
            # Ba nấc, tụt dần theo mức trung thực: tên gốc từ DB → tên đã slug hoá suy từ `doc_id`
            # → nhãn gộp cho dòng chưa gắn tài liệu. Nấc giữa tồn tại vì dòng ghi TRƯỚC kb#64 có
            # `doc_name` rỗng — hiện ô trống ở đó thì người dùng không hành động được với nó.
            name=names.get((doc_id, section_role))
            or (_display_name(doc_id, section_role) if doc_id else _UNGROUPED_NAME),
            section_role=section_role,
            chunk_count=count,
        )
        for (doc_id, section_role), count in sorted(grouped.items())
    ]
    return DocumentListResponse(documents=documents, total_chunks=len(chunks))


@router.post("/delete", response_model=DeleteDocumentsResponse)
async def delete_documents(body: DeleteDocumentsRequest, tenant_id: str | None = None) -> DeleteDocumentsResponse:
    """Xoá các tài liệu được tích chọn (theo `DocumentSummary.id`), không phải xoá cả tenant.

    `POST /delete` chứ không `DELETE` mang body: xoá NHIỀU id trong một lượt cần một danh sách, mà
    body trên `DELETE` là chỗ proxy/CDN được phép bỏ đi theo spec — một lệnh xoá tới nơi với danh
    sách rỗng là kiểu hỏng không được phép có ở đây.

    Trả `not_found` cho id xoá được 0 đoạn thay vì im lặng — xem docstring `DeleteDocumentsResponse`.
    """
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_admin(identity.system_roles)
    _, tenant_uuid = await _resolve_target_tenant(conn, identity, tenant_id, "xoá tài liệu")

    if not body.ids:
        raise HTTPException(status_code=400, detail="chưa chọn tài liệu nào để xoá")

    pipeline = KbPipeline(await get_pool(), ReadOnlyEmbedding())
    deleted_chunks = 0
    deleted_documents: list[str] = []
    not_found: list[str] = []
    # Lặp tuần tự, không gom một câu `IN (...)`: `delete_by_doc_id` là seam của `packages/kb` và nó
    # tự bind `app.tenant_id` cho từng giao dịch (RLS). Tự viết câu gộp ở đây là dựng lại đường ghi
    # thứ hai vào `kb.chunks` nằm ngoài seam đó — đúng thứ hàng rào quadrant tồn tại để chặn.
    for doc_id in dict.fromkeys(body.ids):  # giữ thứ tự, bỏ trùng
        removed = await pipeline.delete_by_doc_id(tenant_uuid, doc_id)
        if removed:
            deleted_chunks += removed
            deleted_documents.append(doc_id)
        else:
            not_found.append(doc_id)

    return DeleteDocumentsResponse(
        deleted_chunks=deleted_chunks, deleted_documents=deleted_documents, not_found=not_found
    )
