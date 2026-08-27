"""`POST /api/admin/golden-sets` — người dùng tự đưa bộ golden case của họ vào hệ thống.

Đường thứ hai vào `eval.golden_sets`, cạnh bộ sinh máy (`studio_kb.golden_from_kb`). Hai đường phục
vụ hai nhu cầu khác nhau và **không** thay thế nhau: bộ sinh máy phủ diện rộng từ tài liệu đã nạp,
còn đường này là chỗ một người đưa vào bộ case họ **đã có** — thường là câu hỏi thật của nghiệp vụ,
thứ không suy ra được từ chunk.

## Route KHÔNG suy nguồn gốc hộ người dùng

`source` giữ **nguyên** giá trị trong payload, kể cả khi vắng (`None` = *chưa khai*). Cám dỗ ở đây là
ép `source="human"` cho mọi case vì "người upload mà" — nhưng route chỉ biết **một** sự thật: *một
người đã nạp tệp này*. Nó không biết từng case do người viết hay do máy sinh rồi người sửa. Ép giá
trị là **khai hộ**, đúng thứ mặc định `None` của `GoldenCase` tồn tại để tránh (`DEC-D18-01`,
`DEC-D16-03`).

## Vì sao validate qua `GoldenCase` chứ không nhận `dict` thô

`extra="forbid"` biến một field gõ sai thành **lỗi ngay tại request**, kèm tên field sai. Nhận dict
thô rồi ghi thẳng JSONB sẽ để một `expected_citaion` (thiếu chữ `t`) nằm im trong DB tới lần chấm
đầu tiên, và lúc đó nó biểu hiện thành `citation_accuracy = 0` chứ không thành một lỗi.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError
from studio_evalhub.golden_case import GoldenCase, GoldenSet
from studio_evalhub.golden_merge import case_key
from studio_evalhub.golden_store import (
    GoldenSetNotFound,
    GoldenSetScopeError,
    list_golden_sets,
    read_golden_set,
    write_golden_set,
)
from studio_kb.pipeline import KbPipeline

from studio_app.authz import fetch_fresh_identity, fetch_tenant_section_names, require_admin
from studio_app.core._db import get_pool
from studio_app.core.golden_autogen import regenerate_for_section
from studio_app.middleware import borrowed_tenant_scope, get_request_connection, get_request_session
from studio_app.providers.factory import ReadOnlyEmbedding

router = APIRouter(prefix="/api/admin/golden-sets", tags=["golden-sets"])

# Trần số case trong MỘT lần nạp. Không phải chống lạm dụng — chống một payload vô tình khổng lồ trở
# thành một hàng JSONB khổng lồ mà mọi lần chấm sau đó phải đọc lại nguyên vẹn. 2000 nằm trên xa mức
# bộ Full lớn nhất mà lộ trình đặt ra (100–500 case), nên nó không siết cửa nào đang mở.
_MAX_CASES = 2000


class UploadGoldenSetRequest(BaseModel):
    golden_set_ref: str = Field(min_length=1)
    cases: list[dict[str, Any]]
    # `tenant_id` CỐ Ý là field TÙY CHỌN chỉ dành cho superadmin — company-admin khai nó sẽ bị 403,
    # không phải bị bỏ qua. Cùng dual-gate `routes/documents.py`: im lặng bỏ qua một tham số quyền
    # hạn là cách một client tưởng mình làm được việc mà thực ra không.
    tenant_id: str | None = None


class UploadGoldenSetResponse(BaseModel):
    golden_set_ref: str
    tenant_id: str
    n_case: int
    """Tổng case của bộ SAU khi hợp nhất — không phải số case vừa nạp lên."""
    n_traps: int
    """Số case bẫy — suy từ `expects_refusal`, không từ một cờ người nạp tự khai."""
    n_uploaded: int
    """Số case trong payload vừa gửi. Tách khỏi `n_case` để giao diện nói được *"nạp 12, bộ giờ có
    31"* thay vì một con số mà người dùng không biết đối chiếu với cái gì."""
    n_kept_from_existing: int
    """Case của bộ cũ còn sống sót qua lần hợp nhất này (case cũ không trùng khoá với case vừa nạp).
    Khai ra vì đây đúng là câu người dùng lo khi bấm nạp đè: *"bộ máy sinh của tôi có mất không?"*"""
    """Số case bẫy — suy từ `expects_refusal` (hai trục tenant/vai), KHÔNG từ một cờ trong payload.

    Trả về để người nạp thấy ngay bộ của họ có nhánh từ-chối hay không: một bộ 0 case bẫy chấm được
    bình thường nhưng **không nói gì** về hàng rào, và đó là điều họ nên biết trước khi tin verdict."""


def _phan_giai_tenant(identity: Any, khai: str | None) -> UUID:
    """Dual-gate y hệt `routes/documents.py`: superadmin **bắt buộc** khai `tenant_id` (JWT của họ
    trỏ `__system__`, không có tenant mặc định); company-admin dùng tenant mình, 403 nếu khai tenant
    khác."""
    if "superadmin" in identity.system_roles:
        if khai is None:
            raise HTTPException(
                status_code=400, detail="superadmin phải khai tenant_id để nạp golden set cho công ty nào"
            )
        try:
            return UUID(khai)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"tenant_id không phải UUID hợp lệ: {khai!r}") from exc
    if khai is not None and khai != str(identity.tenant_id):
        raise HTTPException(status_code=403, detail="company-admin chỉ nạp được golden set cho tenant mình")
    return UUID(str(identity.tenant_id))


def overlay_golden_set(uploaded: GoldenSet, existing: GoldenSet) -> tuple[GoldenSet, int]:
    """Phủ `uploaded` lên `existing`: case vừa nạp thắng trên khoá của nó, case cũ có khoá không nằm
    trong bộ nạp thì giữ nguyên. Trả `(bộ kết quả, số case cũ giữ lại)`.

    Tách khỏi route để test được **không cần Postgres** — đây là toàn bộ luật ghi đè, và nó là chỗ
    đã sai một lần rồi (review app#68, DongAnh2704) nên xứng đáng có bài riêng.

    Dùng `case_key` của evalhub chứ không tự chuẩn hoá câu hỏi lại: hai cách chuẩn hoá lệch nhau là
    hai bộ trông giống hệt mà không khớp được, và lỗi đó chỉ lộ ra khi đã có dữ liệu thật.

    **Không** có khái niệm va chạm ở đây, khác `merge_golden_sets`. Hàm kia hợp nhất hai nguồn NGANG
    HÀNG nên va chạm nghĩa là "hai bên bất đồng, không ai có thẩm quyền" — ném cho người quyết là
    đúng. Còn ở đây người dùng chủ động nạp bộ của mình lên ref của chính mình: họ LÀ bên có thẩm
    quyền trên những khoá họ gửi. Phủ thì không bất đồng với ai.
    """
    uploaded_keys = {case_key(c) for c in uploaded.cases}
    kept = [c for c in existing.cases if case_key(c) not in uploaded_keys]
    return GoldenSet(golden_set_ref=uploaded.golden_set_ref, cases=[*uploaded.cases, *kept]), len(kept)


@router.post("", response_model=UploadGoldenSetResponse)
async def upload_golden_set(body: UploadGoldenSetRequest) -> UploadGoldenSetResponse:
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_admin(identity.system_roles)

    tenant_uuid = _phan_giai_tenant(identity, body.tenant_id)

    if not body.cases:
        raise HTTPException(status_code=422, detail="golden set rỗng — không có case nào để chấm")
    if len(body.cases) > _MAX_CASES:
        raise HTTPException(status_code=422, detail=f"bộ có {len(body.cases)} case, tối đa {_MAX_CASES} mỗi lần nạp")

    try:
        golden = GoldenSet(
            golden_set_ref=body.golden_set_ref,
            cases=[GoldenCase(**c) for c in body.cases],
        )
    except ValidationError as exc:
        # 422 chứ không 400: payload đúng hình dạng JSON, sai **nội dung** case. Trả nguyên
        # `exc.errors()` để người nạp thấy ĐÚNG case nào, field nào — `extra="forbid"` nêu đích danh
        # field gõ sai, và nuốt nó thành một câu chung chung là vứt đi phần giá trị nhất.
        raise HTTPException(status_code=422, detail=f"case không hợp lệ: {exc.errors()!r}") from exc

    # Superadmin nạp hộ tenant khác: `app.tenant_id` của phiên đang trỏ `__system__`, nên
    # `write_golden_set` sẽ từ chối (`GoldenSetScopeError`) — đúng thiết kế của nó. `borrowed_tenant_scope`
    # bind lại TƯỜNG MINH về tenant đích rồi **trả lại** scope của phiên gọi lúc thoát khối.
    #
    # Bản trước tự `SET LOCAL` tại chỗ kèm lời khẳng định "hết hiệu lực khi transaction đóng, nên
    # phần còn lại của request không thừa hưởng quyền này" — sai (review app#71 đợt 2, mục 2), xem
    # docstring `borrowed_tenant_scope`: khối này là SAVEPOINT lồng, RELEASE không revert `SET LOCAL`.
    async with borrowed_tenant_scope(conn, tenant_uuid):
        # PHỦ LÊN bộ đang có, không ghi đè cả bộ và cũng không `merge_golden_sets`.
        #
        # Bản trước gọi thẳng `write_golden_set`, nên nạp một bộ trùng `golden_set_ref` sẽ XOÁ SẠCH
        # bộ máy vừa sinh lúc upload tài liệu (`golden_autogen`, app#61) — mất im lặng.
        #
        # Bản sau đó dùng `merge_golden_sets` và sai theo kiểu khác, DE bắt được ở review app#68:
        # hàm đó phân xử va chạm bằng luật `source` (chỉ `human` thắng `ai`), nên **mọi** cặp khác
        # đều ném — kể cả hai case GIỐNG HỆT nhau. Hệ quả: nạp lại đúng bộ vừa nạp (chạy lại script,
        # thử lại sau lỗi mạng, sửa một case rồi gửi cả bộ) trả 409 trên mọi case trùng khoá. Đó là
        # hồi quy thẳng so với `write_golden_set`, mà docstring của nó khai use-case chính là "nạp
        # lại một bộ đã sửa" — tức upsert idempotent.
        #
        # Gốc rễ là dùng sai công cụ, không phải sai tham số: `merge_golden_sets` hợp nhất hai nguồn
        # NGANG HÀNG, nơi va chạm nghĩa là "hai bên bất đồng và không ai có thẩm quyền" — ném ra cho
        # người quyết là đúng. Còn ở đây người dùng chủ động nạp bộ của mình lên ref của chính mình:
        # họ LÀ bên có thẩm quyền trên những khoá họ gửi. Đó là phép **phủ**, và phủ thì không có
        # khái niệm va chạm.
        #
        # Luật: case vừa nạp thắng trên khoá của nó; case cũ có khoá KHÔNG nằm trong bộ nạp thì giữ
        # nguyên. Dùng `case_key` của evalhub chứ không tự chuẩn hoá lại — hai cách chuẩn hoá câu
        # hỏi lệch nhau là hai bộ trông giống nhau mà không khớp được.
        try:
            existing = await read_golden_set(conn, golden.golden_set_ref, tenant_uuid)
        except GoldenSetNotFound:
            merged, n_kept = golden, 0
        else:
            merged, n_kept = overlay_golden_set(golden, existing)

        try:
            await write_golden_set(conn, merged, tenant_uuid)
        except GoldenSetScopeError as exc:  # pragma: no cover — chỉ tới được khi bind ở trên hỏng
            raise HTTPException(status_code=500, detail=f"golden set scope: {exc}") from exc

    return UploadGoldenSetResponse(
        golden_set_ref=merged.golden_set_ref,
        tenant_id=str(tenant_uuid),
        n_case=len(merged.cases),
        n_traps=sum(1 for c in merged.cases if c.expects_refusal),
        n_uploaded=len(golden.cases),
        n_kept_from_existing=max(n_kept, 0),
    )


class RegenerateRequest(BaseModel):
    section_role: str = Field(min_length=1)
    tenant_id: str | None = None


class RegenerateResponse(BaseModel):
    golden_set_ref: str
    n_cases: int
    n_ai: int
    n_human: int
    """Case người dùng tự viết (`source="human"`) được GIỮ NGUYÊN qua lần sinh lại. Khai riêng vì
    đó đúng là câu người bấm nút lo: *"bấm cái này có mất phần tôi gõ tay không?"*"""

    written: bool
    """Bộ trong DB có được thay bằng kết quả lượt này không.

    `written=False` nghĩa là lượt dựng lại không ra case nào nên bộ CŨ được giữ nguyên (guard
    rỗng-thì-không-ghi ở `regenerate_for_section`, có lý do riêng: bộ 0 case đi tiếp vào
    `EvalHarness.run()` cho `success_rate` trên mẫu số 0). Trước bản vá này route trả `n_cases=0`
    cho ca đó y như ca bộ thật sự rỗng (review app#71, Dozyboy, đợt 2, mục 1) — người bấm nút tin
    bộ cũ đã biến mất, trong khi cổng publish vẫn chấm bằng đúng bộ cũ đó. Cờ này là thứ DUY NHẤT
    phân biệt được hai ca."""


@router.post("/regenerate", response_model=RegenerateResponse)
async def regenerate_golden_set(body: RegenerateRequest) -> RegenerateResponse:
    """Dựng lại bộ câu hỏi kiểm thử của MỘT phòng ban từ tài liệu đang có, không cần upload gì.

    Trước route này, đường sinh máy chỉ chạy như **tác dụng phụ của việc nạp tài liệu**
    (`routes/documents.py`). Nên một tenant đã nạp đủ tài liệu mà muốn dựng lại bộ — vì vừa xoá vài
    tài liệu, vì bộ cũ sinh ra khi KB còn thiếu, hay đơn giản vì muốn bắt đầu lại — **không có cách
    nào** ngoài việc upload lại một tài liệu bất kỳ. Đó là bắt người dùng làm một việc không liên
    quan để đạt được việc họ cần.

    Case `source="human"` **được giữ**: `regenerate_for_section` đọc bộ cũ và mang chúng sang. Đây
    là lý do form nhập tay bắt buộc gắn nhãn đó — không có nhãn, lần sinh lại kế tiếp xoá sạch.
    """
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_admin(identity.system_roles)

    if "superadmin" in identity.system_roles:
        if body.tenant_id is None:
            raise HTTPException(status_code=400, detail="superadmin phải khai tenant_id")
        try:
            tenant_uuid = UUID(body.tenant_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"tenant_id không phải UUID hợp lệ: {body.tenant_id!r}"
            ) from exc
    else:
        if body.tenant_id is not None and body.tenant_id != str(identity.tenant_id):
            raise HTTPException(status_code=403, detail="company-admin chỉ dựng lại được cho tenant mình")
        tenant_uuid = identity.tenant_id

    # Kiểm tenant TỒN TẠI trước, phòng ban sau — review app#71: superadmin gõ nhầm `tenant_id` mà
    # nhận `400 "phòng ban không hợp lệ"` thì thông báo chỉ sai chỗ, và họ sẽ đi sửa tên phòng ban
    # trong khi lỗi nằm ở tenant. Một tenant không tồn tại thì mọi phòng ban đều "không hợp lệ",
    # nên thứ tự cũ biến 404 thành một nhánh gần như không bao giờ tới được.
    cur = await conn.execute("SELECT name FROM core.tenants WHERE id = %s", (tenant_uuid,))
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"tenant {tenant_uuid} không tồn tại")
    tenant_slug = str(row[0])

    valid = await fetch_tenant_section_names(conn, str(tenant_uuid))
    if body.section_role not in valid:
        raise HTTPException(
            status_code=400,
            detail=f"phòng ban {body.section_role!r} không hợp lệ — chỉ chấp nhận {sorted(valid)}",
        )

    # `ReadOnlyEmbedding`, KHÔNG `build_embedding` — review app#71 (Dozyboy) bắt đúng chỗ tôi tái
    # phát defect đã tự vá ở `documents.py`. `regenerate_for_section` chỉ gọi `chunks_for_tenant`,
    # không embed gì; còn `build_embedding` ném **503** khi thiếu `STUDIO_OPENROUTER_API_KEY`.
    #
    # Ở route này nó sai gấp đôi: "dựng lại bộ mà không cần nạp lại tài liệu" đúng là thứ người ta
    # cần **lúc provider đang hỏng** — chặn nó bằng chính lỗi provider là chặn đường thoát hiểm.
    pipeline = KbPipeline(await get_pool(), ReadOnlyEmbedding())
    async with borrowed_tenant_scope(conn, tenant_uuid):
        report = await regenerate_for_section(
            conn, pipeline, tenant_id=tenant_uuid, tenant_slug=tenant_slug, section_role=body.section_role
        )

    return RegenerateResponse(
        golden_set_ref=report.golden_set_ref,
        n_cases=report.n_cases,
        n_ai=report.n_ai,
        n_human=report.n_human,
        written=report.written,
    )


class GoldenSetSummaryResponse(BaseModel):
    golden_set_ref: str
    n_cases: int
    n_ai: int
    n_human: int
    n_trap: int
    created_at: str


class GoldenCaseView(BaseModel):
    """Một case như người dùng NHÌN, không phải như bộ chấm đọc.

    Khác `GoldenCase` ở hai chỗ, và cả hai là cố ý:

    - `expected_citation` rút thành `n_citation` + `citations`: danh sách `chunk_id` đầy đủ có thể
      dài vài chục dòng cho một case, và người đang xem *bộ này hỏi những gì* cần con số trước, chi
      tiết sau.
    - thêm `is_trap` — suy tại đây từ `expected_citation` rỗng, cùng đường suy với `list_golden_sets`
      và `GoldenCase.is_refusal`. Đưa vào payload vì đó là thứ người đọc cần phân biệt ngay: một
      case bẫy KỲ VỌNG agent từ chối, nên "trả lời sai" ở đó lại là đúng.
    """

    case_id: str
    query: str
    expected: str
    section_roles: list[str]
    expected_section_role: str
    source: str | None
    is_trap: bool
    n_citation: int
    citations: list[str]


class GoldenSetDetailResponse(BaseModel):
    golden_set_ref: str
    n_cases: int
    cases: list[GoldenCaseView]


@router.get("", response_model=list[GoldenSetSummaryResponse])
async def list_golden_sets_route() -> list[GoldenSetSummaryResponse]:
    """Danh sách bộ golden của tenant — màn hình mở đầu, chưa mang nội dung case nào.

    Trước route này `eval.golden_sets` chỉ có đường GHI (`POST ""`, `POST /regenerate`) và đường
    đọc-lúc-chấm; **không có đường nào để người dùng nhìn thấy bộ của họ**. Hệ quả không phải là
    thiếu tiện nghi mà là mất khả năng chẩn đoán: bấm Chấm điểm ra `FAIL 0.35` thì không có cách
    nào phân biệt *"agent trả lời kém"* với *"bộ câu hỏi tự sinh đang hỏi những thứ vô nghĩa"* —
    hai kết luận dẫn tới hai việc phải làm hoàn toàn khác nhau.
    """
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_admin(identity.system_roles)

    # Superadmin không có tenant riêng (JWT trỏ `__system__`) nên không có bộ nào để liệt kê ở đây.
    # Trả 400 thay vì danh sách rỗng: rỗng đọc như "công ty này chưa có bộ nào", còn sự thật là câu
    # hỏi không áp dụng cho vai đó — dùng `POST /regenerate` kèm `tenant_id` nếu cần đụng công ty cụ
    # thể.
    if "superadmin" in identity.system_roles:
        raise HTTPException(
            status_code=400,
            detail="superadmin không thuộc công ty nào — đăng nhập bằng admin của công ty để xem bộ golden",
        )

    rows = await list_golden_sets(conn, UUID(str(identity.tenant_id)))
    return [
        GoldenSetSummaryResponse(
            golden_set_ref=row.golden_set_ref,
            n_cases=row.n_cases,
            n_ai=row.n_ai,
            n_human=row.n_human,
            n_trap=row.n_trap,
            created_at=row.created_at.isoformat(),
        )
        for row in rows
    ]


@router.get("/{golden_set_ref}", response_model=GoldenSetDetailResponse)
async def read_golden_set_route(golden_set_ref: str) -> GoldenSetDetailResponse:
    """Nội dung một bộ — câu hỏi, đáp án kỳ vọng, và case nào là bẫy.

    Đây là chỗ trả lời câu *"vì sao điểm thấp"*. Bộ sinh máy đặt `query` từ chính văn bản chunk
    (`ExtractiveQuestionWriter`) và `expected_citation` là **toàn bộ** chunk của lô đã sinh ra câu
    đó — nên một agent lấy top-k vài chunk sẽ không bao giờ trích đủ, và `citation_accuracy` tụt
    thấp mà không phải vì agent sai. Không nhìn được bộ thì không thấy được điều đó.
    """
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_admin(identity.system_roles)

    if "superadmin" in identity.system_roles:
        raise HTTPException(
            status_code=400,
            detail="superadmin không thuộc công ty nào — đăng nhập bằng admin của công ty để xem bộ golden",
        )

    try:
        golden = await read_golden_set(conn, golden_set_ref, UUID(str(identity.tenant_id)))
    except GoldenSetNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return GoldenSetDetailResponse(
        golden_set_ref=golden.golden_set_ref,
        n_cases=len(golden.cases),
        cases=[
            GoldenCaseView(
                case_id=case.case_id,
                query=case.query,
                expected=case.expected,
                section_roles=list(case.section_roles),
                expected_section_role=case.expected_section_role,
                source=case.source,
                is_trap=len(case.expected_citation) == 0,
                n_citation=len(case.expected_citation),
                citations=list(case.expected_citation),
            )
            for case in golden.cases
        ],
    )
