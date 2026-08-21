"""`RealToolDispatch` (engine#32) — dispatcher thật cho `calculator`/`current_datetime`.
Real teeth: mỗi assertion khoá 1 giá trị cụ thể, không bare `pytest.raises`."""

from __future__ import annotations

from datetime import datetime

import pytest
from studio_app.providers.tool_dispatch import RealToolDispatch

_FIXED_NOW = datetime(2026, 8, 22, 12, 0, 0)


def _dispatch(whitelist: list[str]) -> RealToolDispatch:
    return RealToolDispatch(whitelist, clock=lambda: _FIXED_NOW)


# --- whitelist (defense-in-depth, kept per issue engine#32) --------------------------------


async def test_tool_outside_whitelist_raises_before_reaching_logic() -> None:
    dispatcher = _dispatch(["calculator"])
    with pytest.raises(ValueError, match="tool not in whitelist: current_datetime"):
        await dispatcher.dispatch("current_datetime", {"mode": "now"})


async def test_unsupported_whitelisted_tool_raises() -> None:
    """Whitelisted nhưng không phải 1 trong 2 tool này đã biết — không âm thầm trả rác."""
    dispatcher = _dispatch(["kb_search"])
    with pytest.raises(ValueError, match="unsupported tool: kb_search"):
        await dispatcher.dispatch("kb_search", {})


# --- calculator ------------------------------------------------------------------------------


async def test_calculator_basic_arithmetic() -> None:
    dispatcher = _dispatch(["calculator"])
    result = await dispatcher.dispatch("calculator", {"expression": "2 + 2"})
    assert result == {"expression": "2 + 2", "result": 4}


async def test_calculator_parens_and_precedence() -> None:
    dispatcher = _dispatch(["calculator"])
    result = await dispatcher.dispatch("calculator", {"expression": "(3 - 1) * 4 / 2"})
    assert result == {"expression": "(3 - 1) * 4 / 2", "result": 4.0}


async def test_calculator_unary_minus() -> None:
    dispatcher = _dispatch(["calculator"])
    result = await dispatcher.dispatch("calculator", {"expression": "-5 + 2"})
    assert result == {"expression": "-5 + 2", "result": -3}


async def test_calculator_division_by_zero_raises_value_error() -> None:
    dispatcher = _dispatch(["calculator"])
    with pytest.raises(ValueError, match="chia cho 0"):
        await dispatcher.dispatch("calculator", {"expression": "1 / 0"})


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('echo pwned')",
        "exec('1')",
        "(1).__class__",
        "open('x')",
        "'a string'",
        "True",
        "[1, 2, 3]",
    ],
)
async def test_calculator_rejects_anything_beyond_arithmetic(expression: str) -> None:
    """KHÔNG `eval`/`exec`/`ast.literal_eval` (cùng nguyên tắc `_parse_literal`) — name lookup, gọi
    hàm, chuỗi, bool, list đều phải raise `ValueError`, KHÔNG BAO GIỜ thực thi."""
    dispatcher = _dispatch(["calculator"])
    with pytest.raises(ValueError):
        await dispatcher.dispatch("calculator", {"expression": expression})


# --- current_datetime ------------------------------------------------------------------------


async def test_current_datetime_mode_now_uses_injected_clock() -> None:
    dispatcher = _dispatch(["current_datetime"])
    result = await dispatcher.dispatch("current_datetime", {"mode": "now"})
    assert result == {"mode": "now", "date": "2026-08-22"}


async def test_current_datetime_mode_days_between() -> None:
    dispatcher = _dispatch(["current_datetime"])
    result = await dispatcher.dispatch(
        "current_datetime",
        {"mode": "days_between", "from_date": "2026-08-01", "to_date": "2026-08-22"},
    )
    assert result == {"mode": "days_between", "from": "2026-08-01", "to": "2026-08-22", "days": 21}


async def test_current_datetime_unsupported_mode_raises() -> None:
    dispatcher = _dispatch(["current_datetime"])
    with pytest.raises(ValueError, match="mode không hỗ trợ"):
        await dispatcher.dispatch("current_datetime", {"mode": "yesterday"})


async def test_current_datetime_days_between_bad_date_raises() -> None:
    dispatcher = _dispatch(["current_datetime"])
    with pytest.raises(ValueError, match="không phải ISO date"):
        await dispatcher.dispatch(
            "current_datetime",
            {"mode": "days_between", "from_date": "not-a-date", "to_date": "2026-08-22"},
        )
