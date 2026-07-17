"""Provider selector (Decision #9, F3) — `get_llm()`/`get_embedding()` choose Fake vs Gemini by
`STUDIO_USE_FAKE_PROVIDERS`. `get_embedding()` ALWAYS returns the CI-fixture `FakeEmbedding` —
this kit ships no production `EmbeddingService` (that 2-impl is AIE-1's graded deliverable,
R-SPEC A1#5/A4); this selector exists only so quadrant code has a seam to import against during
CI, never a "real" embedding path.
"""

from __future__ import annotations

from studio_contracts.protocols import LLM, EmbeddingService

from studio_app.providers.fakes import FakeEmbedding, FakeLLM
from studio_app.providers.gemini import GeminiProvider
from studio_app.settings import get_settings


def get_llm() -> LLM:
    """Fake (default, CI) or Gemini (`STUDIO_USE_FAKE_PROVIDERS=false` + `STUDIO_GEMINI_API_KEY`)."""
    settings = get_settings()
    if settings.use_fake_providers:
        return FakeLLM()
    if not settings.gemini_api_key:
        raise RuntimeError("STUDIO_USE_FAKE_PROVIDERS=false requires STUDIO_GEMINI_API_KEY")
    return GeminiProvider(api_key=settings.gemini_api_key)


def get_embedding() -> EmbeddingService:
    """CI-fixture ONLY (F3) — NOT a production EmbeddingService. AIE-1's graded 2-impl
    (stub-local + gateway) fills the real seam later; this selector never grows a live branch."""
    return FakeEmbedding()
