"""D20 GATE-2 — thí nghiệm tách biến cho finding (e)① (`engine#26`, review `dholmes0207` F-4).

    uv run python apps/studio/scripts/probe_isolate_retrieval_d20.py

Không cần Postgres (`StaticKbSearch` in-memory + no-op trace writer).

## Vì sao script này tồn tại

`docs/evidence-d20.md` §4.2 (`agentcore-studio-engine`) so hai đường đo `success_rate`/
`citation_accuracy` trên golden-30 và thấy chúng lệch cả 2 biến cùng lúc:

- (a) runner/LLM double: `StubAgentRunner` nạp câu trả lời **đã ghi lại** (`DEC-D17-04`) vs
  `EngineAgentRunner` + `ExtractiveFakeLLM` **sống** (đường D20).
- (b) retrieval backend: `StaticKbSearch` in-memory (`DEC-D17-04`, qua `run_golden_batch.py`) vs
  `PgKbSearch` trên Postgres thật (đường D20).

Bản đầu của §4.2 chỉ quy cho (a). Review bắt đúng: cần đổi **đúng 1 biến** để tách. Script này giữ
nguyên (a) = đường D20 (runner sống + `ExtractiveFakeLLM`), chỉ đổi (b): `PgKbSearch` → `StaticKbSearch`.

Kết quả (tại `engine@bfa19cc`, ghi lại 14/08 — chạy lại để xác nhận, không tin số in sẵn ở đây):

    kb_search=StaticKbSearch (giữ nguyên runner/LLM sống)
    success_rate=0.6667  citation_accuracy=0.9091  n_scored_citation=22  verdict=FAIL  len=30

So với đường D20 gốc (`PgKbSearch`): `success_rate=0.1667`, `citation_accuracy=0.2273`. Đổi 1 biến
(retrieval) kéo `citation_accuracy` từ `0.2273` lên `0.9091` — retrieval backend là biến chính.

## Một điểm CHƯA giải thích được (Duy nêu ở round 2 review, R-3) — ghi lại, không giấu

So với `DEC-D17-04` (`StaticKbSearch` + `StubAgentRunner` ghi lại): đổi tiếp biến (a) từ script này
sang `DEC-D17-04` làm `citation_accuracy` **tăng** (`0.9091 → 1.0000`) nhưng `success_rate` **giảm**
(`0.6667 → 0.2667`) — hai chỉ số đảo dấu khi đổi CÙNG một biến. Runner sống chấm cao hơn stub ghi lại
ở `success_rate` nhưng thấp hơn ở `citation_accuracy`. "Cả 2 biến cùng góp phần tuyến tính" KHÔNG mô
tả được hiện tượng này — hoặc còn biến thứ ba, hoặc là đặc tính của cách `success_rate` được tính
(ví dụ: match trên nội dung câu trả lời, không chỉ trên citation). Chưa điều tra tiếp trong D20 —
chủ: AIE-2 (đo `success_rate` là gì cụ thể) + AIE-1 (nếu chạm interpreter/executor)."""

from __future__ import annotations

import asyncio

from studio_app.eval_adapter import EngineAgentRunner
from studio_app.providers.fakes import ExtractiveFakeLLM
from studio_contracts import TraceEvent
from studio_evalhub.harness import EvalHarness
from studio_kb.doc_factory import TENANT_IDS
from studio_kb.static_search import StaticKbSearch

_ROOT = __import__("pathlib").Path(__file__).resolve().parents[3]
_GOLDEN_30 = _ROOT / "packages" / "kb" / "golden" / "callisto-golden-30-v1.yaml"
_REF = "callisto-golden-30-v1"
_AGENT = "agent-callisto-d4"


class _StubEmbedding:
    """`recipe_d4` không có bước embed; `LlmStepExecutor` nhận qua constructor nhưng không dùng."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 0.0, 0.0] for _ in texts]


class _NoopTraceWriter:
    """Không cần Postgres cho thí nghiệm này — chỉ cần `Scorecard.aggregate`, không cần trace ghi lại."""

    async def write(self, event: TraceEvent) -> None:
        return None


async def main() -> None:
    scorecard = await EvalHarness().run(
        _AGENT,
        _REF,
        golden_set_path=_GOLDEN_30,
        runner=EngineAgentRunner(
            kb_search=StaticKbSearch(),  # biến duy nhất đổi so với đường D20 (PgKbSearch)
            llm=ExtractiveFakeLLM(),
            embedding=_StubEmbedding(),
            trace_writer=_NoopTraceWriter(),
        ),
        tenant_ids=TENANT_IDS,
        threshold_success=0.9,
        threshold_citation_accuracy=0.95,
    )
    print(
        "kb_search=StaticKbSearch (giữ nguyên runner/LLM sống — chỉ đổi retrieval backend)\n"
        f"success_rate={scorecard.aggregate.success_rate:.4f}  "
        f"citation_accuracy={scorecard.aggregate.citation_accuracy:.4f}  "
        f"n_scored_citation={scorecard.aggregate.n_scored_citation}  "
        f"verdict={scorecard.gate.verdict}  len={len(scorecard.results)}"
    )


if __name__ == "__main__":
    asyncio.run(main())
