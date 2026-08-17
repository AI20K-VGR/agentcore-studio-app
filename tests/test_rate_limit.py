"""`rate_limit.py` — token-bucket theo IP (review `app#17`, Important #2, đợt 8). Pure-Python,
không cần DB thật — mọi bài tự truyền `now` (không dùng `time.monotonic()` thật) để test refill mà
không phải `sleep`."""

from __future__ import annotations

from studio_app import rate_limit


def setup_function() -> None:
    rate_limit.reset_all()


def test_burst_up_to_capacity_then_blocked() -> None:
    """Đúng `_CAPACITY` request đầu tiên (cùng `now`, không refill gì) phải qua được, request thứ
    `_CAPACITY + 1` phải bị chặn."""
    now = 1_000.0
    for _ in range(int(rate_limit._CAPACITY)):
        assert rate_limit.check_and_consume("1.2.3.4", now=now) is True
    assert rate_limit.check_and_consume("1.2.3.4", now=now) is False


def test_refill_over_time_restores_tokens() -> None:
    """Dùng hết bucket, chờ đủ `_REFILL_SECONDS` (mô phỏng bằng `now` tăng, không `sleep` thật) ->
    có lại đúng 1 token."""
    now = 2_000.0
    for _ in range(int(rate_limit._CAPACITY)):
        assert rate_limit.check_and_consume("5.6.7.8", now=now) is True
    assert rate_limit.check_and_consume("5.6.7.8", now=now) is False

    now += rate_limit._REFILL_SECONDS
    assert rate_limit.check_and_consume("5.6.7.8", now=now) is True
    assert rate_limit.check_and_consume("5.6.7.8", now=now) is False


def test_different_keys_are_independent() -> None:
    """1 IP dùng hết bucket không ảnh hưởng IP khác — cốt lõi của việc rate-limit THEO IP, không
    phải 1 bộ đếm toàn cục chung cho mọi client."""
    now = 3_000.0
    for _ in range(int(rate_limit._CAPACITY)):
        assert rate_limit.check_and_consume("attacker-ip", now=now) is True
    assert rate_limit.check_and_consume("attacker-ip", now=now) is False

    assert rate_limit.check_and_consume("innocent-ip", now=now) is True


def test_max_tracked_ips_evicts_instead_of_growing_unbounded() -> None:
    """Trần bộ nhớ — vượt `_MAX_TRACKED_IPS` phải dọn bớt thay vì phình vô hạn (phòng 1 client giả
    mạo IP khác nhau mỗi request để né rate-limit VÀ ăn bộ nhớ process cùng lúc)."""
    now = 4_000.0
    for i in range(rate_limit._MAX_TRACKED_IPS + 10):
        rate_limit.check_and_consume(f"ip-{i}", now=now)
    assert len(rate_limit._buckets) <= rate_limit._MAX_TRACKED_IPS
