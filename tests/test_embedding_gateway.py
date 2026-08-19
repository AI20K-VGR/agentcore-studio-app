"""`GatewayEmbedding` — app#30 Phase 3 (+ review PR#32 fixes: malformed-payload/index-permutation
coverage, partial-batch-failure cache persistence).

Mọi test TRỪ `test_cache_hit_skips_http` dùng transport double (`httpx.MockTransport`) + cache
double (`_FakePool`, dưới) — KHÔNG gọi mạng, KHÔNG cần DSN (INV-4). `test_cache_hit_skips_http`
cần DB thật (`admin_pool`), skip khi không có DSN (root `conftest.py::_require_dsn`).
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from studio_app.core._db import Pool
from studio_app.providers.embeddings import DIM, MODEL, EmbeddingGatewayError, GatewayEmbedding, _cache_key


class _FakeCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeConn:
    def __init__(self, store: dict[str, tuple[str, str, int, str]]) -> None:
        self._store = store

    async def execute(self, query: str, params: Sequence[Any] | None = None) -> _FakeCursor:
        if query.startswith("SELECT"):
            keys = params[0] if params else []
            rows = [(k, self._store[k][3]) for k in keys if k in self._store]
            return _FakeCursor(rows)
        if query.startswith("INSERT"):
            assert params is not None
            cache_key_value, model, dim, embedding_literal = params
            self._store[cache_key_value] = (cache_key_value, model, dim, embedding_literal)
            return _FakeCursor([])
        raise AssertionError(f"unexpected query in _FakeConn: {query!r}")


class _FakeConnCtx:
    def __init__(self, store: dict[str, tuple[str, str, int, str]]) -> None:
        self._store = store

    async def __aenter__(self) -> _FakeConn:
        return _FakeConn(self._store)

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakePool:
    """Double tối giản của `Pool` (chỉ `.connection()` -> conn với `.execute()`/`fetchall()`) —
    KHÔNG chạm Postgres, dùng cho mọi test không cần DSN."""

    def __init__(self) -> None:
        self.store: dict[str, tuple[str, str, int, str]] = {}

    def connection(self) -> _FakeConnCtx:
        return _FakeConnCtx(self.store)


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _echo_handler(vector_len: int = DIM, *, unnormalized: bool = False) -> Any:
    """Handler trả một vector hợp lệ (đã chuẩn hoá trừ khi `unnormalized`) cho mỗi text trong batch,
    theo ĐÚNG thứ tự nhận được (index khớp vị trí trong `input`)."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        batch = payload["input"]
        base = [1.0] + [0.0] * (vector_len - 1) if unnormalized is False else [3.0] + [0.0] * (vector_len - 1)
        data = [{"index": i, "embedding": list(base)} for i in range(len(batch))]
        return httpx.Response(200, json={"data": data})

    return handler


async def test_vector_width_is_2048() -> None:
    pool = _FakePool()
    client = _client(_echo_handler())
    gw = GatewayEmbedding(api_key="k", client=client, pool=pool)  # type: ignore[arg-type]
    vectors = await gw.embed(["hello"])
    assert len(vectors) == 1
    assert len(vectors[0]) == DIM


async def test_vector_is_l2_normalized() -> None:
    """Transport trả vector CHƯA chuẩn hoá (norm=3) — GatewayEmbedding phải tự chuẩn hoá.
    DoD 6b, và bài DUY NHẤT giết được mutation "bỏ L2-normalize" (cosine bất biến tỉ lệ, test
    retrieval không giết được nó)."""
    pool = _FakePool()
    client = _client(_echo_handler(unnormalized=True))
    gw = GatewayEmbedding(api_key="k", client=client, pool=pool)  # type: ignore[arg-type]
    vectors = await gw.embed(["hello"])
    assert sum(x * x for x in vectors[0]) == pytest.approx(1.0, rel=1e-6)


async def test_reorders_by_response_index() -> None:
    """Transport trả `data` xáo trộn (index 2,0,1) — vector thứ i phải khớp texts[i], KHÔNG tin
    thứ tự API trả về."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        batch = payload["input"]
        assert batch == ["a", "b", "c"]
        vector_for = {"a": [1.0, 0.0], "b": [0.0, 1.0], "c": [1.0, 1.0]}
        shuffled_order = [2, 0, 1]  # trả theo thứ tự c,a,b nhưng index đúng vị trí gốc
        data = [{"index": shuffled_order[i], "embedding": vector_for[batch[shuffled_order[i]]]} for i in range(3)]
        return httpx.Response(200, json={"data": data})

    pool = _FakePool()
    client = _client(handler)
    gw = GatewayEmbedding(api_key="k", model=MODEL, dim=2, client=client, pool=pool)  # type: ignore[arg-type]
    vectors = await gw.embed(["a", "b", "c"])
    # sau chuẩn hoá, hướng vector vẫn khớp text gốc theo trục lớn nhất
    assert vectors[0][0] > vectors[0][1]  # "a" ~ [1,0]
    assert vectors[1][1] > vectors[1][0]  # "b" ~ [0,1]
    assert vectors[2][0] == pytest.approx(vectors[2][1])  # "c" ~ [1,1]


async def test_partial_batch_failure_keeps_earlier_batch_cached() -> None:
    """Review PR#32, Important #1: 100 text (2 lô 90/10) — lô 1 (90 text) THÀNH CÔNG, lô 2 (10
    text) THẤT BẠI. `embed()` phải raise (fail-closed, không trả kết quả một phần), nhưng 90 hàng
    cache của lô 1 đã trả tiền gọi API PHẢI còn nguyên trong bảng — không bị mất theo lô 2 lỗi."""
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        payload = json.loads(request.content)
        batch = payload["input"]
        if call_count == 2:
            return httpx.Response(500, json={"error": "boom lô 2"})
        data = [{"index": i, "embedding": [1.0] + [0.0] * (DIM - 1)} for i in range(len(batch))]
        return httpx.Response(200, json={"data": data})

    pool = _FakePool()
    client = _client(handler)
    gw = GatewayEmbedding(api_key="k", client=client, pool=pool)  # type: ignore[arg-type]
    texts = [f"text-{i}" for i in range(100)]
    with pytest.raises(EmbeddingGatewayError):
        await gw.embed(texts)

    assert call_count == 2
    assert len(pool.store) == 90  # lô 1 (90 text) đã ghi cache; lô 2 (10 text) không, vì lỗi trước khi tới INSERT


async def test_batches_at_90() -> None:
    """200 text → transport ghi nhận đúng 3 lần gọi (90/90/20)."""
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        batch = payload["input"]
        calls.append(batch)
        data = [{"index": i, "embedding": [1.0, 0.0]} for i in range(len(batch))]
        return httpx.Response(200, json={"data": data})

    texts = [f"text-{i}" for i in range(200)]
    pool = _FakePool()
    client = _client(handler)
    gw = GatewayEmbedding(api_key="k", dim=2, client=client, pool=pool)  # type: ignore[arg-type]
    vectors = await gw.embed(texts)
    assert len(vectors) == 200
    assert [len(c) for c in calls] == [90, 90, 20]


async def test_positional_cardinality_with_duplicates() -> None:
    """texts = ["a","b","a"] → 3 vector, [0] == [2], transport chỉ thấy 2 text (dedupe). Ghim E-1
    (map ngược theo cache_key, không `zip` cắt cụt)."""
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        batch = payload["input"]
        calls.append(batch)
        vector_for = {"a": [1.0, 0.0], "b": [0.0, 1.0]}
        data = [{"index": i, "embedding": vector_for[t]} for i, t in enumerate(batch)]
        return httpx.Response(200, json={"data": data})

    pool = _FakePool()
    client = _client(handler)
    gw = GatewayEmbedding(api_key="k", dim=2, client=client, pool=pool)  # type: ignore[arg-type]
    vectors = await gw.embed(["a", "b", "a"])
    assert len(vectors) == 3
    assert vectors[0] == vectors[2]
    assert vectors[0] != vectors[1]
    assert len(calls) == 1
    assert calls[0] == ["a", "b"]


async def test_missing_key_raises() -> None:
    """Key None ⇒ raise, không trả vector nào. DoD 5."""
    pool = _FakePool()
    gw = GatewayEmbedding(api_key=None, pool=pool)  # type: ignore[arg-type]
    with pytest.raises(EmbeddingGatewayError):
        await gw.embed(["hello"])


async def test_http_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(500, json={"error": "boom"})

    pool = _FakePool()
    client = _client(handler)
    gw = GatewayEmbedding(api_key="k", client=client, pool=pool)  # type: ignore[arg-type]
    with pytest.raises(EmbeddingGatewayError):
        await gw.embed(["hello"])


async def test_wrong_width_from_api_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        batch = payload["input"]
        # trả 1536 chiều thay vì 2048 — API "sai" bề rộng, không được âm thầm pad/cắt
        data = [{"index": i, "embedding": [1.0] * 1536} for i in range(len(batch))]
        return httpx.Response(200, json={"data": data})

    pool = _FakePool()
    client = _client(handler)
    gw = GatewayEmbedding(api_key="k", client=client, pool=pool)  # type: ignore[arg-type]
    with pytest.raises(EmbeddingGatewayError):
        await gw.embed(["hello"])


async def test_malformed_json_body_raises_gateway_error() -> None:
    """Review PR#32, Critical #1: HTTP 200 nhưng body KHÔNG phải JSON hợp lệ — `response.json()`
    raise `json.JSONDecodeError` (subclass `ValueError`). Phải thành `EmbeddingGatewayError`, không
    phải `ValueError` trần (bị `routes/publish.py`'s `except ValueError` bắt nhầm thành 400)."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=b"khong phai json hop le {{{")

    pool = _FakePool()
    client = _client(handler)
    gw = GatewayEmbedding(api_key="k", client=client, pool=pool)  # type: ignore[arg-type]
    with pytest.raises(EmbeddingGatewayError):
        await gw.embed(["hello"])


async def test_json_body_not_an_object_raises_gateway_error() -> None:
    """Review PR#32, Critical #1: JSON hợp lệ nhưng KHÔNG phải object (`payload.get(...)` nổ
    `AttributeError` trên list) — cùng lớp lỗi hình dạng lạ, phải thành `EmbeddingGatewayError`."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=[1, 2, 3])

    pool = _FakePool()
    client = _client(handler)
    gw = GatewayEmbedding(api_key="k", client=client, pool=pool)  # type: ignore[arg-type]
    with pytest.raises(EmbeddingGatewayError):
        await gw.embed(["hello"])


async def test_missing_embedding_field_raises_gateway_error() -> None:
    """Review PR#32, Critical #1: item thiếu field `embedding` — `KeyError` trần phải thành
    `EmbeddingGatewayError`."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        batch = payload["input"]
        data = [{"index": i} for i in range(len(batch))]  # thiếu "embedding"
        return httpx.Response(200, json={"data": data})

    pool = _FakePool()
    client = _client(handler)
    gw = GatewayEmbedding(api_key="k", client=client, pool=pool)  # type: ignore[arg-type]
    with pytest.raises(EmbeddingGatewayError):
        await gw.embed(["hello"])


async def test_non_numeric_embedding_value_raises_gateway_error() -> None:
    """Review PR#32, Critical #1: giá trị trong `embedding` không phải số — `float("khong-phai-so")`
    raise `ValueError` trần phải thành `EmbeddingGatewayError`."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        batch = payload["input"]
        data = [{"index": i, "embedding": ["khong-phai-so"] * DIM} for i in range(len(batch))]
        return httpx.Response(200, json={"data": data})

    pool = _FakePool()
    client = _client(handler)
    gw = GatewayEmbedding(api_key="k", client=client, pool=pool)  # type: ignore[arg-type]
    with pytest.raises(EmbeddingGatewayError):
        await gw.embed(["hello"])


async def test_duplicate_index_raises_gateway_error() -> None:
    """Review PR#32, Critical #2: `index` trùng lặp (cả 2 item cùng index=0) — độ dài `data` vẫn
    khớp `len(batch)` nên check cũ (chỉ so độ dài) lọt qua, nhưng đây KHÔNG phải hoán vị hợp lệ
    `{0..n-1}` — phải raise, không được âm thầm gán sai vector."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        batch = payload["input"]
        assert len(batch) == 2
        data = [
            {"index": 0, "embedding": [1.0] + [0.0] * (DIM - 1)},
            {"index": 0, "embedding": [0.0, 1.0] + [0.0] * (DIM - 2)},
        ]
        return httpx.Response(200, json={"data": data})

    pool = _FakePool()
    client = _client(handler)
    gw = GatewayEmbedding(api_key="k", client=client, pool=pool)  # type: ignore[arg-type]
    with pytest.raises(EmbeddingGatewayError):
        await gw.embed(["a", "b"])


async def test_out_of_range_index_raises_gateway_error() -> None:
    """Review PR#32, Critical #2: `index` ngoài phạm vi `[0, len(batch))` (vd index=5 cho batch 2
    text) — cùng lớp "không phải hoán vị hợp lệ", phải raise."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        batch = payload["input"]
        assert len(batch) == 2
        data = [
            {"index": 0, "embedding": [1.0] + [0.0] * (DIM - 1)},
            {"index": 5, "embedding": [0.0, 1.0] + [0.0] * (DIM - 2)},
        ]
        return httpx.Response(200, json={"data": data})

    pool = _FakePool()
    client = _client(handler)
    gw = GatewayEmbedding(api_key="k", client=client, pool=pool)  # type: ignore[arg-type]
    with pytest.raises(EmbeddingGatewayError):
        await gw.embed(["a", "b"])


async def test_string_index_reorders_numerically_not_lexicographically() -> None:
    """Review PR#32, Critical #2: `index` kiểu CHUỖI ("0","1",..,"10") sort TỪ ĐIỂN sẽ xếp sai thứ
    tự với ≥11 phần tử ("10" < "2" theo chuỗi) — bài này dùng 11 text để bài KHÔNG xanh giả nếu ai
    quay lại `sorted(data, key=lambda item: item["index"])` (so chuỗi) thay vì ép `int()` trước."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        batch = payload["input"]
        assert len(batch) == 11
        # trả ĐÚNG index nhưng dưới dạng CHUỖI — vector thứ i là [1.0 tại vị trí i, còn lại 0]
        data = [
            {"index": str(i), "embedding": ([0.0] * i + [1.0] + [0.0] * (10 - i))[:11] + [0.0] * (DIM - 11)}
            for i in range(11)
        ]
        return httpx.Response(200, json={"data": data})

    pool = _FakePool()
    client = _client(handler)
    gw = GatewayEmbedding(api_key="k", client=client, pool=pool)  # type: ignore[arg-type]
    texts = [f"text-{i}" for i in range(11)]
    vectors = await gw.embed(texts)
    for i in range(11):
        assert vectors[i][i] == pytest.approx(1.0)


async def test_zero_vector_survives_normalize() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        batch = payload["input"]
        data = [{"index": i, "embedding": [0.0] * DIM} for i in range(len(batch))]
        return httpx.Response(200, json={"data": data})

    pool = _FakePool()
    client = _client(handler)
    gw = GatewayEmbedding(api_key="k", client=client, pool=pool)  # type: ignore[arg-type]
    vectors = await gw.embed(["hello"])
    assert vectors[0] == [0.0] * DIM
    assert not math.isnan(vectors[0][0])


def test_recorded_response_fixture_is_provider_shaped() -> None:
    """`tests/fixtures/embedding-gateway-recorded.json` — 2 vector THẬT copy tĩnh từ
    `packages/kb/tests/embedding-tests/cache/gemini-embedding-001-d2048.{bin,index.json}` (R-11:
    KHÔNG path-reach package khác lúc chạy). Ghim đúng hình dạng response OpenRouter thật (2048
    chiều, đã chuẩn hoá sẵn) — chứng minh handler tổng hợp ở các test trên không bịa hình dạng."""
    fixture_path = Path(__file__).parent / "fixtures" / "embedding-gateway-recorded.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert fixture["model"] == MODEL
    assert fixture["dim"] == DIM
    assert len(fixture["data"]) == 2
    for item in fixture["data"]:
        assert len(item["embedding"]) == DIM
        assert sum(x * x for x in item["embedding"]) == pytest.approx(1.0, rel=1e-5)


async def test_recorded_response_roundtrips_through_gateway() -> None:
    """Tiêm nguyên văn response đã ghi lại làm transport double — `GatewayEmbedding` phải trả lại
    ĐÚNG các vector đó (đã chuẩn hoá sẵn nên `_l2_normalize` là no-op thực chất, không đổi giá trị
    quá `rel=1e-6`)."""
    fixture_path = Path(__file__).parent / "fixtures" / "embedding-gateway-recorded.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    recorded = {item["index"]: item["embedding"] for item in fixture["data"]}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        batch = payload["input"]
        data = [{"index": i, "embedding": recorded[i]} for i in range(len(batch))]
        return httpx.Response(200, json={"data": data})

    pool = _FakePool()
    client = _client(handler)
    gw = GatewayEmbedding(api_key="k", client=client, pool=pool)  # type: ignore[arg-type]
    vectors = await gw.embed(["real-text-0", "real-text-1"])
    assert vectors[0] == pytest.approx(recorded[0], rel=1e-6)
    assert vectors[1] == pytest.approx(recorded[1], rel=1e-6)


async def test_cache_hit_skips_http(admin_pool: Pool) -> None:
    """Gọi 2 lần cùng text — lần 2 transport 0 lần gọi, kết quả khớp trong dung sai float4. Cần DB
    thật (bảng `core.embedding_cache` — Phase 2, cột `vector(2048)` = pgvector, lưu float4); skip
    khi không có DSN.

    Review PR#32: bản trước dùng vector toàn 0.0/1.0 rồi assert `==` (bit-for-bit) — nhưng đó là
    trùng hợp, KHÔNG phải bằng chứng tổng quát: 0.0/1.0 tình cờ biểu diễn CHÍNH XÁC được ở float4,
    nên phép so `==` xanh dù không thật sự kiểm tra được mất mát độ chính xác lúc round-trip qua
    cột `vector`. Vector dưới đây có phần thập phân không tròn (sau L2-normalize) — `pytest.approx`
    kiểm round-trip THẬT qua float4, thay vì khớp `==` chỉ vì chọn nhầm giá trị dễ."""
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        batch = payload["input"]
        calls.append(batch)
        data = [{"index": i, "embedding": [1.0, 2.0, 3.0, 4.0] + [0.0] * (DIM - 4)} for i in range(len(batch))]
        return httpx.Response(200, json={"data": data})

    gw1 = GatewayEmbedding(api_key="k", client=_client(handler), pool=admin_pool)
    first = await gw1.embed(["cache-me"])
    assert len(calls) == 1

    gw2 = GatewayEmbedding(api_key="k", client=_client(handler), pool=admin_pool)
    second = await gw2.embed(["cache-me"])
    assert len(calls) == 1  # KHÔNG gọi transport lần 2 — cache hit
    assert second[0] == pytest.approx(first[0], rel=1e-6)

    # sanity: đúng row nằm trong bảng, đúng cache_key
    key = _cache_key(MODEL, DIM, "cache-me")
    async with admin_pool.connection() as conn:
        cur = await conn.execute("SELECT cache_key FROM core.embedding_cache WHERE cache_key = %s", (key,))
        row = await cur.fetchone()
    assert row is not None
