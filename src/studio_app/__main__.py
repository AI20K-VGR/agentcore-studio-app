"""`uv run python -m studio_app` — entrypoint Windows-an-toàn cho backend, tương đương CLI
`uvicorn studio_app.app:create_app --factory ...` (README §"Các bước") nhưng ghi đè `loop=` để
tránh `ProactorEventLoop`.

Trên Windows, `uvicorn.loops.asyncio.asyncio_loop_factory()` (mặc định của `--loop auto`/`asyncio`)
trả THẲNG `asyncio.ProactorEventLoop`, KHÔNG đọc `asyncio.get_event_loop_policy()` — psycopg async
từ chối chạy trên loop đó ("Psycopg cannot use the 'ProactorEventLoop'"), lifespan luôn
`PoolTimeout` sau 10s dù Postgres/DSN hoàn toàn đúng (kiểm chứng bằng repro cô lập, xem
`app.py::_win_loop_factory` cho chi tiết đầy đủ + lý do `set_event_loop_policy` KHÔNG đủ). Truyền
`loop="studio_app.app:_win_loop_factory"` là cách uvicorn hỗ trợ chính thức để ghi đè loop-factory
qua chuỗi import, không phải hack riêng của file này.

Không cần trên Linux/macOS (README đã xác nhận CLI `uv run uvicorn ...` chạy thật trên Ubuntu
24.04 — `SelectorEventLoop` đã là default ở đó, `--loop auto` không có vấn đề gì). Không có
`--reload`/nhiều worker: `_win_loop_factory` cố ý luôn trả `SelectorEventLoop` (không hỗ trợ
subprocess trên Windows) — restart tay sau khi sửa code.

Chạy:
    uv run python -m studio_app
Đổi host/port qua biến môi trường `STUDIO_HOST`/`STUDIO_PORT` (mặc định 127.0.0.1:8000).
"""

from __future__ import annotations

import os
import sys

import uvicorn


def main() -> None:
    uvicorn.run(
        "studio_app.app:create_app",
        factory=True,
        host=os.environ.get("STUDIO_HOST", "127.0.0.1"),
        port=int(os.environ.get("STUDIO_PORT", "8000")),
        proxy_headers=False,
        loop="studio_app.app:_win_loop_factory" if sys.platform == "win32" else "auto",
    )


if __name__ == "__main__":
    main()
