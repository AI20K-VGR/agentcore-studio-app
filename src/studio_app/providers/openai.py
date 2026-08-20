"""`OpenAIProvider` — LLM ONLY (F3, Decision #9), khuôn giống `GeminiProvider`. Provider CHÍNH
(nhóm chốt `o4-mini`) — `GeminiProvider` là đường rollback, KHÔNG bị thay thế.

Deliberately does NOT implement `EmbeddingService` — same reasoning as `gemini.py`
(R-SPEC A1#5/A4, AIE-1's graded deliverable). `test_openai_provides_llm_not_embedding`
(test_providers.py) pins this.

`openai` là OPTIONAL dependency (apps/studio pyproject `[project.optional-dependencies]
openai`) — imported lazily inside `complete()`, same reason as `gemini.py`: default
`STUDIO_USE_FAKE_PROVIDERS=true` CI path never instantiates this class and never installs the
package (`uv sync --frozen` has no `--extra` in CI).

`o4-mini` is a reasoning-line model — `complete()` calls `chat.completions.create` with ONLY
`model` + `messages`; it deliberately does NOT forward `temperature`/`max_tokens` (o-series
rejects/renames these params — verify against current OpenAI docs before adding any parameter).
"""

from __future__ import annotations


class OpenAIProvider:
    """`LLM` Protocol impl only — no `embed()` method exists on this class (F3).

    `base_url` (optional): trỏ sang endpoint tương thích OpenAI khác (vd OpenRouter
    `https://openrouter.ai/api/v1`, model id kiểu `openai/gpt-4o-mini`) thay vì API OpenAI gốc —
    `None` (mặc định) giữ nguyên hành vi cũ, SDK tự dùng endpoint OpenAI chính thức."""

    def __init__(self, *, api_key: str, model: str = "o4-mini", base_url: str | None = None) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url

    async def complete(self, prompt: str, **kwargs: object) -> str:
        del kwargs  # extensibility hook — forwarding to the SDK is a later concern, same as gemini.py
        # Hai error-code (tiền lệ `engine#22`): `openai` giờ là dependency THƯỜNG nên môi trường
        # đủ sẽ không có `import-not-found`, nhưng giữ dạng hai code để dòng này hợp lệ ở cả
        # trạng thái chưa cài (mypy không thấy package) lẫn đã cài (ignore thành thừa).
        from openai import AsyncOpenAI  # type: ignore[import-not-found, unused-ignore]

        client = AsyncOpenAI(api_key=self._api_key, base_url=self._base_url)
        response = await client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.choices[0].message.content
        if text is None:  # pragma: no cover — defensive; SDK normally returns str
            raise RuntimeError("OpenAI response had no content")
        return str(text)
