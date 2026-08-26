"""`routes/publish.py::_evaluate` — nối `core_only=` vào `EvalHarness.run()` (evalhub#52).

Hai thứ khác nhau được kiểm ở đây, và vế thứ hai mới là vế đắt:

1. cổng có THẬT SỰ chạy bộ Core không (`core_only=True` được truyền);
2. tham số route chọn có làm publish **mất khả năng chạy** với bộ nhỏ không.

Vế 2 tồn tại vì bật `core_only` kéo theo một thứ không ai đòi: mặc định `min_answer=10` của
`select_core` là một **tiền điều kiện publish mới** — bộ dưới 10 case trả-lời sẽ `CoreSelectionError`
→ 400, nơi trước đây publish được bình thường. Bán kính đo được: mọi fixture golden của các bài
publish hiện có chỉ 1–2 case, và từ app#61 golden set sinh tự động theo từng phòng ban nên tenant
vừa nạp một tài liệu cũng dưới ngưỡng.

Nên vế 2 KHÔNG assert con số (`core_min_answer == 1` sẽ khoá cứng một lựa chọn sản phẩm vào test).
Nó chạy `select_core` THẬT với đúng tham số route truyền, trên đúng cỡ bộ mà production gặp, rồi
đòi không được ném. Ai nâng ngưỡng lên 10 sẽ thấy bài này đỏ kèm lý do, thay vì thấy CI đỏ ở bốn
bài DB khác vì một lý do trông không liên quan.

Bài ở đây cố ý KHÔNG đụng DB (cùng khuôn `test_publish_wires_judge.py`): chúng đo việc nối dây
quanh `EvalHarness.run()`, không đo nguồn bộ case.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from studio_app.routes import publish as publish_module
from studio_app.routes.publish import PublishRequest, _evaluate
from studio_app.settings import LlmProvider, Settings
from studio_contracts import Aggregate, CaseResult, Gate, GateThreshold, Scorecard
from studio_evalhub.core_set import select_core
from studio_evalhub.golden_case import GoldenCase, GoldenSet
from studio_kb.doc_factory_core import TENANT_IDS
from studio_workbench.tenant_wall import ResolvedContext

ANKOR_ID = TENANT_IDS["ankor"]


class _SentinelPool:
    """`_evaluate()` gọi `get_pool()` để dựng `PgKbSearch`/`PgTraceWriter`. Cả hai chỉ GIỮ tham
    chiếu, không mở connection lúc dựng — nên một sentinel là đủ, và nhờ vậy file này chạy được
    **không cần Postgres**, khác các bài publish khác (chúng bị skip khi thiếu DB).

    Đó là chủ ý: thứ đang đo là việc nối dây quanh `EvalHarness.run()`, kéo cả DB vào chỉ thêm
    một lý do để bài đỏ mà không nói gì về chỗ nối."""


class _SpyRunner:
    def __init__(self, **kwargs: Any) -> None:
        del kwargs


class _StubEvalHarness:
    """Double thay `EvalHarness` chỉ để bắt kwargs `_evaluate()` truyền."""

    last_kwargs: dict[str, Any] = {}

    async def run(self, agent_id: str, golden_set_ref: str, **kwargs: Any) -> Scorecard:
        _StubEvalHarness.last_kwargs = kwargs
        return Scorecard(
            agent_id=agent_id,
            golden_set_ref=golden_set_ref,
            results=[CaseResult(case_id="c1", expected="x", actual="x", success=True, citation_accuracy=1.0)],
            aggregate=Aggregate(success_rate=1.0, citation_accuracy=1.0, n_scored_citation=1),
            gate=Gate(threshold=GateThreshold(success=0.9, citation_accuracy=0.95), verdict="PASS"),
            recipe_hash="stub-not-checked-here",
        )


async def _fake_tenant_name(tenant_id: object) -> str:  # noqa: ARG001
    """Tên tenant giả cho bài không dựng connection request-scope."""
    return "ankor"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql://unused/unused",
        database_url_admin="postgresql://unused/unused",
        jwt_secret="test-secret-at-least-32-bytes-long",
        llm_provider=LlmProvider.GEMINI,
        judge_cache_path=tmp_path / "judge-cache.json",
        judge_cap_path=tmp_path / "judge-cap.json",
    )


def _one_case_golden(ref: str = "callisto-2.0-golden-30-v1") -> GoldenSet:
    """Đúng cỡ bộ mà production gặp ngay sau khi một phòng ban nạp tài liệu đầu tiên (app#61)."""
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


async def _fake_load_golden_set(ref: str, tenant_id: UUID) -> GoldenSet:
    del tenant_id
    return _one_case_golden(ref)


async def _capture_run_kwargs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    async def _sentinel_pool() -> _SentinelPool:
        return _SentinelPool()

    monkeypatch.setattr(publish_module, "get_pool", _sentinel_pool)
    monkeypatch.setattr(publish_module, "EngineAgentRunner", _SpyRunner)
    monkeypatch.setattr(publish_module, "_load_golden_set", _fake_load_golden_set)
    # `_evaluate` giờ đọc `core.tenants.name` để dựng bảng tra tenant cho `EvalHarness` (trước là
    # fixture `TENANT_IDS` chỉ có 2 tenant demo, nên mọi tenant thật ném `KeyError`). Bài này stub
    # tầng DB, nên stub luôn chỗ đọc mới — cùng lý do `_load_golden_set` đã được stub ngay trên.
    monkeypatch.setattr(publish_module, "_tenant_name", _fake_tenant_name)
    monkeypatch.setattr(publish_module, "EvalHarness", _StubEvalHarness)
    monkeypatch.setattr(publish_module, "get_settings", lambda: _settings(tmp_path))

    # app#78 (workbench#48): star topology mới cấm "end" và đòi đúng 1 node llm-step —
    # kb-retrieve -> llm-step (hub-spoke) là hình tối thiểu pass cả `enforce_agent_shape`/
    # `enforce_agent_topology`.
    body = PublishRequest(
        agent_id="agent-core-wiring",
        system_prompt="Answer from KB only.",
        tool_whitelist=[],
        nodes=[{"id": "n1", "type": "kb-retrieve", "params": {}}, {"id": "n2", "type": "llm-step", "params": {}}],
        edges=[{"from": "n1", "to": "n2", "when": None}],
    )
    session = ResolvedContext(tenant_id=ANKOR_ID, user="admin@acme.com", system_roles=["admin"])
    await _evaluate("agent-core-wiring", body, session)
    return _StubEvalHarness.last_kwargs


async def test_evaluate_passes_core_only_to_harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Thiếu kwarg này thì cổng Publish vẫn chấm CẢ BỘ — đúng bài timeout mà evalhub#52 dựng ra để
    giải, và nó hỏng một cách hoàn toàn im lặng (scorecard vẫn hợp lệ, chỉ là request dài hơn)."""
    kwargs = await _capture_run_kwargs(monkeypatch, tmp_path)

    assert kwargs.get("core_only") is True, (
        "_evaluate() phải truyền core_only=True vào EvalHarness.run(). Thiếu nó thì cổng chấm cả bộ "
        "và bài timeout của evalhub#52 còn nguyên — không có dấu hiệu nào ở scorecard để nhận ra."
    )


async def test_core_params_do_not_lock_publish_out_for_small_sets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Chạy `select_core` THẬT với đúng tham số route truyền, trên bộ 1 case.

    Không assert con số: chọn ngưỡng bao nhiêu là quyết định sản phẩm, khoá nó vào test thì mỗi lần
    đổi chính sách là một bài đỏ vô nghĩa. Cái phải giữ là **tính chất**: bật `core_only` không được
    biến publish thành bất khả thi cho tenant mới. Ai nâng ngưỡng sẽ thấy bài này đỏ kèm lý do, thay
    vì thấy bốn bài DB khác đỏ vì một lý do trông không liên quan."""
    kwargs = await _capture_run_kwargs(monkeypatch, tmp_path)

    # `run()` nhận `core_*`, `select_core` nhận tên trần — bỏ tiền tố khi chuyển tiếp, và CHỈ
    # chuyển những khoá route thật sự truyền, để mặc định của `select_core` vẫn là mặc định.
    forwarded = {k.removeprefix("core_"): v for k, v in kwargs.items() if k in ("core_max_cases", "core_min_answer")}
    selection = select_core(_one_case_golden(), **forwarded)

    assert len(selection.golden.cases) == 1
    assert selection.n_answer == 1
