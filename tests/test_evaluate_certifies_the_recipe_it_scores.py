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
(không truyền `recipe=` ⇒ `captured_kwargs` không có khoá đó) — đó là điều kiện để nó có nghĩa."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
import studio_app.routes.publish as publish_module
from studio_app.core._db import Pool, close_pools
from studio_app.routes.publish import _evaluate
from studio_app.routes.runs import RunRequest
from studio_contracts import Aggregate, CaseResult, Gate, GateThreshold, Scorecard
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
    interpreter/LLM. Đủ để `_evaluate()` chạy hết thân hàm mà không cần hạ tầng eval thật; điều
    duy nhất bài test này cần đo là kwarg đã truyền cho `EngineAgentRunner` TRƯỚC bước này."""

    async def run(self, agent_id: str, golden_set_ref: str, **kwargs: Any) -> Scorecard:
        del kwargs
        return Scorecard(
            agent_id=agent_id,
            golden_set_ref=golden_set_ref,
            results=[CaseResult(case_id="c1", expected="x", actual="x", success=True, citation_accuracy=1.0)],
            aggregate=Aggregate(success_rate=1.0, citation_accuracy=1.0, n_scored_citation=1),
            gate=Gate(threshold=GateThreshold(success=0.9, citation_accuracy=0.95), verdict="PASS"),
            recipe_hash="stub-scorecard-recipe-hash-not-checked-by-this-test",
        )


def _minimal_valid_dag() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`kb-retrieve -> end`, 2 node — tối thiểu để qua được `graph_lint()` bên trong `_evaluate()`,
    khác hẳn `create_recipe_d4`'s DAG 6-node cố định (đúng điểm bài này cần phân biệt: recipe canvas
    và recipe fallback phải là 2 thứ RÕ RÀNG khác nhau, không trùng tình cờ)."""
    nodes = [
        {"id": "n1", "type": "kb-retrieve", "params": {}},
        {"id": "n2", "type": "end", "params": {}},
    ]
    edges = [{"from": "n1", "to": "n2", "when": None}]
    return nodes, edges


async def test_evaluate_injects_the_canvas_recipe_it_returns_into_the_runner(
    pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    del pool  # chỉ để trigger skip-nếu-thiếu-DSN đúng quy ước chung của suite, không tự query
    monkeypatch.setattr(publish_module, "EngineAgentRunner", _SpyEngineAgentRunner)
    monkeypatch.setattr(publish_module, "EvalHarness", _StubEvalHarness)

    nodes, edges = _minimal_valid_dag()
    body = RunRequest(
        agent_id="agent-inject-check",
        instructions="Answer from KB only.",
        model="gpt-4o-mini",
        tool_whitelist=[],
        kb_id="kb-1",
        scope="t/public",
        nodes=nodes,
        edges=edges,
    )
    session = ResolvedContext(tenant_id=ANKOR_ID, user="admin@acme.com", roles=["admin"])

    recipe, _scorecard = await _evaluate("agent-inject-check", body, session)

    assert _SpyEngineAgentRunner.last_kwargs.get("recipe") is recipe, (
        "EngineAgentRunner phải được tiêm ĐÚNG object recipe mà _evaluate() trả về (thứ publish() "
        "sẽ băm/ghi) — thiếu recipe= khiến certified_recipe() (eval_adapter.py) tự dựng "
        "create_recipe_d4(...) để chạy, một recipe KHÁC hẳn canvas (kit#127, review app#26 ⛔): "
        "recipe_hash() khi đó băm một object mà harness chưa bao giờ chạm tới."
    )
