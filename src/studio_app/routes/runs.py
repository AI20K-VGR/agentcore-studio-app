"""`POST /api/runs` (Kế hoạch 2, A3 → app#44 mục D) — nút "Test", ĐỔI HẲN Ý NGHĨA.

`PROJECT-SCOPE-DEMO-DAY30.md` mục D: bấm "Test" giờ chỉ xác nhận từng tool trong
`agent_config.tool_whitelist` của agent có executor/dispatcher THẬT hay không (vd `kb_search: OK`,
`calculator: OK`) — đây là **connectivity-check TĨNH**, KHÔNG chạy 1 lượt hội thoại thật, KHÔNG gọi
LLM/KB, KHÔNG tạo trace hội thoại. 3 việc đó (chạy thử câu hỏi, xem trace, money-shot fence-proof)
chuyển hẳn sang mục E (`routes/chat.py`) — đó mới là nơi LLM thật sự chạy vòng lặp với tool.

Trước app#44, route này dựng `Recipe` động từ `nodes`/`edges` client gửi
(`studio_workbench.builder.create_dynamic_recipe`) rồi chạy `interpreter.run()` (DAG-walk cũ, thay
`dev_playground_server.py`). Kiến trúc mới (1 LLM + N tool tự chọn) không còn khái niệm DAG/canvas
ở tầng chạy — `run_agent_loop()` không đọc `recipe.dag` — nên toàn bộ luồng dựng recipe động qua
`nodes`/`edges` không còn cần thiết CHO ENDPOINT NÀY: `RunRequest` chỉ còn `agent_id` +
`tool_whitelist`, đúng dữ liệu tối thiểu để trả lời câu hỏi "tool này nối được chưa".

`RunRequest`'s field DAG-shaped cũ (`nodes`/`edges`/`kb_id`/`scope`/`golden_set_ref`/threshold) dời
sang `routes/publish.py::PublishRequest` — `/evaluate`/`/publish` KHÔNG đổi scope (mục F:
"Eval chạy qua code path riêng, không đi qua nút Test — không bị ảnh hưởng"), vẫn cần
`nodes`/`edges` để `create_dynamic_recipe(...)` dựng `recipe.dag` (field bắt buộc trên contract
`Recipe` dù `run_agent_loop()` không đọc nó).

`GET /api/runs/{run_id}` KHÔNG đổi — nó đọc lại trace CHAT thật (qua `PgTraceReader`), không liên
quan gì tới connectivity-check của `POST` ở trên; `RunResponse` (trace shape) giữ nguyên, chỉ dùng
cho GET từ app#44 trở đi.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from studio_engine.agent_protocol import KB_SEARCH_TOOL
from studio_kb.trace_reader import PgTraceReader

from studio_app.authz import fetch_fresh_identity, require_admin
from studio_app.core._db import get_pool
from studio_app.middleware import get_request_connection, get_request_session
from studio_app.providers.tool_dispatch import SUPPORTED_TOOLS

router = APIRouter(prefix="/api/runs", tags=["runs"])


class RunRequest(BaseModel):
    """app#44 — tối giản hoá theo mục D: chỉ còn 2 field cần cho connectivity-check tĩnh.
    `tenant_id` CỐ Ý KHÔNG có field ở đây (như bản trước app#44) — dù giờ route này không còn chạm
    tenant/DB nào ngoài chính identity-check của `require_admin`, giữ nguyên kỷ luật "request body
    không có chỗ để client khai tenant" cho nhất quán toàn bộ `apps/studio`."""

    agent_id: str
    tool_whitelist: list[str] = Field(default_factory=list)


class ConnectivityCheckResponse(BaseModel):
    """`results`: `list[dict[str, str]]` chứ không phải 1 sub-model riêng (`{"tool": str, "status":
    "OK"|"NOT_IMPLEMENTED"}`) — CỐ Ý, bug thật đã trúng ở CI (không lộ ra local vì DB-dependent test
    này skip khi thiếu Postgres): `_check_tool_connectivity()` trả thẳng `list[dict[str, str]]`,
    và một sub-model Pydantic (`ToolCheckResult`) so `==` với `dict` KHÔNG BAO GIỜ bằng nhau (Pydantic
    `BaseModel.__eq__` so kiểu, không so giá trị với `dict` trần) — làm mọi test so `response.results
    == [{"tool":...,"status":...}, ...]` đỏ dù giá trị JSON ra hệt nhau. Giữ nguyên dict thẳng để
    FastAPI vẫn validate/serialize đúng shape mà không cần lớp trung gian."""

    agent_id: str
    results: list[dict[str, str]]


def _check_tool_connectivity(tool_whitelist: list[str]) -> list[dict[str, str]]:
    """Với mỗi tool trong `tool_whitelist`, trả `"OK"` nếu có executor/dispatcher THẬT, ngược lại
    `"NOT_IMPLEMENTED"`. Thứ tự giữ NGUYÊN theo `tool_whitelist` (không sort/dedupe) — UI hiển thị
    đúng thứ tự agent khai.

    `kb_search` LUÔN `"OK"`: nó có executor riêng (`KbRetrieveExecutor`, đã real) không đi qua
    `ToolDispatch` — cùng bất biến A4 mà `run_agent_loop()` (engine#33) dùng ("kb_search luôn khả
    dụng, không bị `tool_whitelist` chặn"). Tool khác `"OK"` nếu ∈ `SUPPORTED_TOOLS`
    (`providers/tool_dispatch.py::RealToolDispatch` — nguồn sự thật DUY NHẤT dispatch được tool gì),
    ngược lại `"NOT_IMPLEMENTED"` — không raise, không 400: đây là kết quả HIỂN THỊ, không phải lỗi
    input (một agent có tool chưa nối được vẫn là 1 trạng thái hợp lệ để xem trước khi chat thật)."""
    results: list[dict[str, str]] = []
    for tool in tool_whitelist:
        ok = tool == KB_SEARCH_TOOL or tool in SUPPORTED_TOOLS
        results.append({"tool": tool, "status": "OK" if ok else "NOT_IMPLEMENTED"})
    return results


@router.post("", response_model=ConnectivityCheckResponse)
async def create_run(body: RunRequest) -> ConnectivityCheckResponse:
    session = get_request_session()

    # Gate role-gap giữ nguyên từ bản trước app#44 (đã đóng, kit#41 review) — không phải mọi tài
    # khoản đăng nhập đều gọi được, dù endpoint giờ không chạm KB/LLM/DB nào khác ngoài chính
    # identity-check này.
    identity = await fetch_fresh_identity(get_request_connection(), session.user)
    require_admin(identity.roles)

    results = _check_tool_connectivity(body.tool_whitelist)
    return ConnectivityCheckResponse(agent_id=body.agent_id, results=results)


class RunResponse(BaseModel):
    """app#44 — GIỮ NGUYÊN, chỉ còn dùng cho `GET /api/runs/{run_id}` (đọc lại trace CHAT thật, xem
    docstring module). KHÔNG còn là response của `POST` ở trên (đổi tên thành
    `ConnectivityCheckResponse`, shape khác hẳn)."""

    run_id: str
    agent_id: str
    tenant_id: str
    events: list[dict[str, Any]]
    timeline_text: str


@router.get("/{run_id}", response_model=RunResponse)
async def get_run(run_id: str) -> RunResponse:
    session = get_request_session()
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
