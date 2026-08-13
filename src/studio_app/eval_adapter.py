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

from collections.abc import Callable
from uuid import UUID

from studio_contracts import LLM, EmbeddingService, KbSearch, NodeType, Recipe, TraceWriter
from studio_engine import interpreter
from studio_evalhub.agent_runner import AgentAnswer, CaseRun
from studio_workbench import create_recipe_d4
from studio_workbench.tenant_wall import resolve_session


def _llm_answer(final_state: dict[str, object]) -> dict[str, object]:
    """Nhặt output node llm-step từ final_state (không hardcode node id — tìm dict có key 'answer',
    khớp shape chốt §2.7 `scorecard-v0.md`). Fail-closed: không thấy → raise (không đoán rỗng)."""
    for output in final_state.values():
        if isinstance(output, dict) and "answer" in output:
            return output
    raise LookupError("EngineAgentRunner: final_state không có output node llm-step (thiếu 'answer')")


_QUERY_KEY = "query"


def _map_kb_params(recipe: Recipe, fn: Callable[[dict[str, object]], dict[str, object]]) -> Recipe:
    """Áp `fn` lên `params` của node `kb-retrieve` **đầu tiên**, trả bản sao. Không có node đó ⇒ trả
    nguyên trạng.

    Chọn theo **loại** chứ không theo vị trí `nodes[0]`: DAG 6-node của AIE-1 (D20) không bảo đảm node
    đầu là `kb-retrieve`, còn `create_recipe_d4` thì có — khoá theo loại đúng ở cả hai, khoá theo vị
    trí chỉ đúng ở một.

    Không raise khi vắng `kb-retrieve`: một DAG không truy xuất KB là hợp lệ (`end`-only,
    `tool-call`-only). Ném lỗi ở đây sẽ biến một recipe hợp lệ thành lỗi của bộ chấm."""
    for i, node in enumerate(recipe.dag.nodes):
        if node.type is not NodeType.KB_RETRIEVE:
            continue
        patched = node.model_copy(update={"params": fn(dict(node.params))})
        nodes = [*recipe.dag.nodes[:i], patched, *recipe.dag.nodes[i + 1 :]]
        return recipe.model_copy(update={"dag": recipe.dag.model_copy(update={"nodes": nodes})})
    return recipe


def _without_query(recipe: Recipe) -> Recipe:
    """Gỡ `query` khỏi recipe ⇒ **recipe gốc của run**, thứ `recipe_hash` băm.

    Gỡ hẳn khoá chứ không đặt `""`: hai cái cho **hai chuỗi byte khác nhau** khi serialize, mà chuỗi
    byte đó chính là đầu vào của hash. Đây là một trong năm trục canonical-form đang chờ SWE chốt
    (`kit#127` 🅑) — chọn "vắng mặt" ở đây là chọn dạng **không mang dữ liệu đề bài**, nhất quán với
    lý do tách `query` ra ngay từ đầu."""
    return _map_kb_params(recipe, lambda params: {k: v for k, v in params.items() if k != _QUERY_KEY})


def _with_query(recipe: Recipe, query: str) -> Recipe:
    """Trả bản sao của `recipe` với `query` bơm vào node `kb-retrieve` **đầu tiên**.

    Không sửa `recipe` gốc — `Recipe` là `frozen=True`, và cái gốc chính là thứ `recipe_hash` sẽ băm,
    nên nó phải giữ nguyên qua cả 30 case. Một biến thể per-case là **dữ liệu chạy**, không phải một
    recipe khác.

    Chọn node `kb-retrieve` **đầu tiên** chứ không phải `nodes[0]`: DAG 6-node của AIE-1 (D20) không
    bảo đảm node đầu là `kb-retrieve`, và `create_recipe_d4` thì có — khoá theo **loại** thì đúng ở cả
    hai, khoá theo vị trí thì chỉ đúng ở một.

    Không có node `kb-retrieve` nào ⇒ trả recipe **nguyên trạng** (xem `_map_kb_params`)."""
    return _map_kb_params(recipe, lambda params: {**params, _QUERY_KEY: query})


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

        **Không mang `query`.** Đó là điểm khác duy nhất so với bản trước D20, và là điều làm câu
        *"scorecard này chứng nhận recipe nào"* có đáp án đơn nhất: `query` là **dữ liệu đề bài của
        golden-set**, không phải cấu hình agent — một agent đã publish không có query cố định.

        Caller truyền `recipe=` ở constructor (đường `routes/publish.py` sẽ dùng: recipe từ canvas)
        ⇒ trả **đúng recipe đó**, không dựng lại. Đây là chỗ đóng finding của SWE trên `kit#127`:
        *"recipe được CHẤM và recipe được PUBLISH là hai đối tượng khác nhau về cấu trúc"* — hai cái
        chỉ bằng nhau khi caller đưa recipe thật vào thay vì để adapter tự dựng.

        Không truyền ⇒ dựng `create_recipe_d4` như trước, nhưng **một lần cho cả run** và đã gỡ `query`.

        `tenant_id`/`section_roles` vẫn vào recipe vì contract còn hai trường đó, **nhưng chúng là
        nhãn**: `interpreter.run()` không tin chúng (`interpreter.py:180-188`), `session_context` mới
        là hàng rào. Chúng có mặt để recipe tự mô tả đúng, không để quyết định phạm vi chạy."""
        if self._recipe is not None:
            return self._recipe
        scope = f"t/{','.join(section_roles)}" if section_roles else "t/"
        built = create_recipe_d4(agent_id=agent_id or self._agent_id, tenant_id=tenant_id, scope=scope)
        return _without_query(built)

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
        không phải một người dùng thật; nói dối chỗ này sẽ làm trace khó truy nguồn."""
        base = self.certified_recipe(agent_id=agent_id, tenant_id=tenant_id, section_roles=section_roles)
        recipe = _with_query(base, query)
        session_context = resolve_session({"tenant_id": tenant_id, "user": "eval-harness", "roles": section_roles})
        result = await interpreter.run(
            recipe,
            kb_search=self._kb_search,
            llm=self._llm,
            embedding=self._embedding,
            trace_writer=self._trace_writer,
            session_context=session_context,
        )
        llm_out = _llm_answer(result.final_state)
        raw_citations = llm_out.get("citations")
        answer = AgentAnswer(
            answer=str(llm_out.get("answer", "")),
            citations=[str(c) for c in raw_citations] if isinstance(raw_citations, list) else [],
            refused=bool(llm_out.get("refused", False)),
        )
        return CaseRun(answer=answer, events=result.events)
