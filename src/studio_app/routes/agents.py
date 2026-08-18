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
from pydantic import BaseModel
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
    require_admin(identity.roles)

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
