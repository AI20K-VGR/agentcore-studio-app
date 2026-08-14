"""`x-tenant-id` header path removed (`kit#129` §3.2, vấn đề B — VinSOC AV-203064/AV-203754,
High cả 2 lượt quét). Trước bản vá, `_resolve_tenant_id` tin thẳng header client tự gửi, không
verify gì — mentor kết luận không khai thác được qua mạng (mọi route thật đều đòi
`get_request_session()`/JWT trước, fail-closed 401), nhưng vẫn là "súng lên đạn nằm trên bàn":
route mới quên gọi `get_request_session()` sẽ làm nó sống lại thành lỗ hổng thật.

File test cũ (`test_resolve_tenant_header_name_or_uuid_to_immutable_uuid`,
`test_resolve_tenant_runs_on_non_owner_app_pool`) test thẳng `_resolve_tenant_id` — hàm đó đã bị
xoá, không phải đổi tên/refactor. Thay bằng bài chứng minh NGƯỢC LẠI: header `x-tenant-id` giờ
hoàn toàn vô tác dụng, đăng nhập (JWT) là nguồn tenant DUY NHẤT."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from starlette.requests import Request
from starlette.responses import Response
from studio_app.core._db import Pool, close_pools
from studio_app.middleware import get_request_connection, tenant_context_middleware


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    """`tenant_context_middleware` chạm `get_pool()` (singleton process-wide) — cùng kỷ luật
    `test_routes_auth.py`/`test_routes_runs.py` đã tự áp, tránh rò connection sang event loop của
    test chạy sau (`ValueError: The future belongs to a different loop`, chỉ lộ ra khi chạy full
    suite, không lộ khi chạy riêng file này)."""
    yield
    await close_pools()


class _FakeHeaders:
    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    def get(self, key: str) -> str | None:
        return self._data.get(key)


class _FakeRequest:
    """Đủ shape cho `tenant_context_middleware` cần — không dựng ASGI/ đối tượng
    `starlette.Request` thật (chưa có tiền lệ TestClient trong repo)."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = _FakeHeaders(headers)


async def _read_current_app_tenant_id() -> str | None:
    """Đọc `current_setting('app.tenant_id', true)` trên CHÍNH connection request đang giữ —
    cách duy nhất đáng tin để biết middleware có `SET LOCAL` hay không, không suy luận qua
    `_request_session` (contextvar khác, không phải nguồn sự thật cho `SET LOCAL`)."""
    conn = get_request_connection()
    cur = await conn.execute("SELECT current_setting('app.tenant_id', true)")
    row = await cur.fetchone()
    assert row is not None
    value: str | None = row[0]
    return value


async def test_x_tenant_id_header_no_longer_sets_app_tenant_id(admin_pool: Pool) -> None:
    """Kịch bản đúng nghĩa VinSOC nêu: request KHÔNG mang JWT (chưa đăng nhập), chỉ mang header
    `x-tenant-id` client tự khai. Trước bản vá, `app.tenant_id` sẽ bị SET theo header đó. Sau bản
    vá: không có JWT ⇒ không có session ⇒ KHÔNG set gì cả — đúng fail-closed (Decision #3), RLS
    coi NULL là 0 dòng."""
    async with admin_pool.connection() as conn:
        await conn.execute("INSERT INTO core.tenants (name) VALUES (%s)", ("ankor-header-spoof-probe",))

    captured: dict[str, str | None] = {}

    async def _call_next(request: Request) -> Response:
        del request
        captured["app_tenant_id"] = await _read_current_app_tenant_id()
        return Response(status_code=200)

    request = _FakeRequest({"x-tenant-id": "ankor-header-spoof-probe"})
    await tenant_context_middleware(request, _call_next)  # type: ignore[arg-type]

    assert captured["app_tenant_id"] is None, (
        f"header x-tenant-id vẫn còn tác dụng (app.tenant_id = {captured['app_tenant_id']!r}) — "
        "đường cũ chưa thật sự bị xoá"
    )


async def test_no_header_no_token_also_sets_nothing(admin_pool: Pool) -> None:
    """Đối chứng: không JWT, không cả header — cùng kết quả (None), chứng minh hành vi không đổi
    ngẫu nhiên theo việc CÓ header hay không, chỉ còn phụ thuộc duy nhất vào JWT."""
    del admin_pool

    captured: dict[str, str | None] = {}

    async def _call_next(request: Request) -> Response:
        del request
        captured["app_tenant_id"] = await _read_current_app_tenant_id()
        return Response(status_code=200)

    await tenant_context_middleware(_FakeRequest({}), _call_next)  # type: ignore[arg-type]

    assert captured["app_tenant_id"] is None
