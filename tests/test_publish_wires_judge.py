"""`routes/publish.py::_evaluate` — nối `judge=` vào `EvalHarness.run()` (`apps/studio#20`).

## Nhánh judge từng bị bỏ IM LẶNG

`EvalHarness.run` nhận `judge=` từ D18 (`kit#118`), `LLMJudge` điền thật cùng lúc — nhưng đường
production **chưa bao giờ truyền vào**, nên `if judge is not None and …` (`harness.py`) không bao giờ
đúng. Không lỗi, không cảnh báo, chỉ là một tầng chấm chưa từng chạy. Bản vá nối nó lại.

## Vì sao BA bài, và vì sao bài 1 không thay được bài 2

Bài 1 chứng minh **có nối** — assert thẳng vào kwarg `judge` mà `_evaluate()` truyền, và đo cả 2
đường dẫn state có đúng từ `Settings` hay không. Nó **không** chứng minh judge *hoạt động*.

Bài 2/3 chứng minh **judge làm được việc và tụt nấc đúng**, và chúng chạy trên **chính object mà
`_evaluate()` dựng** (bắt lại từ kwarg ở bài 1) chứ không dựng một `LLMJudge` riêng cho test — nếu
`_evaluate()` dựng judge sai (đường dẫn sai, `cap` sai, provider sai), bài 2/3 đỏ theo.

Đo được lý do phải tách: dưới `use_fake_providers=true` (mặc định CI), `ExtractiveFakeLLM` gặp prompt
judge — prompt không có mark `[chunk_id]` — trả `"Không có đoạn trích nào để trả lời."`, và
`_doc_verdict` thấy không phải `PASS`/`FAIL` nên raise `PROVIDER_UNAVAILABLE`. Tức trên CI mặc định
judge **luôn tụt nấc**, và một bài chỉ chạy route rồi assert `Scorecard` trông ổn sẽ **xanh với cả
hai** trạng thái. Vì thế bài 2 tiêm `LLM` double trả `"PASS"` chứ không đi qua `build_llm()` thật.

## An toàn chỉ vì cổng `DEC-05` đã vào con trỏ TRƯỚC bản vá này

`evalhub#30` (`_duoc_hoi_judge`) chặn judge lật cổng `no-trace-no-proof`. Nối `judge=` khi cổng đó
chưa có là **bật một fail-open**: case `events == []` mà `answer` chứa đúng cụm `expected` sẽ được
judge cho `PASS` — tất định, không cần judge phán sai. Thứ tự đó là điều kiện, không phải sở thích.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_DNS, UUID, uuid5

import pytest
import pytest_asyncio
import studio_app.routes.publish as publish_module
from studio_app.core._db import Pool, close_pools
from studio_app.routes.publish import PublishRequest, _evaluate
from studio_app.settings import LlmProvider, Settings
from studio_contracts import Aggregate, CaseResult, Gate, GateThreshold, NodeType, Scorecard, Tokens, TraceEvent
from studio_evalhub.agent_runner import AgentAnswer, CaseRun, StubAgentRunner
from studio_evalhub.golden_case import GoldenCase, GoldenSet
from studio_evalhub.harness import EvalHarness
from studio_evalhub.judge import LLMJudge
from studio_workbench.tenant_wall import ResolvedContext

ANKOR_ID = UUID("a0000000-0000-0000-0000-000000000001")
_REF = "fx-app20-v1"
_TS = 0.9
_TC = 0.95

# Hai case, bất đối xứng theo nhánh chấm: FX-01 khớp exact-match, FX-02 KHÔNG (cùng ý, khác chữ) —
# nên chỉ FX-02 được định tuyến sang judge. Cân hai case cùng kiểu thì một judge chết vẫn ra cùng số.
_YAML = """\
golden_set_ref: fx-app20-v1
cases:
  - case_id: FX-01
    query: "Nghỉ phép năm bao nhiêu ngày?"
    tenant: ankor
    section_roles: [hr]
    expected_tenant: ankor
    expected_section_role: hr
    expected: "12 ngày"
    expected_citation: []
  - case_id: FX-02
    query: "Duyệt chi phí mất bao lâu?"
    tenant: ankor
    section_roles: [hr]
    expected_tenant: ankor
    expected_section_role: hr
    expected: "ba ngày làm việc"
    expected_citation: []
"""


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    """`_evaluate()` chạm `get_pool()` (singleton process-wide) để dựng `PgKbSearch`/`PgTraceWriter`.
    Cùng kỷ luật đóng pool giữa các bài như các file test khác, phòng rò rỉ event loop."""
    yield
    await close_pools()


class _PassLLM:
    """`LLM` double trả `"PASS"` cho MỌI prompt — judge luôn đồng ý. `calls` để đếm."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete(self, prompt: str, **kwargs: object) -> str:
        del kwargs
        self.calls.append(prompt)
        return "PASS"


class _BoomLLM:
    """`LLM` double luôn ném ⇒ `JudgeUnavailable(PROVIDER_UNAVAILABLE)` ⇒ đường tụt nấc."""

    async def complete(self, prompt: str, **kwargs: object) -> str:
        del prompt, kwargs
        raise RuntimeError("no API key configured")


class _SpyRunner:
    def __init__(self, **kwargs: Any) -> None:
        del kwargs


class _StubEvalHarness:
    """Double thay `EvalHarness` **chỉ để bắt kwargs** mà `_evaluate()` truyền. Không chạm
    golden-set/interpreter thật — bài 2/3 sau đó cầm chính `judge` bắt được ở đây và chạy nó qua
    `EvalHarness` THẬT."""

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


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql://unused/unused",
        database_url_admin="postgresql://unused/unused",
        jwt_secret="test-secret-at-least-32-bytes-long",
        llm_provider=LlmProvider.GEMINI,
        judge_cache_path=tmp_path / "judge-cache.json",
        judge_cap_path=tmp_path / "judge-cap.json",
    )


def _minimal_valid_dag() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes = [
        {"id": "n1", "type": "kb-retrieve", "params": {}},
        {"id": "n2", "type": "end", "params": {}},
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


async def _bat_judge_ma_evaluate_dung(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, llm: object) -> LLMJudge:
    """Chạy `_evaluate()` với `EvalHarness` bị stub, trả về **đúng object `LLMJudge`** nó đã dựng.

    `build_llm` bị monkeypatch để judge nhận `llm` double — đúng ô DoD *"tiêm `LLM` double, không đi
    qua `build_llm()`"*: thứ judge dùng là double, không phải `ExtractiveFakeLLM`."""
    monkeypatch.setattr(publish_module, "EngineAgentRunner", _SpyRunner)
    monkeypatch.setattr(publish_module, "_load_golden_set", _fake_load_golden_set)
    monkeypatch.setattr(publish_module, "EvalHarness", _StubEvalHarness)
    monkeypatch.setattr(publish_module, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(publish_module, "build_llm", lambda: llm)

    nodes, edges = _minimal_valid_dag()
    body = PublishRequest(
        agent_id="agent-judge-wiring",
        system_prompt="Answer from KB only.",
        tool_whitelist=[],
        nodes=nodes,
        edges=edges,
    )
    session = ResolvedContext(tenant_id=ANKOR_ID, user="admin@acme.com", roles=["admin"])
    await _evaluate("agent-judge-wiring", body, session)

    judge = _StubEvalHarness.last_kwargs.get("judge")
    assert isinstance(judge, LLMJudge), (
        "_evaluate() phải truyền một LLMJudge THẬT vào EvalHarness.run(judge=…). Thiếu kwarg này thì "
        "`if judge is not None and …` (harness.py) không bao giờ đúng và nhánh judge bị bỏ IM LẶNG — "
        "không lỗi, không cảnh báo (apps/studio#20)."
    )
    return judge


def _tenant_ids() -> Mapping[str, UUID]:
    return {"ankor": uuid5(NAMESPACE_DNS, "ankor")}


def _event(tenant_id: UUID) -> TraceEvent:
    return TraceEvent(
        event_id="e1",
        run_id="r1",
        agent_id="a",
        tenant_id=tenant_id,
        node_id="n1",
        node_type=NodeType.KB_RETRIEVE,
        ts="2026-08-19T00:00:00+00:00",
        inputs_hash="h",
        outputs={"chunks": []},
        tokens=Tokens(prompt=0, completion=0),
        cost=0.0,
        citations=[],
    )


def _runner() -> StubAgentRunner:
    """FX-01 trả đúng cụm `expected`; FX-02 cùng ý khác chữ ⇒ chỉ FX-02 đi qua judge.

    `events` KHÔNG rỗng ở cả hai — nếu rỗng thì `_duoc_hoi_judge` (`evalhub#30`) chặn không hỏi judge
    nữa, và bài này sẽ đo một thứ khác hẳn."""
    tenant_id = _tenant_ids()["ankor"]
    return StubAgentRunner(
        {
            ("Nghỉ phép năm bao nhiêu ngày?", tenant_id, ("hr",)): CaseRun(
                answer=AgentAnswer(answer="Theo tài liệu, nghỉ phép 12 ngày.", citations=[], refused=False),
                events=[_event(tenant_id)],
            ),
            ("Duyệt chi phí mất bao lâu?", tenant_id, ("hr",)): CaseRun(
                answer=AgentAnswer(answer="Khoảng 3 ngày làm việc.", citations=[], refused=False),
                events=[_event(tenant_id)],
            ),
        }
    )


async def _chay_harness_that(golden: Path, judge: LLMJudge | None) -> Scorecard:
    return await EvalHarness().run(
        "agent-judge-wiring",
        _REF,
        golden_set_path=golden,
        runner=_runner(),
        tenant_ids=_tenant_ids(),
        threshold_success=_TS,
        threshold_citation_accuracy=_TC,
        judge=judge,
    )


@pytest.fixture
def golden_fx(tmp_path: Path) -> Path:
    path = tmp_path / "fx-app20.yaml"
    path.write_text(_YAML, encoding="utf-8")
    return path


async def test_evaluate_truyen_judge_that_vao_harness_va_dung_2_duong_tu_settings(
    pool: Pool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**Bài 1 — có nối, và nối bằng đúng cấu hình.**

    Assert thẳng vào kwarg `judge` thay vì suy từ `Scorecard`: dưới `use_fake_providers=true` (mặc
    định CI) judge luôn tụt nấc, nên một bài đo `Scorecard` sẽ xanh cả khi **không** truyền judge.

    Đo cả 2 đường dẫn có đúng từ `Settings`: đọc field private của `LLMJudge` là cố ý — đó là cách
    duy nhất phân biệt *"dựng judge"* với *"dựng judge trỏ đúng chỗ"*, và trỏ sai chỗ chính là lớp
    lỗi làm `cap ≤100/ngày` (`INV-4`) mất hiệu lực mà không có triệu chứng nào."""
    del pool  # chỉ trigger skip-nếu-thiếu-DSN theo quy ước suite, không tự query
    judge = await _bat_judge_ma_evaluate_dung(monkeypatch, tmp_path, _PassLLM())

    assert judge._cache_path == tmp_path / "judge-cache.json", "cache_path phải lấy từ Settings"
    assert judge._cap_path == tmp_path / "judge-cap.json", "cap_path phải lấy từ Settings"


async def test_judge_that_su_lat_duoc_case_truot_exact_match(
    pool: Pool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, golden_fx: Path
) -> None:
    """**Bài 2 — judge làm được việc**, và chạy trên **chính object `_evaluate()` dựng**.

    `FX-02` trượt exact-match (*"Khoảng 3 ngày làm việc."* không chứa cụm *"ba ngày làm việc"*) ⇒ đi
    qua judge ⇒ double trả `"PASS"` ⇒ lật `success`. So với bản `judge=None` chứ không assert một
    hằng số: nó nói *"chính judge là thứ lật"*, không chỉ *"FX-02 xanh"*.

    Không đi qua `build_llm()` thật — đúng ô DoD. Nếu để `ExtractiveFakeLLM` thì judge tụt nấc và bài
    này sẽ xanh-giả (bằng đúng bản `judge=None`)."""
    del pool
    judge = await _bat_judge_ma_evaluate_dung(monkeypatch, tmp_path, _PassLLM())

    co_judge = await _chay_harness_that(golden_fx, judge)
    khong_judge = await _chay_harness_that(golden_fx, None)

    ket_qua = {r.case_id: r.success for r in co_judge.results}
    nen = {r.case_id: r.success for r in khong_judge.results}
    assert nen == {"FX-01": True, "FX-02": False}, "tiền đề: không judge thì FX-02 trượt exact-match"
    assert ket_qua == {"FX-01": True, "FX-02": True}, "judge trả PASS phải lật đúng FX-02"
    assert co_judge != khong_judge, "Scorecard phải KHÁC bản judge=None — nếu bằng thì judge vô tác dụng"


async def test_judge_tut_nac_cho_ra_dung_scorecard_cua_ban_judge_none(
    pool: Pool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, golden_fx: Path
) -> None:
    """**Bài 3 — đối chứng, và nó là thứ làm bài 2 có nghĩa.**

    Provider ném ⇒ `JudgeUnavailable(PROVIDER_UNAVAILABLE)` ⇒ tụt nấc. `Scorecard` phải **bằng đúng**
    bản `judge=None` (`INV-7`): tụt nấc là *quay về* nấc exact-match, không phải rẽ sang một nấc thứ
    ba. So bằng `==` trên cả object thay vì từng field — một field lặng lẽ khác sẽ lộ ra ở đây mà
    không cần bài này biết trước field nào đáng ngờ.

    Không có bài này thì bài 2 không phân biệt được *"judge lật đúng"* với *"judge lật bừa mọi thứ"*."""
    del pool
    judge = await _bat_judge_ma_evaluate_dung(monkeypatch, tmp_path, _BoomLLM())

    tut_nac = await _chay_harness_that(golden_fx, judge)
    khong_judge = await _chay_harness_that(golden_fx, None)

    assert tut_nac == khong_judge
