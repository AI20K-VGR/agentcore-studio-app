"""Token-bucket rate limiter theo IP — hiện chỉ dùng cho `POST /api/auth/login` (review `app#17`,
Important #2, đợt 8): `bcrypt` cost-12 chạy KHÔNG ĐIỀU KIỆN ở đó, kể cả nhánh `DUMMY_PASSWORD_HASH`
(email không tồn tại) — route KHÔNG cần đăng nhập nên không có gì chặn TRƯỚC nó. `run_in_threadpool`
dùng chung AnyIO threadpool (mặc định 40 thread) với MỌI request khác của process — ~110 req/s từ
1 client ẩn danh đủ giữ bận toàn bộ 40 thread bằng bcrypt (~200-370ms/lần), treo NGUYÊN process
(kể cả request không liên quan gì tới login). Không nằm trong danh sách "hệ quả đã chấp nhận" ở
`docs/decisions/real-auth-system.md` — đó là oracle (thời gian/status code), đây là DoS (cạn
threadpool), khác loại rủi ro.

In-process, KHÔNG phân tán (không Redis/Memcached) — đủ để chặn ĐÚNG kịch bản nêu trên (1 client ẩn
danh dồn dập), không giải quyết tấn công phân tán nhiều IP (residual risk chấp nhận: thêm 1
dependency ngoài chỉ cho rate-limit là quá tay so với rủi ro đang chặn, và repo hiện không có hạ
tầng cache dùng chung nào khác). Nhiều worker/process thì mỗi process giữ bucket riêng — giới hạn
hiệu quả nhân theo số worker, cùng đánh đổi.
"""

from __future__ import annotations

import time

_CAPACITY = 10.0
"""Số request tối đa dồn được liên tiếp (burst) trước khi bắt đầu bị chặn."""

_REFILL_SECONDS = 6.0
"""1 token mới mỗi 6 giây/IP -> ~10 request/phút bền vững — đủ rộng cho người dùng thật gõ sai mật
khẩu vài lần, đủ hẹp để triệt tiêu ~110 req/s mà reviewer đo được (còn lại ~0.17 req/s/IP)."""

_MAX_TRACKED_IPS = 10_000
"""Trần bộ nhớ cho `_buckets` — phòng rò rỉ bộ nhớ nếu 1 client giả mạo IP (khác nhau mỗi request)
để né bucket, không phải LRU thật (không đáng để thêm cấu trúc dữ liệu cho 1 giới hạn best-effort)."""

_buckets: dict[str, tuple[float, float]] = {}
"""`key -> (tokens hiện có, lúc cập nhật gần nhất theo time.monotonic())`."""


def check_and_consume(key: str, *, now: float | None = None) -> bool:
    """`True` nếu còn token (đã trừ 1 ngay trong lệnh gọi này), `False` nếu hết — caller phải trả
    429 khi `False`. `now` chỉ để test tự set mốc thời gian (tránh phải `sleep` thật để refill)."""
    if now is None:
        now = time.monotonic()

    if key not in _buckets and len(_buckets) >= _MAX_TRACKED_IPS:
        # Không LRU thật — dict giữ thứ tự chèn ở CPython nên `next(iter(...))` là entry CŨ NHẤT
        # trên thực tế, đủ để chặn phình vô hạn mà không cần thêm cấu trúc dữ liệu riêng.
        oldest_key = next(iter(_buckets))
        del _buckets[oldest_key]

    tokens, last_update = _buckets.get(key, (_CAPACITY, now))
    elapsed = max(0.0, now - last_update)
    tokens = min(_CAPACITY, tokens + elapsed / _REFILL_SECONDS)

    if tokens < 1.0:
        _buckets[key] = (tokens, now)
        return False

    _buckets[key] = (tokens - 1.0, now)
    return True


def reset_all() -> None:
    """Chỉ dùng trong test — xoá sạch state giữa các bài để bài này không ăn hết token của bài kia
    (module-level state, không tự reset theo request/test như ContextVar)."""
    _buckets.clear()
