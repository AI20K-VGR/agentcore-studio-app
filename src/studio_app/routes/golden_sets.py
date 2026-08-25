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
from studio_evalhub.golden_merge import GoldenSetMergeConflict, merge_golden_sets
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

        # HỢP NHẤT với bộ đang có, không ghi đè. Bản trước gọi thẳng `write_golden_set`, nên một
        # người nạp bộ của mình trùng `golden_set_ref` sẽ **xoá sạch** bộ máy vừa sinh lúc upload
        # tài liệu (`golden_autogen.regenerate_for_section`, app#61) — mất im lặng, không cảnh báo,
        # và hai đường ghi vào cùng một `golden_set_ref` lại có hai luật khác nhau (đường sinh máy
        # đã biết giữ `source="human"`, đường này thì không). `merge_golden_sets` là luật ĐÃ CÓ cho
        # đúng việc này: khoá theo `(tenant, query chuẩn hoá, section_roles)` chứ không theo
        # `case_id` — hai nguồn đặt id độc lập nhau nên khoá theo id là khoá theo thứ không so được.
        #
        # Bộ vừa nạp đứng TRƯỚC: người vừa gõ tay thắng bản máy sinh ở case trùng. Đó là toàn bộ lý
        # do `source="human"` tồn tại.
        try:
            existing = await read_golden_set(conn, golden.golden_set_ref, tenant_uuid)
        except GoldenSetNotFound:
            merged = golden
            n_kept = 0
        else:
            try:
                merged = merge_golden_sets(golden, existing, golden_set_ref=golden.golden_set_ref)
            except GoldenSetMergeConflict as exc:
                # 409, không 500: hai bộ **cùng khoá mà khác đáp án kỳ vọng** là mâu thuẫn dữ liệu
                # người dùng giải được (sửa case hoặc đổi ref), không phải hỏng hệ thống. Trả nguyên
                # danh sách xung đột — `merge_golden_sets` liệt kê TẤT CẢ chứ không dừng ở cái đầu,
                # nên người nạp sửa được một lượt thay vì bóc từng cái qua nhiều lần thử.
                raise HTTPException(status_code=409, detail=f"bộ nạp lên xung đột với bộ đang có: {exc}") from exc
            n_kept = len(merged.cases) - len(golden.cases)

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
