"""`providers/factory.py::build_tool_dispatch()` (engine#32) — không có discriminator
`settings.use_fake_providers` như `build_llm()`/`build_embedding()` (xem docstring hàm đó vì sao),
nên không cần monkeypatch settings ở đây: chỉ khoá whitelist được truyền đúng và clock mặc định
chạy được (không tự raise/không cần key nào)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from studio_app.providers import factory
from studio_app.providers.tool_dispatch import RealToolDispatch


def test_build_tool_dispatch_returns_real_dispatch_with_whitelist() -> None:
    dispatcher = factory.build_tool_dispatch(["calculator", "current_datetime"])
    assert isinstance(dispatcher, RealToolDispatch)
    assert dispatcher._whitelist == ["calculator", "current_datetime"]


async def test_build_tool_dispatch_default_clock_returns_real_utc_now() -> None:
    """Đồng hồ mặc định (production) phải là giờ THẬT — không đóng băng, không hằng số.

    Bài này đo *"có đọc đồng hồ hệ thống không"*, không đo múi giờ: múi giờ là việc của
    `test_the_clock_runs_on_vietnam_time_not_utc` ngay dưới. Tách hai chuyện ra vì chúng hỏng độc
    lập — một đồng hồ đóng băng vẫn có thể mang đúng offset, và ngược lại.

    Chỉ test path này gọi `datetime.now()` thật; mọi test hành vi calculator/current_datetime khác
    tự truyền clock cố định (`test_providers_tool_dispatch.py`)."""
    dispatcher = factory.build_tool_dispatch(["current_datetime"])
    result = await dispatcher.dispatch("current_datetime", {"mode": "now"})
    assert isinstance(result, dict)
    reported = datetime.fromisoformat(result["date"])
    # Biên 1 ngày: đồng hồ chạy giờ Việt Nam (UTC+7) nên quanh nửa đêm UTC hai ngày lệch nhau một —
    # đó là hành vi ĐÚNG, không phải sai số cần siết.
    assert abs((datetime.now(UTC).date() - reported.date()).days) <= 1


async def test_the_clock_runs_on_vietnam_time_not_utc() -> None:
    """Đồng hồ mặc định chạy giờ **Việt Nam**, không phải UTC.

    Người dùng hỏi *"bây giờ là mấy giờ"* và nhận về `04:19` trong khi đồng hồ của họ chỉ `11:19` —
    lệch đúng 7 tiếng. Câu trả lời không sai về kỹ thuật (UTC là một giờ có thật) nhưng sai với thứ
    duy nhất người hỏi muốn biết.

    `days_between` không đổi: nó nhận ngày ISO từ tham số, không đọc đồng hồ.

    Kiểm OFFSET chứ không kiểm tên vùng: `ZoneInfo("Asia/Ho_Chi_Minh")` và `timezone(+7)` là hai
    object khác nhau nhưng cùng một giờ, và thứ người dùng thấy là giờ."""
    dispatcher = factory.build_tool_dispatch(["current_datetime"])
    result = await dispatcher.dispatch("current_datetime", {"mode": "now"})

    assert isinstance(result, dict)
    assert datetime.fromisoformat(result["datetime"]).utcoffset() == timedelta(hours=7)
