"""GATE-2 — `gate.verdict` sinh ra từ một run **THẬT**, lần đầu tiên trong lịch sử workspace.

`kit#129` hỏi *"spine 4 mảng ghép thật, và eval v1 ra verdict"*. Đo bằng đếm call-site chứ không
bằng cảm giác, đầu D20:

    compute_scorecard   ← call-site trong evalhub/src/  :  1   (harness.py:551, trong EvalHarness.run)
    EvalHarness().run   ← call-site toàn repo           : 11   → 11/11 nằm trong tests/, runner = stub
    EngineAgentRunner   ← instantiation trong apps/studio:  4   → 4/4 dừng ở score_case
    render_scorecard    ← call-site ngoài tests         :  0

⇒ **Verdict tồn tại trên đường stub; run thật tồn tại trên đường không có verdict.** Hai đường, hai
sự thật, và mỗi đường đều xanh. File này là mắt nối giữa chúng.

## Vì sao file này phải nằm ở `apps/studio/tests/` — `DEC-D20-01`

`.importlinter:18-21` xếp `studio_app` **trên**, còn `studio_kb | studio_engine | studio_workbench |
studio_evalhub` là **sibling cùng layer** ⇒ `studio_evalhub` không import được `PgKbSearch` (kb) hay
`interpreter` (engine). Phép nối cần cả ba trong **một tiến trình**, nên composition root là chỗ
**duy nhất** hợp lệ.

Tiền lệ, không phải ngoại lệ mới: `test_spine_scored_from_postgres.py` đã sống ở đây từ D7, bút
AIE-2, merge qua PR#2, với **cùng lập luận**. Và `.importlinter` ràng buộc `src/`, **không quét
`tests/`**.

Ranh giới giữ nguyên: file này **chỉ thêm test**. Không sửa `src/studio_app/`, không sửa
`eval_adapter.py`, không sửa `e2e_smoke_eval.py`.

## Verdict sẽ là `FAIL`, và đó là kết quả ĐÚNG — `DEC-D20-03`

Hai câu, **không gộp**:

1. **Chuỗi đã thông** — verdict ra được từ golden-30 thật → `EngineAgentRunner` → engine thật →
   `PgKbSearch` trên Postgres thật → `PgTraceWriter` → `EvalHarness.run` → `compute_scorecard`.
2. **Verdict là `FAIL` vì `ExtractiveFakeLLM` là một double trích câu, không phải một agent.**
   `DEC-D17-04` đã đo trên golden-30: `success_rate = 0.2667`. Con số đó **không đo chất lượng
   agent** và **không** nói hàng rào hỏng.

Ngưỡng `0.9/0.95` giữ nguyên (`DEC-D16-05`/`DEC-D17-04`, ngưỡng thuộc recipe —
`workbench/builder.py:169`). Hạ số vào đúng ngày gate là hiệu chỉnh theo thứ mình muốn nhìn thấy.

## `FAIL` là giá trị DỄ TRÚNG NHẤT — nên một mình nó không chứng minh gì

Mọi cài đặt hỏng đều ra `FAIL`: runner chết, KB rỗng, harness chấm sai, hoặc `compute_scorecard` trả
hằng `"FAIL"`. Một bài chỉ assert `verdict == "FAIL"` **xanh với tất cả những thứ đó**. Vì thế file
này có **hai** bài, và bài thứ hai mới là bài giữ ô DoD *có thể đỏ*:

    test_verdict_fail_tu_run_that     ← chuỗi thật, verdict FAIL     (mutant M-G3 phải giết bài này)
    test_runner_tot_lat_verdict_sang_pass ← cùng chỗ nối, runner tốt ⇒ PASS  (mutant M-G1)

Bài học D19 nguyên văn: *"việc của ngày không phải làm ô DoD xanh; nó là làm ô DoD **có thể đỏ**"*.

Cần Postgres: `docker compose -f docker-compose.test.yml up -d` + `STUDIO_DATABASE_URL` +
`STUDIO_DATABASE_URL_ADMIN`. Thiếu ⇒ fixture `pool` **skip** (không fail), cùng hành vi
`test_trace_writer.py`.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

from studio_app.core._db import Pool
from studio_app.eval_adapter import EngineAgentRunner
from studio_app.obs.trace_writer import PgTraceWriter
from studio_app.providers.fakes import ExtractiveFakeLLM
from studio_contracts import NodeType, Tokens, TraceEvent
from studio_evalhub.agent_runner import AgentAnswer, CaseRun, StubAgentRunner
from studio_evalhub.golden_case import GoldenSet
from studio_evalhub.golden_loader import load_golden_set
from studio_evalhub.harness import EvalHarness
from studio_kb.doc_factory import TENANT_IDS, load_callisto
from studio_kb.embeddings import derive_vector
from studio_kb.postgres import KbIngest, PgKbSearch

_GOLDEN_30 = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "kb"
    / "src"
    / "studio_kb"
    / "golden"
    / "callisto-golden-30-v1.yaml"
)
_REF = "callisto-golden-30-v1"
_AGENT = "agent-callisto-d4"

# Ngưỡng của recipe (`workbench/builder.py:169`), KHÔNG phải của bộ chấm. Viết ra ở đây là để phép đo
# đọc được, không phải để sở hữu chúng — `DEC-D16-05`. Bất kỳ diff nào chạm hai số này trong ngày
# gate là thứ `DEC-D20-03` cấm.
_THRESHOLD_SUCCESS = 0.9
_THRESHOLD_CITATION = 0.95

# 30 case, 8 case từ-chối ⇒ mẫu số citation là 22 (`DEC-04`). Con số này là một phép đo, không phải
# một hằng số trang trí: sai mẫu số là đúng chỗ một bản đáng FAIL lại PASS ngay ngưỡng.
_N_CASE = 30
_N_SCORED_CITATION = 22


class _StubEmbedding:
    """`recipe_d4` không có bước embed; `LlmStepExecutor` nhận qua constructor nhưng không dùng."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 0.0, 0.0] for _ in texts]


class _CallistoEmbedding:
    """`EmbeddingService` cho `PgKbSearch`/seed — CÙNG không gian với `derive_vector` (SSOT
    `studio_kb.embeddings`), khác khe trơ của `EngineAgentRunner(embedding=...)`."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [derive_vector(text) for text in texts]


def _golden() -> GoldenSet:
    """Ref assert từ **nội dung** file, không suy từ tên file (`DEC-D16-01`)."""
    return load_golden_set(_GOLDEN_30, expect_ref=_REF)


def _trace_event(tenant_id: UUID, citations: list[str], *, chunks: list[dict[str, object]] | None = None) -> TraceEvent:
    """Event `kb-retrieve` mang **hai** mặt quan sát — `citations` (tầng *cited*) và
    `outputs["chunks"]` (tầng *retrieved*).

    Sau `F-6` (`DEC-D17-02`) nhánh từ-chối chấm `no_leak` trên **`chunks`**. Một stub chỉ mang
    `citations` là stub không mô hình được thứ bộ chấm đọc, và nó lặng lẽ biến runner-tốt thành
    **fixture thuận lợi**: `all(...)` trên tập rỗng luôn `True` nên luật `no_leak` sai kiểu gì cũng
    không lộ."""
    return TraceEvent(
        event_id="e1",
        run_id="r1",
        agent_id=_AGENT,
        tenant_id=tenant_id,
        node_id="n1",
        node_type=NodeType.KB_RETRIEVE,
        ts="2026-08-14T00:00:00+00:00",
        inputs_hash="h",
        outputs={"chunks": chunks if chunks is not None else []},
        tokens=Tokens(prompt=0, completion=0),
        cost=0.0,
        citations=citations,
    )


def _runner_tot(golden: GoldenSet, tenant_map: Mapping[str, UUID]) -> StubAgentRunner:
    """`StubAgentRunner` trả lời **đúng** mọi case — sinh từ chính golden-set, nên nó đúng theo định
    nghĩa chứ không theo một bảng chép tay sẽ mục khi DE đổi bộ case.

    Đây là **đối chứng** của bài live, không phải một đường tắt: nó tồn tại để chứng minh
    `gate.verdict` là một giá trị **ra từ dữ liệu** — cùng chỗ nối, đổi mỗi chất lượng câu trả lời,
    verdict phải lật. Nếu không lật, `"FAIL"` của bài trên có thể chỉ là một hằng số.

    Khoá fixture **ba** thành phần `(query, tenant_id, tuple(section_roles))`: golden-30 có hai cặp
    trùng `(query, tenant_id)` khác `section_roles` (trục T6), khoá hai thành phần sẽ nuốt mất một
    nửa mỗi cặp và cho ra `28/30` thay vì `30/30`."""
    fixtures: dict[tuple[str, UUID, tuple[str, ...]], CaseRun] = {}
    for case in golden.cases:
        tenant_id = tenant_map[case.tenant]
        if case.expects_refusal:
            answer = AgentAnswer(answer="Tôi không thể trả lời câu hỏi này.", citations=[], refused=True)
            # Trace mang MỘT chunk hợp lệ của CHÍNH kho người hỏi — không phải rỗng. Rỗng là ca
            # không giống đời thật (agent thật gần như luôn retrieve trước khi quyết định từ chối)
            # và nó làm `no_leak` vacuous.
            chunk_id = f"{case.tenant}-handbook-000#c1"
            role = case.section_roles[0] if case.section_roles else "public"
            chunks: list[dict[str, object]] = [
                {"chunk_id": chunk_id, "tenant_id": str(tenant_id), "section_role": role, "score": 0.5, "text": "t"}
            ]
            events = [_trace_event(tenant_id, [chunk_id], chunks=chunks)]
        else:
            answer = AgentAnswer(
                answer=f"Theo tài liệu, {case.expected}.",
                citations=list(case.expected_citation),
                refused=False,
            )
            events = [_trace_event(tenant_id, list(case.expected_citation))]
        fixtures[(case.query, tenant_id, tuple(case.section_roles))] = CaseRun(answer=answer, events=events)
    return StubAgentRunner(fixtures)


async def _dem_trace_rows(pool: Pool) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM obs.trace_events")
        row = await cur.fetchone()
    return int(row[0]) if row is not None else 0


async def test_verdict_fail_tu_run_that(admin_pool: Pool, pool: Pool) -> None:
    """**Ô DoD chính của D20.** golden-30 thật → `EngineAgentRunner` (engine + `PgKbSearch` +
    `PgTraceWriter`) → `EvalHarness.run` → `compute_scorecard` → `Scorecard` có verdict.

    Bốn assert, mỗi cái canh một thứ **khác nhau** — không cái nào thay được cái nào:

    | Assert | Canh gì |
    |---|---|
    | `len(results) == 30` | Chạy đủ bộ. Một mình nó **không** phân biệt *"chạy đúng 30"* với |
    | | *"chạy 30 lần rồi chấm sai một nửa"* |
    | `n_scored_citation == 22` | Mẫu số citation loại 8 refusal (`DEC-04`). Sai mẫu số là chỗ |
    | | một bản đáng FAIL lại PASS ngay ngưỡng |
    | `verdict == "FAIL"` | Verdict ra được từ dữ liệu. **Chưa đủ** — xem bài thứ hai |
    | `trace rows > 0` | Chuỗi đi qua **engine + Postgres**, không chỉ qua một object trong RAM |

    Assert thứ tư là thứ phân biệt file này với 11 call-site `EvalHarness().run` đang có: tất cả
    chúng chạy `StubAgentRunner`, và một stub **không ghi một dòng nào** vào `obs.trace_events`. Nó
    là lưới thứ hai cho `M-G3` bên cạnh việc verdict lật — cần cả hai vì `M-G3` là phép đo **duy
    nhất** phân biệt *"chỗ nối chạy thật"* với *"chỗ nối trông như chạy thật"*.

    `ExtractiveFakeLLM` **chỉ đọc prompt**, không bao giờ thấy `expected`/`expected_citation` — nên
    citation nó trích đến từ dữ liệu KB vừa truy xuất. Một fixture recorded sẽ cho điểm tuyệt đối bất
    kể KB làm gì, và phép đo ở đây mất nghĩa."""
    del admin_pool  # ordering: schema/grants/truncate phải xong trước khi seed
    await KbIngest(pool, _CallistoEmbedding()).ingest(list(load_callisto()))

    scorecard = await EvalHarness().run(
        _AGENT,
        _REF,
        golden_set_path=_GOLDEN_30,
        runner=EngineAgentRunner(
            kb_search=PgKbSearch(pool, _CallistoEmbedding()),
            llm=ExtractiveFakeLLM(),
            embedding=_StubEmbedding(),
            trace_writer=PgTraceWriter(pool),
        ),
        tenant_ids=TENANT_IDS,
        threshold_success=_THRESHOLD_SUCCESS,
        threshold_citation_accuracy=_THRESHOLD_CITATION,
    )

    assert len(scorecard.results) == _N_CASE
    assert scorecard.golden_set_ref == _REF
    assert scorecard.aggregate.n_scored_citation == _N_SCORED_CITATION

    # `FAIL` là kết quả ĐÚNG hôm nay (`DEC-D20-03`): `ExtractiveFakeLLM` là double trả câu canned,
    # không phải hàng rào hỏng và không phải agent tệ.
    assert scorecard.gate.verdict == "FAIL"
    assert scorecard.gate.threshold.success == _THRESHOLD_SUCCESS
    assert scorecard.gate.threshold.citation_accuracy == _THRESHOLD_CITATION

    # `DEC-D20-02`: evalhub NHẬN `recipe_hash`, không tự dẫn xuất. `EvalHarness.run` chưa có caller
    # nào cầm `Recipe` ổn định cho cả run (ask ① câu 🅐 — `eval_adapter` dựng recipe MỖI case ⇒
    # golden-30 sinh 30 recipe), nên `None` ở đây là giá trị trung thực, không phải một lỗ hổng.
    # Ghim lại để nó không trôi trong im lặng ngày `DEC-03` có producer.
    assert scorecard.recipe_hash is None

    assert await _dem_trace_rows(pool) > 0, (
        "obs.trace_events rỗng ⇒ chuỗi KHÔNG đi qua engine thật + PgTraceWriter. "
        "Đây đúng ca `M-G3`: một chỗ nối trông như chạy thật."
    )


async def test_runner_tot_lat_verdict_sang_pass(admin_pool: Pool, pool: Pool) -> None:
    """**Bài giữ ô DoD *có thể đỏ*** — `M-G1`. Cùng chỗ nối, đổi mỗi chất lượng câu trả lời ⇒ verdict
    lật `FAIL` → `PASS`.

    Không có bài này, `assert verdict == "FAIL"` ở trên xanh với cả một `compute_scorecard` trả hằng
    `"FAIL"`, và với mọi cài đặt hỏng khác — vì `FAIL` là giá trị **dễ trúng nhất** của cả hai giá
    trị. Đây là bài duy nhất trong file chứng minh verdict là một **hàm của dữ liệu**.

    Runner tốt sinh từ chính golden-set ⇒ `success_rate == 1.0` và `citation_accuracy == 1.0`, cả
    hai vượt ngưỡng ⇒ `PASS`. `28/30` ở đây nghĩa là hai case trục T6 bị khoá fixture nuốt, không
    phải bộ chấm sai.

    Bài này **không** phải một run được uốn cho đẹp (`DEC-D20-03` ranh giới 4): nó không đổi ngưỡng,
    không đổi dữ liệu golden, và không báo cáo `PASS` như kết quả của ngày. Nó là một **đối chứng**
    có nhãn, đọc cùng một `compute_scorecard` với bài trên."""
    del admin_pool
    golden = _golden()

    scorecard = await EvalHarness().run(
        _AGENT,
        _REF,
        golden_set_path=_GOLDEN_30,
        runner=_runner_tot(golden, TENANT_IDS),
        tenant_ids=TENANT_IDS,
        threshold_success=_THRESHOLD_SUCCESS,
        threshold_citation_accuracy=_THRESHOLD_CITATION,
    )

    assert len(scorecard.results) == _N_CASE
    assert scorecard.aggregate.success_rate == 1.0
    assert scorecard.aggregate.citation_accuracy == 1.0
    assert scorecard.aggregate.n_scored_citation == _N_SCORED_CITATION
    assert scorecard.gate.verdict == "PASS"
