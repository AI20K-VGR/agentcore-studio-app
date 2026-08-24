"""EngineAgentRunner — adapter nối smoke-eval (AIE-2) vào interpreter thật (AIE-1). Issue #29.

Sống ở `studio_app` (composition root) — nơi *duy nhất* được import cả `studio_engine`,
`studio_kb`, `studio_evalhub` (`.importlinter`: 4 quadrant sibling cấm import lẫn nhau; chỉ
`studio_app` ở tầng trên gom được). Vì thế mapper `RunResult → CaseRun` phải nằm ở đây, không phải
trong evalhub.

Hiện thực Protocol `studio_evalhub.AgentRunner`: `run_case(query, tenant_id, section_roles) →
CaseRun`. Map:
- `RunResult.final_state[<llm-step node>]` → `AgentAnswer{answer, citations, refused}`.
- `RunResult.events` → `CaseRun.events` (nguồn chấm citations của evalhub, đọc node-agnostic).

Co-author: AIE-1 (nguồn — interpreter/final_state/events) + AIE-2 (đích — CaseRun/AgentAnswer).

tenant_id là UUID (D-13). **Từ `engine#12` (D8, INV-1 Tenant-Wall), `recipe.tenant_id` KHÔNG còn là
nguồn danh tính**: `interpreter.run()` đòi `session_context` bắt buộc keyword-only và lấy tenant từ đó
cho cả node `kb-retrieve` lẫn mọi `TraceEvent` (`rg "recipe.tenant_id" interpreter.py` → 0 hit).
Recipe vẫn mang `tenant_id` — contract chưa bỏ trường đó, và workbench vẫn đổ vào — nhưng nó là nhãn
đi kèm, không phải thứ quyết định phạm vi chạy. Adapter vì thế phải dựng danh tính qua
`studio_workbench.tenant_wall.resolve_session`, và đó là lý do `session_context` xuất hiện ở đây.

## Một run = MỘT recipe, `query` bơm per-case (`DEC-D20-07`, D20)

Trước D20 hàm này gọi `create_recipe_d4(query=…)` **mỗi case** ⇒ golden-30 sinh **30 recipe khác
nhau**, và câu *"`Scorecard` này chứng nhận recipe nào"* **không có câu trả lời đơn nhất** — bất kể ai
băm và băm bằng gì. Đó là thứ chặn `recipe_hash` (`DEC-03`), không phải bản thân phép băm.

Nay: **một recipe gốc ổn định cho cả run**, và `query` của từng case được bơm bằng `model_copy` tại
call site. Recipe gốc **không mang `query`** — xem `certified_recipe`.

**Vì sao bơm được mà không phải đổi contract:** `query` là dữ liệu per-case **duy nhất** còn sống
trong `node.params` sau `workbench#23` (PR đó bỏ `tenant_id`/`section_roles` vì
`interpreter.run()` luôn ghi đè chúng từ `session_context` — `interpreter.py:320-327`). Hai khoá kia
đi qua **session**; `query` không có session để lấy, nên nó đi qua `model_copy` — **cùng khuôn
`model_copy` mà interpreter đã dùng**, không đổi chữ ký `interpreter.run()`.

**Ai quyết:** AIE-1 (chủ `apps/studio` + bút interpreter) chốt đường này trên `kit#126`, chọn (b) thay
vì (a) *"`interpreter.run()` nhận input per-run"* — (a) để dành cho khi `query` cần thành first-class
run-input thật sự.

**Ranh giới:** `DEC-D20-01` tự áp *"AIE-2 chỉ thêm file test mới trong `apps/studio`"*. Lần sửa `src/`
này được chủ repo giao **tường minh bằng chữ** trên `kit#126` (*"bạn draft PR vào `apps/studio`… tôi
review + approve trong hôm nay"*) — nới ranh giới có dấu vết, không phải tự ý.

**Cái này KHÔNG tự sinh `recipe_hash`.** Nó chỉ làm cho một hash **có nghĩa**: giờ đã có đúng một
recipe để băm. Băm trên chuỗi byte nào (`by_alias`? `exclude_none`?) vẫn là câu hỏi mở của **SWE**
(bút `Recipe`) — `DEC-D20-02` giữ nguyên: evalhub **nhận** giá trị, tuyệt đối không tự dẫn xuất.

## app#44 — `run_case()` giờ chạy qua `agent_loop.run_agent_loop()`, không còn `interpreter.run()`

Kiến trúc agent đổi (`PROJECT-SCOPE-DEMO-DAY30.md`): bỏ DAG 6-node tĩnh, thay bằng 1 LLM node + N
tool tự chọn (`packages/engine` #33). `run_case()` KHÔNG còn `with_query(base, query)` +
`interpreter.run()` — `query` đi thẳng qua kwarg `question=` của `run_agent_loop()`, và
`run_agent_loop()` KHÔNG đọc `recipe.dag` (chỉ đọc `agent_config`/`kb_binding` của recipe). Mọi đoạn
docstring dưới đây còn nhắc `interpreter.run()`/`model_copy` `params["query"]` mô tả HÀNH VI CŨ vẫn
đúng cho `packages/engine::interpreter.run()` (fallback DAG-walk vẫn tồn tại, K2 engine#33) — chỉ
KHÔNG còn là đường mà `run_case()` này đi qua nữa.
"""

from __future__ import annotations

from uuid import UUID

from studio_contracts import LLM, Edge, EmbeddingService, KbSearch, Node, NodeType, Recipe, TraceWriter
from studio_engine import agent_loop
from studio_evalhub.agent_runner import AgentAnswer, CaseRun
from studio_workbench import create_recipe
from studio_workbench.recipe_ops import without_query
from studio_workbench.tenant_wall import resolve_session

from studio_app.providers.factory import build_tool_dispatch


def _llm_answer(final_state: dict[str, object]) -> dict[str, object]:
    """Nhặt output node llm-step từ final_state (không hardcode node id — tìm dict có key 'answer',
    khớp shape chốt §2.7 `scorecard-v0.md`). Fail-closed: không thấy → raise (không đoán rỗng)."""
    for output in final_state.values():
        if isinstance(output, dict) and "answer" in output:
            return output
    raise LookupError("EngineAgentRunner: final_state không có output node llm-step (thiếu 'answer')")


class EngineAgentRunner:
    """Adapter thật cho seam `AgentRunner`: bọc `studio_engine.agent_loop.run_agent_loop` (app#44 —
    trước đó `studio_engine.interpreter.run`, DAG-walk cũ; vẫn tồn tại trong `packages/engine` làm
    fallback, K2 engine#33, chỉ không còn là đường adapter này gọi).

    Constructor-DI 4 collaborator (dựng/tiêm ở composition root): `kb_search` (DE `StaticKbSearch`),
    `llm`/`embedding` (providers), `trace_writer` (obs sink). Không giữ trạng thái giữa các case."""

    def __init__(
        self,
        *,
        kb_search: KbSearch,
        llm: LLM,
        embedding: EmbeddingService,
        trace_writer: TraceWriter,
        agent_id: str = "agent-callisto-d4",
        recipe: Recipe | None = None,
    ) -> None:
        self._kb_search = kb_search
        self._llm = llm
        self._embedding = embedding
        self._trace_writer = trace_writer
        self._agent_id = agent_id
        self._recipe = recipe

    def certified_recipe(self, *, agent_id: str, tenant_id: UUID, section_roles: list[str]) -> Recipe:
        """Recipe **gốc** của run — thứ mà `recipe_hash` phải băm, và thứ `publish()` phải chứng nhận.

        **Không mang `query` — CHỈ ĐÚNG cho nhánh không tiêm `recipe=` (dựng qua `create_recipe`
        với DAG cố định rồi gỡ query bên dưới).** Đó là điều làm câu *"scorecard này chứng nhận
        recipe nào"* có đáp án
        đơn nhất cho nhánh đó: `query` là **dữ liệu đề bài của golden-set**, không phải cấu hình
        agent — một agent đã publish không có query cố định. Nhánh tiêm (dòng dưới) KHÔNG áp lại
        bất biến này: nếu canvas admin vẽ có `params["query"]` gõ sẵn trong 1 node `kb-retrieve`,
        giá trị đó đi thẳng vào hash — vô hại cho tính nhất quán (cùng object được băm lẫn publish),
        nhưng là 1 trục canonical-form còn bỏ ngỏ nếu sau này cần so sánh giữa 2 lần chấm.

        Caller truyền `recipe=` ở constructor (đường `routes/publish.py` ĐANG dùng: recipe từ canvas,
        từ review `app#26` ⛔) ⇒ trả **đúng recipe đó**, không dựng lại. Đây là chỗ đóng finding của
        SWE trên `kit#127`: *"recipe được CHẤM và recipe được PUBLISH là hai đối tượng khác nhau về
        cấu trúc"* — hai cái chỉ bằng nhau khi caller đưa recipe thật vào thay vì để adapter tự dựng.

        Không truyền ⇒ dựng qua `create_recipe` (workbench#41 — `create_recipe_d4`/`_parse_kb_scope`
        đã bị xoá), **một lần cho cả run**, và đã gỡ `query`.

        `create_recipe` hardcode `kb_binding` cố định (`ankor/public` — xem module docstring của
        `studio_workbench.recipe`), không còn nhận `scope`/`kb_id` làm tham số: nhánh này **đổi
        hành vi thật** so với trước — `kb_binding.scope` không còn phản ánh `section_roles` của
        caller. Vô hại cho việc chạy thật (`run_agent_loop()` không đọc `kb_binding` từ recipe để
        quyết định phạm vi — `session_context` mới là hàng rào, xem docstring `run_case` dưới), chỉ
        đổi giá trị NHÃN tự mô tả trên recipe được băm/publish.

        `tenant_id`/`section_roles` vẫn vào recipe vì contract còn hai trường đó, **nhưng chúng là
        nhãn**: `interpreter.run()` không tin chúng (`interpreter.py:180-188`), `session_context` mới
        là hàng rào. Chúng có mặt để recipe tự mô tả đúng, không để quyết định phạm vi chạy."""
        if self._recipe is not None:
            return self._recipe
        # DAG cố định 3-node (KB_RETRIEVE -> LLM_STEP -> END), giữ nguyên hình dạng `create_recipe_d4`
        # từng dựng — `run_agent_loop()` không đọc `recipe.dag` (module docstring, mục app#44), nên
        # node/edge ở đây chỉ tồn tại để thoả `Recipe.dag` (field bắt buộc trên contract).
        nodes = [
            Node(id="n1", type=NodeType.KB_RETRIEVE, params={"top_k": 3}),
            Node(id="n2", type=NodeType.LLM_STEP, params={"temperature": 0.0}),
            Node(id="n4", type=NodeType.END, params={}),
        ]
        edges = [Edge(from_="n1", to="n2"), Edge(from_="n2", to="n4")]
        built = create_recipe(
            agent_id=agent_id or self._agent_id,
            tenant_id=tenant_id,
            instructions="Tra cứu quy trình và bảo mật Callisto.",
            tool_whitelist=[],
            nodes=nodes,
            edges=edges,
        )
        return without_query(built)

    async def run_case(
        self,
        *,
        agent_id: str,
        query: str,
        tenant_id: UUID,
        section_roles: list[str],
    ) -> CaseRun:
        """Lấy **recipe gốc của run** (`certified_recipe`), chạy qua `run_agent_loop()` thật với
        `question=query` (app#44 — KHÔNG còn bơm `query` vào `recipe.dag` bằng `with_query`/
        `model_copy`: vòng lặp mới không đọc `recipe.dag`), map `RunResult` → `CaseRun`.

        **Không dựng recipe mới mỗi case nữa** (`DEC-D20-07`) — đó là thứ làm golden-30 sinh 30 recipe
        và khiến `recipe_hash` vô nghĩa. `query` giờ là **input trực tiếp của lời gọi**
        (`question=`), không còn là 1 khoá bơm vào recipe — recipe gốc dùng NGUYÊN VẸN, cùng object
        cho mọi case trong 1 run (giữ đúng bất biến `DEC-D20-07`, chỉ đổi CƠ CHẾ truyền `query`).

        `section_roles` đi qua `session_context.roles` — phần danh tính do server resolve. Từ
        `workbench#23` nó **không còn** xuống `node.params`; vòng lặp mới không đọc `node.params` của
        `recipe.dag` chút nào (`run_agent_loop` chỉ đọc `agent_config`/`kb_binding`), nên câu hỏi
        "params có ghi đè được không" không còn áp dụng nữa — `tenant_id`/`section_roles` chỉ còn
        sống trong `session_context` (fence tại `agent_loop.fenced_kb_params`, cùng cơ chế
        `interpreter.run()` dùng cho DAG-walk, dùng chung 1 helper — `fence.py`). Nó vẫn vào
        `kb_binding.scope` của recipe gốc như một **nhãn tự mô tả**.

        Thứ tự `section_roles` giữ **nguyên trạng**, không sắp lại: `scope` dựng bằng
        `','.join(section_roles)` nên đã phụ thuộc thứ tự đó từ trước D8, và `list[str]` là kiểu khai ở
        cả `GoldenCase.section_roles` lẫn Protocol `AgentRunner.run_case`. Sắp riêng một phía sẽ làm
        `scope` và `roles` mô tả hai thứ tự khác nhau cho cùng một case.

        `tenant_id` KHÔNG còn đi qua `recipe.tenant_id` để tới `kb_search` (xem docstring module):
        `resolve_session` dựng danh tính server-side, và `run_agent_loop()` lấy tenant từ
        `session_context` (fence giống hệt `interpreter.run()`, cùng helper `fenced_kb_params`).
        Recipe vẫn nhận `tenant_id` vì contract còn trường đó, nhưng nếu hai bên lệch thì **session
        thắng** — đó chính là INV-1.

        Đi qua `resolve_session` thật của SWE chứ không tự dựng `ResolvedContext`: nó là cổng
        fail-closed theo contract của chủ hàm (`tenant_wall.py`), nên adapter được kiểm đầu vào miễn phí
        thay vì tự tin rằng `tenant_id` luôn hợp lệ. `user` là `"eval-harness"` — đây là harness chạy,
        không phải một người dùng thật; nói dối chỗ này sẽ làm trace khó truy nguồn.

        **`graph_lint()` (app#44)**: hàm này KHÔNG tự gọi, và giờ KHÔNG CẦN — `run_agent_loop()`
        không walk `recipe.dag`, nên hợp đồng "phải graph_lint trước khi tới DAG-walk" (kit#129 item
        3, `DEC-A`) không còn áp dụng cho đường này nữa; `tests/test_graph_lint_before_interpreter_run.py`
        đã bị RETIRE cùng app#44 vì chính lý do đó (đọc commit đó nếu cần lịch sử). `certified_recipe()`
        ở trên vẫn có 2 nhánh (a) recipe tiêm từ constructor (`routes/publish.py::_evaluate`) hoặc (b)
        tự dựng `create_recipe_d4(...)` — cả hai giờ đều an toàn KHÔNG CẦN graph_lint đi trước, vì
        không nhánh nào của `run_agent_loop()` từng đọc `dag.nodes`/`dag.edges`."""
        base = self.certified_recipe(agent_id=agent_id, tenant_id=tenant_id, section_roles=section_roles)
        session_context = resolve_session({"tenant_id": tenant_id, "user": "eval-harness", "roles": section_roles})
        # `tool_dispatch=` (engine#32 review finding): trước fix này thiếu tham số, nên MỌI recipe
        # eval-gate qua đây âm thầm fallback về `WhitelistToolDispatch` (stub `interpreter.py`).
        # Chỉ tiêm `RealToolDispatch` khi `self._recipe` được caller đưa vào (branch (a) —
        # `routes/publish.py::_evaluate`, recipe THẬT, đã qua `graph_lint`, và chính comment ở đó
        # xác nhận nó có thể mang 1 node `tool-call` thật, không phải giả định: "recipe được chấm
        # còn có 1 node tool-call admin chưa từng vẽ"). Đây LÀ đường sản xuất cao rủi ro nhất
        # (eval-gate/publish) mà `routes/runs.py`/`routes/chat.py` (`app#41`) đã nối đúng còn adapter
        # thì chưa — lỗ hổng issue #32 muốn đóng.
        #
        # CỐ Ý không tiêm ở branch (b) (`self._recipe is None` — tự dựng `create_recipe_d4`, dùng khi
        # eval-harness chạy rời khỏi route thật, ví dụ `test_eval_adapter.py`): fixture đó khai
        # `tool_whitelist=["kb_search"]` mặc định và đặt "kb_search" vào node `tool-call` — tổ hợp
        # `RealToolDispatch` không hỗ trợ (kb_search có kind `kb_retrieve` riêng, không đi qua
        # `ToolDispatch`; xem `providers/tool_dispatch.py`). Tiêm ở đây sẽ đổi `RealToolDispatch`'s
        # `unsupported tool: kb_search` thành lỗi cứng cho MỌI test hiện có, và đổi cả recipe được
        # hash (`certified_recipe()` docstring, D16 golden-batch determinism) — hai side-effect nằm
        # ngoài phạm vi finding này. Fixture `create_recipe_d4` là của `packages/workbench` (SWE) —
        # sửa mặc định đó là quyết định riêng, không phải phần của fix này.
        result = await agent_loop.run_agent_loop(
            base,
            kb_search=self._kb_search,
            llm=self._llm,
            embedding=self._embedding,
            trace_writer=self._trace_writer,
            session_context=session_context,
            question=query,
            tool_dispatch=(build_tool_dispatch(base.agent_config.tool_whitelist) if self._recipe is not None else None),
        )
        llm_out = _llm_answer(result.final_state)
        raw_citations = llm_out.get("citations")
        answer = AgentAnswer(
            answer=str(llm_out.get("answer", "")),
            citations=[str(c) for c in raw_citations] if isinstance(raw_citations, list) else [],
            refused=bool(llm_out.get("refused", False)),
        )
        return CaseRun(answer=answer, events=result.events)
