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
tenant_id là UUID (D-13): interpreter tự inject `recipe.tenant_id` xuống kb-retrieve, nên adapter chỉ
cần truyền UUID vào recipe. Recipe lấy từ workbench `create_recipe_d4` (form→recipe); bản production
sẽ load recipe theo `agent_id` thay vì dựng mỗi lần.
"""

from __future__ import annotations

from uuid import UUID

from studio_contracts import LLM, EmbeddingService, KbSearch, TraceWriter
from studio_engine import interpreter
from studio_evalhub.agent_runner import AgentAnswer, CaseRun
from studio_workbench.builder_d4 import create_recipe_d4


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
        map `RunResult` → `CaseRun`. `section_roles` mã vào `scope` để workbench đưa vào node.params;
        `tenant_id` UUID vào `recipe.tenant_id` (interpreter tự inject xuống kb-retrieve)."""
        scope = f"t/{','.join(section_roles)}" if section_roles else "t/"
        recipe = create_recipe_d4(
            agent_id=agent_id or self._agent_id,
            tenant_id=tenant_id,
            scope=scope,
            query=query,
        )
        result = await interpreter.run(
            recipe,
            kb_search=self._kb_search,
            llm=self._llm,
            embedding=self._embedding,
            trace_writer=self._trace_writer,
        )
        llm_out = _llm_answer(result.final_state)
        raw_citations = llm_out.get("citations")
        answer = AgentAnswer(
            answer=str(llm_out.get("answer", "")),
            citations=[str(c) for c in raw_citations] if isinstance(raw_citations, list) else [],
            refused=bool(llm_out.get("refused", False)),
        )
        return CaseRun(answer=answer, events=result.events)
