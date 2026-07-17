"""CI-fixture provider doubles — deterministic, hash-seeded. NEITHER class is a deliverable.

`FakeLLM` — CI fixture — KHÔNG phải deliverable AIE-1. A throwaway `LLM` double so `llm-step`
wiring can be exercised in CI without a live Gemini key.

`FakeEmbedding` — CI fixture — KHÔNG phải deliverable AIE-1. A test-double for `EmbeddingService`
ONLY (F3) — the graded 2-impl `EmbeddingService` (stub-local + gateway, R-SPEC A1#5/A4) is AIE-1's
own deliverable; this class does NOT count toward that 2-impl requirement and must never be
mistaken for one.

Both are deterministic (same input -> same output, hash-seeded `[ASSUMED]`) so CI stays
reproducible — no wall-clock, no `random` module anywhere in this file.
"""

from __future__ import annotations

import hashlib


class FakeLLM:
    """CI fixture — KHÔNG phải deliverable AIE-1."""

    async def complete(self, prompt: str, **kwargs: object) -> str:
        del kwargs  # accepted for Protocol-shape parity; the fake ignores generation params
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        return f"fake-completion:{digest}"


class FakeEmbedding:
    """CI fixture — KHÔNG phải deliverable AIE-1. Test-double for EmbeddingService's shape
    only; NOT one of AIE-1's 2 graded impls (stub-local + gateway)."""

    dim: int = 8

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [digest[i % len(digest)] / 255.0 for i in range(self.dim)]
