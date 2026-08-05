"""E2E smoke-eval — bộ golden chạy qua LUỒNG THẬT xuyên 4 quadrant, in bảng điểm + chẩn đoán.

    uv run python apps/studio/scripts/e2e_smoke_eval.py

Tiền thân là `e2e_thonluong_demo.py` (Day 5, sinh evidence cho #59). Đổi tên vì mục tiêu đã đổi:
Day 5 chỉ cần chứng minh **đấu nối thông**; từ Day 6 nó là bộ **smoke-eval** có chấm điểm, nên
tên phải nói đúng việc. Bản Day-5 nguyên trạng nằm ở commit trước trong lịch sử file này
(`git log --follow`) — số ở #59 tái lập được từ đó.

## Luồng

    workbench.create_recipe_d4        form → Recipe (tenant_id UUID)
      → engine.interpreter.run        walk theo dag.edges, inject recipe.tenant_id, emit TraceEvent
        → kb.PgKbSearch                fence RLS tenant_id UUID + section_role, Postgres/pgvector thật
        → engine.build_prompt         render `[chunk_id]\\ntext` vào prompt  ← engine#10
      → RunResult{final_state, events}
      → studio_app.EngineAgentRunner  adapter RunResult → CaseRun            (#29, co-author AIE-1)
      → evalhub.score_case            chấm trên citations ĐÃ GROUNDED lấy từ trace

## THẬT vs FIXTURE — nói thẳng, đây là chỗ dễ tự lừa nhất

**THẬT**: recipe · interpreter · `kb.search` (fence tenant/role, `PgKbSearch` trên Postgres/pgvector
thật — D13, không còn corpus in-memory) · trace 4 event · `build_prompt` · luật grounding citation ·
adapter · scorecard. Cần `docker compose -f docker-compose.test.yml up -d` + 2 biến DSN
(`STUDIO_DATABASE_URL`, `STUDIO_DATABASE_URL_ADMIN`) trước khi chạy — script fail loud, không tự
lùi về in-memory, nếu thiếu.

**FIXTURE**: `ExtractiveFakeLLM` (`studio_app.providers.fakes`) — double CHỈ đọc prompt, không bao
giờ thấy `expected`/`expected_citation`. Nó chép đoạn trích ĐẦU TIÊN và trích đúng `chunk_id` của
đoạn đó. Ý gốc của DE (@DongAnh2704, `_NaiveExtractiveLLM`).

Vì sao đổi từ **câu trả lời recorded** sang double này — và vì sao đó là nâng cấp thật, không phải
đổi cho khác:

- Bản Day-5 dùng 5 câu recorded soạn cho khớp `expected`, nên nó **luôn** ra 5/5. Phê bình của
  @DongAnh2704 đúng: *"luôn ra điểm tuyệt đối và chứng minh số không"*.
- Bản này thì citation đến từ **dữ liệu KB vừa truy xuất**, không từ người viết fixture. Đổi kho
  tài liệu hoặc đổi công thức xếp hạng thì điểm tự đi theo.
- Lý do cũ để dùng recorded (*"llm-step chưa build prompt từ chunk retrieved"*) **đã hết hiệu lực**:
  engine#10 merge 28/07 thêm `build_prompt(query, chunks)`.

Đây là **baseline của một prompt-aware deterministic fixture bị giới hạn có chủ ý**, không phải
benchmark hoặc cận dưới được bảo đảm của model thật. `ExtractiveFakeLLM` chỉ đọc đoạn đầu tiên và
không có năng lực quyết định refusal.

Provider thật được truyền cả `top_k` đoạn cùng header hướng dẫn refusal, nhưng mức độ tuân thủ và
chất lượng của model chưa được đo trong script này.

⚠️ **Mục "≥1 case FAIL với LLM THẬT" của mentor (#59) VẪN TREO.** Bộ này có case đỏ thật, nhưng đỏ
bởi một *fixture*, không phải bởi một model chạy thật. Đóng mục đó cần một lượt gọi Gemini thật
(P9 · `apps/studio/providers` nhánh `gemini`). Không nhận vơ.

## Ba nhóm cột

1. **ĐẤU-NỐI** — 4 event · 1 `run_id` · `ts` đơn điệu · `tenant_id` nhất quán · `#chunk` → THÔNG/TẮC.
   Đây là phần Day-5 đã chứng minh và phải giữ xanh.
2. **CHẤT-LƯỢNG TRÊN FIXTURE ĐỌC PROMPT** — `success` + `citation_accuracy` từ
   `evalhub.score_case`, chấm trên `ExtractiveFakeLLM` chứ không phải model thật.
3. **CHẨN ĐOÁN** (mượn ý @DongAnh2704) — `KB-lấy-đúng` chấm `expected_citation` trên chunk **ĐÃ TRUY
   XUẤT** (`event.outputs["chunks"]`), tách bạch với `citation_accuracy` vốn chấm trên chunk **ĐÃ
   GROUNDED** (`event.citations`, tức LLM-trích ∩ retrieved). Hai nguồn khác nhau nên trả lời được
   câu *"0 điểm là tại KB lấy sai chunk, hay tại đoạn LLM→citation đứt?"*. Cột `quy-trách-nhiệm`
   đọc ra kết luận đó. KHÔNG phải điểm số, KHÔNG thay `citation_accuracy`.

## RED-CHECK — 2 negative-control của BỘ CHẤM

*"Bộ chấm chưa bao giờ đỏ là bộ chấm chưa được kiểm chứng."* Hai case dưới đây PHẢI đỏ:

- **XF-01** (nhánh trả-lời): clone nhãn SC-01 nhưng `expected`/`expected_citation` cố ý sai → phải
  đỏ CẢ `success` LẪN `citation_accuracy`. Chạy trên đúng luồng thật, không cần canned.
- **XF-02** (nhánh từ-chối, conjunct `no_leak`): clone nhãn SC-04 nhưng thay `kb_search` bằng
  `_LeakyKb` — một KB **cố ý hỏng fence**, trả về chunk của `expected_tenant`. Đây là câu hỏi thật
  sự đáng hỏi: *nếu hàng rào bị thủng, bảng điểm có nhận ra không?* Fence thật không cho dựng tình
  huống này, nên phải dựng bằng KB hỏng.

RED-CHECK **không** tính vào headline chất-lượng.

## Exit code (CLI hygiene, KHÔNG phải eval-gate publish — S3-D24)

`1` khi **luồng** hỏng (có case TẮC) hoặc **bộ chấm** hỏng (XF không đỏ). **`0` dù chất-lượng chưa
đạt 100%** — một case đỏ vì model kém là *kết quả đo*, không phải lỗi hạ tầng. Gate chất-lượng thật
thuộc `ScorecardThreshold`/`gate.verdict` ở tầng publish, không thuộc script này.

## Nợ đã biết, phơi ra chứ không giấu

- `_demo_golden_set()` (5 case, in-code trong `studio_evalhub.cli`) là **nguồn sự thật thứ hai** của
  `packages/kb/golden/smoke-5.yaml`, và bộ 10 case của @DongAnh2704 (`smoke-10.yaml`, kb#2 đã merge)
  chưa dùng được ở đây. Chặn: đọc YAML cần khai `pyyaml`, mà `uv.lock` nằm ở repo cha — `uv lock
  --check` đỏ nếu khai ở `apps/studio` (đo rồi). Sẽ đóng bằng PR ở repo cha (điểm gãy #8/#12).
- `citation_accuracy = 1.0` cứng ở nhánh từ-chối (`harness.py:135`, Q2 `scorecard-v0.md` §3 chưa
  chốt) → một case từ-chối ĐÃ ĐỎ vẫn in `1.00`, thổi phồng `aggregate`. Bảng dưới in `n/a*` thay vì
  `1.00` để không lan tiếp con số vô nghĩa; điểm gãy #9, agenda freeze D11.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from uuid import UUID

from studio_app.core._db import Pool, close_pools, get_admin_pool, get_pool
from studio_app.core.schema import ensure_all_schemas, grant_app_privileges
from studio_app.eval_adapter import EngineAgentRunner
from studio_app.providers.fakes import ExtractiveFakeLLM
from studio_contracts import EmbeddingService, KbSearchResultItem, TraceEvent
from studio_evalhub import GoldenCase
from studio_evalhub.cli import _demo_golden_set
from studio_evalhub.harness import citations_from_trace, score_case
from studio_kb.doc_factory import TENANT_IDS, load_callisto
from studio_kb.embeddings import derive_vector
from studio_kb.postgres import KbIngest, PgKbSearch


class _CallistoEmbedding:
    """`EmbeddingService` cho seed + query của `PgKbSearch` — cùng không gian với `derive_vector`
    (SSOT `studio_kb.embeddings`). Bản trùng thứ ba của cùng adapter (đã có ở
    `test_kb_search_live_readiness.py` và `test_spine_scored_from_postgres.py`) — trùng có chủ đích
    (DRY note, plan D13): cả ba đều gọi cùng SSOT, chỗ trùng chỉ là lớp vỏ 2 dòng, không phải công
    thức. Khác `_StubEmbedding` dưới đây, vốn phục vụ khe `EngineAgentRunner(embedding=...)` (trơ)."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [derive_vector(text) for text in texts]


class _CountingKb:
    """`PgKbSearch` THẬT (Postgres/pgvector) + đếm số chunk trả về (evidence cột `#chunk`)."""

    def __init__(self, pool: Pool, embedding: EmbeddingService) -> None:
        self._inner = PgKbSearch(pool, embedding)
        self.last_count = 0

    async def search(
        self, query: str, tenant_id: UUID, section_roles: list[str], top_k: int
    ) -> list[KbSearchResultItem]:
        result = await self._inner.search(query, tenant_id, section_roles, top_k)
        self.last_count = len(result)
        return result


class _LeakyKb:
    """KB **CỐ Ý HỎNG FENCE** — chỉ dùng cho RED-CHECK XF-02, không bao giờ cho case thật.

    Nó bỏ qua `tenant_id` được truyền vào và tra bằng tenant KHÁC, đúng như một lỗi RLS thật sẽ gây
    ra. Mục đích: kiểm conjunct `no_leak` của nhánh từ-chối (`harness.py:133`) có thật sự đỏ khi
    chunk chéo tenant lọt vào trace hay không. Fence đang đúng nên không dựng được tình huống này
    bằng dữ liệu thật — phải dựng bằng KB hỏng.
    """

    def __init__(self, pool: Pool, embedding: EmbeddingService, leak_as: UUID) -> None:
        self._inner = PgKbSearch(pool, embedding)
        self._leak_as = leak_as
        self.last_count = 0

    async def search(
        self, query: str, tenant_id: UUID, section_roles: list[str], top_k: int
    ) -> list[KbSearchResultItem]:
        del tenant_id  # ← chính là cái lỗi đang mô phỏng
        result = await self._inner.search(query, self._leak_as, section_roles, top_k)
        self.last_count = len(result)
        return result


class _StubEmbedding:
    """`recipe_d4` không có bước embed; `LlmStepExecutor` nhận qua constructor nhưng không dùng."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 0.0, 0.0] for _ in texts]


class _NoopTraceWriter:
    """Trace mà bộ chấm dùng nằm sẵn trên `RunResult.events`; sink Postgres (`PgTraceWriter` của
    @DongAnh2704) cần Docker, không cần cho lượt chạy local này."""

    async def write(self, event: TraceEvent) -> None:
        return None


@dataclass(frozen=True)
class _Row:
    case_id: str
    expects_refusal: bool
    # ── nhóm ĐẤU-NỐI
    n_events: int
    run_ids_unique: int
    ts_monotonic: bool
    tenant_consistent: bool
    n_chunks: int
    # ── nhóm CHẤT-LƯỢNG
    success: bool
    citation_acc: float
    refused: bool
    # ── nhóm CHẨN ĐOÁN
    kb_hits: int
    kb_expected: int
    leaked: bool
    # Chỉ dùng cho RED-CHECK: lý do case này ĐƯỢC THIẾT KẾ để đỏ. Có giá trị thì `blame` trả nó
    # thay vì suy đoán — vì heuristic quy-trách-nhiệm giả định NHÃN GOLDEN là đúng, mà XF thì cố ý
    # sai nhãn. Không có field này, XF-01 bị đọc thành "KB không lấy về chunk kỳ vọng" trong khi KB
    # hoàn toàn đúng: `#c999` là chunk không tồn tại do chính test bịa ra.
    designed_red: str | None = None

    @property
    def thong(self) -> bool:
        """Verdict đấu-nối: đủ 4 event · 1 run_id · ts tăng · tenant_id nhất quán."""
        return self.n_events == 4 and self.run_ids_unique == 1 and self.ts_monotonic and self.tenant_consistent

    @property
    def kb_label(self) -> str:
        return f"{self.kb_hits}/{self.kb_expected}" if self.kb_expected else "—"

    @property
    def blame(self) -> str:
        """Quy trách nhiệm khi case đỏ — đọc từ CHẨN ĐOÁN, không đoán.

        Đây là lý do cột chẩn đoán tồn tại: `success=False` một mình không nói được nên đi sửa KB
        hay sửa đoạn LLM→citation.
        """
        # `success` xét TRƯỚC `designed_red`: một case XF mà lại PASS là chính cái sự cố RED-CHECK
        # tồn tại để bắt, nên nó phải hiện ra là "—" (không có lý do đỏ) chứ không được in tiếp lý
        # do thiết kế — in lý do đỏ trên một dòng PASS thì đọc bảng sẽ tưởng nó vẫn đỏ.
        if self.success:
            return "—"
        if self.designed_red is not None:
            return self.designed_red
        if self.expects_refusal:
            if self.leaked:
                return "KB — chunk chéo tenant lọt vào trace (fence thủng)"
            if not self.refused:
                return "LLM — không từ chối dù đoạn trích không trả lời được"
            return "scorer — refused đúng, leak không, mà vẫn đỏ: xem lại luật"
        if self.kb_expected and self.kb_hits < self.kb_expected:
            return "KB — không lấy về chunk kỳ vọng"
        return "LLM — có chunk trong prompt nhưng không dùng/không trích"


def _trace_table(events: list[TraceEvent]) -> str:
    """Bảng trace thô mỗi node — bằng chứng dữ liệu đi xuyên submodule trong CÙNG một run."""
    header = (
        f"  {'#':<2}{'node_id':<8}{'node_type':<13}{'ts (giờ.µs)':<18}"
        f"{'tenant_id':<15}{'tok(p/c)':<9}{'cost':<6}citations"
    )
    lines = [header, "  " + "-" * (len(header) - 2)]
    for i, e in enumerate(events, 1):
        ts_time = e.ts.split("T", 1)[1].replace("+00:00", "Z") if "T" in e.ts else e.ts
        tok = f"{e.tokens.prompt}/{e.tokens.completion}"
        cites = ",".join(e.citations) if e.citations else "—"
        lines.append(
            f"  {i:<2}{e.node_id:<8}{e.node_type.value:<13}{ts_time:<18}"
            f"{str(e.tenant_id)[:13] + '…':<15}{tok:<9}{e.cost:<6.2f}{cites}"
        )
    return "\n".join(lines)


async def run_one(
    case: GoldenCase,
    kb: _CountingKb | _LeakyKb,
    *,
    verbose: bool = True,
    designed_red: str | None = None,
) -> _Row:
    """Chạy MỘT case qua luồng thật và in block evidence. `kb` truyền tường minh để RED-CHECK
    XF-02 thay được bằng `_LeakyKb` mà không cần cờ ẩn nào trong hàm này."""
    llm = ExtractiveFakeLLM()
    runner = EngineAgentRunner(kb_search=kb, llm=llm, embedding=_StubEmbedding(), trace_writer=_NoopTraceWriter())
    case_run = await runner.run_case(
        agent_id="agent-callisto-d4",
        query=case.query,
        tenant_id=TENANT_IDS[case.tenant],
        section_roles=case.section_roles,
    )

    grounded = citations_from_trace(case_run.events)
    result = score_case(case, case_run.answer, grounded)
    events = case_run.events

    # CHẨN ĐOÁN: chunk ĐÃ TRUY XUẤT — nguồn khác `grounded`. Lý do khác nhau: node `kb-retrieve`
    # trả `list` nên interpreter đặt `TraceEvent.citations = None`; chunk thật nằm ở
    # `outputs["chunks"]`. Còn `grounded` (`citations_from_trace`) gom `.citations` mọi event, nên
    # thực chất là citations của `llm-step` = LLM-trích ∩ retrieved.
    # `outputs` khai là `dict[str, object]` nên phải narrow từng tầng, không `.get(..., [])` suông.
    retrieved_ids: set[str] = set()
    for event in events:
        raw_chunks = event.outputs.get("chunks")
        if not isinstance(raw_chunks, list):
            continue
        for chunk in raw_chunks:
            if isinstance(chunk, dict) and "chunk_id" in chunk:
                retrieved_ids.add(str(chunk["chunk_id"]))
    kb_hits = sum(1 for c in case.expected_citation if c in retrieved_ids)
    leaked = any(cid.partition("-")[0] == case.expected_tenant for cid in grounded) if case.expects_refusal else False

    run_ids_unique = len({e.run_id for e in events})
    ts_monotonic = all(events[i].ts < events[i + 1].ts for i in range(len(events) - 1))
    tenant_consistent = all(e.tenant_id == TENANT_IDS[case.tenant] for e in events)

    if verbose:
        print(
            f"\n{case.case_id} | tenant={case.tenant} ({str(TENANT_IDS[case.tenant])[:13]}…) "
            f"roles={case.section_roles} | kb.search → {kb.last_count} chunk"
        )
        print(
            f"  TRACE: run_id={(events[0].run_id if events else '?')[:8]}… · {len(events)} event "
            f"· {run_ids_unique} run_id duy nhất · ts đơn điệu={ts_monotonic} "
            f"· tenant nhất quán={tenant_consistent}"
        )
        print(_trace_table(events))
        print(f"  PROMPT: {len(llm.prompts_seen)} lượt gọi LLM · {len(retrieved_ids)} chunk đã vào prompt")
        print(
            f"  → refused={case_run.answer.refused} | citations grounded={grounded} "
            f"| success={result.success} citation_acc={result.citation_accuracy:.2f}"
        )

    return _Row(
        case_id=case.case_id,
        expects_refusal=case.expects_refusal,
        n_events=len(events),
        run_ids_unique=run_ids_unique,
        ts_monotonic=ts_monotonic,
        tenant_consistent=tenant_consistent,
        n_chunks=kb.last_count,
        success=result.success,
        citation_acc=result.citation_accuracy,
        refused=case_run.answer.refused,
        kb_hits=kb_hits,
        kb_expected=len(case.expected_citation),
        leaked=leaked,
        designed_red=designed_red,
    )


def _xf_cases(cases: list[GoldenCase]) -> tuple[GoldenCase, GoldenCase]:
    """2 case PHẢI-ĐỎ, clone bằng `model_copy` từ case thật.

    Nhãn `tenant`/`expected_tenant`/`expected_section_role` giữ NGUYÊN để case rơi đúng nhánh chấm
    — `expects_refusal` suy từ 2 trục nhãn, không từ chuỗi tên case.
    """
    by_id = {c.case_id: c for c in cases}
    xf1 = by_id["SC-01"].model_copy(
        update={
            "case_id": "XF-01",
            "expected": "99 ngày làm việc",  # luồng trả "3 ngày làm việc" → success FAIL
            "expected_citation": ["ankor-leave-001#c999"],  # không tồn tại → cit_acc 0.00
        }
    )
    xf2 = by_id["SC-04"].model_copy(update={"case_id": "XF-02"})  # nhãn refusal nguyên vẹn; KB mới hỏng
    return xf1, xf2


_H_CONN = "─ ĐẤU-NỐI (luồng thật) ─"
_H_QUAL = "─ CHẤT-LƯỢNG ─"
_H_DIAG = "─ CHẨN ĐOÁN ─"


def _fmt_row(r: _Row, note: str = "") -> str:
    tick, cross = "✓", "✗"
    # Nhánh từ-chối: KHÔNG in 1.00 (placeholder cứng, điểm gãy #9) để con số vô nghĩa không lan.
    acc = "n/a*" if r.expects_refusal else f"{r.citation_acc:.2f}"
    return (
        f"{r.case_id:<8}{r.n_events:<4}{(tick if r.run_ids_unique == 1 else cross):<5}"
        f"{(tick if r.ts_monotonic else cross):<4}{(tick if r.tenant_consistent else cross):<7}"
        f"{r.n_chunks:<7}{('THÔNG' if r.thong else 'TẮC'):<7}"
        f"{('PASS' if r.success else 'FAIL'):<8}{acc:<8}{r.kb_label:<7}{r.blame}{note}"
    )


def _header() -> str:
    return (
        f"{'':8}{_H_CONN:<34}{_H_QUAL:<16}{_H_DIAG}\n"
        f"{'case':<8}{'#ev':<4}{'1run':<5}{'ts↑':<4}{'tenant':<7}{'#chunk':<7}{'THÔNG?':<7}"
        f"{'success':<8}{'cit_acc':<8}{'KB':<7}quy-trách-nhiệm"
    )


async def _run(pool: Pool) -> int:
    embedding = _CallistoEmbedding()
    golden = _demo_golden_set()
    bar = "=" * 100

    print(bar)
    print(f"E2E SMOKE-EVAL — {golden.golden_set_ref} ({len(golden.cases)} case) qua luồng thật")
    print(
        "THẬT: recipe · interpreter · kb.search (PgKbSearch, Postgres/pgvector thật, fence) · "
        "trace · build_prompt · grounding · adapter · scorer"
    )
    print("FIXTURE: ExtractiveFakeLLM — chỉ đọc prompt, không thấy golden (ý @DongAnh2704)")
    print(bar)

    written = await KbIngest(pool, embedding).ingest(list(load_callisto()))
    print(f"KB seed: {written} chunk nạp vào kb.chunks (idempotent — chạy lại không nhân đôi)\n")

    rows = [await run_one(case, _CountingKb(pool, embedding)) for case in golden.cases]

    print("\n" + bar)
    print("RED-CHECK — 2 negative-control của BỘ CHẤM, PHẢI ĐỎ (không tính vào headline)")
    print("  XF-01 nhãn golden cố ý sai → nhánh trả-lời phải đỏ cả success lẫn cit_acc")
    print("  XF-02 KB cố ý HỎNG FENCE (trả chunk chéo tenant) → conjunct no_leak phải đỏ")
    print(bar)
    xf1, xf2 = _xf_cases(list(golden.cases))
    # `expected_tenant` là `str | None` trong hợp đồng GoldenCase. XF-02 dựng KB rò bằng chính
    # tenant đó, nên thiếu nó thì negative-control mất nghĩa — dừng ồn ào, đừng lặng lẽ bỏ qua.
    leak_tenant = xf2.expected_tenant
    if leak_tenant is None:
        print(f"BỎ RED-CHECK XF-02: case gốc {xf2.case_id} không có `expected_tenant`", file=sys.stderr)
        return 1
    xf_rows = [
        await run_one(
            xf1, _CountingKb(pool, embedding), designed_red="nhãn golden cố ý sai (expected + #c999 bịa)"
        ),
        await run_one(
            xf2,
            _LeakyKb(pool, embedding, leak_as=TENANT_IDS[leak_tenant]),
            designed_red="KB cố ý hỏng fence → conjunct no_leak bắt được",
        ),
    ]

    print("\n" + "-" * 100)
    print(_header())
    print("-" * 100)
    for r in rows:
        print(_fmt_row(r))
    print("-" * 100)
    for r in xf_rows:
        print(_fmt_row(r, note="  ← PHẢI đỏ (thiết kế)"))
    print("-" * 100)

    thong = sum(1 for r in rows if r.thong)
    n_pass = sum(1 for r in rows if r.success)
    all_thong = thong == len(rows)
    xf_all_fail = all(not r.success for r in xf_rows)

    print(
        "* nhánh từ-chối in `n/a*`: `citation_accuracy` ở nhánh này là hằng 1.0 cứng\n"
        "  (`harness.py:135`, Q2 `scorecard-v0.md` §3 chưa chốt) — nó KHÔNG đo gì, kể cả khi case đã đỏ.\n"
        "  Điểm gãy #9, mang vào agenda freeze D11."
    )
    print(f"\nĐẤU-NỐI (wiring) : {thong}/{len(rows)} THÔNG")
    print(
        f"CHẤT-LƯỢNG TRÊN FIXTURE ĐỌC PROMPT: {n_pass}/{len(rows)} PASS "
        "— chấm trên ExtractiveFakeLLM, KHÔNG phải model thật"
    )
    print(
        f"RED-CHECK : {sum(1 for r in xf_rows if not r.success)}/{len(xf_rows)} FAIL → "
        f"{'bộ chấm biết đỏ ✓' if xf_all_fail else '⚠️ BỘ CHẤM KHÔNG BIẾT ĐỎ — kiểm ngay'}"
    )
    if n_pass < len(rows):
        print("\nCase đỏ + quy trách nhiệm (đọc từ cột CHẨN ĐOÁN, không suy diễn):")
        for r in rows:
            if not r.success:
                print(f"  {r.case_id}: {r.blame}")
    print("\nLive Gemini evaluation: chưa chạy.")
    print(
        '⚠️ Mục "≥1 case FAIL với LLM THẬT" (#59) VẪN TREO: case đỏ ở trên do FIXTURE gây ra,\n'
        "   không phải model chạy thật. Đóng mục đó cần một lượt gọi Gemini thật (P9)."
    )
    print(
        "ℹ️ `exit 0` ở script này nghĩa là wiring thông và negative controls được phát hiện;\n"
        "   nó KHÔNG có nghĩa mọi golden case đã vượt quality gate."
    )

    # Gate: luồng hỏng hoặc bộ chấm hỏng → 1. Chất-lượng thấp KHÔNG gate (đó là kết quả đo).
    return 0 if (all_thong and xf_all_fail) else 1


async def main() -> int:
    """Mở 2 pool (owner cho DDL/grant boot — cùng cách `studio_app.app.create_app()` làm ở lifespan;
    non-owner cho DML/RLS thật), chạy `_run`, rồi đóng cả hai dù `_run` raise hay không.

    Không tự dựng DSN thủ công: `get_admin_pool()`/`get_pool()` đọc qua `studio_app.settings.Settings`
    (pydantic, `STUDIO_DATABASE_URL`/`STUDIO_DATABASE_URL_ADMIN`) — thiếu biến môi trường thì
    pydantic tự raise `ValidationError` rõ ràng, không chạy câm rồi vỡ sâu trong pool."""
    admin = await get_admin_pool()
    await ensure_all_schemas(admin)
    await grant_app_privileges(admin)
    pool = await get_pool()
    try:
        return await _run(pool)
    finally:
        await close_pools()


if __name__ == "__main__":
    if sys.platform == "win32":
        # Windows defaults to `ProactorEventLoop`; psycopg async refuses it outright ("Psycopg
        # cannot use the 'ProactorEventLoop' to run in async mode"). `Selector*` is the loop
        # psycopg's own error message recommends, and it is stdlib on every platform this script
        # runs on — no extra dependency.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.exit(asyncio.run(main()))
