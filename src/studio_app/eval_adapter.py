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
"""

from __future__ import annotations

from uuid import UUID

from studio_contracts import LLM, EmbeddingService, KbSearch, Recipe, TraceWriter
from studio_engine import interpreter
from studio_evalhub.agent_runner import AgentAnswer, CaseRun
from studio_workbench import create_recipe_d4
from studio_workbench.recipe_ops import with_query, without_query
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
    """Adapter thật cho seam `AgentRunner`: bọc `studio_engine.interpreter.run`.

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

        **Không mang `query` — CHỈ ĐÚNG cho nhánh không tiêm `recipe=` (dựng `create_recipe_d4` rồi
        gỡ query bên dưới).** Đó là điều làm câu *"scorecard này chứng nhận recipe nào"* có đáp án
        đơn nhất cho nhánh đó: `query` là **dữ liệu đề bài của golden-set**, không phải cấu hình
        agent — một agent đã publish không có query cố định. Nhánh tiêm (dòng dưới) KHÔNG áp lại
        bất biến này: nếu canvas admin vẽ có `params["query"]` gõ sẵn trong 1 node `kb-retrieve`,
        giá trị đó đi thẳng vào hash — vô hại cho tính nhất quán (cùng object được băm lẫn publish),
        nhưng là 1 trục canonical-form còn bỏ ngỏ nếu sau này cần so sánh giữa 2 lần chấm.

        Caller truyền `recipe=` ở constructor (đường `routes/publish.py` ĐANG dùng: recipe từ canvas,
        từ review `app#26` ⛔) ⇒ trả **đúng recipe đó**, không dựng lại. Đây là chỗ đóng finding của
        SWE trên `kit#127`: *"recipe được CHẤM và recipe được PUBLISH là hai đối tượng khác nhau về
        cấu trúc"* — hai cái chỉ bằng nhau khi caller đưa recipe thật vào thay vì để adapter tự dựng.

        Không truyền ⇒ dựng `create_recipe_d4` như trước, nhưng **một lần cho cả run** và đã gỡ `query`.

        `tenant_id`/`section_roles` vẫn vào recipe vì contract còn hai trường đó, **nhưng chúng là
        nhãn**: `interpreter.run()` không tin chúng (`interpreter.py:180-188`), `session_context` mới
        là hàng rào. Chúng có mặt để recipe tự mô tả đúng, không để quyết định phạm vi chạy."""
        if self._recipe is not None:
            return self._recipe
        scope = f"t/{','.join(section_roles)}" if section_roles else "t/"
        built = create_recipe_d4(agent_id=agent_id or self._agent_id, tenant_id=tenant_id, scope=scope)
        return without_query(built)

    async def run_case(
        self,
        *,
        agent_id: str,
        query: str,
        tenant_id: UUID,
        section_roles: list[str],
    ) -> CaseRun:
        """Lấy **recipe gốc của run** (`certified_recipe`), bơm `query` của case vào bằng `model_copy`,
        chạy qua interpreter thật, map `RunResult` → `CaseRun`.

        **Không dựng recipe mới mỗi case nữa** (`DEC-D20-07`) — đó là thứ làm golden-30 sinh 30 recipe
        và khiến `recipe_hash` vô nghĩa. Biến thể per-case chỉ khác đúng một khoá `params["query"]`,
        và nó là **dữ liệu chạy**, không phải một recipe khác.

        `section_roles` đi qua `session_context.roles` — phần danh tính do server resolve. Từ
        `workbench#23` nó **không còn** xuống `node.params`: `interpreter.run()` luôn ghi đè hai khoá
        `tenant_id`/`section_roles` từ session (`interpreter.py:320-327`), nên để chúng trong params là
        dữ liệu chết. Nó vẫn vào `kb_binding.scope` của recipe gốc như một **nhãn tự mô tả**.

        Thứ tự `section_roles` giữ **nguyên trạng**, không sắp lại: `scope` dựng bằng
        `','.join(section_roles)` nên đã phụ thuộc thứ tự đó từ trước D8, và `list[str]` là kiểu khai ở
        cả `GoldenCase.section_roles` lẫn Protocol `AgentRunner.run_case`. Sắp riêng một phía sẽ làm
        `scope` và `roles` mô tả hai thứ tự khác nhau cho cùng một case.

        `tenant_id` KHÔNG còn đi qua `recipe.tenant_id` để tới kb-retrieve (xem docstring module):
        `resolve_session` dựng danh tính server-side, và `interpreter.run()` lấy tenant từ đó. Recipe
        vẫn nhận `tenant_id` vì contract còn trường đó, nhưng nếu hai bên lệch thì **session thắng** —
        đó chính là INV-1.

        Đi qua `resolve_session` thật của SWE chứ không tự dựng `ResolvedContext`: nó là cổng
        fail-closed theo contract của chủ hàm (`tenant_wall.py`), nên adapter được kiểm đầu vào miễn phí
        thay vì tự tin rằng `tenant_id` luôn hợp lệ. `user` là `"eval-harness"` — đây là harness chạy,
        không phải một người dùng thật; nói dối chỗ này sẽ làm trace khó truy nguồn.

        GRAPH-LINT-CONTRACT (kit#129 item 3, thẩm định VinSOC finding C / DEC-A): hàm này KHÔNG tự
        gọi `graph_lint()`. `certified_recipe()` ở trên có hai nhánh: (a) trả `self._recipe` —
        recipe được tiêm ở constructor — NGUYÊN VẸN; caller sản xuất DUY NHẤT trong production
        (`routes/publish.py::_evaluate`) giờ ĐÃ truyền `recipe=` (`kit#127` đóng ở review `app#26`
        ⛔ — trước đó nhánh này chưa từng chạy thật, KHÔNG truyền, khiến recipe được CHẤM khác hẳn
        recipe được PUBLISH), và an toàn vì `_evaluate` gọi `graph_lint(recipe)` NGAY TRƯỚC khi dựng
        `EngineAgentRunner(recipe=recipe, ...)`, đúng tiền điều kiện dòng dưới đòi hỏi; hoặc (b) tự
        dựng `create_recipe_d4(...)` — một recipe cố định, KHÔNG bắt nguồn từ `nodes`/`edges` người
        dùng gửi lên (đường còn lại, dùng khi caller không tiêm `recipe=`, ví dụ eval-harness chạy
        rời khỏi route thật), nên không có DAG chưa-kiểm nào của người dùng chạm tới
        `interpreter.run()` qua đường này. Đổi thứ tự 2 lệnh gọi đó ở `_evaluate` (graph_lint sau
        khi dựng runner, thay vì trước) làm mất tiền điều kiện này —
        `tests/test_graph_lint_before_interpreter_run.py` đang allowlist hàm này theo tên; đọc lại
        docstring bài test đó trước khi đổi hợp đồng này."""
        base = self.certified_recipe(agent_id=agent_id, tenant_id=tenant_id, section_roles=section_roles)
        recipe = with_query(base, query)
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
        result = await interpreter.run(
            recipe,
            kb_search=self._kb_search,
            llm=self._llm,
            embedding=self._embedding,
            trace_writer=self._trace_writer,
            session_context=session_context,
            tool_dispatch=(
                build_tool_dispatch(recipe.agent_config.tool_whitelist) if self._recipe is not None else None
            ),
        )
        llm_out = _llm_answer(result.final_state)
        raw_citations = llm_out.get("citations")
        answer = AgentAnswer(
            answer=str(llm_out.get("answer", "")),
            citations=[str(c) for c in raw_citations] if isinstance(raw_citations, list) else [],
            refused=bool(llm_out.get("refused", False)),
        )
        return CaseRun(answer=answer, events=result.events)
