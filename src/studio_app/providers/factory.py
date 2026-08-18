"""Factory dùng CHUNG cho mọi route cần chạy `interpreter.run()` thật (`routes/runs.py`,
`routes/publish.py`, `routes/chat.py`) — tách ra khỏi `routes/runs.py` để 3 route không phải
"mượn" hàm nội bộ (`_`-prefix) của nhau."""

from __future__ import annotations

from typing import assert_never

from fastapi import HTTPException
from studio_contracts.protocols import LLM
from studio_kb.embeddings import derive_vector

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
            return OpenAIProvider(api_key=settings.openai_api_key)
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
