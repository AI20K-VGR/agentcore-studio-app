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

## app#44 — `query` giờ là kwarg `question=`, không còn bơm vào `recipe.dag`

`run_case()` không còn `with_query`/`model_copy` — `run_agent_loop()` (thay `interpreter.run()`)
không đọc `recipe.dag` chút nào, nên "khác ĐÚNG một khoá `params['query']`" (đoạn trên, mô tả hành
vi TRƯỚC app#44) không còn là phép đo đúng nữa. Bài `test_query_duoc_bom_dung_vao_kb_retrieve` đổi
tên + phép đo: giờ khoá đúng **recipe truyền cho `run_agent_loop` là CHÍNH `certified_recipe()`
(không đổi 1 byte nào)**, và `question=` kwarg mang đúng `query` của case — mạnh hơn phép đo cũ (cũ
chỉ đảm bảo "khác đúng 1 khoá", giờ đảm bảo "không đổi khoá nào cả")."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from studio_app import eval_adapter
from studio_app.eval_adapter import EngineAgentRunner
from studio_app.providers.fakes import FakeEmbedding
from studio_contracts import AgentConfig, Dag, Edge, KbBinding, Node, NodeType, Recipe, ScorecardThreshold, TraceEvent
from studio_engine.interpreter import RunResult

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


class _RecipeRecordingAgentLoop:
    """Namespace fake thay `eval_adapter.agent_loop` (app#44 — trước đó `eval_adapter.interpreter`)
    — ghi lại **recipe + question `run_agent_loop` thật sự nhận**, thành CẶP (không tách riêng như
    file cũ): app#44 xoá hẳn `with_query`/`model_copy`, nên "recipe nhận được" và "query của case"
    giờ là 2 tham số ĐỘC LẬP (`recipe`, `question=`) chứ không còn gộp vào 1 object nữa — ghi lại
    thành cặp là cách duy nhất còn đo được "case nào ứng với recipe nào" mà không suy đoán.

    `question` khai `keyword-only, positional-or-keyword=False` trong chữ ký thật của
    `run_agent_loop` — chặn `**kwargs` ở đây bằng cách LẤY THẲNG `question` ra khỏi `kwargs` thay vì
    nhận nó như 1 tham số riêng, để chữ ký fake khớp đúng cách gọi thật (`question=` luôn qua
    kwargs)."""

    def __init__(self) -> None:
        self.calls: list[tuple[Recipe, str]] = []

    @property
    def recipes(self) -> list[Recipe]:
        """Tương thích ngược với tên cũ (`stub.recipes`) cho phần thân bài chưa cần đọc `question`."""
        return [recipe for recipe, _ in self.calls]

    async def run_agent_loop(self, recipe: Recipe, **kwargs: object) -> RunResult:
        question = kwargs["question"]
        assert isinstance(question, str)
        self.calls.append((recipe, question))
        return RunResult(run_id="r-test", events=[], final_state={"n2": {"answer": "x"}})


def _patched(monkeypatch: pytest.MonkeyPatch, *, recipe: Recipe | None = None) -> tuple[EngineAgentRunner, Any]:
    stub = _RecipeRecordingAgentLoop()
    monkeypatch.setattr(eval_adapter, "agent_loop", stub)
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


async def test_query_di_qua_kwarg_question_recipe_khong_doi_1_byte(monkeypatch: pytest.MonkeyPatch) -> None:
    """app#44 — `query` của case đi qua kwarg `question=` của `run_agent_loop`, KHÔNG còn bơm vào
    recipe bằng `model_copy`/`with_query`.

    Vế thứ hai (`nhan == goc`, bằng nhau TOÀN BỘ, không chỉ `without_query(nhan) == goc`) là vế thật
    sự khoá — MẠNH HƠN phép đo cũ ("khác đúng 1 khoá"): giờ recipe không được đổi 1 byte nào cả. Một
    cài đặt tiện tay quay lại `model_copy`/bơm `query` vào `node.params` sẽ làm `nhan != goc` và bài
    này đỏ."""
    runner, stub = _patched(monkeypatch)

    await runner.run_case(agent_id="a1", query="câu hỏi A", tenant_id=TENANT_ID, section_roles=["public"])

    nhan, question = stub.calls[0]
    assert question == "câu hỏi A"

    goc = runner.certified_recipe(agent_id="a1", tenant_id=TENANT_ID, section_roles=["public"])
    assert nhan == goc, "recipe run_agent_loop nhận phải giống HỆT recipe gốc — không còn model_copy nào bơm query"


async def test_ba_muoi_case_chi_sinh_MOT_recipe_goc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ở **quy mô 30 case**: mọi biến thể per-case khác nhau **đúng ở `query`**, không ở gì khác.

    **Bài này KHÔNG phân biệt được bản vá với hành vi cũ** — đo thật, không suy đoán: gieo `M-G7`
    (`run_case` quay lại dựng recipe mỗi case) thì nó **vẫn xanh**, vì trong một run
    `agent_id`/`tenant_id`/`scope` vốn đã không đổi. Bản nháp đầu của file gọi nó là *"bài trả lời
    🅐"* — sai, và giữ nguyên nhãn đó sẽ là một bài **tự khai mạnh hơn thực tế**.

    Vai thật của nó: **lưới quy mô** cho `test_query_di_qua_kwarg_question_recipe_khong_doi_1_byte`.
    Bài kia đo trên **một** case; bài này đo trên 30, nên nó bắt được thứ chỉ lộ ra khi lặp — ví dụ
    một cài đặt tương lai bơm thêm cái gì đó **theo chỉ số case** (`top_k` tăng dần, `trace_id`,
    timestamp) vào RECIPE, vốn xanh trên một case và hỏng trên 30.

    app#44: "30 recipe" của bản mô tả cũ giờ là **30 lần gọi CÙNG một recipe** (recipe không còn bị
    `model_copy` theo case) — assert đổi từ "gỡ `query` thì 30 biến thể trùng nhau" (đo trên object
    ĐÃ bị sửa) sang "cả 30 lần recipe truyền đi giống hệt object gốc" (đo trên object CHƯA từng bị
    sửa, mạnh hơn). Assert `len(questions) == 30` giữ nguyên vai chống-degenerate: không có nó, một
    cài đặt nuốt hết `question` (mọi lần gọi giống hệt nhau) vẫn xanh."""
    runner, stub = _patched(monkeypatch)

    for i in range(30):
        await runner.run_case(agent_id="a1", query=f"câu hỏi {i}", tenant_id=TENANT_ID, section_roles=["public"])

    assert len(stub.calls) == 30

    questions = {question for _, question in stub.calls}
    assert len(questions) == 30, f"30 case phải mang 30 question khác nhau, đo được {len(questions)}"

    goc = runner.certified_recipe(agent_id="a1", tenant_id=TENANT_ID, section_roles=["public"])
    recipes_khac_nhau = {recipe.model_dump_json() for recipe, _ in stub.calls}
    assert recipes_khac_nhau == {goc.model_dump_json()}, (
        f"cả 30 lần gọi phải truyền CÙNG recipe gốc (không model_copy theo case), đo được "
        f"{len(recipes_khac_nhau)} biến thể khác nhau"
    )


async def test_recipe_caller_truyen_vao_duoc_dung_NGUYEN_TRANG(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caller truyền `recipe=` ⇒ adapter dùng **đúng recipe đó**, không dựng lại.

    Đây là vế đóng finding của SWE (`kit#127`): *"recipe được CHẤM và recipe được PUBLISH là hai đối
    tượng khác nhau về cấu trúc"*. Hai cái chỉ bằng nhau khi caller đưa recipe thật vào — nên khe
    `recipe=` này **là** bản vá, không phải tiện ích.

    Dùng một recipe có `tenant_id`/`scope` **khác hẳn** mặc định của adapter: nếu adapter lỡ dựng lại
    thay vì dùng cái được đưa, hai giá trị đó sẽ lộ ra ngay.

    workbench#41 — `create_recipe_d4` đã bị xoá, và `create_recipe` hardcode `kb_binding` cố định
    (`ankor/public`, không nhận `scope`/`kb_id` làm tham số) nên không dựng được recipe có
    `kb_binding.scope` tuỳ ý qua nó nữa. Dựng `Recipe` thủ công tại đây — bài này CỐ Ý cần một giá
    trị `scope` khác hẳn mặc định của adapter để phát hiện adapter lỡ dựng lại thay vì dùng nguyên
    recipe được tiêm."""
    ngoai = Recipe(
        agent_id="agent-canvas",
        tenant_id=OTHER_TENANT,
        agent_config=AgentConfig(
            system_prompt="Tra cứu quy trình và bảo mật Callisto.", model="gemini-2.5-flash", tool_whitelist=[]
        ),
        dag=Dag(
            nodes=[
                Node(id="n1", type=NodeType.KB_RETRIEVE, params={"top_k": 3}),
                Node(id="n2", type=NodeType.LLM_STEP, params={"temperature": 0.0}),
                Node(id="n4", type=NodeType.END, params={}),
            ],
            edges=[Edge(from_="n1", to="n2"), Edge(from_="n2", to="n4")],
        ),
        kb_binding=KbBinding(kb_id="kb-callisto-v1", scope="borea/finance"),
        golden_set_ref="callisto-golden-30-v1",
        scorecard_threshold=ScorecardThreshold(success=0.9, citation_accuracy=0.95),
    )
    runner, stub = _patched(monkeypatch, recipe=ngoai)

    base = runner.certified_recipe(agent_id="bị-bỏ-qua", tenant_id=TENANT_ID, section_roles=["public"])
    assert base is ngoai

    await runner.run_case(agent_id="bị-bỏ-qua", query="q?", tenant_id=TENANT_ID, section_roles=["public"])
    nhan, question = stub.calls[0]

    assert nhan is ngoai, "app#44: recipe tiêm phải đi tới run_agent_loop NGUYÊN VẸN, không model_copy"
    assert nhan.agent_id == "agent-canvas"
    assert nhan.tenant_id == OTHER_TENANT
    assert nhan.kb_binding.scope == "borea/finance"
    assert question == "q?"
