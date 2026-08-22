"""`routes/runs.py::create_run` — T1 IDOR ở TẦNG ROUTE (Kế hoạch 2, A3, honest-TODO khai ở
`docs/reports/gate-2/swe-Dozyboy.md` §7). Test đơn vị cho `tenant_wall.resolve_session` đã có từ
lâu (`packages/workbench/tests/test_wiring_d8.py`), nhưng chưa test nào đi qua chính route HTTP
thật — chỗ client thật sự chạm tới. Không dựng `TestClient`/ASGI (chưa có tiền lệ trong repo này,
xem `test_routes_auth.py`): gọi thẳng `create_run()` + set `_request_session` contextvar bằng
tay, đúng khuôn `test_middleware_jwt.py` đã dùng cho session, cộng `admin_pool` fixture đúng khuôn
`test_routes_auth.py` dùng cho DB.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import Token
from uuid import UUID

import pytest
import pytest_asyncio
from studio_app import middleware
from studio_app.core._db import Pool, close_pools, get_pool
from studio_app.routes.runs import RunRequest, create_run
from studio_workbench.tenant_wall import ResolvedContext

# Shape 4-node đã biết chắc qua được graph_lint() — y hệt studio_workbench.builder
# create_sample_recipe_d3(), chỉ viết lại thành dict thô vì RunRequest.nodes/edges là
# list[dict[str, Any]] (client gửi JSON, không phải đối tượng Node/Edge Python).
_NODES = [
    {"id": "node_1", "type": "kb-retrieve", "params": {"query": "Callisto policy"}},
    {"id": "node_2", "type": "llm-step", "params": {"temperature": 0.0}},
    # "calculator", not "kb_search": this node exists only to exercise the tool-call SHAPE through
    # graph_lint()/create_run() — since routes/runs.py now wires RealToolDispatch unconditionally
    # (engine#32), the placeholder tool here must be one RealToolDispatch actually implements.
    # "kb_search" is a kb-retrieve node KIND (node_1 above), never a real tool-call target; using
    # it here was a leftover placeholder that RealToolDispatch correctly rejects as unsupported.
    {"id": "node_3", "type": "tool-call", "params": {"tool": "calculator", "expression": "6 * 7"}},
    {"id": "node_4", "type": "end", "params": {}},
]
_EDGES = [
    {"from": "node_1", "to": "node_2"},
    {"from": "node_2", "to": "node_3"},
    {"from": "node_3", "to": "node_4"},
]


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    """`create_run()` chạm `get_pool()` (singleton process-wide) — cùng lý do/kỷ luật
    `test_routes_auth.py` đã tự áp cho `login()` (tránh `ValueError: The future belongs to a
    different loop` khi chạy full suite, event loop mới mỗi test)."""
    yield
    await close_pools()


async def _seed_tenant(admin_pool: Pool, name: str) -> UUID:
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", (name,))
        row = await cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


async def _seed_admin_user(admin_pool: Pool, tenant_id: UUID, email: str) -> None:
    """`create_run` từ bản vá gate role-gap (`authz.fetch_fresh_identity` + `require_admin`) giờ
    đòi `session.user` có dòng thật trong `core.users` với role `"admin"` — cùng lý do/convention
    `test_admin_routes.py::_seed_admin_user`."""
    async with admin_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, roles) VALUES (%s, %s, %s, %s)",
            (str(tenant_id), email, "not-a-real-hash", ["admin"]),
        )


@asynccontextmanager
async def _simulate_request_connection() -> AsyncIterator[None]:
    """`create_run` giờ chạm `get_request_connection()` (qua `fetch_fresh_identity`) — cùng
    convention `test_admin_routes.py::_simulate_request_connection`, xem docstring ở đó."""
    pool = await get_pool()
    async with pool.connection() as conn:
        token = middleware._request_conn.set(conn)
        try:
            yield
        finally:
            middleware._request_conn.reset(token)


def _set_session(tenant_id: UUID) -> Token[ResolvedContext | None]:
    """Set `_request_session` contextvar bằng tay — mô phỏng đúng việc `tenant_context_middleware`
    làm sau khi verify JWT thật, không cần dựng JWT vì đây không phải test của tầng chữ ký
    (`test_jwt_auth.py`/`test_middleware_jwt.py` đã phủ tầng đó)."""
    session = ResolvedContext(tenant_id=tenant_id, user="victim@ankor.vn", roles=["admin", "public"])
    return middleware._request_session.set(session)


async def test_create_run_ignores_client_declared_tenant_in_body(admin_pool: Pool) -> None:
    """T1 IDOR end-to-end tại chính route `POST /api/runs`. Kịch bản tấn công: attacker đăng nhập
    hợp lệ với tư cách tenant A (session thật), nhưng cố nhét `tenant_id` của tenant B (nạn nhân)
    thẳng vào JSON body, hy vọng server tin theo body thay vì session.

    Khoá thật của bài này KHÔNG nằm ở logic if/else nào — nó nằm ở chỗ `RunRequest` (Pydantic)
    CỐ Ý không khai field `tenant_id` (`routes/runs.py:49-52`), nên Pydantic tự bỏ field lạ
    (`model_config` mặc định không có `extra="allow"`) TRƯỚC KHI code của route kịp chạy dòng nào.
    Assert cả hai lớp: (1) field lạ không sống sót qua Pydantic, (2) response cuối cùng mang đúng
    tenant của SESSION, không phải bất kỳ giá trị nào trong body."""
    tenant_a = await _seed_tenant(admin_pool, "ankor-t1-victim")
    tenant_b_attacker_claims = await _seed_tenant(admin_pool, "borea-t1-attacker-target")
    await _seed_admin_user(admin_pool, tenant_a, "victim@ankor.vn")

    raw_body: dict[str, object] = {
        "agent_id": "agent-t1-idor-probe",
        "instructions": "irrelevant cho bài này",
        "model": "gemini-2.5-flash",
        "tool_whitelist": ["calculator"],
        "kb_id": "kb-callisto-v1",
        "scope": "ankor/public",
        "nodes": _NODES,
        "edges": _EDGES,
        # Trường tấn công — client cố khai tenant KHÁC session của chính nó:
        "tenant_id": str(tenant_b_attacker_claims),
    }

    body = RunRequest.model_validate(raw_body)
    assert not hasattr(body, "tenant_id"), (
        "RunRequest không có field tenant_id — nếu dòng này đỏ, ai đó đã thêm field tenant_id vào "
        "RunRequest và mở lại đúng lỗ IDOR mà route này cố tình đóng bằng schema"
    )

    token = _set_session(tenant_a)
    try:
        async with _simulate_request_connection():
            response = await create_run(body)
    finally:
        middleware._request_session.reset(token)

    assert response.tenant_id == str(tenant_a), (
        f"IDOR: response mang tenant {response.tenant_id}, đáng lẽ phải là tenant session "
        f"{tenant_a} — KHÔNG được là {tenant_b_attacker_claims} (giá trị attacker nhét vào body)"
    )
    assert response.tenant_id != str(tenant_b_attacker_claims)
    # Mọi event trong trace cũng phải mang đúng tenant session — không chỉ mỗi field đỉnh response.
    for event in response.events:
        assert event["tenant_id"] == str(tenant_a)


async def test_create_run_without_session_raises_401(admin_pool: Pool) -> None:
    """Đối trọng bắt buộc của bài trên: KHÔNG có session (chưa đăng nhập) phải chặn ở
    `get_request_session()` (401), không được lặng lẽ chạy tiếp với tenant rỗng/None — đúng
    contract fail-closed `middleware.py::get_request_session` đã ghi (`middleware.py:45-67`)."""
    del admin_pool  # bảng core.tenants cần tồn tại (schema đã ensure) nhưng không seed gì
    from fastapi import HTTPException

    body = RunRequest.model_validate(
        {
            "agent_id": "agent-t1-idor-probe-2",
            "instructions": "irrelevant",
            "model": "gemini-2.5-flash",
            "kb_id": "kb-callisto-v1",
            "scope": "ankor/public",
            "nodes": _NODES,
            "edges": _EDGES,
        }
    )

    with pytest.raises(HTTPException) as exc_info:
        await create_run(body)
    assert exc_info.value.status_code == 401


def _threshold_body(success_threshold: float, citation_accuracy_threshold: float) -> dict[str, object]:
    return {
        "agent_id": "agent-threshold-probe",
        "instructions": "irrelevant",
        "model": "gemini-2.5-flash",
        "tool_whitelist": ["calculator"],
        "kb_id": "kb-callisto-v1",
        "scope": "ankor/public",
        "nodes": _NODES,
        "edges": _EDGES,
        "success_threshold": success_threshold,
        "citation_accuracy_threshold": citation_accuracy_threshold,
    }


def test_run_request_rejects_out_of_range_thresholds() -> None:
    """`kit#129` §3.1, vấn đề A (VinSOC AV-203052) — trước bản vá, `RunRequest` nhận thẳng
    `success_threshold`/`citation_accuracy_threshold` từ body, không kiểm gì: gửi `-999` được
    chấp nhận, mọi agent qua `POST /api/runs`/`/evaluate`/`/publish` đều "đạt". Test ở CHÍNH
    `RunRequest` (không cần DB/session) — lỗi phải bắt được ngay lúc Pydantic parse body, trước
    khi route chạy dòng nào."""
    from pydantic import ValidationError

    for success, citation in ((-999.0, -999.0), (-0.01, 0.5), (0.5, 1.01), (2.0, 0.5)):
        with pytest.raises(ValidationError):
            RunRequest.model_validate(_threshold_body(success, citation))


def test_run_request_accepts_threshold_boundaries() -> None:
    """Đối xứng có chủ đích với bài trên: `0.0`/`1.0` HỢP LỆ (chấp mọi thứ / đòi tuyệt đối) —
    giết mutant `ge→gt`/`le→lt`."""
    for success, citation in ((0.0, 0.0), (1.0, 1.0), (0.9, 0.95)):
        body = RunRequest.model_validate(_threshold_body(success, citation))
        assert body.success_threshold == success
        assert body.citation_accuracy_threshold == citation
