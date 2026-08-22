"""`RealToolDispatch` — dispatcher THẬT cho 2 tool kind `tool_call` (engine#32), thay
`WhitelistToolDispatch` (`studio_engine.demo_stubs`, stub) ở composition root này, đúng docstring
gốc của chính stub đó ("Day 6 replaces with real composition in `apps/studio`"). `kb_search`
(kind `kb_retrieve`) KHÔNG đi qua đây — nó có executor riêng (`KbRetrieveExecutor`, đã real, không
đụng).

Không có cặp fake/real như `build_llm()`/`build_embedding()`: 2 tool này (`calculator`,
`current_datetime`) tự thân đã xong xuôi, tất định, không gọi API ngoài — KHÔNG có "bản thật" nào
khác để chọn qua `settings.use_fake_providers`. Biến duy nhất cần tách để test được là ĐỒNG HỒ
(`current_datetime` không được tự gọi `datetime.now()` bên trong — bắt buộc để CI/golden-set
reproducible, đúng cảnh báo Z4/D03 `kit#67`, mutant-kill clock còn thiếu ở đó) — nên `Clock` đi qua
constructor-DI, không phải qua settings flag.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from datetime import date, datetime

from studio_contracts import NodeType, Recipe

Clock = Callable[[], datetime]
"""`() -> datetime` — production truyền `lambda: datetime.now(UTC)` (`providers/factory.py::
build_tool_dispatch()`), test truyền 1 hàm trả `datetime` cố định. Không phải `Protocol` (chỉ 1
method, `Callable` đủ, không cần class rỗng)."""

_ALLOWED_BINOP: dict[type[ast.operator], Callable[[int | float, int | float], int | float]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
}
_ALLOWED_UNARYOP: dict[type[ast.unaryop], Callable[[int | float], int | float]] = {
    ast.UAdd: lambda a: +a,
    ast.USub: lambda a: -a,
}
"""Whitelist node-type cho `calculator` (engine#32): CHỈ `BinOp`/`UnaryOp`/`Constant` số học
`+-*/` + ngoặc, đúng phạm vi `PROJECT-SCOPE-DEMO-DAY30.md` §1 khai — không mở rộng thêm `**`/`%`
dù `ast` hỗ trợ, để không vượt phạm vi issue đã chốt. Cùng nguyên tắc `_parse_literal`
(`studio_engine.executors`, `when` grammar cũ) đã dùng: KHÔNG `eval`/`exec`/`ast.literal_eval` —
tự đi bộ cây AST và tính tay, 1 node lạ (tên biến, gọi hàm, chuỗi, ...) là `ValueError` ngay, không
có nhánh nào rơi về thực thi code tuỳ ý."""


def _eval_node(node: ast.AST) -> int | float:
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, int | float):
            raise ValueError(f"calculator: hằng số không hợp lệ (chỉ int/float): {node.value!r}")
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOP:
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        try:
            return _ALLOWED_BINOP[type(node.op)](left, right)
        except ZeroDivisionError as exc:
            raise ValueError("calculator: chia cho 0") from exc
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOP:
        return _ALLOWED_UNARYOP[type(node.op)](_eval_node(node.operand))
    raise ValueError(f"calculator: cú pháp không được phép: {ast.dump(node)}")


def _calculate(params: dict[str, object]) -> dict[str, object]:
    raw_expression = params.get("expression", "")
    expression = raw_expression if isinstance(raw_expression, str) else str(raw_expression)
    # `RecursionError` (review finding, engine#32): một `expression` dài/lồng sâu (vd. chuỗi
    # `"1+"*3000 + "1"`, verify thực nghiệm) làm CHÍNH `ast.parse()` raise `RecursionError` — không
    # phải `SyntaxError` — nên phải bắt riêng, không tự lọt lên crash request. Cùng nguyên tắc
    # `_parse_literal` (`studio_engine.executors`, `when` grammar) đã dùng cho hazard này.
    try:
        tree = ast.parse(expression, mode="eval")
        result = _eval_node(tree.body)
    except SyntaxError as exc:
        raise ValueError(f"calculator: biểu thức không hợp lệ: {expression!r}") from exc
    except RecursionError as exc:
        raise ValueError("calculator: biểu thức quá phức tạp (quá nhiều lớp lồng nhau)") from exc
    return {"expression": expression, "result": result}


def _current_datetime(params: dict[str, object], clock: Clock) -> dict[str, object]:
    mode = params.get("mode")
    if mode == "now":
        return {"mode": "now", "date": clock().date().isoformat()}
    if mode == "days_between":
        raw_from = params.get("from_date")
        raw_to = params.get("to_date")
        if not isinstance(raw_from, str) or not isinstance(raw_to, str):
            raise ValueError("current_datetime: mode 'days_between' cần from_date/to_date dạng chuỗi ISO")
        try:
            from_d = date.fromisoformat(raw_from)
            to_d = date.fromisoformat(raw_to)
        except ValueError as exc:
            raise ValueError(f"current_datetime: from_date/to_date không phải ISO date: {exc}") from exc
        return {"mode": "days_between", "from": raw_from, "to": raw_to, "days": (to_d - from_d).days}
    raise ValueError(f"current_datetime: mode không hỗ trợ: {mode!r}")


SUPPORTED_TOOLS: frozenset[str] = frozenset({"calculator", "current_datetime"})
"""Tool `RealToolDispatch` thực sự dispatch được — nguồn sự thật DUY NHẤT, dùng bởi cả
`dispatch()` (raise nếu tool ngoài tập này) VÀ `find_unsupported_tool_call()` bên dưới (preflight
ở `routes/runs.py`/`routes/chat.py`, bắt sớm TRƯỚC `interpreter.run()` thay vì để `dispatch()`
raise giữa chừng DAG, review app#41). `kb_search` (kind `kb_retrieve`,
`PROJECT-SCOPE-DEMO-DAY30.md` §1) cố ý KHÔNG có ở đây — nó có executor riêng (`KbRetrieveExecutor`,
đã real), dùng node `kb-retrieve`, không phải node `tool-call`."""


def find_unsupported_tool_call(recipe: Recipe) -> str | None:
    """Tool đầu tiên (nếu có) mà một node `tool-call` trong `recipe.dag` tham chiếu nhưng KHÔNG
    nằm trong `SUPPORTED_TOOLS` — vd `kb_search` (kind `kb_retrieve`, không bao giờ nên là
    `tool-call`) hay bất kỳ tool tương lai nào chưa implement. `None` nếu mọi node `tool-call`
    trong recipe đều hợp lệ.

    Không tự raise/không tự chọn HTTP status — `graph_lint()` đã đảm bảo tool nằm trong
    `agent_config.tool_whitelist` trước khi hàm này chạy (Rule 7), nên ở đây chỉ còn cần hỏi ĐÚNG
    MỘT câu: dispatcher có dispatch được tool này không. Caller (route) chọn status phù hợp ngữ
    cảnh — 400 cho recipe client-tạo (`routes/runs.py`), 500 cho recipe đã published
    (`routes/chat.py`), cùng cách phân biệt `graph_lint()` đã dùng ở cả hai route đó."""
    for node in recipe.dag.nodes:
        if node.type == NodeType.TOOL_CALL:
            tool = node.params.get("tool")
            if tool not in SUPPORTED_TOOLS:
                return str(tool)
    return None


class RealToolDispatch:
    """`ToolDispatch` thật (engine#32) cho `calculator`/`current_datetime` — implement
    `studio_engine.executors.ToolDispatch` bằng structural typing (duck-typed, protocol không đòi
    kế thừa tường minh).

    Whitelist-check GIỮ NGUYÊN hành vi của `WhitelistToolDispatch` cũ (raise `ValueError` cùng
    format thông điệp, khi tool ngoài `agent_config.tool_whitelist`) — defense-in-depth alongside
    recipe-validator layer, không bỏ (issue engine#32 nói rõ)."""

    def __init__(self, whitelist: list[str], clock: Clock) -> None:
        self._whitelist = whitelist
        self._clock = clock

    async def dispatch(self, tool: str, params: dict[str, object]) -> object:
        if tool not in self._whitelist:
            raise ValueError(f"tool not in whitelist: {tool}")
        if tool not in SUPPORTED_TOOLS:
            raise ValueError(f"unsupported tool: {tool}")
        if tool == "calculator":
            return _calculate(params)
        return _current_datetime(params, self._clock)
