"""Factory dùng CHUNG cho mọi route cần chạy `interpreter.run()` thật (`routes/runs.py`,
`routes/publish.py`, `routes/chat.py`) — tách ra khỏi `routes/runs.py` để 3 route không phải
"mượn" hàm nội bộ (`_`-prefix) của nhau."""

from __future__ import annotations

import importlib.util
from typing import assert_never

from fastapi import HTTPException
from studio_contracts.protocols import LLM, EmbeddingService
from studio_kb.embeddings import derive_vector
from studio_kb.schema import EMBEDDING_DIM

from studio_app.providers.embeddings import GatewayEmbedding
from studio_app.providers.fakes import ExtractiveFakeLLM
from studio_app.settings import LlmProvider, get_settings


class CallistoStubEmbedding:
    """`EmbeddingService` STUB-LOCAL (app#30, QĐ-7) — nửa "stub local fixtures (CI, 100%)" của cặp
    2-impl mà `protocols.py:18-20` đòi (nửa kia là `GatewayEmbedding`, production thật). KHÔNG BAO
    GIỜ là đường production — đường production là `GatewayEmbedding` (`build_embedding()` dưới).

    Vỏ 2 dòng quanh `studio_kb.embeddings.derive_vector`, trùng có chủ đích với
    `_CallistoEmbedding` của `scripts/e2e_smoke_eval.py` (DRY note gốc, plan D13). Truyền
    `dim=EMBEDDING_DIM` TƯỜNG MINH (Đính chính B, app#30): `derive_vector` mặc định bám hằng số
    cột — không khai tường minh thì coupling với `studio_kb.schema.EMBEDDING_DIM` là NGẦM, và một
    lần lật hằng số đó (`kb#43`, 8 → 2048) mà chỗ gọi không đổi theo sẽ sinh vector SAI SỐ CHIỀU
    của KHÔNG-GIAN-CŨ dưới nhãn số-chiều-mới — đúng con bug app#30 đang đóng. `FakeEmbedding`
    (`providers/fakes.py`) KHÔNG dùng được ở đây: `dim=8` cứng, chưa từng L2-normalize, và tự
    docstring của nó cấm đếm vào cặp 2-impl này."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [derive_vector(text, dim=EMBEDDING_DIM) for text in texts]


def build_llm() -> LLM:
    """`STUDIO_USE_FAKE_PROVIDERS=true` (mặc định) -> `ExtractiveFakeLLM` (đọc prompt thật, không
    thấy golden set — cùng fixture `e2e_smoke_eval.py` dùng). `false` -> chọn provider thật theo
    discriminator `settings.llm_provider` (app#19): `openai` -> `OpenAIProvider` (đòi
    `STUDIO_OPENAI_API_KEY`), `gemini` -> `GeminiProvider` (đòi `STUDIO_GEMINI_API_KEY`, đường
    rollback)."""
    settings = get_settings()
    if settings.use_fake_providers:
        return ExtractiveFakeLLM()

    match settings.llm_provider:
        case LlmProvider.OPENAI:
            from studio_app.providers.openai import OpenAIProvider

            if not settings.openai_api_key:
                raise HTTPException(
                    status_code=500,
                    detail="STUDIO_LLM_PROVIDER=openai nhưng thiếu STUDIO_OPENAI_API_KEY",
                )
            # `importlib.util.find_spec` chứ không `import openai`: dựng provider phải rẻ, và chỗ
            # này chỉ cần biết package CÓ hay KHÔNG. `openai.py::complete()` import lazy nên thiếu
            # package sẽ nổ `ModuleNotFoundError` tận trong `interpreter.run()` ⇒ 500 trần, không
            # nói được phải làm gì (đo được ở tổng duyệt demo 20/08: `/evaluate` 500 sau 4.4s).
            # Kiểm ở composition root biến nó thành 503 có lời khuyên, cùng khuôn ca thiếu key.
            if importlib.util.find_spec("openai") is None:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "thiếu package `openai` trong môi trường — chạy `uv sync --frozen` lại sau khi "
                        "bump con trỏ `apps/studio` (openai giờ là dependency thường, không còn extra)"
                    ),
                )
            return OpenAIProvider(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
                # `gpt-4o-mini` chứ không `o4-mini` (đổi 20/08). Đo trên golden 2.0 + embedding thật,
                # 3 lượt mỗi model, cùng harness/judge/retrieval — chỉ đổi model trả lời:
                #   o4-mini      success 0.6667–0.8333 (tb 0.7556) · citation 0.6818–0.8636 · FAIL 3/3
                #   gpt-4o-mini  success 0.9667–1.0000 (tb 0.9889) · citation 1.0000          · PASS 3/3
                # `o4-mini` là dòng reasoning: nó hay BỎ marker `[chunk_id]` dù chunk đúng nằm trong
                # ngữ cảnh (recall@3 = 22/22), nên trục citation tụt mà retrieval không hề sai — xem
                # `evalhub#31`. Override bằng `STUDIO_OPENAI_MODEL` khi cần đo model khác.
                model=settings.openai_model or "gpt-4o-mini",
            )
        case LlmProvider.GEMINI:
            from studio_app.providers.gemini import GeminiProvider

            if not settings.gemini_api_key:
                raise HTTPException(
                    status_code=500,
                    detail="STUDIO_LLM_PROVIDER=gemini nhưng thiếu STUDIO_GEMINI_API_KEY",
                )
            return GeminiProvider(api_key=settings.gemini_api_key)
        case _ as unreachable:
            assert_never(unreachable)


def build_embedding() -> EmbeddingService:
    """Gương đúng hình `build_llm()` ở trên (app#30). `STUDIO_USE_FAKE_PROVIDERS=true` (mặc định)
    -> `CallistoStubEmbedding` (đúng bề rộng `EMBEDDING_DIM`, KHÔNG phải `FakeEmbedding` — xem
    docstring `CallistoStubEmbedding`). `false` -> `GatewayEmbedding` thật (đòi
    `STUDIO_OPENROUTER_API_KEY`). `GatewayEmbedding` import top-level (không lazy như
    `OpenAIProvider`/`GeminiProvider` ở trên): `httpx` đã là base dep (`pyproject.toml:19`), không
    phải extra, nên không có ImportError nào để tránh trên đường CI fake.

    Ánh xạ lỗi CỐ Ý khác `build_llm()` (503 ở đây, không phải 500): `providers/embeddings.py`
    raise `EmbeddingGatewayError` (domain error, không FastAPI) khi gọi API thất bại lúc runtime;
    ở ĐÂY chỉ bắt ca THIẾU KEY lúc dựng provider — giữ coupling `HTTPException` đúng chỗ
    `build_llm()` đã đặt (`factory.py` đã import `HTTPException` sẵn), không mở chỗ mới."""
    settings = get_settings()
    if settings.use_fake_providers:
        return CallistoStubEmbedding()

    if not settings.openrouter_api_key:
        raise HTTPException(
            status_code=503,
            detail="STUDIO_USE_FAKE_PROVIDERS=false nhưng thiếu STUDIO_OPENROUTER_API_KEY",
        )
    return GatewayEmbedding(api_key=settings.openrouter_api_key)
