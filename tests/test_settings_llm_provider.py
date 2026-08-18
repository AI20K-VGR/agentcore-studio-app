"""`Settings.llm_provider` — discriminator đóng cho việc chọn LLM provider thật (app#19). Khác
`test_providers.py` (test hành vi provider), file này chỉ test `Settings` tự nó: field required
không default (cùng lý lẽ `jwt_secret`, `settings.py:29-31`) + closed vocabulary phải raise trên
giá trị lạ thay vì im lặng rơi về Gemini (cùng lý lẽ `_VOCABULARY` của `agreement.py:64`,
`DEC-D18-01`).

Mọi test dùng `_env_file=None` (tắt đọc `.env` qua kwarg đặc biệt của pydantic-settings) CỘNG
`monkeypatch.delenv("STUDIO_LLM_PROVIDER", raising=False)` (cô lập khỏi biến OS-level nếu máy chạy
đã set sẵn) — thiếu 1 trong 2 bước cô lập này, `test_llm_provider_missing_raises` flaky theo môi
trường: máy dev có `.env` thật (hoặc biến OS đã set từ phiên làm việc dở) sẽ pass giả vì
`Settings()` đọc được giá trị đâu đó ngoài lệnh gọi test.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from studio_app.settings import LlmProvider, Settings

_DATABASE_URL = "postgresql://unused/unused"
_JWT_SECRET = "test-secret-llm-provider-at-least-32-bytes"


def test_llm_provider_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """KHÓA: thiếu `STUDIO_LLM_PROVIDER` phải raise `ValidationError` rõ ràng lúc khởi động —
    không được đoán/rơi về nhánh mặc định nào (cùng precedent `jwt_secret`, không
    `Field(default=...)`)."""
    monkeypatch.delenv("STUDIO_LLM_PROVIDER", raising=False)

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            database_url=_DATABASE_URL,
            database_url_admin=_DATABASE_URL,
            jwt_secret=_JWT_SECRET,
        )


def test_llm_provider_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """KHÓA "giá trị lạ phải ồn": typo/giá trị ngoài vocabulary đóng phải raise, không được âm
    thầm coerce hay rơi về Gemini."""
    monkeypatch.delenv("STUDIO_LLM_PROVIDER", raising=False)

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            database_url=_DATABASE_URL,
            database_url_admin=_DATABASE_URL,
            jwt_secret=_JWT_SECRET,
            llm_provider="openia",
        )


def test_llm_provider_accepts_closed_vocabulary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cả 2 dạng hợp lệ đều construct được: enum member trực tiếp VÀ string thô (đường
    pydantic-settings đọc từ env thật sự đi qua — StrEnum phải coerce đúng)."""
    monkeypatch.delenv("STUDIO_LLM_PROVIDER", raising=False)

    from_enum = Settings(
        _env_file=None,
        database_url=_DATABASE_URL,
        database_url_admin=_DATABASE_URL,
        jwt_secret=_JWT_SECRET,
        llm_provider=LlmProvider.OPENAI,
    )
    assert from_enum.llm_provider == LlmProvider.OPENAI

    from_string = Settings(
        _env_file=None,
        database_url=_DATABASE_URL,
        database_url_admin=_DATABASE_URL,
        jwt_secret=_JWT_SECRET,
        llm_provider="openai",
    )
    assert from_string.llm_provider == LlmProvider.OPENAI
