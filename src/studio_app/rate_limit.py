"""Token-bucket rate limiter theo IP — hiện chỉ dùng cho `POST /api/auth/login` (review `app#17`,
Important #2, đợt 8): `bcrypt` cost-12 chạy KHÔNG ĐIỀU KIỆN ở đó, kể cả nhánh `DUMMY_PASSWORD_HASH`
(email không tồn tại) — route KHÔNG cần đăng nhập nên không có gì chặn TRƯỚC nó. `run_in_threadpool`
dùng chung AnyIO threadpool (mặc định 40 thread) với MỌI request khác của process — ~110 req/s từ
1 client ẩn danh đủ giữ bận toàn bộ 40 thread bằng bcrypt (~200-370ms/lần), treo NGUYÊN process
(kể cả request không liên quan gì tới login). Không nằm trong danh sách "hệ quả đã chấp nhận" ở
`docs/decisions/real-auth-system.md` — đó là oracle (thời gian/status code), đây là DoS (cạn
threadpool), khác loại rủi ro.

In-process, KHÔNG phân tán (không dùng Redis) — đủ để chặn ĐÚNG kịch bản nêu trên (1 client ẩn
danh dồn dập), không giải quyết tấn công phân tán nhiều IP. `docker-compose.yml` CÓ Redis, nhưng
CHỈ trong `--profile obs` (Langfuse self-host, khối comment `docker-compose.yml:47-48`: "redis/
clickhouse/minio serve Langfuse ONLY") — KHÔNG bật mặc định, KHÔNG có ở `--profile app` (cách app
này thật sự chạy). Buộc rate-limit phụ thuộc Redis nghĩa là bắt buộc bật `obs` profile chỉ để login
hoạt động đúng — coupling sai hướng cho 1 dependency vốn dành riêng cho observability (review
`app#17` đợt 9: bản đợt 8 nói nhầm "repo không có Redis nào" — có, nhưng không phải hạ tầng dùng
chung sẵn sàng cho việc này). Nhiều worker/process thì mỗi process giữ bucket riêng — giới hạn hiệu
quả nhân theo số worker, đánh đổi chấp nhận được cho best-effort trong-process.
"""

from __future__ import annotations

import time

_CAPACITY = 10.0
"""Số request tối đa dồn được liên tiếp (burst) trước khi bắt đầu bị chặn."""

REFILL_SECONDS = 6.0
"""1 token mới mỗi 6 giây/IP -> ~10 request/phút bền vững — đủ rộng cho người dùng thật gõ sai mật
khẩu vài lần, đủ hẹp để triệt tiêu ~110 req/s mà reviewer đo được (còn lại ~0.17 req/s/IP).

Public (không `_` — khác `_CAPACITY`/`_MAX_TRACKED_IPS`): caller (`routes/auth.py`) cần giá trị
này để set header `Retry-After` đúng số giây thật, không đoán mò/hardcode 1 con số lệch khỏi logic
refill thật."""

_MAX_TRACKED_IPS = 10_000
"""Trần bộ nhớ cho `_buckets` — phòng rò rỉ bộ nhớ nếu 1 client giả mạo IP (khác nhau mỗi request)
để né bucket. `check_and_consume` bên dưới ĐÃ là LRU thật (đợt 9: mọi lần chạm, kể cả bị từ chối,
đều re-insert để dời key ra cuối `dict` — chỉ key lâu không bị chạm mới bị evict) — sửa lại docstring
này cho khớp, bản cũ (đợt 8) viết "không phải LRU thật" trước khi hành vi đó được vá."""

_buckets: dict[str, tuple[float, float]] = {}
"""`key -> (tokens hiện có, lúc cập nhật gần nhất theo time.monotonic())`."""


def check_and_consume(key: str, *, now: float | None = None) -> bool:
    """`True` nếu còn token (đã trừ 1 ngay trong lệnh gọi này), `False` nếu hết — caller phải trả
    429 khi `False`. `now` chỉ để test tự set mốc thời gian (tránh phải `sleep` thật để refill)."""
    if now is None:
        now = time.monotonic()

    if key not in _buckets and len(_buckets) >= _MAX_TRACKED_IPS:
        # Dict giữ thứ tự CHÈN ở CPython — `next(iter(...))` là entry cũ nhất theo lần chèn ĐẦU
        # TIÊN, không phải lần CHẠM gần nhất. Ghi đè giá trị của 1 key đã tồn tại (`_buckets[key] =
        # ...` bên dưới) KHÔNG dời nó ra cuối — nên nếu chỉ ghi đè, 1 IP bị rate-limit liên tục
        # (chạm bucket dồn dập) vẫn nằm ở vị trí CŨ (lần đầu nó xuất hiện), tức là ứng viên bị dọn
        # ĐẦU TIÊN khi chạm trần — kẻ bị chặn nhiều nhất lại được xoá sạch state, reset về đầy bucket
        # (đợt 8 → 9, review: "attacker bị chặn liên tục là key cũ nhất nên bị evict trước, entry
        # nhàn rỗi hợp lệ lại sống sót"). Sửa: LUÔN `pop` trước khi ghi (xem 2 chỗ bên dưới) để mọi
        # lần CHẠM (kể cả bị từ chối) dời key ra cuối — đúng nghĩa LRU: chỉ key nào lâu KHÔNG bị
        # chạm mới nằm ở đầu, mới là ứng viên evict hợp lý.
        oldest_key = next(iter(_buckets))
        del _buckets[oldest_key]

    tokens, last_update = _buckets.pop(key, (_CAPACITY, now))
    elapsed = max(0.0, now - last_update)
    tokens = min(_CAPACITY, tokens + elapsed / REFILL_SECONDS)

    if tokens < 1.0:
        _buckets[key] = (tokens, now)  # re-insert -> dời ra cuối (LRU)
        return False

    _buckets[key] = (tokens - 1.0, now)  # re-insert -> dời ra cuối (LRU)
    return True


def reset_all() -> None:
    """Chỉ dùng trong test — xoá sạch state giữa các bài để bài này không ăn hết token của bài kia
    (module-level state, không tự reset theo request/test như ContextVar)."""
    _buckets.clear()
