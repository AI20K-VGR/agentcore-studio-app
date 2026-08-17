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
    """Dùng hết bucket, chờ đủ `REFILL_SECONDS` (mô phỏng bằng `now` tăng, không `sleep` thật) ->
    có lại đúng 1 token."""
    now = 2_000.0
    for _ in range(int(rate_limit._CAPACITY)):
        assert rate_limit.check_and_consume("5.6.7.8", now=now) is True
    assert rate_limit.check_and_consume("5.6.7.8", now=now) is False

    now += rate_limit.REFILL_SECONDS
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


def test_eviction_prefers_least_recently_touched_over_first_seen() -> None:
    """Review `app#17` đợt 9: eviction cũ dùng thứ tự CHÈN đầu tiên (`next(iter(_buckets))`), không
    phải LẦN CHẠM gần nhất — kẻ bị rate-limit liên tục (`busy-key`, chèn SỚM NHẤT rồi bị chạm dồn
    dập sau đó) lại là ứng viên bị dọn ĐẦU TIÊN khi chạm trần (vì ghi đè giá trị 1 key ĐÃ TỒN TẠI
    không dời nó ra cuối dict — chỉ `pop` rồi chèn lại mới dời), trong khi `idle-key` (chèn SAU
    `busy-key`, không hề bị chạm lại) sống sót — ngược hoàn toàn ý nghĩa LRU.

    Bố trí ĐÚNG thứ tự để phân biệt được 2 cách cài (quan trọng — 1 bản dựng lỗi trước đó của bài
    này vẫn xanh dù dùng code cũ, vì `busy-key` được chạm lại ngay sau khi chèn, lúc chưa có key
    nào khác chen giữa để "vượt qua"): `busy-key` chèn TRƯỚC `idle-key`, rồi `busy-key` mới bị chạm
    lại NHIỀU LẦN — bản sửa đúng (`pop` + chèn lại) phải dời `busy-key` ra SAU `idle-key`, khiến
    `idle-key` mới là key cũ nhất thật sự lúc chạm trần bộ nhớ."""
    now = 5_000.0
    rate_limit.check_and_consume("busy-key", now=now)
    rate_limit.check_and_consume("idle-key", now=now)
    for _ in range(5):  # chạm lại busy-key SAU khi idle-key đã chèn — đây mới là chỗ 2 cách cài lệch nhau
        rate_limit.check_and_consume("busy-key", now=now)

    for i in range(rate_limit._MAX_TRACKED_IPS - 2):  # lấp gần đầy, chừa đúng 1 slot
        rate_limit.check_and_consume(f"filler-{i}", now=now)
    assert len(rate_limit._buckets) == rate_limit._MAX_TRACKED_IPS

    rate_limit.check_and_consume("newcomer", now=now)  # key mới -> buộc evict đúng 1 entry
    assert "busy-key" in rate_limit._buckets, "busy-key (vừa bị chạm gần nhất) không được evict trước idle-key"
    assert "idle-key" not in rate_limit._buckets, "idle-key (lâu không bị chạm) phải bị evict trước"
