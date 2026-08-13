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

    # JWT ký/verify identity (Kế hoạch 2, A2/A1) — KHÔNG có provider identity thật đứng sau (không
    # password/OAuth/IdP nào check `user`/`tenant`/`roles` trước khi ký): việc ký/verify chữ ký là
    # THẬT (không ai giả mạo được token nếu không có `jwt_secret`), nhưng việc "ai được phép xin
    # token cho tenant/roles nào" vẫn CHƯA được xác thực — đó là hệ thống identity provider riêng,
    # ngoài phạm vi hiện tại. `jwt_secret` không có default: thiếu biến môi trường phải raise rõ
    # ràng lúc khởi động (pydantic ValidationError), không được chạy với khoá rỗng/đoán được.
    jwt_secret: str
    jwt_expire_minutes: int = 480

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
