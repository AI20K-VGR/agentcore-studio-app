"""`routes/agents.py` — `GET /api/agents` (list agent đã publish của tenant) +
`POST /api/agents/{agent_id}/rollback` (nối dây `studio_workbench.publish.rollback()` đã có sẵn)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import HTTPException
from studio_app import middleware
from studio_app.core._db import Pool, close_pools, get_pool
from studio_app.routes.agents import (
    RollbackRequest,
    get_agent_recipe,
    list_agent_versions,
    list_agents,
    rollback_agent,
)
from studio_contracts import Edge, Node, NodeType, Recipe
from studio_workbench import create_recipe
from studio_workbench.tenant_wall import ResolvedContext


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    yield
    await close_pools()


def _set_session(*, tenant_id: UUID, user: str, roles: list[str]) -> object:
    session = ResolvedContext(tenant_id=tenant_id, user=user, roles=roles)
    return middleware._request_session.set(session)


async def _bind_tenant(conn: object, tenant_id: UUID) -> None:
    """`wb.recipes`/`wb.recipe_versions` bật `FORCE ROW LEVEL SECURITY` (`schema.py`, kit#117 Q7)
    — cắn CẢ `studio_owner` (`admin_pool`), không chỉ `studio_app`. Mọi đọc/ghi 2 bảng này (kể cả
    seed dữ liệu test) phải tự bind `app.tenant_id` TRƯỚC, transaction-scoped, đúng cơ chế
    `tenant_context_middleware` dùng cho request thật (`middleware.py`) — cùng pattern
    `test_gate2_publish_money_shot.py::_bind_tenant`."""
    await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))  # type: ignore[attr-defined]


@asynccontextmanager
async def _simulate_request_connection(tenant_id: UUID) -> AsyncIterator[None]:
    """`tenant_id` bắt buộc truyền vào (khác `test_admin_routes.py`'s bản không cần, vì `core.*`
    không có RLS) — route dưới đây chạm `wb.recipes` (CÓ RLS), nên phải bind đúng tenant của
    session trước khi route chạy, y hệt việc `tenant_context_middleware` làm cho request thật."""
    pool = await get_pool()
    async with pool.connection() as conn:
        await _bind_tenant(conn, tenant_id)
        token = middleware._request_conn.set(conn)
        try:
            yield
        finally:
            middleware._request_conn.reset(token)


async def _seed_tenant(admin_pool: Pool, name: str) -> UUID:
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", (name,))
        row = await cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


async def _seed_user(admin_pool: Pool, tenant_id: UUID, email: str, roles: list[str]) -> None:
    async with admin_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, roles) VALUES (%s, %s, %s, %s)",
            (str(tenant_id), email, "not-a-real-hash", roles),
        )


def _seed_recipe(agent_id: str, tenant_id: UUID) -> Recipe:
    """workbench#41 — `create_recipe_d4()` đã bị xoá. Dựng thủ công cùng hình dạng DAG 3-node nó
    từng tự sinh (KB_RETRIEVE -> LLM_STEP -> END) — 2 test dùng hàm này chỉ cần recipe có cạnh
    thật, không đụng chi tiết `kb_binding`/`system_prompt`."""
    nodes = [
        Node(id="n1", type=NodeType.KB_RETRIEVE, params={"top_k": 3}),
        Node(id="n2", type=NodeType.LLM_STEP, params={"temperature": 0.0}),
        Node(id="n4", type=NodeType.END, params={}),
    ]
    edges = [Edge(from_="n1", to="n2"), Edge(from_="n2", to="n4")]
    return create_recipe(
        agent_id=agent_id,
        tenant_id=tenant_id,
        system_prompt="Tra cứu quy trình và bảo mật Callisto.",
        tool_whitelist=[],
        nodes=nodes,
        edges=edges,
    )


_RECIPE_JSON = '{"agent_id": "placeholder", "tenant_id": "placeholder"}'
"""Nội dung `wb.recipes.recipe`/`wb.recipe_versions.recipe` không quan trọng cho 2 route này —
`list_agents`/`rollback_agent` chỉ đọc `agent_id`/`version`/`status`, không đụng payload recipe."""


async def _seed_published_recipe(
    admin_pool: Pool, *, tenant_id: UUID, agent_id: str, version: int, status: str = "published"
) -> None:
    async with admin_pool.connection() as conn, conn.transaction():
        await _bind_tenant(conn, tenant_id)
        await conn.execute(
            "INSERT INTO wb.recipes (agent_id, tenant_id, recipe, version, status) VALUES (%s, %s, %s, %s, %s)",
            (agent_id, str(tenant_id), _RECIPE_JSON, version, status),
        )
        await conn.execute(
            "INSERT INTO wb.recipe_versions (recipe_id, agent_id, tenant_id, recipe, version, status) "
            "SELECT id, agent_id, tenant_id, recipe, version, status FROM wb.recipes "
            "WHERE agent_id = %s AND tenant_id = %s AND version = %s",
            (agent_id, str(tenant_id), version),
        )


async def _read_recipe_rows(admin_pool: Pool, tenant_id: UUID, agent_id: str) -> list[tuple[int, str]]:
    async with admin_pool.connection() as conn, conn.transaction():
        await _bind_tenant(conn, tenant_id)
        cur = await conn.execute(
            "SELECT version, status FROM wb.recipes WHERE agent_id = %s AND tenant_id = %s ORDER BY version",
            (agent_id, str(tenant_id)),
        )
        return [(int(v), str(s)) for v, s in await cur.fetchall()]


async def test_list_agents_scoped_to_own_tenant_via_rls(admin_pool: Pool) -> None:
    tenant_a = await _seed_tenant(admin_pool, "agents-probe-a")
    tenant_b = await _seed_tenant(admin_pool, "agents-probe-b")
    await _seed_user(admin_pool, tenant_a, "user-a@acme.com", ["public"])
    await _seed_published_recipe(admin_pool, tenant_id=tenant_a, agent_id="agent-a1", version=1)
    await _seed_published_recipe(admin_pool, tenant_id=tenant_b, agent_id="agent-b1", version=1)

    token = _set_session(tenant_id=tenant_a, user="user-a@acme.com", roles=["public"])
    try:
        async with _simulate_request_connection(tenant_a):
            result = await list_agents()
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    agent_ids = {a.agent_id for a in result}
    assert agent_ids == {"agent-a1"}, "RLS phải tự lọc — không thấy agent-b1 của tenant khác"


async def test_list_agents_returns_latest_published_version(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "agents-probe-c")
    await _seed_user(admin_pool, tenant_id, "user@acme.com", ["public"])
    await _seed_published_recipe(admin_pool, tenant_id=tenant_id, agent_id="agent-c1", version=1)
    await _seed_published_recipe(admin_pool, tenant_id=tenant_id, agent_id="agent-c1", version=2)

    token = _set_session(tenant_id=tenant_id, user="user@acme.com", roles=["public"])
    try:
        async with _simulate_request_connection(tenant_id):
            result = await list_agents()
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert len(result) == 1
    assert result[0].agent_id == "agent-c1"
    assert result[0].latest_published_version == 2


async def test_list_agents_excludes_unpublished_status(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "agents-probe-d")
    await _seed_user(admin_pool, tenant_id, "user@acme.com", ["public"])
    await _seed_published_recipe(admin_pool, tenant_id=tenant_id, agent_id="agent-draft", version=1, status="draft")

    token = _set_session(tenant_id=tenant_id, user="user@acme.com", roles=["public"])
    try:
        async with _simulate_request_connection(tenant_id):
            result = await list_agents()
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert result == []


async def test_rollback_requires_admin(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "agents-probe-e")
    await _seed_user(admin_pool, tenant_id, "user@acme.com", ["public"])
    await _seed_published_recipe(admin_pool, tenant_id=tenant_id, agent_id="agent-e1", version=1)

    token = _set_session(tenant_id=tenant_id, user="user@acme.com", roles=["public"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection(tenant_id):
                await rollback_agent("agent-e1", RollbackRequest(to_version=1))
        assert exc_info.value.status_code == 403
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_rollback_missing_version_returns_404(admin_pool: Pool) -> None:
    tenant_id = await _seed_tenant(admin_pool, "agents-probe-f")
    await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])
    await _seed_published_recipe(admin_pool, tenant_id=tenant_id, agent_id="agent-f1", version=1)

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection(tenant_id):
                await rollback_agent("agent-f1", RollbackRequest(to_version=99))
        assert exc_info.value.status_code == 404
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_rollback_restores_prior_version(admin_pool: Pool) -> None:
    """`rollback()` (`studio_workbench.publish`) không xoá row cũ — nó chỉ đổi `status` (row v1:
    draft->published, row v2: published->rolled_back), cả 2 row VẪN CÙNG TỒN TẠI trong `wb.recipes`
    (đúng `UNIQUE(agent_id,tenant_id,version)`, khác version nên không đụng nhau). Seed v1=`draft`
    (đúng trạng thái thật `publish()` để lại khi v2 xuất bản đè lên), v2=`published` (đang live) —
    verify sau rollback phải lọc `status='published'` tường minh, không suy đoán thứ tự trả về."""
    tenant_id = await _seed_tenant(admin_pool, "agents-probe-g")
    await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])
    await _seed_published_recipe(admin_pool, tenant_id=tenant_id, agent_id="agent-g1", version=1, status="draft")
    await _seed_published_recipe(admin_pool, tenant_id=tenant_id, agent_id="agent-g1", version=2, status="published")

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        async with _simulate_request_connection(tenant_id):
            result = await rollback_agent("agent-g1", RollbackRequest(to_version=1))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert result.status == "rolled_back"
    assert result.version == 1

    rows = await _read_recipe_rows(admin_pool, tenant_id, "agent-g1")
    assert dict(rows) == {1: "published", 2: "rolled_back"}, (
        f"v1 phải thành published, v2 phải thành rolled_back, cả 2 row vẫn tồn tại — thấy {rows}"
    )


async def test_list_agent_versions_requires_admin(admin_pool: Pool) -> None:
    """`list_agent_versions` (`routes/agents.py`, review app#27 finding #4 — TranBaDat2607: route
    ship KHÔNG test) — cùng gate `require_admin` với `rollback_agent`."""
    tenant_id = await _seed_tenant(admin_pool, "agents-probe-h")
    await _seed_user(admin_pool, tenant_id, "user@acme.com", ["public"])
    await _seed_published_recipe(admin_pool, tenant_id=tenant_id, agent_id="agent-h1", version=1)

    token = _set_session(tenant_id=tenant_id, user="user@acme.com", roles=["public"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection(tenant_id):
                await list_agent_versions("agent-h1")
        assert exc_info.value.status_code == 403
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def test_list_agent_versions_returns_real_rows_desc(admin_pool: Pool) -> None:
    """Đọc THẬT `wb.recipe_versions` (không phải danh sách giả) — dropdown Rollback ở UI dựng
    trực tiếp từ đây, đúng lý do route này tồn tại (thay input số tự do gõ tay)."""
    tenant_id = await _seed_tenant(admin_pool, "agents-probe-i")
    await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])
    await _seed_published_recipe(admin_pool, tenant_id=tenant_id, agent_id="agent-i1", version=1, status="draft")
    await _seed_published_recipe(admin_pool, tenant_id=tenant_id, agent_id="agent-i1", version=2, status="published")

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        async with _simulate_request_connection(tenant_id):
            result = await list_agent_versions("agent-i1")
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert [(r.version, r.status) for r in result] == [(2, "published"), (1, "draft")]


async def test_list_agent_versions_scoped_to_own_tenant_via_rls(admin_pool: Pool) -> None:
    """`wb.recipe_versions` bật RLS — 2 tenant cùng khai `agent_id` không được thấy version của
    nhau, cùng nguyên tắc `test_list_agents_scoped_to_own_tenant_via_rls` ở trên."""
    tenant_a = await _seed_tenant(admin_pool, "agents-probe-j-a")
    tenant_b = await _seed_tenant(admin_pool, "agents-probe-j-b")
    await _seed_user(admin_pool, tenant_a, "admin-a@acme.com", ["admin"])
    await _seed_published_recipe(admin_pool, tenant_id=tenant_a, agent_id="agent-shared", version=1)
    await _seed_published_recipe(admin_pool, tenant_id=tenant_b, agent_id="agent-shared", version=1)

    token = _set_session(tenant_id=tenant_a, user="admin-a@acme.com", roles=["admin"])
    try:
        async with _simulate_request_connection(tenant_a):
            result = await list_agent_versions("agent-shared")
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert len(result) == 1, "RLS phải tự lọc — không thấy version của tenant_b dù cùng agent_id"


async def test_get_agent_recipe_normalizes_from_alias_regardless_of_write_path(admin_pool: Pool) -> None:
    """Review app#37 F1 (dholmes0207, ⛔): trước test này, cả app#37 lẫn workbench#30 là mutation
    sống — xoá `by_alias=True` ở route (`Recipe.model_validate(...).model_dump(by_alias=True)`) hay
    ở `publish()` thì suite vẫn xanh 100%, vì `get_agent_recipe` chưa từng có test nào chạm tới.

    Seed THẲNG bằng đúng đường ghi hỏng đã gây bug (`recipe.model_dump_json()` KHÔNG `by_alias`,
    cùng công thức `test_routes_chat_as_roles.py::_seed_published_recipe`) — tái dựng đúng row
    `publish()` cũ để lại: DB lưu `"from_"` thay vì `"from"`. Route phải tự chuẩn hoá lại khi đọc,
    bất kể đường ghi nào tạo ra hàng đó."""
    tenant_id = await _seed_tenant(admin_pool, "agents-probe-k")
    await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])

    agent_id = "agent-k1"
    recipe = _seed_recipe(agent_id, tenant_id)
    async with admin_pool.connection() as conn, conn.transaction():
        await _bind_tenant(conn, tenant_id)
        await conn.execute(
            "INSERT INTO wb.recipes (agent_id, tenant_id, recipe, version, status) "
            "VALUES (%s, %s, %s::jsonb, 1, 'published')",
            (agent_id, str(tenant_id), recipe.model_dump_json()),
        )

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        async with _simulate_request_connection(tenant_id):
            result = await get_agent_recipe(agent_id)
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    edges = result.recipe["dag"]["edges"]  # type: ignore[index]
    assert edges, "seed (_seed_recipe) phải sinh cạnh thật — nếu rỗng, assert dưới vô nghĩa"
    assert all("from" in e and "from_" not in e for e in edges), (
        f"route phải chuẩn hoá lại alias dây bất kể đường ghi cũ để lại 'from_' — thấy {edges}"
    )


async def test_get_agent_recipe_rejects_row_invalid_under_current_contract(admin_pool: Pool) -> None:
    """Review app#37 F2 (dholmes0207, ⚠️): `model_validate()` biến row cũ hợp lệ-lúc-ghi nhưng
    không còn hợp lệ theo contract HIỆN TẠI thành `ValidationError` trần — `app.py` không có
    handler cho lỗi này (chỉ bắt `RequestValidationError`), nên bay thẳng thành 500 không detail.

    Ca thật: `scorecard_threshold.success = -999` được server CHẤP NHẬN trước khi kit#129 §3.1 siết
    `ge=0.0, le=1.0` (docstring `ScorecardThreshold`). Row publish trước lần siết đó giờ nằm vĩnh
    viễn trong `wb.recipe_versions` (append-only) — endpoint phải trả 500 CÓ detail nêu rõ version
    nào hỏng, không phải 500 câm, vì đây đúng đường phục hồi của admin (soi version cũ → rollback).

    Review app#37 round 2 (dholmes0207, M4): assertion gốc (`"version" in detail and "1" in
    detail`) SỐNG SÓT qua mutation đổi `row[1]` (đúng version của row) thành tham số query
    `version` (`None` ở đây vì không truyền) — `"version"` là chữ cứng trong template nên luôn có,
    `"1"` tình cờ khớp `"1 validation error for Recipe"` bên trong thông điệp `pydantic`, không
    chứng minh được version có in đúng hay không. Khoá chặt bằng chuỗi liền `"version 1"` — mutation
    trên sẽ đổi thành `"version None"`, giết được assertion."""
    tenant_id = await _seed_tenant(admin_pool, "agents-probe-l")
    await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])

    agent_id = "agent-l1"
    recipe_dict = _seed_recipe(agent_id, tenant_id).model_dump(mode="json", by_alias=True)
    recipe_dict["scorecard_threshold"]["success"] = -999
    async with admin_pool.connection() as conn, conn.transaction():
        await _bind_tenant(conn, tenant_id)
        await conn.execute(
            "INSERT INTO wb.recipes (agent_id, tenant_id, recipe, version, status) "
            "VALUES (%s, %s, %s::jsonb, 1, 'published')",
            (agent_id, str(tenant_id), json.dumps(recipe_dict)),
        )

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", roles=["admin"])
    try:
        with pytest.raises(HTTPException) as exc_info:
            async with _simulate_request_connection(tenant_id):
                await get_agent_recipe(agent_id)
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 500
    detail = str(exc_info.value.detail)
    assert "version 1" in detail, f"detail phải nêu đúng version của row hỏng — thấy {detail!r}"
    assert "success" in detail, "detail phải nêu field trượt validate để admin biết chỗ hỏng"
    assert "-999" not in detail, (
        "N1 (review app#37 round 2): detail KHÔNG được echo input_value trần — dùng "
        "exc.errors(include_input=False, ...), không phải str(exc)"
    )
