"""`build_llm()` — model mặc định + hàng rào thiếu package (tổng duyệt demo 20/08).

Ba bài, ba hỏng khác nhau:

- **model mặc định**: `o4-mini` (giá trị cũ) trượt gate trên chính bộ golden production dùng —
  đo 3/3 lượt `success 0.7556` · `citation 0.6818–0.8636` FAIL, so với `gpt-4o-mini`
  `0.9889`/`1.0000` PASS (`evalhub#31`). Một lần trôi ngược về `o4-mini` làm buổi demo FAIL mà
  không có gì đỏ, nên giá trị đó phải bị ghim.
- **`openai` là dependency THƯỜNG**: khi nó còn ở `[optional-dependencies]`, `uv sync --frozen`
  (đúng lệnh README) không cài nó và `/evaluate` chết `ModuleNotFoundError` **thành 500 trần**
  ở tận `interpreter.run()`. Bài này đọc `pyproject.toml` nên nó đỏ ngay nếu ai đẩy `openai`
  trở lại thành extra.
- **thiếu package ⇒ 503 có lời khuyên**, không phải 500: đây là ca đã xảy ra thật, và thông điệp
  là thứ duy nhất giúp người dựng máy demo biết phải làm gì.
"""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path

import pytest
from fastapi import HTTPException
from studio_app.providers import factory
from studio_app.providers.openai import OpenAIProvider
from studio_app.settings import Settings

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _settings(**over: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "postgresql://u:p@localhost:5433/d",
        "database_url_admin": "postgresql://u:p@localhost:5433/d",
        "jwt_secret": "x" * 32,
        "llm_provider": "openai",
        "use_fake_providers": False,
        "openai_api_key": "sk-test-not-real",
    }
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


def test_openai_la_dependency_thuong_khong_phai_extra() -> None:
    """`uv sync --frozen` phải cài `openai` — nó là đường production duy nhất chạy được."""
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    deps = " ".join(data["project"]["dependencies"])
    extras = data["project"].get("optional-dependencies", {})
    assert "openai" in deps, "openai phải nằm trong [project.dependencies]"
    assert "openai" not in extras, (
        "openai KHÔNG được là extra: `uv sync --frozen` bỏ qua extra ⇒ /evaluate 500 "
        "ModuleNotFoundError ở llm-step (đã xảy ra thật ở tổng duyệt demo 20/08)"
    )


def test_model_mac_dinh_la_gpt_4o_mini(monkeypatch: pytest.MonkeyPatch) -> None:
    """Không khai `STUDIO_OPENAI_MODEL` ⇒ `gpt-4o-mini`, model duy nhất đo được là PASS."""
    monkeypatch.setattr(factory, "get_settings", lambda: _settings())
    llm = factory.build_llm()
    assert isinstance(llm, OpenAIProvider)
    assert llm._model == "gpt-4o-mini"


def test_thieu_package_openai_thi_503_khong_phai_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """Thiếu package ⇒ `HTTPException(503)` kèm cách sửa, KHÔNG để `ModuleNotFoundError` rơi tới
    `interpreter.run()` (ở đó nó thành 500 trần, không nói được gì)."""
    monkeypatch.setattr(factory, "get_settings", lambda: _settings())
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None if name == "openai" else object())
    with pytest.raises(HTTPException) as err:
        factory.build_llm()
    assert err.value.status_code == 503
    assert "openai" in str(err.value.detail)
