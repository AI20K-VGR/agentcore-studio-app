"""Contract tĩnh (báo cáo `kit#129` mục 3 — việc cần làm sau khi VinSOC finding C
(`interpreter.py` không tự kiểm cấu trúc DAG, DEC-A) được thẩm định là Won't Fix).

DEC-A tách trách nhiệm làm hai: `graph_lint` (SWE, `studio_workbench.validator`) kiểm cấu trúc DAG,
`interpreter.run()` (AIE-1, `studio_engine.interpreter`) TIN nó đã sạch và không tự kiểm lại (phần
đó đã được pin kỹ ở phía engine — xem `packages/engine/tests/test_dag_edge_walk.py`: không còn
guard 1-điểm-bắt-đầu, không guard >1 cạnh ra, không guard vòng lặp, không guard "phải kết thúc ở
`end`", tất cả bỏ CÓ CHỦ ĐÍCH). Sự tách đó chỉ an toàn khi MỌI nơi gọi `interpreter.run()` đã gọi
`graph_lint()` trước — hôm nay đúng theo cách đọc mã tay (3 chỗ: `chat.py`, `runs.py`,
`eval_adapter.py::EngineAgentRunner.run_case`). Bài này biến "đúng theo cách đọc mã tay" thành "đúng
theo cách máy kiểm": quét TĨNH (AST) mọi hàm dưới `src/studio_app/` gọi `interpreter.run(...)`, và
đòi mỗi hàm đó HOẶC tự gọi `graph_lint(...)` ở dòng sớm hơn trong CHÍNH hàm đó, HOẶC nằm trong
`_ALLOWLIST` bên dưới kèm một marker `GRAPH-LINT-CONTRACT` giải thích tại sao ngoại lệ đó an toàn.

Cố ý dùng AST thay vì import + gọi thật: import các route module kéo theo `get_pool()`/DB wiring
không liên quan gì tới hợp đồng này, và giá trị của bài test nằm ở chỗ đảm bảo NGUỒN — không chỉ
những đường mà một bài test cụ thể tình cờ đi qua. Một route mới quên `graph_lint()`, hoặc một khúc
refactor đảo thứ tự hai lệnh gọi, phải làm bài này đỏ thay vì âm thầm mở lại finding C.

Câu hỏi tu từ ở mục 6 điều 2 của báo cáo thẩm định — "nếu ngày mai có người thêm đường thứ tư, thứ
gì sẽ chặn họ?" — câu trả lời cho ĐÚNG lớp rủi ro này (quên `graph_lint()` trước `interpreter.run()`)
chính là bài test này."""

from __future__ import annotations

import ast
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "studio_app"

_TRUST_MARKER = "GRAPH-LINT-CONTRACT"

# Ngoại lệ đã xét duyệt cho "graph_lint() phải chạy sớm hơn TRONG CHÍNH hàm gọi interpreter.run()".
# Mỗi mục ở đây BẮT BUỘC có marker `GRAPH-LINT-CONTRACT` trong file nguồn (kiểm bên dưới) giải
# thích vì sao ngoại lệ an toàn HÔM NAY và điều gì phải còn đúng để nó tiếp tục an toàn. Đừng thêm
# một mục vào đây trước khi viết marker đó.
_ALLOWLIST: set[tuple[str, str]] = {
    # `run_case` không tự gọi graph_lint(): recipe nó dùng hôm nay hoặc là fixture cố định
    # (`create_recipe_d4`, không bắt nguồn từ nodes/edges người dùng), hoặc là recipe tiêm từ
    # constructor mà đường sản xuất DUY NHẤT (`publish.py::_evaluate`) chưa thật sự truyền
    # (`kit#127`, còn mở). Chi tiết đầy đủ ở marker trong `eval_adapter.py`.
    ("eval_adapter.py", "EngineAgentRunner.run_case"),
}

# Biết trước hôm nay (báo cáo `kit#129` mục 3) — nếu số này rơi về 0 thì bản THÂN PHÉP QUÉT hỏng,
# không phải codebase sạch. Đây là SÀN, không phải TRẦN: thêm route thứ 4 làm đúng không cần sửa
# con số này.
_KNOWN_CALLSITE_FLOOR = 3


def _qualname(stack: list[str], name: str) -> str:
    return ".".join([*stack, name])


def _iter_functions(tree: ast.Module) -> list[tuple[str, ast.AST]]:
    """`(qualname, function-node)` cho mọi `def`/`async def` trong `tree`, cả top-level lẫn trong
    class — `qualname` kiểu `EngineAgentRunner.run_case` để thông báo lỗi trỏ đúng chỗ."""
    out: list[tuple[str, ast.AST]] = []

    def _visit(node: ast.AST, stack: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                _visit(child, [*stack, child.name])
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                out.append((_qualname(stack, child.name), child))
                _visit(child, [*stack, child.name])
            else:
                _visit(child, stack)

    _visit(tree, [])
    return out


def _calls(func_node: ast.AST, *, name: str | None = None, attr: str | None = None, on: str | None = None) -> list[ast.Call]:
    """Mọi `ast.Call` bên trong `func_node` khớp `name(...)` (lời gọi trần, vd `graph_lint(recipe)`)
    hoặc `on.attr(...)` (lời gọi thuộc tính, vd `interpreter.run(...)`)."""
    found: list[ast.Call] = []
    for node in ast.walk(func_node):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if name is not None and isinstance(fn, ast.Name) and fn.id == name:
            found.append(node)
        elif (
            attr is not None
            and isinstance(fn, ast.Attribute)
            and fn.attr == attr
            and (on is None or (isinstance(fn.value, ast.Name) and fn.value.id == on))
        ):
            found.append(node)
    return found


def _scan() -> list[tuple[str, str, ast.AST, str]]:
    """`(đường dẫn tương đối, qualname, function-node, source)` cho mọi hàm dưới `src/studio_app/`
    có gọi `interpreter.run(...)` ở đâu đó trong thân hàm."""
    hits: list[tuple[str, str, ast.AST, str]] = []
    for path in sorted(_SRC_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        rel = path.relative_to(_SRC_DIR).as_posix()
        for qualname, func_node in _iter_functions(tree):
            if _calls(func_node, attr="run", on="interpreter"):
                hits.append((rel, qualname, func_node, source))
    return hits


def test_interpreter_run_callsite_scan_finds_the_known_sites() -> None:
    """Sàn kiểm tra (mục 3): nếu số điểm gọi tìm được tụt dưới mức biết trước, hoặc phép quét AST
    hỏng (vd đổi tên biến `interpreter`/`graph_lint` làm mẫu không khớp nữa), hoặc một điểm gọi thật
    đã bị xoá — cả hai đều cần người đọc lại, không phải một bài test âm thầm rỗng rồi vẫn xanh."""
    hits = _scan()
    found = sorted(f"{rel}::{qualname}" for rel, qualname, *_ in hits)
    assert len(hits) >= _KNOWN_CALLSITE_FLOOR, (
        f"chỉ tìm thấy {len(hits)} điểm gọi interpreter.run() dưới src/studio_app/ (cần >= "
        f"{_KNOWN_CALLSITE_FLOOR}): {found}. Hoặc phép quét AST hỏng, hoặc một điểm gọi đã biến "
        "mất — cả hai cần người kiểm lại tay, đừng chỉnh con số sàn này cho qua."
    )


def test_every_interpreter_run_callsite_is_graph_linted_or_allowlisted() -> None:
    """Hợp đồng thật (mục 3): mọi hàm gọi `interpreter.run(...)` phải HOẶC tự gọi `graph_lint(...)`
    sớm hơn trong CHÍNH hàm đó, HOẶC là một ngoại lệ đã xét duyệt, có marker, trong `_ALLOWLIST`. Một
    route/adapter mới chạm `interpreter.run()` mà không qua một trong hai đường đó làm bài này đỏ
    thay vì âm thầm mở lại lỗ hổng DEC-A mà VinSOC finding C từng chỉ ra."""
    for rel, qualname, func_node, source in _scan():
        run_calls = _calls(func_node, attr="run", on="interpreter")
        first_run_line = min(c.lineno for c in run_calls)
        lint_calls = _calls(func_node, name="graph_lint")
        linted_before = any(c.lineno < first_run_line for c in lint_calls)

        if linted_before:
            continue

        if (rel, qualname) in _ALLOWLIST:
            assert _TRUST_MARKER in source, (
                f"{rel}::{qualname} nằm trong _ALLOWLIST nhưng file nguồn không còn chứa marker "
                f"'{_TRUST_MARKER}' giải thích vì sao ngoại lệ đó an toàn — lời giải thích đã bị xoá "
                "mà mục allowlist thì chưa. Viết lại marker, hoặc nếu ngoại lệ không còn đúng nữa "
                "thì nối graph_lint() vào và xoá mục này."
            )
            continue

        raise AssertionError(
            f"{rel}::{qualname} gọi interpreter.run() ở dòng {first_run_line} mà không có "
            "graph_lint() chạy trước trong cùng hàm, và không có trong _ALLOWLIST. Đây chính là "
            "kiểu lỗ hổng VinSOC finding C (kit#129) chỉ ra — hoặc thêm graph_lint(...) trước lệnh "
            "gọi interpreter.run(), hoặc thêm một mục _ALLOWLIST đã xét duyệt kèm marker "
            f"'{_TRUST_MARKER}' giải thích vì sao đường này an toàn không cần nó."
        )


def test_allowlist_entries_still_exist() -> None:
    """Một mục allowlist trỏ tới hàm không còn tồn tại (đổi tên, xoá, refactor đi chỗ khác) là một
    lỗ hổng âm thầm: nó cấp một ngoại lệ không ai dùng, và giấu đi việc logic thật đã chuyển chỗ mà
    chưa ai kiểm lại hợp đồng graph_lint ở chỗ mới."""
    found = {(rel, qualname) for rel, qualname, *_ in _scan()}
    for entry in _ALLOWLIST:
        assert entry in found, (
            f"_ALLOWLIST không còn khớp điểm gọi interpreter.run() nào cho {entry} — xoá mục này, "
            "hoặc tìm xem logic đã chuyển đi đâu và kiểm lại hợp đồng graph_lint ở chỗ mới."
        )
