"""Provider selector (Decision #9, F3) — `get_llm()` chooses Fake vs Gemini by
`STUDIO_USE_FAKE_PROVIDERS`. The real `EmbeddingService` seam lives at
`providers/factory.py::build_embedding()` (app#30) — this module no longer carries an embedding
selector (see that module's docstring for the fake/gateway split; `get_llm()`'s LLM-only cousin,
`providers/factory.py::build_llm()`, is the pattern it mirrors).
"""

from __future__ import annotations

from studio_contracts.protocols import LLM

from studio_app.providers.fakes import FakeLLM
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
