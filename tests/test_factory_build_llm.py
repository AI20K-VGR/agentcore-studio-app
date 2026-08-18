"""`providers/factory.py::build_llm()` — chọn provider theo `settings.llm_provider`
(discriminator, app#19). Monkeypatch `factory.get_settings` — không phụ thuộc
STUDIO_DATABASE_URL/env thật, cùng pattern test_jwt_auth.py."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from studio_app.providers import factory
from studio_app.providers.fakes import ExtractiveFakeLLM
from studio_app.providers.gemini import GeminiProvider
from studio_app.providers.openai import OpenAIProvider
from studio_app.settings import LlmProvider, Settings


def _settings(
    *,
    use_fake_providers: bool,
    llm_provider: LlmProvider,
    openai_api_key: str | None = None,
    gemini_api_key: str | None = None,
) -> Settings:
    return Settings(
        database_url="postgresql://unused/unused",
        database_url_admin="postgresql://unused/unused",
        jwt_secret="test-secret-at-least-32-bytes-long",
        use_fake_providers=use_fake_providers,
        llm_provider=llm_provider,
        openai_api_key=openai_api_key,
        gemini_api_key=gemini_api_key,
    )


def test_fake_providers_wins_regardless_of_llm_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """KHÓA: nhánh fake luôn thắng trước, không đổi hành vi cũ — bất kể `llm_provider` là gì."""
    monkeypatch.setattr(
        factory,
        "get_settings",
        lambda: _settings(use_fake_providers=True, llm_provider=LlmProvider.OPENAI),
    )
    assert isinstance(factory.build_llm(), ExtractiveFakeLLM)


def test_openai_selected_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        factory,
        "get_settings",
        lambda: _settings(
            use_fake_providers=False,
            llm_provider=LlmProvider.OPENAI,
            openai_api_key="sk-test",
        ),
    )
    assert isinstance(factory.build_llm(), OpenAIProvider)


def test_openai_missing_key_raises_500(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        factory,
        "get_settings",
        lambda: _settings(
            use_fake_providers=False,
            llm_provider=LlmProvider.OPENAI,
            openai_api_key=None,
        ),
    )
    with pytest.raises(HTTPException) as exc_info:
        factory.build_llm()
    assert exc_info.value.status_code == 500


def test_gemini_selected_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Song sinh test_openai_selected_with_key — xác nhận rollback path vẫn chạy y hệt trước khi
    có discriminator."""
    monkeypatch.setattr(
        factory,
        "get_settings",
        lambda: _settings(
            use_fake_providers=False,
            llm_provider=LlmProvider.GEMINI,
            gemini_api_key="key-test",
        ),
    )
    assert isinstance(factory.build_llm(), GeminiProvider)
