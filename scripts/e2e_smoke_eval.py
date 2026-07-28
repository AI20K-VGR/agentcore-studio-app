"""E2E 'đấu nối thông' — weekly demo #1 evidence (Sprint 1, Day 5).

Chạy bộ smoke-5 (golden của DE) qua LUỒNG THẬT xuyên 4 quadrant, in bảng điểm:

    workbench.create_recipe_d4        (form → Recipe, tenant_id UUID)
      → engine.interpreter.run        (walk 4-node, inject recipe.tenant_id, emit trace)
        → kb.StaticKbSearch           (filter RLS tenant_id UUID + section_role, chunk thật)
      → RunResult{final_state, events}
      → studio_app.EngineAgentRunner  (adapter: RunResult → CaseRun)   [#29, co-author AIE-1+AIE-2]
      → evalhub.score_case            (chấm citation-accuracy đọc từ TRACE)

THẬT: recipe · interpreter · kb.search (fence tenant/role) · trace 4-event · grounding citation ·
      adapter · scorecard. Đây là phần chứng minh "đấu nối thông".
STUB (nhãn rõ): LLM = câu trả lời recorded per-case (VCR-style). Lý do: llm-step chưa build prompt
      từ chunk retrieved (việc tiếp theo của AIE-1/SWE), nên LLM generic chưa phân biệt case được.
      Refusal dùng sentinel engine `[[REFUSED]]`.

────────────────────────────────────────────────────────────────────────────
CÁCH CHẠY (cần checkout đúng feature-branch từng submodule — các fix Day-5 chưa merge main):

    cd <repo-cha>
    git -C packages/workbench checkout main                          && git -C packages/workbench pull --ff-only
    git -C packages/engine    checkout day5/fix-tenant-id-contract-sync && git -C packages/engine pull --ff-only
    git -C packages/kb        checkout feat/day5-reader-d13          && git -C packages/kb pull --ff-only
    git -C packages/evalhub   checkout aie-2/day-05-scorecard-read-trace
    uv sync
    uv run python apps/studio/scripts/e2e_thonluong_demo.py

Kỳ vọng: 5/5 PASS; cột '#chunk' = 3/3/1/3/0 (kb.search thật, đúng tenant+role fence).
────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio

from studio_app.eval_adapter import EngineAgentRunner
from studio_contracts import TraceEvent
from studio_evalhub.cli import _demo_golden_set
from studio_evalhub.harness import _retrieved_citations, score_case
from studio_kb.doc_factory import TENANT_IDS
from studio_kb.static_search import StaticKbSearch

# LLM recorded per-case (VCR). chunk_id trong [] khớp golden DE; [[REFUSED]] = sentinel từ chối engine.
_CANNED_LLM: dict[str, str] = {
    "SC-01": "Nhân viên cần báo trước 3 ngày làm việc [ankor-leave-001#c1].",
    "SC-02": "Nhân viên cần báo trước 7 ngày làm việc [borea-leave-001#c1].",
    "SC-03": "Trưởng nhóm được duyệt chi tối đa 20 triệu đồng [ankor-expense-001#c2].",
    "SC-04": "[[REFUSED]]",
    "SC-05": "[[REFUSED]]",
}


class _RecordedLLM:
    """STUB: replay câu trả lời recorded (bỏ qua prompt). Thay bằng LLM provider thật khi llm-step
    build prompt từ chunk (AIE-1/SWE)."""

    def __init__(self, response: str) -> None:
        self._response = response

    async def complete(self, prompt: str, **kwargs: object) -> str:  # noqa: ARG002
        return self._response


class _StubEmbedding:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 0.0, 0.0] for _ in texts]


class _NoopTraceWriter:
    """Trace nằm sẵn trên RunResult.events; sink Postgres (DE PgTraceWriter) không cần cho demo local."""

    async def write(self, event: object) -> None:  # noqa: ARG002
        return None


class _CountingKb(StaticKbSearch):
    """StaticKbSearch thật + đếm số chunk trả về (để in evidence)."""

    last_count: int = 0

    async def search(self, query, tenant_id, section_roles, top_k):  # type: ignore[no-untyped-def]
        result = await super().search(query, tenant_id, section_roles, top_k)
        _CountingKb.last_count = len(result)
        return result


def _trace_table(events: list[TraceEvent]) -> str:
    """Bảng trace thô mỗi node — bằng chứng dữ liệu đi xuyên submodule trong CÙNG một run:
    cùng `run_id`, đủ 4 node (mọi node emit), `ts` đơn điệu tăng, `tenant_id` UUID threaded,
    citations grounded ở llm-step."""
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


async def _main() -> None:
    golden = _demo_golden_set()
    print("=" * 78)
    print("E2E ĐẤU NỐI THÔNG — smoke-5 qua luồng thật (workbench→engine→kb→trace→adapter→eval)")
    print("THẬT: recipe/interpreter/kb.search/trace/grounding/adapter/scorecard · STUB: LLM (recorded)")
    print("=" * 78)

    rows: list[tuple[str, bool, float, int]] = []
    for case in golden.cases:
        kb = _CountingKb()
        runner = EngineAgentRunner(
            kb_search=kb,
            llm=_RecordedLLM(_CANNED_LLM[case.case_id]),
            embedding=_StubEmbedding(),
            trace_writer=_NoopTraceWriter(),
        )
        case_run = await runner.run_case(
            agent_id="agent-callisto-d4",
            query=case.query,
            tenant_id=TENANT_IDS[case.tenant],
            section_roles=case.section_roles,
        )
        retrieved = _retrieved_citations(case_run.events)
        result = score_case(case, case_run.answer, retrieved)
        rows.append((case.case_id, result.success, result.citation_accuracy, kb.last_count))
        events = case_run.events
        run_id = events[0].run_id if events else "?"
        run_ids_unique = len({e.run_id for e in events})
        ts_monotonic = all(events[i].ts < events[i + 1].ts for i in range(len(events) - 1))
        print(
            f"\n{case.case_id} | tenant={case.tenant} ({str(TENANT_IDS[case.tenant])[:13]}…) "
            f"roles={case.section_roles} | kb.search → {kb.last_count} chunk"
        )
        print(
            f"  TRACE: run_id={run_id[:8]}… · {len(events)} event · {run_ids_unique} run_id duy nhất "
            f"· ts đơn điệu={ts_monotonic}  (workbench→engine→kb→trace, 1 run xuyên mọi node)"
        )
        print(_trace_table(events))
        print(
            f"  → refused={case_run.answer.refused} | grounded citations(từ trace)={retrieved} "
            f"| success={result.success} citation_acc={result.citation_accuracy:.2f}"
        )

    print("\n" + "-" * 78)
    print(f"{'case_id':<10}{'success':<10}{'citation_acc':<14}{'#chunk (kb thật)':<18}")
    print("-" * 78)
    for case_id, success, accuracy, count in rows:
        print(f"{case_id:<10}{('PASS' if success else 'FAIL'):<10}{accuracy:<14.2f}{count:<18}")
    passed = sum(1 for _, success, _, _ in rows if success)
    print("-" * 78)
    print(f"{passed}/{len(rows)} PASS")


if __name__ == "__main__":
    asyncio.run(_main())
