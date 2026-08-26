"""`routes/publish.py::_evaluate` — khoá lại review `app#26` ⛔ (dholmes0207): recipe được CHẤM
(chạy qua `EngineAgentRunner`/harness) phải LÀ đúng recipe mà `_evaluate()` trả về cho `publish()`
băm và ghi vào `wb.recipes`. Trước bản vá, `EngineAgentRunner(...)` không được tiêm `recipe=`, nên
`certified_recipe()` (`eval_adapter.py`) tự dựng MỘT RECIPE KHÁC (`create_recipe_d4(...)`, cố định,
không liên quan gì tới canvas) để thật sự chạy qua từng case golden-set — trong khi
`recipe_hash(recipe)` truyền vào harness lại băm recipe CANVAS. Đo được thật (review, workbench
`3413e29`): 2 recipe khác nhau ở `agent_config`/`dag`/`kb_binding`, recipe được chấm còn có thêm 1
node `tool-call` admin chưa từng vẽ. Cổng đối chiếu hash mới thêm ở `publish()` khi đó so hash
canvas với CHÍNH NÓ (cùng biến, băm 2 lần) — luôn khớp một cách vô nghĩa, không chứng minh được gì.

Bài dưới đây KHÔNG chạy `EvalHarness`/interpreter/LLM thật (không cần golden-set/DB thật cho phần
đó) — chỉ ghi lại kwarg `recipe=` mà `_evaluate()` truyền vào `EngineAgentRunner`, và khoá bằng
`is` (identity), không phải `==`: phải ĐÚNG cùng 1 object Python với recipe mà `_evaluate()` trả về
để `publish()` băm/ghi, không chỉ một recipe "giống hệt". Bài này PHẢI đỏ trên code trước bản vá
(không truyền `recipe=` ⇒ `_SpyEngineAgentRunner.last_kwargs` không có khoá đó) — đó là điều kiện để nó có nghĩa.

**Khoá thêm nửa còn lại của DEC-03** (review `app#26`, pr-test-analyzer — trước bản vá này chỉ có
vế `recipe=` được khoá, vế `recipe_hash=` truyền vào `EvalHarness().run()` không ai kiểm): bài
dưới cũng ghi lại kwarg `recipe_hash` mà `_evaluate()` truyền cho harness, và assert nó bằng đúng
`recipe_hash(recipe)` tính TRÊN CÙNG recipe canvas mà `EngineAgentRunner` nhận — cùng lớp lỗi
"đúng tên biến, sai đối tượng" mà vế `recipe=` từng mắc phải, giờ được khoá luôn cả 2 vế bằng 1
bài test."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
import studio_app.routes.publish as publish_module
from studio_app.core._db import Pool, close_pools
from studio_app.routes.publish import PublishRequest, _evaluate
from studio_contracts import Aggregate, CaseResult, Gate, GateThreshold, Scorecard
from studio_evalhub.golden_case import GoldenCase, GoldenSet
from studio_workbench.publish import recipe_hash as _real_recipe_hash
from studio_workbench.tenant_wall import ResolvedContext

ANKOR_ID = UUID("a0000000-0000-0000-0000-000000000001")


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    """`_evaluate()` chạm `get_pool()` (singleton process-wide) để dựng `PgKbSearch`/`PgTraceWriter`
    — dù bài này không thật sự query DB (harness bị stub ở dưới), constructor của 2 lớp đó vẫn giữ
    tham chiếu `pool`, nên cùng kỷ luật đóng pool giữa các bài như mọi test khác trong file này đã
    dùng, phòng rò rỉ event loop sang bài chạy sau."""
    yield
    await close_pools()


class _SpyEngineAgentRunner:
    """Double thay `EngineAgentRunner` thật — ghi lại MỌI kwarg constructor nhận được, đặc biệt là
    `recipe=`, không tự chạy gì (không cần `run_case` thật vì `EvalHarness` cũng bị stub ở dưới,
    không bao giờ gọi tới `run_case`)."""

    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        _SpyEngineAgentRunner.last_kwargs = kwargs


class _StubEvalHarness:
    """Double thay `EvalHarness` thật — trả thẳng 1 `Scorecard` cố định, không chạm golden-set/
    interpreter/LLM. Đủ để `_evaluate()` chạy hết thân hàm mà không cần hạ tầng eval thật; ghi lại
    `kwargs` (đặc biệt `recipe_hash=`) để bài test đo được nửa còn lại của DEC-03, cùng lớp với
    `_SpyEngineAgentRunner.last_kwargs` ở trên."""

    last_kwargs: dict[str, Any] = {}

    async def run(self, agent_id: str, golden_set_ref: str, **kwargs: Any) -> Scorecard:
        _StubEvalHarness.last_kwargs = kwargs
        return Scorecard(
            agent_id=agent_id,
            golden_set_ref=golden_set_ref,
            results=[CaseResult(case_id="c1", expected="x", actual="x", success=True, citation_accuracy=1.0)],
            aggregate=Aggregate(success_rate=1.0, citation_accuracy=1.0, n_scored_citation=1),
            gate=Gate(threshold=GateThreshold(success=0.9, citation_accuracy=0.95), verdict="PASS"),
            recipe_hash="stub-scorecard-recipe-hash-not-checked-by-this-test",
        )


async def _fake_tenant_name(tenant_id: object) -> str:  # noqa: ARG001
    """Tên tenant giả cho bài không dựng connection request-scope."""
    return "ankor"


def _minimal_valid_dag() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`kb-retrieve -> llm-step`, 2 node — tối thiểu để qua được `enforce_agent_shape`/
    `enforce_agent_topology` bên trong `_evaluate()` (app#78/workbench#48 — trước đây 1
    `graph_lint()`, cấm `end` giờ và đòi đúng 1 node llm-step), khác hẳn DAG 3-node cố định
    (`eval_adapter.py::_CERTIFIED_NODES`, workbench#41 — trước đây `create_recipe_d4`) mà
    `certified_recipe()` tự dựng ở nhánh không tiêm `recipe=` (đúng điểm bài này cần phân biệt:
    recipe canvas và recipe fallback phải là 2 thứ RÕ RÀNG khác nhau, không trùng tình cờ)."""
    nodes = [
        {"id": "n1", "type": "kb-retrieve", "params": {}},
        {"id": "n2", "type": "llm-step", "params": {}},
    ]
    edges = [{"from": "n1", "to": "n2", "when": None}]
    return nodes, edges


async def _fake_load_golden_set(ref: str, tenant_id: UUID) -> GoldenSet:
    """Thay `_load_golden_set` (đọc `eval.golden_sets`) — cutover file→DB.

    Bài trong file này CỐ Ý không đụng DB (xem `_SentinelPool`): chúng đo việc nối dây quanh
    `EvalHarness.run()`, không đo nguồn bộ case. Sau cutover `_evaluate()` cần một request-scoped
    connection đã bind `app.tenant_id` — dựng thật ở đây sẽ kéo cả DB + middleware vào một bài
    không nói gì về hai thứ đó, và mỗi tầng thêm vào là một chỗ bài đỏ vì lý do khác.

    Nguồn bộ case có bài riêng, đúng chỗ: `test_publish_reads_golden_from_db.py`.
    """
    del tenant_id
    return GoldenSet(
        golden_set_ref=ref,
        cases=[
            GoldenCase(
                case_id="c1",
                query="q?",
                tenant="ankor",
                section_roles=["public"],
                expected_tenant="ankor",
                expected_section_role="public",
                expected="a",
            )
        ],
    )


async def test_evaluate_injects_the_canvas_recipe_it_returns_into_the_runner(
    pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    del pool  # chỉ để trigger skip-nếu-thiếu-DSN đúng quy ước chung của suite, không tự query
    monkeypatch.setattr(publish_module, "EngineAgentRunner", _SpyEngineAgentRunner)
    monkeypatch.setattr(publish_module, "_load_golden_set", _fake_load_golden_set)
    # `_evaluate` giờ đọc `core.tenants.name` để dựng bảng tra tenant cho `EvalHarness` (trước là
    # fixture `TENANT_IDS` chỉ có 2 tenant demo, nên mọi tenant thật ném `KeyError`). Bài này stub
    # tầng DB, nên stub luôn chỗ đọc mới — cùng lý do `_load_golden_set` đã được stub ngay trên.
    monkeypatch.setattr(publish_module, "_tenant_name", _fake_tenant_name)
    monkeypatch.setattr(publish_module, "EvalHarness", _StubEvalHarness)

    nodes, edges = _minimal_valid_dag()
    body = PublishRequest(
        agent_id="agent-inject-check",
        system_prompt="Answer from KB only.",
        tool_whitelist=[],
        nodes=nodes,
        edges=edges,
    )
    session = ResolvedContext(tenant_id=ANKOR_ID, user="admin@acme.com", system_roles=["admin"])

    recipe, _scorecard = await _evaluate("agent-inject-check", body, session)

    assert _SpyEngineAgentRunner.last_kwargs.get("recipe") is recipe, (
        "EngineAgentRunner phải được tiêm ĐÚNG object recipe mà _evaluate() trả về (thứ publish() "
        "sẽ băm/ghi) — thiếu recipe= khiến certified_recipe() (eval_adapter.py) tự dựng qua "
        "create_recipe() để chạy, một recipe KHÁC hẳn canvas (kit#127, review app#26 ⛔): "
        "recipe_hash() khi đó băm một object mà harness chưa bao giờ chạm tới."
    )

    # Nửa còn lại của DEC-03 (review app#26, pr-test-analyzer) — `recipe_hash=` truyền vào
    # `EvalHarness().run()` phải là hash của ĐÚNG recipe canvas đã tiêm ở trên, không phải tính
    # nhầm trên 1 object khác hay 1 tham số bị gõ sai tên (2 lỗi đó không raise gì — kwarg sai tên
    # rơi vào `**kwargs` của stub y hệt kwarg đúng tên, chỉ assert trực tiếp mới bắt được).
    assert _StubEvalHarness.last_kwargs.get("recipe_hash") == _real_recipe_hash(recipe), (
        "EvalHarness().run() phải nhận recipe_hash=recipe_hash(recipe) của ĐÚNG recipe canvas vừa "
        "tiêm vào EngineAgentRunner ở trên — cùng lớp lỗi với vế recipe= (kit#127, review app#26 "
        "⛔), chỉ khác đây là nửa còn lại của DEC-03 mà lần vá trước chưa có bài nào khoá."
    )
