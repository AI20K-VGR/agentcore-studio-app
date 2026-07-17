"""`GeminiProvider` — LLM ONLY (F3, Decision #9). Shared infra for the `llm-step` node executor
(AIE-1 owns the executor; this ships the provider it calls). Live opt-in via
`STUDIO_USE_FAKE_PROVIDERS=false` + `STUDIO_GEMINI_API_KEY`.

Deliberately does NOT implement `EmbeddingService` — the concrete 2-impl (stub-local + gateway)
is AIE-1's graded deliverable (R-SPEC A1#5/A4). Shipping a Gemini `embed()` here would silently
do that work for them; `test_gemini_provides_llm_not_embedding` (test_providers.py) pins this.

Precedent: document-intake wraps its Gemini call for tracing at `obs/tracing.py:205-326`
(TracingExtractor) — the same wrap point applies once `llm-step` calls through this provider,
via `studio_app.obs.tracing.traced_call`.

`google-genai` is an OPTIONAL dependency (apps/studio pyproject `[project.optional-dependencies]
gemini`) — imported lazily inside `complete()` so this module (and the default
`STUDIO_USE_FAKE_PROVIDERS=true` CI path, which never instantiates this class) never requires
the package to be installed.
"""

from __future__ import annotations


class GeminiProvider:
    """`LLM` Protocol impl only — no `embed()` method exists on this class (F3)."""

    def __init__(self, *, api_key: str, model: str = "gemini-2.0-flash") -> None:
        self._api_key = api_key
        self._model = model

    async def complete(self, prompt: str, **kwargs: object) -> str:
        del kwargs  # extensibility hook (temperature/etc.) — forwarding to the SDK is a later concern
        from google import genai  # type: ignore[import-not-found]

        client = genai.Client(api_key=self._api_key)
        response = await client.aio.models.generate_content(model=self._model, contents=prompt)
        text = response.text
        if text is None:  # pragma: no cover — defensive; SDK normally returns str
            raise RuntimeError("Gemini response had no text")
        return str(text)
