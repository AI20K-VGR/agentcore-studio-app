"""Phase 4 gate tests — provider determinism (F3 CI-fixture), Gemini LLM-only boundary (F3),
tracing fail-open (R-DI §14 tracing.py:1-5,95-115). Pure-Python, no live DB — the DB round-trip
test lives in test_trace_writer.py.
"""

from __future__ import annotations

import pytest
from studio_app.obs import tracing
from studio_app.providers.fakes import FakeEmbedding, FakeLLM
from studio_app.providers.gemini import GeminiProvider
from studio_contracts.protocols import LLM, EmbeddingService


async def test_fake_llm_deterministic() -> None:
    """KHÓA: FakeLLM.complete() is a pure hash-seeded function — same prompt twice -> same
    output (CI reproducibility; catches a regression that introduces wall-clock/random)."""
    llm = FakeLLM()
    first = await llm.complete("hello world")
    second = await llm.complete("hello world")
    assert first == second


async def test_fake_embedding_deterministic() -> None:
    """KHÓA: FakeEmbedding.embed() same text twice -> same vector (hash-seeded, no random)."""
    embedding = FakeEmbedding()
    first = await embedding.embed(["hello world"])
    second = await embedding.embed(["hello world"])
    assert first == second


def test_gemini_provides_llm_not_embedding() -> None:
    """KHÓA F3 (provider-boundary): GeminiProvider implements LLM only. It must NOT implement
    EmbeddingService — that concrete 2-impl (stub-local + gateway) is AIE-1's graded deliverable
    (R-SPEC A1#5/A4); shipping a Gemini embed() here would silently do that work for them."""
    provider = GeminiProvider(api_key="unused-in-this-test")
    assert isinstance(provider, LLM)
    assert not isinstance(provider, EmbeddingService)
    assert not hasattr(provider, "embed")


async def test_tracing_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """KHÓA: a Langfuse-side failure (span setup raising, e.g. Langfuse down) must never break
    the wrapped business call — traced_call() still returns the business result unchanged."""

    class _BrokenClient:
        def start_as_current_observation(self, **kwargs: object) -> object:
            raise RuntimeError("langfuse down")

    monkeypatch.setattr(tracing, "get_langfuse", lambda: _BrokenClient())

    async def business() -> str:
        return "business-result"

    result = await tracing.traced_call("test-span", business)
    assert result == "business-result"
