"""Factory dùng CHUNG cho mọi route cần chạy `interpreter.run()` thật (`routes/runs.py`,
`routes/publish.py`, `routes/chat.py`) — tách ra khỏi `routes/runs.py` để 3 route không phải
"mượn" hàm nội bộ (`_`-prefix) của nhau."""

from __future__ import annotations

from typing import assert_never
from uuid import UUID

from fastapi import HTTPException
from studio_contracts import KbSearch, NodeType, Recipe
from studio_contracts.protocols import LLM

from studio_kb.embeddings import derive_vector
from studio_workbench.tenant_wall import ResolvedContext

from studio_app.providers.fakes import ExtractiveFakeLLM
from studio_app.settings import LlmProvider, get_settings


class CallistoEmbedding:
    """`EmbeddingService` khớp không gian vector của dữ liệu ĐÃ INGEST vào `kb.chunks`
    (`scripts/ingest_callisto.py`, DE) — SSOT `studio_kb.embeddings.derive_vector`. Trùng có chủ
    đích với `_CallistoEmbedding` của `scripts/e2e_smoke_eval.py` (DRY note gốc, plan D13): cả hai
    chỉ là lớp vỏ 2 dòng quanh CÙNG một hàm, không phải công thức riêng — `FakeEmbedding`
    (`providers/fakes.py`) KHÔNG dùng được ở đây vì nó không khớp không gian vector đã ingest."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [derive_vector(text) for text in texts]


def build_llm() -> LLM:
    """`STUDIO_USE_FAKE_PROVIDERS=true` (mặc định) -> `ExtractiveFakeLLM` (đọc prompt thật, không
    thấy golden set — cùng fixture `e2e_smoke_eval.py` dùng). `false` -> chọn provider thật theo
    discriminator `settings.llm_provider` (app#19): `openai` -> `OpenAIProvider` (đòi
    `STUDIO_OPENAI_API_KEY`), `gemini` -> `GeminiProvider` (đòi `STUDIO_GEMINI_API_KEY`, đường
    rollback)."""
    settings = get_settings()
    if settings.use_fake_providers:
        return ExtractiveFakeLLM()

    match settings.llm_provider:
        case LlmProvider.OPENAI:
            from studio_app.providers.openai import OpenAIProvider

            if not settings.openai_api_key:
                raise HTTPException(
                    status_code=500,
                    detail="STUDIO_LLM_PROVIDER=openai nhưng thiếu STUDIO_OPENAI_API_KEY",
                )
            return OpenAIProvider(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
                model=settings.openai_model or "o4-mini",
            )
        case LlmProvider.GEMINI:
            from studio_app.providers.gemini import GeminiProvider

            if not settings.gemini_api_key:
                raise HTTPException(
                    status_code=500,
                    detail="STUDIO_LLM_PROVIDER=gemini nhưng thiếu STUDIO_GEMINI_API_KEY",
                )
            return GeminiProvider(api_key=settings.gemini_api_key)
        case _ as unreachable:
            assert_never(unreachable)


class KbSearchToolDispatch:
    """Real `tool-call` dispatcher (composition root) — thay cho
    `studio_engine.demo_stubs.WhitelistToolDispatch` (Day-3 demo-only stub, luôn trả
    `{"status": "stub-dispatched"}`). Tool `"kb_search"` gọi THẬT `KbSearch.search()` — CÙNG
    instance đã dùng cho node `kb-retrieve` trong lượt chạy này, không phải một service khác —
    trên `tenant_id`/`section_roles` lấy từ session đã resolve server-side (không tin recipe tự
    khai, cùng nguyên tắc INV-1 dùng khắp `interpreter.py`). Tool ngoài whitelist raise
    `ValueError` (giữ nguyên phòng thủ 2 lớp hiện có, cùng hành vi `WhitelistToolDispatch`); tool
    trong whitelist nhưng chưa có handler thật cũng raise `ValueError` rõ ràng — không giả vờ
    chạy được."""

    def __init__(
        self,
        *,
        kb_search: KbSearch,
        whitelist: list[str],
        tenant_id: UUID,
        section_roles: list[str],
        query: str,
        top_k: int = 3,
    ) -> None:
        self._kb_search = kb_search
        self._whitelist = whitelist
        self._tenant_id = tenant_id
        self._section_roles = section_roles
        self._query = query
        self._top_k = top_k

    async def dispatch(self, tool: str) -> object:
        if tool not in self._whitelist:
            raise ValueError(f"tool not in whitelist: {tool}")
        if tool == "kb_search":
            results = await self._kb_search.search(
                query=self._query,
                tenant_id=self._tenant_id,
                section_roles=self._section_roles,
                top_k=self._top_k,
            )
            return {
                "tool": tool,
                "status": "dispatched",
                "results": [r.model_dump(mode="json") for r in results],
            }
        raise ValueError(f"no real handler wired for tool: {tool!r}")


def build_tool_dispatch(recipe: Recipe, kb_search: KbSearch, session: ResolvedContext) -> KbSearchToolDispatch:
    """Dựng dispatcher `tool-call` thật cho 1 lượt `interpreter.run()`. `query` lấy từ node
    `kb-retrieve` **đầu tiên** trong `recipe.dag.nodes` (rỗng nếu recipe không có node đó — DAG
    `end`-only/`tool-call`-only vẫn hợp lệ, cùng quy ước `eval_adapter.py::_map_kb_params`).
    `tenant_id`/`section_roles` LUÔN lấy từ `session` (server-resolved), không bao giờ từ
    `recipe.tenant_id`/`recipe.kb_binding.scope` client tự khai."""
    query = next(
        (str(n.params.get("query", "")) for n in recipe.dag.nodes if n.type is NodeType.KB_RETRIEVE),
        "",
    )
    return KbSearchToolDispatch(
        kb_search=kb_search,
        whitelist=recipe.agent_config.tool_whitelist,
        tenant_id=session.tenant_id,
        section_roles=list(session.roles),
        query=query,
    )
