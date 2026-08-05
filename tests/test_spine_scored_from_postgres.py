"""Mắt nối CUỐI của chuỗi a→z: chấm điểm từ trace **đọc ra từ Postgres** — D7, DoD #30 + #34.

DoD #30 (Day 6, cả nhóm) viết: *"form → interpreter → kb.search → mọi node emit trace Postgres
(`obs.trace_events`) → smoke-eval **đọc trace in bảng điểm**"*. Vế cuối cùng của câu đó chưa ai
chứng minh, và đây là chỗ chứng minh nó.

## Chuỗi đã có gì, thiếu gì

    engine → PgTraceWriter → obs.trace_events → PgTraceReader        ✅ kb `test_spine_live.py` (DE)
                                              → CaseRun → score_case   ❌ file NÀY

`packages/kb/tests/test_spine_live.py` của DE (8 test) chạy spine thật rồi **đọc lại từ Postgres** —
nhưng nó **không import `studio_evalhub`**, nên nó dừng ở reader. Nghĩa là trước file này, **chưa
từng có case nào được chấm từ trace đọc ra từ DB**: `scripts/e2e_smoke_eval.py` đọc
`RunResult.events` trong **bộ nhớ**, và `packages/evalhub/tests/` chấm trên `CaseRun` dựng bằng tay.

## Vì sao file này phải nằm ở `apps/studio/tests/`

`.importlinter` xếp `studio_app` **trên** 4 quadrant, còn 4 quadrant là sibling — nên
`studio_evalhub` **không** import được `studio_kb`. Phép so này cần cả `PgTraceReader` (kb) lẫn
`score_case` (evalhub) trong một tiến trình, nên composition root là chỗ **duy nhất** hợp lệ. Cùng
đường mà DE đã dùng khi để `test_spine_live.py` import `studio_app`/`studio_engine`: `.importlinter`
ràng buộc `src/`, không quét `tests/`.

## Ý nghĩa của phép so, và vì sao nó cũng là một phép determinism

Điểm chấm từ trace **bộ nhớ** và từ trace **Postgres** phải bằng nhau. Nó khoá hai thứ cùng lúc:

1. **DoD #30** — bảng điểm dựng được từ dữ liệu đã bền hoá, không chỉ từ object trong RAM.
2. **DoD #34** — DB round-trip là một biến đổi có thật (JSONB serialize, `numeric` cho `cost`, `text`
   cho `ts`, thứ tự row do `ORDER BY` quyết). Nếu bộ chấm nhạy với bất kỳ thứ nào trong đó thì bảng
   điểm đổi theo nguồn trace, tức CI flaky vì một lý do không liên quan tới chất lượng câu trả lời.

Cần Postgres: `docker compose -f docker-compose.test.yml up -d` + 2 biến DSN. Thiếu thì fixture
`pool` ở `conftest.py` gốc **skip**, không fail — cùng hành vi như `test_trace_writer.py`.
"""

from __future__ import annotations

from uuid import UUID

from psycopg.types.json import Jsonb
from studio_app.core._db import Pool
from studio_app.eval_adapter import EngineAgentRunner
from studio_app.obs.trace_writer import PgTraceWriter
from studio_app.providers.fakes import ExtractiveFakeLLM
from studio_evalhub.agent_runner import CaseRun
from studio_evalhub.cli import _demo_golden_set
from studio_evalhub.harness import citations_from_trace, score_case
from studio_kb.doc_factory import TENANT_IDS, load_callisto
from studio_kb.embeddings import derive_vector
from studio_kb.postgres import KbIngest, PgKbSearch
from studio_kb.trace_reader import PgTraceReader


class _StubEmbedding:
    """`recipe_d4` không có bước embed; `LlmStepExecutor` nhận qua constructor nhưng không dùng
    (`executors.py`: *"embedding is wired via constructor-DI but unused here"*)."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 0.0, 0.0] for _ in texts]


class _CallistoEmbedding:
    """`EmbeddingService` cho `PgKbSearch`/seed — CÙNG không gian với `derive_vector` (SSOT
    `studio_kb.embeddings`). Không dùng `_StubEmbedding` ở đây: đó là khe của
    `EngineAgentRunner(embedding=...)` (trơ, feed `LlmStepExecutor`), khác khe seed/query của
    `PgKbSearch` — hai adapter phục vụ hai collaborator khác nhau."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [derive_vector(text) for text in texts]


async def _run_case_through_postgres(pool: Pool) -> tuple[CaseRun, UUID, str]:
    """Chạy SC-01 qua luồng thật với `PgTraceWriter` → trả `(case_run, tenant_id, run_id)`.

    `ExtractiveFakeLLM` (không phải câu trả lời recorded): nó **chỉ đọc prompt**, không bao giờ thấy
    `expected`/`expected_citation`, nên citation nó trích đến từ dữ liệu KB vừa truy xuất. Fixture
    recorded sẽ cho điểm tuyệt đối bất kể KB làm gì và phép so ở đây mất nghĩa.

    Seed corpus Callisto qua `KbIngest` trước khi chạy case: `conftest.py::_truncate_all` dọn
    `kb.chunks` trước mỗi test, nên `PgKbSearch` cần dữ liệu thật mới trả kết quả (khác
    `StaticKbSearch`, vốn có corpus in-memory sẵn không phụ thuộc DB).
    """
    await KbIngest(pool, _CallistoEmbedding()).ingest(list(load_callisto()))

    case = next(c for c in _demo_golden_set().cases if c.case_id == "SC-01")
    tenant_id = TENANT_IDS[case.tenant]

    runner = EngineAgentRunner(
        kb_search=PgKbSearch(pool, _CallistoEmbedding()),
        llm=ExtractiveFakeLLM(),
        embedding=_StubEmbedding(),
        trace_writer=PgTraceWriter(pool),
    )
    case_run = await runner.run_case(
        agent_id="agent-callisto-d4",
        query=case.query,
        tenant_id=tenant_id,
        section_roles=case.section_roles,
    )
    assert case_run.events, "interpreter phải emit event — rỗng là hồi quy, không phải trạng thái hợp lệ"
    return case_run, tenant_id, case_run.events[0].run_id


async def test_score_from_postgres_matches_score_from_memory(admin_pool: Pool, pool: Pool) -> None:
    """KHÓA P1: cùng một run → chấm từ trace **Postgres** ra ĐÚNG `SmokeResult` như chấm từ **bộ nhớ**.

    So `SmokeResult` bằng `==` (pydantic `frozen=True`, bao mọi field) chứ không so từng field: field
    thêm vào model sau này tự động được so.

    Cũng assert `citations_from_trace` của hai nguồn bằng nhau — tách được "payload citation sống sót
    round-trip" khỏi "điểm tình cờ giống nhau". Hai case khác nhau vẫn có thể ra cùng điểm; hai danh
    sách citation bằng nhau thì cụ thể hơn nhiều.
    """
    del admin_pool  # chỉ để xếp thứ tự — schema/grants phải tồn tại trước DML của app-role
    case = next(c for c in _demo_golden_set().cases if c.case_id == "SC-01")
    case_run, tenant_id, run_id = await _run_case_through_postgres(pool)

    from_memory = score_case(case, case_run.answer, citations_from_trace(case_run.events))

    db_events = await PgTraceReader(pool).read_run(run_id, tenant_id)
    assert db_events, "reader không thấy event nào — sink không ghi, hoặc run_id/tenant lệch"
    assert len(db_events) == len(case_run.events)

    assert citations_from_trace(db_events) == citations_from_trace(case_run.events)
    assert score_case(case, case_run.answer, citations_from_trace(db_events)) == from_memory


async def test_the_score_really_comes_from_postgres_not_from_memory(admin_pool: Pool, pool: Pool) -> None:
    """KHÓA P2 (negative control): sửa `citations` TRONG BẢNG → điểm chấm từ DB phải ĐỔI.

    Không có test này thì P1 rỗng nghĩa: nó vẫn xanh nếu reader lặng lẽ trả về đúng thứ đang nằm
    trong RAM, hoặc nếu `citations_from_trace(db_events)` tình cờ ra cùng kết quả vì cả hai đều rỗng.
    Ở đây bơm một citation SAI vào `obs.trace_events`, đọc lại, rồi đòi điểm **khác** điểm bộ nhớ —
    tức chứng minh đường DB thật sự nằm trên đường chấm, không phải đồ trang trí.

    Sửa trực tiếp bằng SQL thay vì qua writer: đây là mô phỏng "dữ liệu trong bảng khác dữ liệu trong
    RAM", không phải một luồng nghiệp vụ nào — dùng writer sẽ chỉ ghi thêm row mới.
    """
    del admin_pool
    case = next(c for c in _demo_golden_set().cases if c.case_id == "SC-01")
    case_run, tenant_id, run_id = await _run_case_through_postgres(pool)

    from_memory = score_case(case, case_run.answer, citations_from_trace(case_run.events))
    assert from_memory.citation_accuracy == 1.0, "SC-01 phải grounded đúng chunk kỳ vọng trước khi phá"

    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE obs.trace_events SET citations = %s WHERE run_id = %s AND citations IS NOT NULL",
            (Jsonb(["chunk-khong-ton-tai#c999"]), run_id),
        )

    db_events = await PgTraceReader(pool).read_run(run_id, tenant_id)
    tampered = score_case(case, case_run.answer, citations_from_trace(db_events))

    assert citations_from_trace(db_events) == ["chunk-khong-ton-tai#c999"]
    assert tampered != from_memory, "sửa bảng mà điểm không đổi ⇒ điểm KHÔNG đến từ Postgres"
    assert tampered.citation_accuracy == 0.0
