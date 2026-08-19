"""`GatewayEmbedding` — impl THẬT duy nhất của `EmbeddingService` trong repo (app#30, QĐ-1/QĐ-4).

`gemini-embedding-001` @2048 qua OpenRouter (khớp hiệu chỉnh `kb#43`,
`packages/kb/tests/embedding-tests/providers.py:32-36`) — KHÔNG chọn lại transport, sao y. Đọc-ghi
qua `core.embedding_cache` (Phase 2) để đường ĐỌC 1-text/call (`postgres.py:213`, sau ranh giới
package DE) không phải trả tiền HTTP cho câu đã hỏi trước đó.

Class RIÊNG, KHÔNG method `embed()` gắn vào một LLM provider (QĐ-4 — `providers/openai.py:4-6`
"Deliberately does NOT implement `EmbeddingService`", `test_providers.py:40,49`).

`httpx`, không `urllib` (QĐ-5): `embed()` là `async def` (`protocols.py:23`), `urllib` chặn event
loop; `httpx>=0.28` đã là base dep (`pyproject.toml:19`) — không dep mới, không relock.

Fail-closed (QĐ-6): `EmbeddingGatewayError` khai TẠI ĐÂY, module này KHÔNG import FastAPI — ánh xạ
sang HTTP 503 là việc của composition root (`providers/factory.py::build_embedding()`, Phase 4).
Không nhánh nào rơi về `derive_vector`/bag-of-words khi lỗi — rơi êm = báo số dim-8 dưới nhãn
provider thật, đúng thứ `MissingVectorError` của kb sinh ra để chặn (issue §4.1).
"""

from __future__ import annotations

import hashlib
import math
from typing import TYPE_CHECKING

import httpx

from studio_app.core._db import get_pool

if TYPE_CHECKING:
    from studio_app.core._db import Pool

_OPENROUTER_URL = "https://openrouter.ai/api/v1/embeddings"
MODEL = "google/gemini-embedding-001"
DIM = 2048
_BATCH = 90
"""Số text mỗi lần gọi — khớp `_BATCH = 90` (`packages/kb/tests/embedding-tests/providers.py:44`),
giữ vừa phải để một lần đứt mạng chỉ mất một lô, không mất cả batch lớn."""


class EmbeddingGatewayError(RuntimeError):
    """Fail-closed (QĐ-6). Raise khi: thiếu key, HTTP != 200, payload thiếu/lệch `data`, vector sai
    bề rộng, hoặc lỗi mạng/timeout. KHÔNG BAO GIỜ fallback về provider khác."""


def _cache_key(model: str, dim: int, text: str) -> str:
    """`sha256(model|dim|text)` — khớp nguyên văn `kb#40`
    (`packages/kb/tests/embedding-tests/_vector_cache.py:56-63`). Gộp model+dim vào khoá: cùng câu
    qua `gemini-embedding-001` ở 2048 vs 1536 là HAI vector khác nhau."""
    return hashlib.sha256(f"{model}|{dim}|{text}".encode()).hexdigest()


def _l2_normalize(vector: list[float]) -> list[float]:
    """Chuẩn hoá L2 — bắt buộc với `gemini-embedding-001` ở số chiều != 3072 (docs Google: API chỉ
    chuẩn hoá sẵn ở 3072 gốc, cắt Matryoshka xuống 2048 trả vector CHƯA chuẩn hoá). Vector 0 trả
    nguyên, không chia cho 0."""
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        return vector
    return [x / norm for x in vector]


def _vector_literal(values: list[float]) -> str:
    """Mã hoá vector thành literal pgvector `'[1.0,2.0,...]'` — đi qua text + `::vector` thay vì
    dùng adapter gói `pgvector` (cùng lý do `postgres.py:97-102`: không thêm dependency)."""
    return "[" + ",".join(repr(float(v)) for v in values) + "]"


def _parse_vector_literal(text: str) -> list[float]:
    """Ngược lại `_vector_literal` — đọc `embedding::text` từ Postgres về `list[float]`."""
    return [float(x) for x in text.strip("[]").split(",")]


class GatewayEmbedding:
    """`EmbeddingService` thật — xem docstring module. `client`/`pool` injectable qua constructor
    cho test (transport double / cache double), mặc định `None` dùng `httpx.AsyncClient()` mới mỗi
    lần gọi + `core._db.get_pool()` (singleton runtime pool, role `studio_app`)."""

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str = MODEL,
        dim: int = DIM,
        client: httpx.AsyncClient | None = None,
        pool: Pool | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._dim = dim
        self._client = client
        self._pool = pool

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Luồng dữ liệu (xem phase-3-gateway-provider.md §Luồng dữ liệu):
        dedupe giữ thứ tự → cache lookup 1 round-trip → miss thì gọi API theo lô ≤90, L2-normalize,
        sắp theo `index` response → ghi cache `ON CONFLICT DO NOTHING` → trả về ĐÚNG thứ tự `texts`
        gốc (kể cả phần tử trùng — map ngược qua `cache_key`, không `zip` cắt cụt, giữ E-1)."""
        if not self._api_key:
            raise EmbeddingGatewayError("thiếu OpenRouter API key — không gọi được embedding thật")
        if not texts:
            return []

        unique_texts = list(dict.fromkeys(texts))
        key_by_text = {t: _cache_key(self._model, self._dim, t) for t in unique_texts}

        pool = self._pool if self._pool is not None else await get_pool()
        vector_by_key: dict[str, list[float]] = {}
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT cache_key, embedding::text FROM core.embedding_cache WHERE cache_key = ANY(%s::text[])",
                (list(key_by_text.values()),),
            )
            for cache_key_value, embedding_text in await cur.fetchall():
                vector_by_key[cache_key_value] = _parse_vector_literal(embedding_text)

        missing_texts = [t for t in unique_texts if key_by_text[t] not in vector_by_key]
        if missing_texts:
            await self._fetch_and_cache(missing_texts, pool, key_by_text, vector_by_key)

        return [vector_by_key[key_by_text[t]] for t in texts]

    async def _fetch_and_cache(
        self,
        texts: list[str],
        pool: Pool,
        key_by_text: dict[str, str],
        vector_by_key: dict[str, list[float]],
    ) -> None:
        """Gọi API theo lô ≤90, ghi cache NGAY SAU MỖI LÔ THÀNH CÔNG — không đợi mọi lô xong rồi
        mới ghi (review PR#32, Important #1). `_BATCH`'s docstring hứa "một lần đứt mạng chỉ mất
        một lô" — trước bản vá này lời hứa đó SAI: `embed()` gọi hết mọi lô rồi mới ghi cache MỘT
        LẦN, nên lô thứ N/M lỗi làm mất TOÀN BỘ (N-1) lô đã trả tiền gọi API trước đó, không chỉ
        lô cuối. Ghi ngay sau mỗi lô giữ đúng lời hứa: một lần đứt giữa chừng chỉ mất lô đang chạy
        dở, các lô trước đã persist xong."""
        for start in range(0, len(texts), _BATCH):
            batch = texts[start : start + _BATCH]
            vectors = await self._call_api(batch)
            async with pool.connection() as conn:
                for text, vector in zip(batch, vectors, strict=True):
                    cache_key_value = key_by_text[text]
                    vector_by_key[cache_key_value] = vector
                    await conn.execute(
                        "INSERT INTO core.embedding_cache (cache_key, model, dim, embedding) "
                        "VALUES (%s, %s, %s, %s::vector) ON CONFLICT (cache_key) DO NOTHING",
                        (cache_key_value, self._model, self._dim, _vector_literal(vector)),
                    )

    async def _call_api(self, batch: list[str]) -> list[list[float]]:
        client = self._client
        owns_client = client is None
        if client is None:
            client = httpx.AsyncClient()
        try:
            try:
                response = await client.post(
                    _OPENROUTER_URL,
                    json={"model": self._model, "input": batch, "dimensions": self._dim},
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=120,
                )
            except httpx.HTTPError as exc:
                raise EmbeddingGatewayError(f"OpenRouter embeddings request lỗi mạng: {exc}") from exc

            if response.status_code != 200:
                # KHÔNG forward response.text ra client: đó là nội dung upstream chưa kiểm soát
                # (review PR#32, Important) — có thể chứa chi tiết vendor không nên lộ qua API
                # public của chính app này. Chi tiết đầy đủ vẫn nằm trong log server (uvicorn access
                # log ghi status; thân exception này chỉ mang status code, không mang body).
                raise EmbeddingGatewayError(f"OpenRouter embeddings trả HTTP {response.status_code}")

            # Toàn bộ khối dưới đây đọc payload upstream KHÔNG kiểm soát được hình dạng — mọi lỗi
            # parse (JSON hỏng, thiếu field, giá trị không phải số, `index` không phải hoán vị hợp
            # lệ) PHẢI thành `EmbeddingGatewayError`, không phải lọt ra ngoài dưới dạng
            # `JSONDecodeError`/`KeyError`/`TypeError`/`ValueError` thô (review PR#32, Critical #1):
            # `ValueError` trần bị `routes/publish.py::_evaluate`'s `except ValueError` bắt NHẦM
            # thành lỗi input 400 ("golden_set_ref sai"), che mất một sự cố gateway thật đằng sau
            # một mã lỗi hoàn toàn sai hướng chẩn đoán.
            try:
                payload = response.json()
                data = payload.get("data")
                if data is None or len(data) != len(batch):
                    got = 0 if data is None else len(data)
                    raise EmbeddingGatewayError(f"OpenRouter embeddings trả {got} vector cho {len(batch)} text")

                # Sắp lại theo `index` — KHÔNG tin thứ tự API trả (spec OpenAI-compatible cho phép
                # trả không theo thứ tự); lệch một nấc ở đây gán sai vector cho MỌI text sau đó,
                # hỏng câm. Review PR#32, Critical #2: kiểm ĐỘ DÀI thôi chưa đủ — `index` trùng
                # lặp, ngoài phạm vi, hoặc kiểu chuỗi (sort từ điển "0","1","10"...) làm `sorted()`
                # xếp sai mà không exception nào nổ. Ép `int()` + xác nhận tập kết quả ĐÚNG BẰNG
                # `{0, ..., len(batch)-1}` mới thật sự chặn được lớp lỗi "hoán vị giả".
                indices = [int(item["index"]) for item in data]
                if sorted(indices) != list(range(len(batch))):
                    raise EmbeddingGatewayError(
                        f"OpenRouter embeddings trả index không phải hoán vị hợp lệ 0..{len(batch) - 1}: {indices}"
                    )
                ordered = sorted(data, key=lambda item: int(item["index"]))

                vectors: list[list[float]] = []
                for item in ordered:
                    vector = _l2_normalize([float(x) for x in item["embedding"]])
                    if len(vector) != self._dim:
                        raise EmbeddingGatewayError(
                            f"OpenRouter embeddings trả vector {len(vector)} chiều, kỳ vọng {self._dim}"
                        )
                    vectors.append(vector)
            except EmbeddingGatewayError:
                raise
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                # `AttributeError` phủ ca `response.json()` trả về một giá trị JSON hợp lệ nhưng
                # KHÔNG phải object (`payload.get(...)` nổ trên list/str/số/null) — cùng lớp "hình
                # dạng lạ" với 3 exception còn lại, không phải nhánh riêng.
                raise EmbeddingGatewayError(f"OpenRouter embeddings trả payload không đọc được: {exc}") from exc
            return vectors
        finally:
            if owns_client:
                await client.aclose()
