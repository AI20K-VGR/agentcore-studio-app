"""4 call-site production đổi sang `build_embedding()` (app#30 Phase 5, DoD 2+3). Anti-tamper
source-level test khuôn `packages/kb/tests/test_leak_meta.py` (`docs/code-standards.md:103-105`).
Route DB test đi qua gọi thẳng hàm route + set `ContextVar` tay — cùng convention
`test_documents_routes.py` (chưa có tiền lệ `TestClient` trong repo cho loại test đó)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import UploadFile
from studio_app import middleware
from studio_app.core._db import Pool, close_pools, get_pool
from studio_app.providers.embeddings import EmbeddingGatewayError
from studio_app.routes import documents as documents_module
from studio_app.routes.documents import upload_document
from studio_workbench.tenant_wall import ResolvedContext

_ROUTE_FILES = ("runs.py", "publish.py", "chat.py", "documents.py", "test_chat.py")
# app#44: `runs.py` (`GET /api/runs/{run_id}`, chỉ đọc trace qua `PgTraceReader`) không còn chạm
# KB/LLM/embedding nào cả — `build_embedding()` rơi khỏi wiring của nó, chỉ còn ĐÚNG cho việc kiểm
# "không nhắc CallistoStubEmbedding"/"không đọc env" (2 test dưới, vẫn hợp lệ trên cả 5 file kể cả
# khi 1 file không dùng build_embedding). `test_chat.py` (nút Test — chat thật trên draft) MỚI
# thêm, cùng khuôn `publish.py`/`chat.py`: gọi `build_embedding()` đúng 1 lần để chạy
# `run_agent_loop()` thật.
_EMBEDDING_WIRED_ROUTE_FILES = ("publish.py", "chat.py", "documents.py", "test_chat.py")
_ROUTES_DIR = Path(__file__).resolve().parents[1] / "src" / "studio_app" / "routes"


def test_no_route_imports_stub_embedding() -> None:
    """Anti-tamper source-level (không phải chạy hành vi): 4 route KHÔNG được nhắc
    `CallistoStubEmbedding` lẫn `derive_vector` — chặn ai đó âm thầm nối lại đường stub/dim-8
    vào production. Test này luôn xanh SAU khi implement, đứng canh regression."""
    for name in _ROUTE_FILES:
        source = (_ROUTES_DIR / name).read_text(encoding="utf-8")
        assert "CallistoStubEmbedding" not in source, f"{name} nhắc CallistoStubEmbedding"
        assert "derive_vector" not in source, f"{name} nhắc derive_vector"
        assert "CallistoEmbedding" not in source, f"{name} vẫn còn CallistoEmbedding (tên cũ)"


def test_build_embedding_wired_in_all_four_routes() -> None:
    """`git grep -n "build_embedding" apps/studio/src/studio_app/routes/` khớp đúng 8 dòng (4
    import + 4 gọi) — DoD 2 GỐC (app#30). app#44: `runs.py` rơi khỏi danh sách này — connectivity-
    check tĩnh không chạm KB/LLM/embedding nào cả (mục D `PROJECT-SCOPE-DEMO-DAY30.md`) — nên giờ
    chỉ còn ĐÚNG 3 route (`publish.py`/`chat.py`/`documents.py`), không phải 4 (tên hàm giữ nguyên
    để không phá lịch sử git blame; nội dung mới chốt phản ánh bằng danh sách kiểm bên dưới)."""
    for name in _EMBEDDING_WIRED_ROUTE_FILES:
        source = (_ROUTES_DIR / name).read_text(encoding="utf-8")
        assert source.count("import build_embedding") + source.count(", build_embedding") >= 1, (
            f"{name} thiếu import build_embedding"
        )
        assert source.count("build_embedding()") == 1, f"{name} không đúng 1 lời gọi build_embedding()"


def test_no_env_reads_in_routes() -> None:
    """DoD 1 — 'không đọc env rải rác trong route', anti-tamper cùng khuôn 2 test trên."""
    for name in _ROUTE_FILES:
        source = (_ROUTES_DIR / name).read_text(encoding="utf-8")
        assert "os.environ" not in source, f"{name} đọc os.environ trực tiếp"
        assert "getenv" not in source, f"{name} đọc os.getenv trực tiếp"


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    yield
    await close_pools()


def _set_session(*, tenant_id: UUID, user: str, system_roles: list[str]) -> object:
    session = ResolvedContext(tenant_id=tenant_id, user=user, system_roles=system_roles)
    return middleware._request_session.set(session)


@asynccontextmanager
async def _simulate_request_connection() -> AsyncIterator[None]:
    pool = await get_pool()
    async with pool.connection() as conn:
        token = middleware._request_conn.set(conn)
        try:
            yield
        finally:
            middleware._request_conn.reset(token)


async def _seed_tenant(admin_pool: Pool, name: str) -> UUID:
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", (name,))
        row = await cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


async def _seed_user(admin_pool: Pool, tenant_id: UUID, email: str, system_roles: list[str]) -> UUID:
    async with admin_pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, system_roles) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (str(tenant_id), email, "not-a-real-hash", system_roles),
        )
        row = await cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


async def _seed_section(admin_pool: Pool, tenant_id: UUID, name: str, created_by: UUID) -> None:
    async with admin_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO core.sections (tenant_id, name, created_by) VALUES (%s, %s, %s)",
            (str(tenant_id), name, str(created_by)),
        )


def _md_upload_file(filename: str, content: bytes) -> UploadFile:
    return UploadFile(filename=filename, file=BytesIO(content))


# Route dùng `chunk_window.cut_window` (cửa sổ 850 từ/overlap 170), KHÔNG còn `_cut_document`
# (cắt theo heading `##`) — 1000 từ PHÂN BIỆT nằm trong khoảng (850, 1530] cho ĐÚNG 2 chunk
# (chunk1 = từ 1..850, chunk2 = từ 681..1000) theo cùng công thức đã test ở
# `packages/kb/tests/test_chunk_window.py`. Nội dung vô nghĩa CỐ Ý — test này chỉ cần "nhiều hơn
# 1 chunk", không quan tâm heading hay ngữ nghĩa.
_TWO_CHUNK_MD = " ".join(f"tu{i}" for i in range(1, 1001)).encode()


class _SpyEmbedding:
    """Bọc một `EmbeddingService` thật, đếm số lần `embed()` được gọi + độ dài mỗi lô — dùng để
    ghim Đính chính A vế 1 (đường ghi đã batch sẵn) bằng SỐ, không bằng lời."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.calls: list[int] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        result: list[list[float]] = await self._inner.embed(texts)  # type: ignore[attr-defined]
        return result


class _RaisingEmbedding:
    """`EmbeddingService` double raise `EmbeddingGatewayError` ngay khi gọi — mô phỏng
    `GatewayEmbedding` gặp lỗi mạng/API thật (DoD 5)."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        del texts
        raise EmbeddingGatewayError("OpenRouter trả HTTP 500 (mô phỏng test)")


async def test_documents_upload_batches_in_one_call(admin_pool: Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    """Upload file sinh 2 chunk (`cut_window` cửa sổ 850/170) — spy trên `embed()` thấy ĐÚNG 1 lời
    gọi với `len(texts) == 2`. Ghim Đính chính A vế 1 bằng số thật."""
    from studio_app.providers.factory import CallistoStubEmbedding

    spy = _SpyEmbedding(CallistoStubEmbedding())
    monkeypatch.setattr(documents_module, "build_embedding", lambda: spy)

    tenant_id = await _seed_tenant(admin_pool, "wiring-probe-batch")
    admin_id = await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])
    await _seed_section(admin_pool, tenant_id, "hr", admin_id)

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", system_roles=["admin"])
    try:
        async with _simulate_request_connection():
            result = await upload_document(
                file=_md_upload_file("policy.md", _TWO_CHUNK_MD),
                section_role="hr",
                tenant_id=None,
            )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert result.chunk_count == 2
    assert spy.calls == [2]


async def test_gateway_error_from_route_is_503(admin_pool: Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    """`use_fake_providers=False`, `build_embedding()` trả một `GatewayEmbedding` hỏng ⇒ lỗi thoát
    khỏi route là ĐÚNG `EmbeddingGatewayError` (không phải 200-với-vector-rác, không phải exception
    khác bị nuốt) — và composition root (`app.py::_embedding_gateway_error_to_503`, ghim ở
    `test_factory_build_embedding.py::test_gateway_error_maps_to_503`) ánh xạ đúng nó sang 503. Gọi
    trực tiếp handler đó ở đây để chứng minh COMPOSITION đúng, không lặp lại việc dựng ASGI+JWT đầy
    đủ (đã tốn kém, và không thêm tín hiệu mới so với 2 test đã có)."""
    from studio_app.app import _embedding_gateway_error_to_503

    monkeypatch.setattr(documents_module, "build_embedding", lambda: _RaisingEmbedding())

    tenant_id = await _seed_tenant(admin_pool, "wiring-probe-503")
    admin_id = await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])
    await _seed_section(admin_pool, tenant_id, "hr", admin_id)

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", system_roles=["admin"])
    try:
        with pytest.raises(EmbeddingGatewayError) as exc_info:
            async with _simulate_request_connection():
                await upload_document(
                    file=_md_upload_file("leave.md", _TWO_CHUNK_MD),
                    section_role="hr",
                    tenant_id=None,
                )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    response = await _embedding_gateway_error_to_503(request=None, exc=exc_info.value)  # type: ignore[arg-type]
    assert response.status_code == 503
