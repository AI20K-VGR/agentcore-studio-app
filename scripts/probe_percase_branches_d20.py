"""D20 — bung `success_rate` theo NHÁNH, để trả lời finding (e)① (`kit#126`).

    uv run python apps/studio/scripts/probe_percase_branches_d20.py

Không cần Postgres (`StaticKbSearch` in-memory + no-op trace writer). Chạy từ **kit root**.

## Câu hỏi nó trả lời, và vì sao câu hỏi CŨ sai

`kit#126` (e)① nêu: `success_rate = 0.1667` (đường D20) trong khi `DEC-D17-04` đo `0.2667` trên
"cùng golden-30" — rồi đặt điều kiện lật: *"chạy lại cùng golden-30 trên `engine@62773ba` và so"*.

Điều kiện đó **không phân biệt được gì**, và `engine#26` §4.1 đã chứng minh: diff engine giữa hai
commit không chạm dòng nào tính `citations`/`refused`, nên "sai số 0" là kết quả bị đảm bảo trước
khi chạy. Vòng sau đổi sang tách biến retrieval (`contracts` không liên quan; xem
`probe_isolate_retrieval_d20.py`) và thấy retrieval là biến chính — nhưng vẫn còn một hiện tượng
chưa giải thích: đổi runner làm `citation_accuracy` **tăng** mà `success_rate` **giảm**.

Script này bung theo nhánh và cho thấy **hai số kia chưa từng so sánh được với nhau.**

## Kết quả (14/08, `engine@ad3ce8f`) — chạy lại để xác nhận, không tin số in sẵn

    nhánh TỪ-CHỐI : 0/8 success
    nhánh TRẢ-LỜI : 20/22 success
    tổng          : 20/30 = 0.6667   ·  citation_accuracy 0.9091 trên n=22

Ghép với hai lần đo trước thì ra bảng này:

    | Đường đo                       | từ-chối | trả-lời | tổng          | citation |
    | DEC-D17-04 (stub GHI LẠI)      |   8/8   |  0/22   | 8/30  = 0.2667| 22/22 = 1.0000 |
    | Tách biến (live + Static)      |   0/8   | 20/22   | 20/30 = 0.6667| 20/22 = 0.9091 |
    | Đường D20 (live + PgKbSearch)  |   0/8   |  5/22   | 5/30  = 0.1667|  5/22 = 0.2273 |

Hàng 1 suy ra từ số học chứ không đo lại: `citation_accuracy = 22/22` nghĩa là cả 22 case nhánh
trả-lời có citation hoàn hảo, mà tổng success chỉ 8 — và golden-30 có **đúng 8** case từ-chối
(`HB-23`…`HB-30`), nên phân rã duy nhất khớp là `8/8` và `0/22`.

## Hai nguyên nhân, không liên quan gì nhau, ở hai nửa ngược nhau

**Nhánh từ-chối, `0/8` ở mọi đường live.** `ExtractiveFakeLLM` khai trước trong docstring của chính
nó: *"Không biết từ chối. Truy xuất ra chunk nào là chép chunk đó… nên case đòi từ-chối mà retrieval
KHÔNG rỗng sẽ đỏ."* Đây là hành vi **có chủ đích**, và docstring nói rõ điểm của nó là **cận dưới**
của hệ thống với model tệ nhất còn đọc được, *"không phải điểm của hệ thống"*. Hệ quả đo được: mọi
đường dùng `ExtractiveFakeLLM` bị chặn trần ở `22/30 = 0.7333` bất kể retrieval tốt đến đâu.

**Nhánh trả-lời, `0/22` ở đường stub.** `harness.py:346` — `success = (answer.refused is False) and
_contains_phrase(answer.answer, case.expected)`. `success` gate bằng **VĂN BẢN** câu trả lời chứa cụm
`expected`; `citation_accuracy` gate bằng **TẬP CITATION**. Hai trục khác nhau trên cùng một case, nên
một runner citation hoàn hảo mà văn bản không chứa cụm thì `citation = 1.0` đi cùng `success = False`.
Đó là toàn bộ "đảo dấu" — không có biến thứ ba.

## Kết luận cho (e)①

`0.2667` và `0.1667` **không phải hai phép đo cùng một thứ trên hai trạng thái khác nhau.** Chúng là
hai double hỏng ở hai nửa ngược nhau của golden-set, vì hai lý do không liên quan, và cả hai đều là
cận dưới hợp lệ của chính double đó. Không có số nào là "artifact" cần đính chính; **phép so sánh**
mới là chỗ sai — và điều kiện lật viết trên `kit#126` đã mã hoá đúng cái sai đó.

Điều kiện lật ĐÚNG cho câu hỏi này (giữ lại làm mẫu cho lần sau): so `success_rate` giữa hai đường
đo chỉ có nghĩa khi **cùng một runner**; khác runner thì phải bung theo nhánh trước, vì hai nhánh có
hai mẫu số và hai vị từ khác nhau.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from studio_app.eval_adapter import EngineAgentRunner
from studio_app.providers.fakes import ExtractiveFakeLLM
from studio_contracts import TraceEvent
from studio_evalhub.golden_loader import load_golden_set
from studio_evalhub.harness import EvalHarness
from studio_kb.doc_factory import TENANT_IDS
from studio_kb.static_search import StaticKbSearch

_ROOT = Path(__file__).resolve().parents[3]
_GOLDEN_30 = _ROOT / "packages" / "kb" / "golden" / "callisto-golden-30-v1.yaml"
_REF = "callisto-golden-30-v1"
_AGENT = "agent-callisto-d4"


class _StubEmbedding:
    """`recipe_d4` không có bước embed; `LlmStepExecutor` nhận qua constructor nhưng không dùng."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0, 0.0, 0.0] for _ in texts]


class _NoopTraceWriter:
    """Thí nghiệm này chỉ cần `Scorecard.results`, không cần trace ghi lại ⇒ không cần Postgres."""

    async def write(self, event: TraceEvent) -> None:
        return None


async def main() -> None:
    # Nhánh của mỗi case là thuộc tính DẪN XUẤT (`GoldenCase.expects_refusal`, xét cả T1 chéo-tenant
    # lẫn T6 chéo-vai), KHÔNG phải một field trong YAML. Đọc YAML tay rồi tìm khoá `expects_refusal`
    # sẽ ra 0 case từ-chối và bảng trông như mọi case đều là nhánh trả-lời — sai lặng lẽ.
    golden = load_golden_set(_GOLDEN_30, expect_ref=_REF)
    refusal_ids = {case.case_id for case in golden.cases if case.expects_refusal}

    scorecard = await EvalHarness().run(
        _AGENT,
        _REF,
        golden_set_path=_GOLDEN_30,
        runner=EngineAgentRunner(
            kb_search=StaticKbSearch(),
            llm=ExtractiveFakeLLM(),
            embedding=_StubEmbedding(),
            trace_writer=_NoopTraceWriter(),
        ),
        tenant_ids=TENANT_IDS,
        threshold_success=0.9,
        threshold_citation_accuracy=0.95,
    )

    print(f"{'case':<9}{'nhánh':<13}{'success':<10}citation")
    tally = {"từ-chối": [0, 0], "trả-lời": [0, 0]}
    for result in scorecard.results:
        branch = "từ-chối" if result.case_id in refusal_ids else "trả-lời"
        print(f"{result.case_id:<9}{branch:<13}{str(result.success):<10}{result.citation_accuracy:.4f}")
        tally[branch][1] += 1
        tally[branch][0] += int(bool(result.success))

    agg = scorecard.aggregate
    print()
    for branch, (ok, total) in tally.items():
        print(f"nhánh {branch.upper():<9}: {ok}/{total} success")
    print(f"tổng{'':<11}: {sum(v[0] for v in tally.values())}/{len(scorecard.results)} = {agg.success_rate:.4f}")
    print(f"citation_acc{'':<3}: {agg.citation_accuracy:.4f} trên n={agg.n_scored_citation}")
    print(f"verdict{'':<8}: {scorecard.gate.verdict}")


if __name__ == "__main__":
    asyncio.run(main())
