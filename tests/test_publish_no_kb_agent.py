"""`_evaluate` chấm agent **không gắn KB** bằng bộ dựng sẵn, không bằng bộ của tenant.

Agent không có node `kb-retrieve` không trích dẫn được gì ⇒ trục `citation_accuracy` của bộ thường
luôn ra 0.0 ⇒ loại agent đó không bao giờ publish được. Route chọn `studio_evalhub.no_kb_golden`
cho đúng ca đó — mọi case nhánh trả-lời, `expected` là câu nói-không-biết, nên cả hai trục đo được
mà không nới chốt nào.

Mỗi bài đi theo cặp bất đối xứng với nhánh CÓ node KB: thiếu vế đó thì không bài nào phân biệt
được "route chọn đúng bộ" với "route luôn dùng một bộ".
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
from studio_evalhub.golden_case import GoldenCase, GoldenSet
from studio_evalhub.no_kb_golden import NO_KB_GOLDEN_SET_REF, NO_KB_TENANT_LABEL
from studio_kb.doc_factory_core import TENANT_IDS
from studio_workbench.tenant_wall import ResolvedContext

ANKOR_ID = TENANT_IDS["ankor"]
TENANT_NAME = "Ankor"
"""Tên THẬT trong `core.tenants` — cố ý khác khoá `"ankor"` của fixture `TENANT_IDS`, vì chính thế
lệch đó là lỗi `KeyError` mà bảng tra mới vá."""


class _SentinelPool: ...


class _SpyRunner:
    def __init__(self, **kwargs: Any) -> None:
        del kwargs


class _StubEvalHarness:
    last_kwargs: dict[str, Any] = {}
    last_ref: str = ""

    async def run(self, agent_id: str, golden_set_ref: str, **kwargs: Any) -> Scorecard:
        _StubEvalHarness.last_kwargs = kwargs
        _StubEvalHarness.last_ref = golden_set_ref
        return Scorecard(
            agent_id=agent_id,
            golden_set_ref=golden_set_ref,
            results=[CaseResult(case_id="c1", expected="x", actual="x", success=True, citation_accuracy=1.0)],
            aggregate=Aggregate(success_rate=1.0, citation_accuracy=1.0, n_scored_citation=1),
            gate=Gate(threshold=GateThreshold(success=0.9, citation_accuracy=0.95), verdict="PASS"),
            recipe_hash="stub-not-checked-here",
        )


async def _fake_tenant_name(tenant_id: object) -> str:  # noqa: ARG001
    return TENANT_NAME


async def _fake_load_golden_set(ref: str, tenant_id: UUID) -> GoldenSet:
    """Bộ của tenant — chỉ được gọi ở nhánh CÓ node KB. Case mang tên tenant THẬT, đúng như
    `golden_autogen.regenerate_for_section` sinh ra."""
    del tenant_id
    return GoldenSet(
        golden_set_ref=ref,
        cases=[
            GoldenCase(
                case_id="tenant-01",
                query="q?",
                tenant=TENANT_NAME,
                section_roles=["public"],
                expected_tenant=TENANT_NAME,
                expected_section_role="public",
                expected="a",
            )
        ],
    )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql://unused/unused",
        database_url_admin="postgresql://unused/unused",
        jwt_secret="test-secret-at-least-32-bytes-long",
        llm_provider=LlmProvider.GEMINI,
        judge_cache_path=tmp_path / "judge-cache.json",
        judge_cap_path=tmp_path / "judge-cap.json",
    )


async def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, with_kb_node: bool) -> None:
    async def _sentinel_pool() -> _SentinelPool:
        return _SentinelPool()

    monkeypatch.setattr(publish_module, "get_pool", _sentinel_pool)
    monkeypatch.setattr(publish_module, "EngineAgentRunner", _SpyRunner)
    monkeypatch.setattr(publish_module, "_load_golden_set", _fake_load_golden_set)
    monkeypatch.setattr(publish_module, "_tenant_name", _fake_tenant_name)
    monkeypatch.setattr(publish_module, "EvalHarness", _StubEvalHarness)
    monkeypatch.setattr(publish_module, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(publish_module, "build_llm", lambda: object())
    monkeypatch.setattr(publish_module, "build_embedding", lambda: object())
    monkeypatch.setattr(publish_module, "LLMJudge", lambda *a, **k: object())

    nodes: list[dict[str, Any]] = [{"id": "n2", "type": "llm-step", "params": {}}]
    edges: list[dict[str, Any]] = []
    if with_kb_node:
        nodes.insert(0, {"id": "n1", "type": "kb-retrieve", "params": {}})
        edges = [{"from": "n1", "to": "n2", "when": None}]

    body = PublishRequest(agent_id="agent-no-kb", system_prompt="p", tool_whitelist=[], nodes=nodes, edges=edges)
    session = ResolvedContext(tenant_id=ANKOR_ID, user="admin@ankor.vn", system_roles=["admin"])
    await _evaluate("agent-no-kb", body, session)


async def test_recipe_without_kb_node_is_scored_by_the_builtin_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    await _run(monkeypatch, tmp_path, with_kb_node=False)
    golden = _StubEvalHarness.last_kwargs["golden_set"]
    assert golden.golden_set_ref == NO_KB_GOLDEN_SET_REF
    assert [c.case_id for c in golden.cases if c.expects_refusal] == [], "bộ dựng sẵn không có case từ-chối nào"


async def test_recipe_with_kb_node_still_uses_the_tenant_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Vế bất đối xứng: có node KB thì KHÔNG đụng bộ dựng sẵn."""
    await _run(monkeypatch, tmp_path, with_kb_node=True)
    golden = _StubEvalHarness.last_kwargs["golden_set"]
    assert golden.golden_set_ref != NO_KB_GOLDEN_SET_REF
    assert [c.case_id for c in golden.cases] == ["tenant-01"]


async def test_scorecard_declares_the_set_it_actually_ran(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`golden_set_ref` truyền vào harness phải là bộ THẬT SỰ chạy, không phải `recipe.golden_set_ref`.

    Khai ref của recipe ở nhánh không-KB sẽ cho ra một Scorecard nói nó được chấm bằng một bộ nó
    chưa từng chạy — đúng lớp "chứng nhận sai đối tượng" mà `recipe_hash` được dựng để chặn."""
    await _run(monkeypatch, tmp_path, with_kb_node=False)
    assert _StubEvalHarness.last_ref == NO_KB_GOLDEN_SET_REF


async def test_builtin_label_maps_to_the_session_tenant(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Nhãn hằng số của bộ dựng sẵn phải trỏ về tenant của PHIÊN.

    Thiếu ánh xạ này, `EvalHarness` tra `tenant_ids["__no_kb_agent__"]` và ném `KeyError` thô."""
    await _run(monkeypatch, tmp_path, with_kb_node=False)
    assert _StubEvalHarness.last_kwargs["tenant_ids"][NO_KB_TENANT_LABEL] == ANKOR_ID


async def test_real_tenant_name_is_resolvable_in_the_lookup_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Tên tenant THẬT (`core.tenants.name`) phải tra được.

    Trước bản vá, bảng tra là `dict(TENANT_IDS)` — fixture chỉ có `ankor`/`borea`, còn
    `GoldenCase.tenant` mang tên thật, nên mọi tenant khác hai cái đó làm cổng ném `KeyError`."""
    await _run(monkeypatch, tmp_path, with_kb_node=True)
    assert _StubEvalHarness.last_kwargs["tenant_ids"][TENANT_NAME] == ANKOR_ID
