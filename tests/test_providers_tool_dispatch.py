"""`RealToolDispatch` (engine#32) — dispatcher thật cho `calculator`/`current_datetime`.
Real teeth: mỗi assertion khoá 1 giá trị cụ thể, không bare `pytest.raises`."""

from __future__ import annotations

from datetime import datetime

import pytest
from studio_app.providers.tool_dispatch import SUPPORTED_TOOLS, RealToolDispatch, find_unsupported_tool_call
from studio_contracts import Edge, Node, NodeType, Recipe
from studio_workbench import create_recipe

_FIXED_NOW = datetime(2026, 8, 22, 12, 0, 0)
_ANKOR_ID = "a0000000-0000-0000-0000-000000000001"


def _dispatch(whitelist: list[str]) -> RealToolDispatch:
    return RealToolDispatch(whitelist, clock=lambda: _FIXED_NOW)


def _recipe_with_tool_call(tool: str, whitelist: list[str]) -> Recipe:
    """Dựng 1 Recipe với đúng 1 node `tool-call{tool}` — workbench#31 đã bỏ node này khỏi
    `create_recipe_d4()` (kb_search có kind kb_retrieve, không bao giờ nên là tool-call), nên các
    test dưới không còn mượn được d4 để dựng lớp lỗi này nữa; dựng tường minh qua
    `create_recipe` để vẫn khoá đúng hành vi `find_unsupported_tool_call()`."""
    return create_recipe(
        agent_id="agent-tool-dispatch-test",
        tenant_id=_ANKOR_ID,
        instructions="x",
        tool_whitelist=whitelist,
        nodes=[
            Node(id="n1", type=NodeType.TOOL_CALL, params={"tool": tool}),
            Node(id="n2", type=NodeType.END, params={}),
        ],
        edges=[Edge(from_="n1", to="n2")],
    )


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


async def test_calculator_deeply_nested_expression_raises_value_error_not_recursion_error() -> None:
    """Review finding (engine#32): verify thực nghiệm — một `expression` dài kiểu chuỗi cộng liên
    tiếp làm CHÍNH `ast.parse()` raise `RecursionError` (không phải `SyntaxError`). Trước fix, đó
    xuyên thẳng lên thành crash request. Phải là `ValueError` sạch, cùng nguyên tắc `_parse_literal`
    (`studio_engine.executors`, `except (ValueError, RecursionError):`)."""
    dispatcher = _dispatch(["calculator"])
    hostile_expression = "1+" * 3000 + "1"
    with pytest.raises(ValueError, match="quá phức tạp"):
        await dispatcher.dispatch("calculator", {"expression": hostile_expression})


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


# --- find_unsupported_tool_call (PA-4, app#41 review finding dholmes0207) --------------------
#
# `graph_lint()` (packages/workbench/validator.py Rule 7) chỉ kiểm tool ∈ tool_whitelist, KHÔNG
# kiểm dispatcher có hỗ trợ tool đó không — nên đây là lớp preflight riêng, dùng bởi
# routes/runs.py + routes/chat.py TRƯỚC khi gọi interpreter.run().


def test_supported_tools_is_exactly_calculator_and_current_datetime() -> None:
    """Nguồn sự thật duy nhất `dispatch()` VÀ `find_unsupported_tool_call()` cùng đọc — đổi giá
    trị này ở đây tức đổi cả 2 nơi cùng lúc, không có nguy cơ trôi lệch giữa chúng."""
    assert frozenset({"calculator", "current_datetime"}) == SUPPORTED_TOOLS


def test_find_unsupported_tool_call_flags_kb_search_tool_call_node() -> None:
    """`kb_search` có kind `kb_retrieve` — không bao giờ nên là node `tool-call` (workbench#31);
    đây là instance thật của lớp lỗi mà hàm này phải bắt được, dựng tường minh vì
    `create_recipe_d4()` không còn tự sinh node này nữa (bug gốc đã vá ở nguồn)."""
    recipe = _recipe_with_tool_call("kb_search", whitelist=["kb_search"])
    assert find_unsupported_tool_call(recipe) == "kb_search"


def test_find_unsupported_tool_call_returns_none_when_tool_supported() -> None:
    """Không false-positive trên tool hợp lệ (∈ SUPPORTED_TOOLS)."""
    recipe = _recipe_with_tool_call("calculator", whitelist=["calculator"])
    assert find_unsupported_tool_call(recipe) is None


def test_find_unsupported_tool_call_generalizes_beyond_kb_search() -> None:
    """Lớp lỗi đúng phải là 'mọi tool ∈ whitelist mà ∉ SUPPORTED_TOOLS', không riêng kb_search —
    `http_fetch` (tool ma từng lộ ở apps/web AVAILABLE_TOOLS) là instance thứ hai."""
    recipe = _recipe_with_tool_call("http_fetch", whitelist=["http_fetch"])
    assert find_unsupported_tool_call(recipe) == "http_fetch"


def test_find_unsupported_tool_call_returns_none_when_recipe_has_no_tool_call_node() -> None:
    """`create_recipe_d4()` (production call path, `eval_adapter.py`) không còn sinh node
    `tool-call` nào từ workbench#31 — khoá lại rằng đó là None hợp lệ (không tool-call = không có
    gì để soi), không phải false-negative của hàm này."""
    from studio_workbench import create_recipe_d4

    recipe = create_recipe_d4()
    assert find_unsupported_tool_call(recipe) is None
