"""`providers/factory.py::build_embedding()` — song sinh `test_factory_build_llm.py` (app#30 Phase
4). Monkeypatch `factory.get_settings` — không phụ thuộc STUDIO_DATABASE_URL/env thật."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from studio_app.providers import factory
from studio_app.providers.embeddings import EmbeddingGatewayError, GatewayEmbedding
from studio_app.providers.factory import CallistoStubEmbedding
from studio_app.settings import LlmProvider, Settings, get_settings
from studio_contracts.protocols import LLM


def _settings(*, use_fake_providers: bool, openrouter_api_key: str | None = None) -> Settings:
    return Settings(
        database_url="postgresql://unused/unused",
        database_url_admin="postgresql://unused/unused",
        jwt_secret="test-secret-at-least-32-bytes-long",
        use_fake_providers=use_fake_providers,
        llm_provider=LlmProvider.GEMINI,
        openrouter_api_key=openrouter_api_key,
    )


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """`get_settings` là `@lru_cache` (settings.py) — clear trước/sau mỗi test để test sau không
    đọc phải settings của test trước."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_fake_path_returns_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(factory, "get_settings", lambda: _settings(use_fake_providers=True))
    assert isinstance(factory.build_embedding(), CallistoStubEmbedding)


async def test_stub_width_matches_column() -> None:
    """Ghim nhánh fake KHÔNG làm nổ pipeline.py:57-59 — vector đúng EMBEDDING_DIM (2048), không 8."""
    from studio_kb.schema import EMBEDDING_DIM

    vectors = await CallistoStubEmbedding().embed(["x"])
    assert len(vectors) == 1
    assert len(vectors[0]) == EMBEDDING_DIM


def test_real_path_returns_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        factory,
        "get_settings",
        lambda: _settings(use_fake_providers=False, openrouter_api_key="or-test-key"),
    )
    assert isinstance(factory.build_embedding(), GatewayEmbedding)


def test_missing_key_raises_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        factory,
        "get_settings",
        lambda: _settings(use_fake_providers=False, openrouter_api_key=None),
    )
    with pytest.raises(HTTPException) as exc_info:
        factory.build_embedding()
    assert exc_info.value.status_code == 503


def test_gateway_is_not_an_llm() -> None:
    """Gương ngược test_providers.py::test_gemini_provides_llm_not_embedding — QĐ-4, ranh giới F3:
    GatewayEmbedding KHÔNG BAO GIỜ được bolt thêm complete()."""
    provider = GatewayEmbedding(api_key="unused-in-this-test")
    assert not isinstance(provider, LLM)
    assert not hasattr(provider, "complete")


def test_get_embedding_is_gone() -> None:
    """QĐ-8 — không ai lặng lẽ khôi phục selector fake-only đã xoá."""
    import studio_app.providers as providers_pkg

    assert not hasattr(providers_pkg, "get_embedding")


async def test_gateway_error_maps_to_503() -> None:
    """Ghim mapping app.py::_embedding_gateway_error_to_503 — dựng app tối giản (không qua
    create_app()/lifespan, tránh cần DB thật), tiêm 1 route kích EmbeddingGatewayError. Cùng
    `httpx.AsyncClient` + `ASGITransport` mà `test_http_asgi.py` đã dùng (chưa có tiền lệ
    `starlette.testclient.TestClient` trong repo cho loại test này)."""
    from studio_app.app import _embedding_gateway_error_to_503

    app = FastAPI()
    app.add_exception_handler(EmbeddingGatewayError, _embedding_gateway_error_to_503)  # type: ignore[arg-type]

    @app.get("/boom")
    def _boom() -> None:
        raise EmbeddingGatewayError("thiếu OpenRouter API key")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/boom")
    assert response.status_code == 503
