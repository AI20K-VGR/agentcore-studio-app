"""Một run = MỘT recipe — `DEC-D20-07`, đường (b) do AIE-1 chốt trên `kit#126`.

## Vấn đề bài này khoá

Trước D20, `EngineAgentRunner.run_case` gọi `create_recipe_d4(query=…)` **mỗi case** ⇒ golden-30 sinh
**30 recipe khác nhau**, và câu *"`Scorecard` này chứng nhận recipe nào"* **không có câu trả lời đơn
nhất** — bất kể ai băm và băm bằng gì. Đó là thứ chặn `recipe_hash` (`DEC-03`, quá hạn từ D12), không
phải bản thân phép băm.

SWE đo thêm một vế nặng hơn (`kit#127`, ĐÃ ĐÓNG ở review `app#26` ⛔): trước bản vá đó,
`routes/publish.py` gọi `EvalHarness().run(recipe.agent_id, recipe.golden_set_ref, …)` mà **không
truyền `recipe` canvas vào đâu cả** ⇒ recipe được **chấm** và recipe được **publish** là hai đối
tượng khác nhau **về cấu trúc** — dù `recipe_hash` có producer, `publish()` vẫn chứng nhận **nhầm
đối tượng**. `_evaluate()` giờ truyền `recipe=recipe` vào `EngineAgentRunner` (xem
`routes/publish.py`), đóng đúng khe mà đoạn dưới đây gọi tên.

## Bài nào bắt được gì — đo bằng mutant, không tự khai

Bảng này ghi kết quả **đã gieo thật**, vì bản nháp đầu của file dán nhãn sai cho một bài:

| Mutant gieo vào `eval_adapter.py` | Bài đỏ |
|---|---|
| `M-G7` — `run_case` quay lại `create_recipe_d4(query=…)` mỗi case | **chỉ** `…caller_truyen_vao…` |
| `M-G7a` — `certified_recipe` **giữ** `query` | `…goc_khong_mang_query` + `…query_duoc_bom_dung…` |
| `M-G7b` — nuốt `query`, mọi case dùng chuỗi cố định | 3 bài, gồm guard degenerate |

**Phát hiện đáng ghi, và nó ngược trực giác:** quay `run_case` về hành vi cũ (`M-G7`) **không đổi một
giá trị nào quan sát được** — 30 case vẫn ra 30 recipe khác nhau đúng ở `query`, y như sau bản vá.
Bởi vì trong một run, `agent_id`/`tenant_id`/`scope` **vốn đã** không đổi.

⇒ Giá trị thật của `DEC-D20-07` **không** nằm ở chỗ *"30 recipe thành 1"* ở tầng giá trị. Nó nằm ở
chỗ: giờ tồn tại **một artifact có tên, không mang `query`** (`certified_recipe`) để `recipe_hash`
băm, và **`run_case` chứng minh được là dẫn xuất từ nó** — cộng khe `recipe=` để caller đưa **đúng
recipe sắp publish** vào. Khe đó mới là thứ đóng finding của SWE, và nó là thứ duy nhất `M-G7` giết.

Nói ra vì bản nháp đầu gọi bài 30-case là *"bài trả lời 🅐"* — **sai**: nó xanh ở cả hai phía mutant.
Nó được giữ lại với đúng vai thật của nó (xem docstring của chính bài đó), không phải vai đã dán.

**Bài này KHÔNG tự chứng minh `recipe_hash` đã có** — nó chứng minh *đã có đúng một thứ để băm*,
điều kiện **cần**. Băm trên chuỗi byte nào đã được SWE chốt (`studio_workbench.publish.
recipe_hash()`: `sha256` trên `model_dump(mode="json", by_alias=True)` + `sort_keys=True`, xem
docstring hàm đó để biết lý do từng cờ), và `DEC-D20-02` giữ nguyên: evalhub **nhận** giá trị,
không tự dẫn xuất.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from studio_app import eval_adapter
from studio_app.eval_adapter import EngineAgentRunner
from studio_app.providers.fakes import FakeEmbedding
from studio_contracts import NodeType, Recipe, TraceEvent
from studio_engine.interpreter import RunResult
from studio_workbench import create_recipe_d4
from studio_workbench.recipe_ops import without_query

TENANT_ID = UUID("a0000000-0000-0000-0000-000000000001")
OTHER_TENANT = UUID("b0000000-0000-0000-0000-000000000001")


class _StubKbSearch:
    async def search(self, query: str, tenant_id: UUID, section_roles: list[str], top_k: int) -> list[Any]:  # noqa: ARG002
        return []


class _StubLLM:
    async def complete(self, prompt: str, **kwargs: object) -> str:  # noqa: ARG002
        return "x"


class _StubTraceWriter:
    async def write(self, event: TraceEvent) -> None:
        del event


class _RecipeRecordingInterpreter:
    """Namespace fake thay `eval_adapter.interpreter` — ghi lại **recipe interpreter thật sự nhận**.

    Đây là chỗ đo đúng thứ cần đo: không phải recipe adapter *định* dựng, mà recipe **đi tới**
    interpreter. Hai cái chỉ bằng nhau khi không có ai `model_copy` ở giữa mà quên."""

    def __init__(self) -> None:
        self.recipes: list[Recipe] = []

    async def run(self, recipe: Recipe, **kwargs: object) -> RunResult:  # noqa: ARG002
        self.recipes.append(recipe)
        return RunResult(run_id="r-test", events=[], final_state={"n2": {"answer": "x"}})


def _patched(monkeypatch: pytest.MonkeyPatch, *, recipe: Recipe | None = None) -> tuple[EngineAgentRunner, Any]:
    stub = _RecipeRecordingInterpreter()
    monkeypatch.setattr(eval_adapter, "interpreter", stub)
    runner = EngineAgentRunner(
        kb_search=_StubKbSearch(),
        llm=_StubLLM(),
        embedding=FakeEmbedding(),
        trace_writer=_StubTraceWriter(),
        recipe=recipe,
    )
    return runner, stub


def _kb_params(recipe: Recipe) -> dict[str, object]:
    node = next(n for n in recipe.dag.nodes if n.type is NodeType.KB_RETRIEVE)
    return dict(node.params)


def test_recipe_goc_khong_mang_query() -> None:
    """Recipe **gốc** không có khoá `query` — không phải `query=""`, mà **vắng mặt**.

    Hai cái cho **hai chuỗi byte khác nhau** khi serialize, và chuỗi byte đó chính là đầu vào của
    `recipe_hash`. Đây là một trong năm trục canonical-form đang chờ SWE chốt (`kit#127` 🅑); chọn
    *"vắng mặt"* là chọn dạng **không mang dữ liệu đề bài**, nhất quán với lý do tách `query` ra.

    Assert `not in` chứ không `== ""`: một cài đặt đặt chuỗi rỗng sẽ **xanh** với `== ""` và vẫn làm
    hash khác đi."""
    runner = EngineAgentRunner(
        kb_search=_StubKbSearch(), llm=_StubLLM(), embedding=FakeEmbedding(), trace_writer=_StubTraceWriter()
    )

    base = runner.certified_recipe(agent_id="a1", tenant_id=TENANT_ID, section_roles=["public"])

    assert "query" not in _kb_params(base)


async def test_hai_case_khac_query_dung_chung_mot_recipe_goc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hai case khác `query` ⇒ recipe **gốc** bằng nhau.

    Đo bằng `==` trên `Recipe` (pydantic `frozen=True`, so mọi field) chứ không so từng field: field
    thêm vào contract sau này tự động được so."""
    runner, _ = _patched(monkeypatch)

    a = runner.certified_recipe(agent_id="a1", tenant_id=TENANT_ID, section_roles=["public"])
    b = runner.certified_recipe(agent_id="a1", tenant_id=TENANT_ID, section_roles=["public"])

    assert a == b


async def test_query_duoc_bom_dung_vao_kb_retrieve(monkeypatch: pytest.MonkeyPatch) -> None:
    """Biến thể per-case khác recipe gốc **đúng một khoá** `params["query"]`.

    Đo trên recipe **interpreter thật sự nhận**, không trên recipe adapter định dựng — hai cái chỉ
    bằng nhau khi không ai `model_copy` ở giữa mà quên.

    Vế thứ hai (`without_query(nhận) == gốc`) là vế thật sự khoá: nó nói *"khác ĐÚNG một khoá"*, chứ
    không chỉ *"có khoá query"*. Một cài đặt tiện tay đổi thêm `top_k` hay `tenant_id` vẫn xanh nếu
    chỉ assert vế đầu."""
    runner, stub = _patched(monkeypatch)

    await runner.run_case(agent_id="a1", query="câu hỏi A", tenant_id=TENANT_ID, section_roles=["public"])

    nhan = stub.recipes[0]
    assert _kb_params(nhan)["query"] == "câu hỏi A"

    goc = runner.certified_recipe(agent_id="a1", tenant_id=TENANT_ID, section_roles=["public"])
    assert without_query(nhan) == goc


async def test_ba_muoi_case_chi_sinh_MOT_recipe_goc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ở **quy mô 30 case**: mọi biến thể per-case khác nhau **đúng ở `query`**, không ở gì khác.

    **Bài này KHÔNG phân biệt được bản vá với hành vi cũ** — đo thật, không suy đoán: gieo `M-G7`
    (`run_case` quay lại dựng recipe mỗi case) thì nó **vẫn xanh**, vì trong một run
    `agent_id`/`tenant_id`/`scope` vốn đã không đổi. Bản nháp đầu của file gọi nó là *"bài trả lời
    🅐"* — sai, và giữ nguyên nhãn đó sẽ là một bài **tự khai mạnh hơn thực tế**.

    Vai thật của nó: **lưới quy mô** cho `test_query_duoc_bom_dung_vao_kb_retrieve`. Bài kia đo trên
    **một** case; bài này đo trên 30, nên nó bắt được thứ chỉ lộ ra khi lặp — ví dụ một cài đặt tương
    lai bơm thêm cái gì đó **theo chỉ số case** (`top_k` tăng dần, `trace_id`, timestamp), vốn xanh
    trên một case và hỏng trên 30.

    Assert thứ hai (`len(queries) == 30`) là thứ giữ bài này khỏi **degenerate**: không có nó, một
    cài đặt nuốt hết `query` (mọi biến thể giống hệt nhau) cũng cho `len(goc) == 1` và bài vẫn xanh —
    lúc đó nó khẳng định *"đồng nhất"* trên một tập đã bị làm cho đồng nhất."""
    runner, stub = _patched(monkeypatch)

    for i in range(30):
        await runner.run_case(agent_id="a1", query=f"câu hỏi {i}", tenant_id=TENANT_ID, section_roles=["public"])

    assert len(stub.recipes) == 30

    queries = {_kb_params(r)["query"] for r in stub.recipes}
    assert len(queries) == 30, f"30 case phải mang 30 query khác nhau, đo được {len(queries)} — tập đã bị làm phẳng"

    goc_khac_nhau = {without_query(r).model_dump_json() for r in stub.recipes}
    assert len(goc_khac_nhau) == 1, f"gỡ query ra thì 30 biến thể phải trùng nhau, đo được {len(goc_khac_nhau)}"


async def test_recipe_caller_truyen_vao_duoc_dung_NGUYEN_TRANG(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caller truyền `recipe=` ⇒ adapter dùng **đúng recipe đó**, không dựng lại.

    Đây là vế đóng finding của SWE (`kit#127`): *"recipe được CHẤM và recipe được PUBLISH là hai đối
    tượng khác nhau về cấu trúc"*. Hai cái chỉ bằng nhau khi caller đưa recipe thật vào — nên khe
    `recipe=` này **là** bản vá, không phải tiện ích.

    Dùng một recipe có `tenant_id`/`scope` **khác hẳn** mặc định của adapter: nếu adapter lỡ dựng lại
    thay vì dùng cái được đưa, hai giá trị đó sẽ lộ ra ngay."""
    ngoai = create_recipe_d4(agent_id="agent-canvas", tenant_id=OTHER_TENANT, scope="borea/finance")
    runner, stub = _patched(monkeypatch, recipe=ngoai)

    base = runner.certified_recipe(agent_id="bị-bỏ-qua", tenant_id=TENANT_ID, section_roles=["public"])
    assert base is ngoai

    await runner.run_case(agent_id="bị-bỏ-qua", query="q?", tenant_id=TENANT_ID, section_roles=["public"])
    nhan = stub.recipes[0]

    assert nhan.agent_id == "agent-canvas"
    assert nhan.tenant_id == OTHER_TENANT
    assert nhan.kb_binding.scope == "borea/finance"
    assert _kb_params(nhan)["query"] == "q?"
