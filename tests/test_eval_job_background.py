"""Chấm điểm chạy nền — task sống NGOÀI vòng đời request, nên nó tự cầm hàng rào tenant.

Đây là chỗ dễ rò tenant nhất của cả việc này: `tenant_context_middleware` không còn cầm hộ, và
connection lấy từ pool được **dùng lại** cho request sau. Một `app.tenant_id` sót lại là request kế
tiếp đọc dữ liệu công ty khác mà không có gì đỏ.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from studio_app.core._db import close_pools, get_pool
from studio_app.routes import publish as publish_module
from studio_app.routes.publish import PublishRequest, _run_eval_job, _tenant_scoped_connection
from studio_contracts import Aggregate, CaseResult, Gate, GateThreshold, Scorecard
from studio_evalhub.eval_job_store import create_eval_job, read_eval_job
from studio_kb.doc_factory_core import TENANT_IDS
from studio_workbench.tenant_wall import ResolvedContext

ANKOR_ID = TENANT_IDS["ankor"]
BOREA_ID = TENANT_IDS["borea"]


@pytest.fixture(autouse=True)
async def _close_pools() -> Any:
    yield
    await close_pools()


def _session(tenant_id: UUID = ANKOR_ID) -> ResolvedContext:
    return ResolvedContext(tenant_id=tenant_id, user="admin@ankor.vn", system_roles=["admin"])


def _body() -> PublishRequest:
    return PublishRequest(
        agent_id="agent-nen",
        system_prompt="p",
        tool_whitelist=[],
        nodes=[{"id": "n2", "type": "llm-step", "params": {}}],
        edges=[],
    )


def _card(recipe_hash: str) -> Scorecard:
    return Scorecard(
        agent_id="agent-nen",
        golden_set_ref="bo-thu",
        results=[CaseResult(case_id="c1", expected="x", actual="x", success=True, citation_accuracy=1.0)],
        aggregate=Aggregate(success_rate=1.0, citation_accuracy=1.0, n_scored_citation=1),
        gate=Gate(threshold=GateThreshold(success=0.9, citation_accuracy=0.95), verdict="PASS"),
        recipe_hash=recipe_hash,
    )


async def test_binding_tenant_khong_song_sot_sang_connection_sau(pool: Any) -> None:
    """Bài quan trọng nhất: `app.tenant_id` KHÔNG được sót lại khi connection về pool.

    Dùng `SET LOCAL` trong transaction ngắn chứ không `set_config(..., false)` mức session, chính
    vì lý do này — giá trị mức session sống sót qua việc trả connection, nên một task chết giữa
    chừng để lại một connection còn bind cho request kế tiếp nhặt được. `SET LOCAL` tự hết hiệu lực
    khi transaction đóng, không phụ thuộc vào việc ta có nhớ dọn hay không."""
    del pool  # `_tenant_scoped_connection` tự lấy pool riêng
    async with _tenant_scoped_connection(ANKOR_ID) as conn:
        cur = await conn.execute("SELECT current_setting('app.tenant_id', true)")
        row = await cur.fetchone()
    assert row is not None and row[0] == str(ANKOR_ID), "chưa bind được tenant trong khối"

    app_pool = await get_pool()
    async with app_pool.connection() as conn2:
        cur = await conn2.execute("SELECT current_setting('app.tenant_id', true)")
        row2 = await cur.fetchone()
    assert row2 is not None
    assert row2[0] in (None, ""), f"binding tenant SÓT lại trên connection dùng lại: {row2[0]!r}"


async def test_task_nen_ghi_diem_va_danh_dau_xong(pool: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Đường thành công: chấm xong ⇒ Scorecard vào `eval.scorecards`, job sang `done`.

    Job KHÔNG giữ bản sao Scorecard — `GET /eval-jobs/{id}` ghép hai thứ bằng
    `(agent_id, recipe_hash)`. Bài này ghim cả hai vế của phép ghép đó."""
    recipe_hashes: list[str] = []

    async def _fake_evaluate(agent_id: str, body: Any, session: Any, *, on_progress: Any = None) -> Any:
        if on_progress is not None:
            await on_progress(1, 3)
        recipe = await publish_module._build_recipe(agent_id, body, session)
        from studio_workbench.publish import recipe_hash

        rhash = recipe_hash(recipe)
        recipe_hashes.append(rhash)
        return recipe, _card(rhash)

    monkeypatch.setattr(publish_module, "_evaluate", _fake_evaluate)

    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(ANKOR_ID),))
        job_id = await create_eval_job(conn, ANKOR_ID, "agent-nen", "se-bi-ghi-de")

    await _run_eval_job(job_id, "agent-nen", _body(), _session())

    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(ANKOR_ID),))
        job = await read_eval_job(conn, job_id)
        from studio_evalhub.scorecard_store import read_pending_scorecard

        card = await read_pending_scorecard(conn, "agent-nen", recipe_hashes[0])
    assert job is not None and job.status == "done"
    assert (job.done, job.total) == (1, 3), "tiến độ không được ghi lại"
    assert card is not None and card.gate.verdict == "PASS"


async def test_task_nen_hong_thi_danh_dau_failed_chu_khong_treo(pool: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Vế bất đối xứng: lỗi phải thành `failed` kèm thông điệp, KHÔNG để job treo `running`.

    Task nền không có ai bắt exception hộ — một job treo mãi là người dùng ngồi đợi vô vọng."""

    async def _no_evaluate(agent_id: str, body: Any, session: Any, *, on_progress: Any = None) -> Any:
        raise RuntimeError("bộ golden chưa nạp")

    monkeypatch.setattr(publish_module, "_evaluate", _no_evaluate)

    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(ANKOR_ID),))
        job_id = await create_eval_job(conn, ANKOR_ID, "agent-nen", "h1")

    await _run_eval_job(job_id, "agent-nen", _body(), _session())  # KHÔNG được ném ra ngoài

    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(ANKOR_ID),))
        job = await read_eval_job(conn, job_id)
    assert job is not None
    assert job.status == "failed"
    assert job.detail is not None and "bộ golden chưa nạp" in job.detail


async def test_task_nen_chay_duoi_dung_tenant_cua_phien(pool: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Task nền phải ghi vào tenant của PHIÊN đã khởi động nó, không phải tenant nào khác.

    Ghi nhầm tenant ở đây là điểm của công ty A xuất hiện trong danh sách của công ty B — và RLS
    không cứu được vì chính task tự chọn giá trị bind."""

    async def _fake_evaluate(agent_id: str, body: Any, session: Any, *, on_progress: Any = None) -> Any:
        recipe = await publish_module._build_recipe(agent_id, body, session)
        from studio_workbench.publish import recipe_hash

        return recipe, _card(recipe_hash(recipe))

    monkeypatch.setattr(publish_module, "_evaluate", _fake_evaluate)

    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(BOREA_ID),))
        job_id = await create_eval_job(conn, BOREA_ID, "agent-nen", "h1")

    await _run_eval_job(job_id, "agent-nen", _body(), _session(BOREA_ID))

    from studio_evalhub.scorecard_store import read_pending_scorecard
    from studio_workbench.publish import recipe_hash

    rhash = recipe_hash(await publish_module._build_recipe("agent-nen", _body(), _session(BOREA_ID)))

    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(BOREA_ID),))
        job_borea = await read_eval_job(conn, job_id)
        card_borea = await read_pending_scorecard(conn, "agent-nen", rhash)
    # Vế xuôi: chạy xong SẠCH dưới tenant của phiên. Thiếu vế này thì bài chỉ chứng minh "công ty
    # kia không thấy gì" — điều vẫn đúng khi task nền hỏng hoàn toàn, nên nó không phân biệt được
    # "ghi đúng chỗ" với "không ghi được gì" (mutant ghi tenant cứng sống sót đúng vì lỗ này).
    assert job_borea is not None and job_borea.status == "done", "task nền không hoàn tất dưới tenant của phiên"
    assert card_borea is not None and card_borea.gate.verdict == "PASS", "Scorecard không vào tenant của phiên"

    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(ANKOR_ID),))
        assert await read_eval_job(conn, job_id) is None, "job của công ty khác lọt sang tenant này"
        assert await read_pending_scorecard(conn, "agent-nen", rhash) is None, "điểm của công ty khác lọt sang"
