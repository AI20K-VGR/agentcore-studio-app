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

Recipe lấy từ workbench `create_recipe_d4` (form→recipe); bản production sẽ load recipe theo `agent_id`
thay vì dựng mỗi lần.
"""

from __future__ import annotations

from uuid import UUID

from studio_contracts import LLM, EmbeddingService, KbSearch, TraceWriter
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
    ) -> None:
        self._kb_search = kb_search
        self._llm = llm
        self._embedding = embedding
        self._trace_writer = trace_writer
        self._agent_id = agent_id

    async def run_case(
        self,
        *,
        agent_id: str,
        query: str,
        tenant_id: UUID,
        section_roles: list[str],
    ) -> CaseRun:
        """Dựng recipe cho case (query + tenant_id UUID + section_roles), chạy qua interpreter thật,
        map `RunResult` → `CaseRun`.

        `section_roles` đi hai đường, có chủ đích khác nhau:

        - vào `scope` để workbench đưa xuống `node.params` — trục hàng rào thứ hai (T6 chéo-vai);
        - vào `session_context.roles` — phần danh tính do server resolve.

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
        scope = f"t/{','.join(section_roles)}" if section_roles else "t/"
        recipe = create_recipe_d4(
            agent_id=agent_id or self._agent_id,
            tenant_id=tenant_id,
            scope=scope,
            query=query,
        )
        session_context = resolve_session(
            {"tenant_id": tenant_id, "user": "eval-harness", "roles": section_roles}
        )
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
