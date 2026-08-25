"""`GET /api/agents` + `POST /api/agents/{agent_id}/rollback` — cùng `prefix="/api/agents"` với
`routes/chat.py`/`routes/publish.py` (FastAPI cho nhiều `APIRouter` chung prefix, mỗi router 1
concern riêng — đúng convention module-per-concern đã dùng xuyên suốt `routes/`).

`GET ""` phục vụ UI chọn agent (dropdown chat của employee, dropdown rollback của admin) — quyết
định "nhiều agent/công ty" (không phải 1 agent/công ty) đã chốt: mỗi tenant có thể có nhiều
`agent_id` khác nhau, mỗi cái published độc lập.

`rollback()` (`studio_workbench.publish`) đã implement đầy đủ từ trước — route này chỉ NỐI DÂY,
không viết lại logic rollback."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError
from studio_contracts import Recipe
from studio_workbench.publish import rollback

from studio_app.authz import fetch_fresh_identity, require_admin
from studio_app.middleware import get_request_connection, get_request_session

router = APIRouter(prefix="/api/agents", tags=["agents"])


class AgentSummary(BaseModel):
    agent_id: str
    latest_published_version: int


@router.get("", response_model=list[AgentSummary])
async def list_agents() -> list[AgentSummary]:
    """Bất kỳ ai đã đăng nhập (KHÔNG cần role admin) — RLS trên `wb.recipes` đã tự lọc theo
    `app.tenant_id` của connection (`tenant_context_middleware`), đúng lý do `_load_published_recipe`
    (`routes/chat.py`) cũng không tự thêm `WHERE tenant_id` tay: RLS là hàng rào, không phải lớp
    phụ. `get_request_session()` chỉ để 401 nếu chưa đăng nhập — không dùng giá trị trả về."""
    get_request_session()
    conn = get_request_connection()

    cur = await conn.execute(
        """
        SELECT DISTINCT ON (agent_id) agent_id, version
        FROM wb.recipes
        WHERE status = 'published'
        ORDER BY agent_id, version DESC
        """
    )
    rows = await cur.fetchall()
    return [AgentSummary(agent_id=row[0], latest_published_version=row[1]) for row in rows]


class VersionSummary(BaseModel):
    version: int
    status: str
    created_at: str


@router.get("/{agent_id}/versions", response_model=list[VersionSummary])
async def list_agent_versions(agent_id: str) -> list[VersionSummary]:
    """Liệt kê THẬT các version đã từng publish cho `agent_id` (đọc `wb.recipe_versions`, RLS tự
    lọc theo tenant) — cho UI Rollback dựng dropdown chọn đúng version tồn tại, thay vì để admin
    tự gõ số tuỳ ý rồi chờ 404 (`rollback()` vẫn fail-closed như cũ, đây chỉ thêm gợi ý UX thật
    dựa trên dữ liệu có sẵn, không thay đổi hàng rào)."""
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_admin(identity.system_roles)

    cur = await conn.execute(
        """
        SELECT version, status, created_at
        FROM wb.recipe_versions
        WHERE agent_id = %s
        ORDER BY version DESC
        """,
        (agent_id,),
    )
    rows = await cur.fetchall()
    return [VersionSummary(version=row[0], status=row[1], created_at=row[2].isoformat()) for row in rows]


class AgentRecipeResponse(BaseModel):
    recipe: dict[str, object]
    version: int
    status: str


@router.get("/{agent_id}/recipe", response_model=AgentRecipeResponse)
async def get_agent_recipe(agent_id: str, version: int | None = None) -> AgentRecipeResponse:
    """Nạp recipe THẬT cho Canvas — trước bản vá này Canvas không hề đọc `wb.recipes`, luôn khởi
    tạo từ 1 sample cứng (`apps/web/src/recipe/sample.ts`), nên sau publish/rollback Canvas không
    có cách nào hiện đúng nội dung/version đang sống.

    Không truyền `version` → bản `status='published'` mới nhất, cùng nguồn `_load_published_recipe`
    (`routes/chat.py`). Có `version` → đọc `wb.recipe_versions` (lịch sử append-only, KHÔNG phải
    `wb.recipes`) — `rollback()` (`studio_workbench.publish`) tự thừa nhận `wb.recipes` có thể
    KHÔNG còn giữ row của version cũ (rollback tái tạo lại từ `wb.recipe_versions` khi cần, xem
    docstring hàm đó), nên đây là nguồn duy nhất luôn có đủ MỌI version từng tồn tại, kể cả đã
    `rolled_back` — cho phép Canvas soi đúng "đang xem bản nào" bất kể trạng thái hiện tại.

    Admin-only (giống `list_agent_versions`) — Canvas là công cụ dựng agent, cùng nhóm quyền với
    Rollback, không phải màn nhân viên như Chat."""
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_admin(identity.system_roles)

    if version is None:
        cur = await conn.execute(
            "SELECT recipe, version, status FROM wb.recipes WHERE agent_id = %s AND status = 'published' "
            "ORDER BY version DESC LIMIT 1",
            (agent_id,),
        )
    else:
        cur = await conn.execute(
            "SELECT recipe, version, status FROM wb.recipe_versions WHERE agent_id = %s AND version = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (agent_id, version),
        )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"agent_id {agent_id!r} không có recipe version {version!r} cho tenant hiện tại",
        )
    # KHÔNG trả thẳng `row[0]` (JSONB thô) — `Edge.from_` (`Field(alias="from")`,
    # `populate_by_name=True`) khiến `wb.recipes.recipe` có thể lưu bằng TÊN FIELD PYTHON
    # (`"from_"`) thay vì alias dây thật (`"from"`), tuỳ đường ghi nào tạo ra hàng đó
    # (`model_dump()` mặc định không `by_alias`). Đọc lại qua `Recipe.model_validate()`
    # (`populate_by_name=True` chấp nhận CẢ HAI dạng) rồi dump lại `by_alias=True` đảm bảo response
    # LUÔN đúng hình dạng dây (`"from"`) — client (`WireRecipe`/`fromCanvas.ts`) không phải đoán
    # đường nào đã ghi hàng đó. Thiếu bước này: `toCanvas.ts::fromRecipe()` đọc `edge.from` ra
    # `undefined`, mọi cạnh trỏ về 1 node "…__undefined" không tồn tại — canvas nạp xong TRÔNG như
    # có cạnh (handle nối vẫn vẽ chấm) nhưng `graph_lint`/`buildRecipe` thấy 0 cạnh thật, báo "4
    # start node" (đã tái hiện + xác nhận đúng nguyên nhân qua DB thật, không phải suy đoán).
    #
    # Đánh đổi đi kèm: `model_validate()` + `model_dump()` KHÔNG passthrough — field nào contract
    # hiện tại không khai (vd. ghi bởi 1 rev contract mới hơn, additive-only) bị Pydantic
    # `extra='ignore'` (mặc định) âm thầm cắt khỏi response, dù vẫn còn nguyên trong DB.
    #
    # Row cũ không còn hợp lệ theo contract HIỆN TẠI (vd. `scorecard_threshold.success` publish
    # trước khi kit#129 §3.1 siết `ge=0.0, le=1.0`) làm `model_validate()` ném `ValidationError` —
    # không có handler nào cho lỗi này (`app.py` chỉ bắt `RequestValidationError`), nên trần ra sẽ
    # thành 500 không detail. Bắt riêng để admin soi version cũ (đường phục hồi/rollback) thấy lý do
    # thay vì 500 câm.
    #
    # Review app#37 round 2 (dholmes0207, N1): `str(exc)` echo `input_value` của field TRƯỢT
    # validate — hôm nay an toàn (field trượt luôn là `scorecard_threshold.success`, không nhạy
    # cảm), nhưng `agent_config.instructions`/`kb_binding.scope` (`schema.py` xếp loại tenant PII,
    # đúng lý do bật RLS `FORCE`) nằm CÙNG model. Ngày nào một ràng buộc mới rơi lên 1 trong 2 field
    # đó (đúng khuôn kit#129 §3.1 vừa làm với `scorecard_threshold`), `str(exc)` sẽ mang tenant PII
    # vào body 500 → access log của reverse-proxy/APM — đúng hạng lỗ mà
    # `_redact_sensitive_validation_errors` (`app.py:153`) đóng ở chiều REQUEST, chỉ khác chiều
    # RESPONSE. `include_input=False` cắt `input_value` khỏi mọi error entry (giữ lại `loc`/`type`/
    # `msg` — đủ để admin biết field nào hỏng), `include_url=False` bỏ link docs pydantic.io không
    # cần thiết trong response lỗi.
    try:
        recipe = Recipe.model_validate(row[0])
    except ValidationError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                f"recipe version {row[1]!r} không đọc được theo contract hiện tại: "
                f"{exc.errors(include_input=False, include_url=False)}"
            ),
        ) from exc
    return AgentRecipeResponse(recipe=recipe.model_dump(mode="json", by_alias=True), version=row[1], status=row[2])


class RollbackRequest(BaseModel):
    to_version: int


class RollbackResponse(BaseModel):
    agent_id: str
    tenant_id: str
    status: str = "rolled_back"
    version: int


@router.post("/{agent_id}/rollback", response_model=RollbackResponse)
async def rollback_agent(agent_id: str, body: RollbackRequest) -> RollbackResponse:
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_admin(identity.system_roles)

    # `session.tenant_id` (JWT), KHÔNG phải `identity.tenant_id` (tươi) — khớp đúng convention đã
    # có ở `routes/chat.py`/`routes/runs.py` (cả 2 dùng `session.tenant_id` cho mọi thao tác đọc/ghi
    # nghiệp vụ, chỉ `routes/admin.py` mới cần tenant TƯƠI vì nó GHI vào đúng tenant người gọi hiện
    # tại). Ca admin bị re-home tenant giữa chừng là lỗ hổng ĐÃ BIẾT, ghi nhận ở `routes/admin.py`
    # (dòng comment "CÒN SÓT" trong `create_user`) — ngoài phạm vi PR này, không tự vá lặt vặt ở
    # 1 route mà để hở chỗ khác.
    try:
        await rollback(agent_id, session.tenant_id, to_version=body.to_version, conn=conn)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return RollbackResponse(agent_id=agent_id, tenant_id=str(session.tenant_id), version=body.to_version)
