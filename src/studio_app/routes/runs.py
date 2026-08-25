"""`GET /api/runs/{run_id}` — đọc lại trace CHAT thật (qua `PgTraceReader`), dùng chung cho cả
tab "Dùng thử" (agent đã publish, `routes/chat.py`) lẫn nút Test (draft, `routes/test_chat.py`) —
trace ghi vào `obs.trace_events` độc lập hoàn toàn với `wb.recipes`/trạng thái publish (không
FK/JOIN), nên đọc lại bằng đúng `run_id` là đủ, không cần biết run đó tới từ đường nào.

`POST /api/runs` (connectivity-check tĩnh OK/NOT_IMPLEMENTED, nút Test bản cũ) đã BỎ HẲN — thay
bằng `routes/test_chat.py::POST /api/agents/{agent_id}/test-chat` (chat thật trên draft, xem
docstring file đó). 1 lượt chat thật tự nói lên tool chạy được hay không, rõ hơn hẳn bảng tĩnh cũ.

`require_admin` thêm vào route GET dưới đây (trước đây không có gate quyền nào ngoài tenant RLS) —
nhân viên (tab Chat published) không được phép đọc trace, chỉ admin (tab "Dùng thử"/nút Test) mới
xem được các bước hệ thống chạy qua. Ẩn nút "Xem trace" ở FE (`ChatPage.tsx`) chỉ là UX — hàng rào
thật nằm ở đây, chặn cả trường hợp nhân viên tự gọi thẳng API biết trước `run_id`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from studio_kb.trace_reader import PgTraceReader

from studio_app.authz import fetch_fresh_identity, require_admin
from studio_app.core._db import get_pool
from studio_app.middleware import get_request_connection, get_request_session

router = APIRouter(prefix="/api/runs", tags=["runs"])


class RunResponse(BaseModel):
    run_id: str
    agent_id: str
    tenant_id: str
    events: list[dict[str, Any]]
    timeline_text: str


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(run_id: str) -> RunResponse:
    session = get_request_session()

    identity = await fetch_fresh_identity(get_request_connection(), session.user)
    require_admin(identity.roles)

    pool = await get_pool()
    events = await PgTraceReader(pool).read_run(run_id, session.tenant_id)
    if not events:
        raise HTTPException(status_code=404, detail=f"run_id {run_id!r} không tìm thấy cho tenant hiện tại")
    return RunResponse(
        run_id=run_id,
        agent_id=events[0].agent_id,
        tenant_id=str(session.tenant_id),
        events=[e.model_dump(mode="json") for e in events],
        # `render_timeline` cần `expected` từ `dag` gốc — GET không giữ recipe (chỉ đọc lại
        # trace), nên không tái tạo được cột "gap" chính xác ở đây; để trống có chủ đích, khác
        # `dev_playground_server.py` (nó cache `timeline_text` lúc POST). Theo dõi như 1 hạn chế
        # đã biết, không phải lỗi — client nên dùng `timeline_text` từ response POST gốc.
        #
        # app#44: "response POST gốc" ở trên nói tới THỜI KỲ TRƯỚC app#44 — `POST /api/runs` giờ
        # không còn chạy interpreter/tạo trace (đổi hẳn thành connectivity-check), nên timeline
        # thật chỉ còn tới từ `POST /api/agents/{id}/chat` (`routes/chat.py`). `render_timeline`
        # (từng dùng ở đây) đã bỏ khỏi import — không còn `dag`/`nodes` nào ở tầng route này để
        # dựng `expected` từ đó nữa.
        timeline_text="",
    )
