"""Chạy `studio_app.app:create_app` trên Windows — CLI `uvicorn` trần (README §"Chạy backend")
mặc định dùng `ProactorEventLoop` trên Windows, mà `psycopg` async TỪ CHỐI thẳng loop đó
("Psycopg cannot use the 'ProactorEventLoop' to run in async mode"). Cùng lớp gap đã vá cho
`scripts/seed_superadmin.py`/`scripts/seed_demo_tenants.py` — ở đây phải set policy TRƯỚC KHI
`uvicorn.run()` tự dựng event loop, và gọi `uvicorn.run()` bằng import-string (`factory=True`)
thay vì truyền thẳng app object, để `--reload` (nếu bật) respawn đúng.

Linux/macOS không cần script này — `uvicorn studio_app.app:create_app --factory ...` (CLI trần,
đúng README) chạy thẳng vì `SelectorEventLoop` đã là default ở đó.

Chạy (thay cho lệnh `uvicorn` trần trong README khi ở Windows):
    uv run python apps/studio/scripts/dev_server.py
"""

from __future__ import annotations

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uvicorn  # noqa: E402 — phải import SAU khi set policy ở trên


def main() -> None:
    uvicorn.run(
        "studio_app.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=8000,
        reload=True,
        # Khớp `--no-proxy-headers` ở README/Dockerfile (app#18 — SỬA lại đợt review app#21 🔸:
        # bản cũ dẫn nhầm "kit#18", đó là issue KHÁC của Day-4 về `kb_binding`) — thiếu cờ này thì
        # `ProxyHeadersMiddleware` mặc định của uvicorn vẫn tin `X-Forwarded-For` từ MỌI kết nối
        # tới từ 127.0.0.1, ghi đè `request.client` TRƯỚC KHI app thấy request — mở lại đúng
        # đường né rate-limit `/api/auth/login` mà app#18 mô tả, riêng cho đường chạy Windows
        # dev này (script cố ý thay thế lệnh uvicorn trần trong README, nên phải mang đủ cờ đó).
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
