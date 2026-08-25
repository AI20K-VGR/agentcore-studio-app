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
from psycopg import sql
from pydantic import BaseModel, Field, ValidationError
from studio_evalhub.golden_case import GoldenCase, GoldenSet
from studio_evalhub.golden_merge import case_key
from studio_evalhub.golden_store import GoldenSetNotFound, GoldenSetScopeError, read_golden_set, write_golden_set

from studio_app.authz import fetch_fresh_identity, require_admin
from studio_app.middleware import get_request_connection, get_request_session

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

    async with conn.transaction():
        # Superadmin nạp hộ tenant khác: `app.tenant_id` của phiên đang trỏ `__system__`, nên
        # `write_golden_set` sẽ từ chối (`GoldenSetScopeError`) — đúng thiết kế của nó. Bind lại
        # TƯỜNG MINH ở đây là cách **thoả** cổng đó, không phải vượt qua nó: connection thật sự được
        # scope về tenant đích trong đúng transaction này. `SET LOCAL` hết hiệu lực khi transaction
        # đóng, nên phần còn lại của request không thừa hưởng quyền này.
        await conn.execute(sql.SQL("SET LOCAL app.tenant_id = {}").format(sql.Literal(str(tenant_uuid))))

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
