"""`providers/factory.py::build_tool_dispatch()` (engine#32) — không có discriminator
`settings.use_fake_providers` như `build_llm()`/`build_embedding()` (xem docstring hàm đó vì sao),
nên không cần monkeypatch settings ở đây: chỉ khoá whitelist được truyền đúng và clock mặc định
chạy được (không tự raise/không cần key nào)."""

from __future__ import annotations

from datetime import UTC, datetime

from studio_app.providers import factory
from studio_app.providers.tool_dispatch import RealToolDispatch


def test_build_tool_dispatch_returns_real_dispatch_with_whitelist() -> None:
    dispatcher = factory.build_tool_dispatch(["calculator", "current_datetime"])
    assert isinstance(dispatcher, RealToolDispatch)
    assert dispatcher._whitelist == ["calculator", "current_datetime"]


async def test_build_tool_dispatch_default_clock_returns_real_utc_now() -> None:
    """Đồng hồ mặc định (production) phải là UTC now thật — không đóng băng, không lệch múi giờ —
    chỉ test path này gọi `datetime.now()` thật; mọi test hành vi calculator/current_datetime khác
    tự truyền clock cố định (`test_providers_tool_dispatch.py`)."""
    dispatcher = factory.build_tool_dispatch(["current_datetime"])
    result = await dispatcher.dispatch("current_datetime", {"mode": "now"})
    assert isinstance(result, dict)
    reported = datetime.fromisoformat(result["date"])
    assert abs((datetime.now(UTC).date() - reported.date()).days) <= 1
