"""Pydantic-settings, env prefix `STUDIO_` (Decision #3 2-DSN split + Decision #9 provider
flags). `.env.example` at the repo root documents every field this class reads."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STUDIO_", env_file=".env", extra="ignore")

    # 2-DSN split (F1/F2) — never point database_url at the studio_owner role, that silently
    # disables the RLS fence (core/_db.py docstring explains why).
    database_url: str
    database_url_admin: str

    enable_tracing: bool = False
    use_fake_providers: bool = True

    gemini_api_key: str | None = None

    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Process-wide cached Settings — reads env once, not once per call site."""
    # pydantic-settings resolves `database_url`/`database_url_admin` (no Python default) from
    # STUDIO_* env vars / .env at call time — the pydantic.mypy plugin models BaseSettings so no
    # call-arg ignore is needed.
    return Settings()
