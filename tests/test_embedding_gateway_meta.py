"""Anti-tamper source-level (app#30 Phase 7) — khuôn `packages/kb/tests/test_leak_meta.py`
(`docs/code-standards.md:103-105`). Đọc SOURCE TEXT, không import runtime — kiểm rằng hàng rào
CHƯA bị tháo, không kiểm hành vi. Tự nó luôn xanh sau khi implement."""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "studio_app"
_GATEWAY_SOURCE = (_SRC / "providers" / "embeddings.py").read_text(encoding="utf-8")
_GATEWAY_AST = ast.parse(_GATEWAY_SOURCE)


def _imported_module_names(tree: ast.Module) -> set[str]:
    """Tên module thật sự bị `import`/`from ... import` — dùng AST thay vì khớp chuỗi thô, vì
    docstring của chính module này nhắc tên `derive_vector`/`FastAPI` khi GIẢI THÍCH lý do KHÔNG
    dùng chúng (QĐ-6), và một check thô sẽ tự đỏ vào văn xuôi giải thích của chính mình."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


_GATEWAY_TEST_SOURCE = (Path(__file__).resolve().parent / "test_embedding_gateway.py").read_text(encoding="utf-8")
_ROUTE_FILES = ("runs.py", "publish.py", "chat.py", "documents.py")
_ROUTES_DIR = _SRC / "routes"


def test_no_derive_vector_in_gateway_source() -> None:
    """M3 chống-đột-biến: `providers/embeddings.py` KHÔNG BAO GIỜ import HAY gọi `derive_vector`
    — không nhánh fallback nào rơi về bag-of-words khi gateway lỗi (QĐ-6, fail-closed tuyệt đối).
    AST-based (không phải khớp chuỗi thô) vì docstring của chính module này nhắc tên
    `derive_vector` khi GIẢI THÍCH lý do không dùng nó."""
    # `alias.name` (tên NGUỒN, không phải `asname`) — review PR#32: `from studio_kb.embeddings
    # import derive_vector as _x` né được nếu check chỉ soi tên cục bộ sau `as`; tên nguồn thì
    # không né được kiểu đó.
    imported_names = {
        alias.name for node in ast.walk(_GATEWAY_AST) if isinstance(node, ast.ImportFrom) for alias in node.names
    }
    assert "derive_vector" not in imported_names

    called_names = {
        node.func.id
        for node in ast.walk(_GATEWAY_AST)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "derive_vector" not in called_names


def test_no_fastapi_in_gateway_source() -> None:
    """QĐ-6: module provider KHÔNG **import** FastAPI — coupling HTTP chỉ ở composition root
    (`factory.py`/`app.py`, Phase 4). AST-based vì docstring của chính module này nhắc tên
    FastAPI khi giải thích QĐ-6."""
    assert "fastapi" not in _imported_module_names(_GATEWAY_AST)


def test_normalize_assertion_still_present() -> None:
    """M1 chống-đột-biến: `test_embedding_gateway.py` còn chứa assertion trực tiếp `sum(x*x ...)`
    + `pytest.approx` — bài DUY NHẤT giết được mutation "bỏ L2-normalize" (cosine bất biến tỉ lệ,
    test retrieval không giết được nó)."""
    assert "sum(x * x for x in vectors[0])" in _GATEWAY_TEST_SOURCE
    assert "pytest.approx(1.0" in _GATEWAY_TEST_SOURCE


def test_index_reorder_assertion_still_present() -> None:
    """M2 chống-đột-biến: `test_reorders_by_response_index` còn tồn tại — bài giết mutation "bỏ
    sorted(data, key=index)"."""
    assert "def test_reorders_by_response_index" in _GATEWAY_TEST_SOURCE


def test_routes_free_of_stub_embedding() -> None:
    """4 route KHÔNG khớp `CallistoStubEmbedding` lẫn `derive_vector` — cùng hàng rào
    `test_routes_embedding_wiring.py::test_no_route_imports_stub_embedding` (P5), lặp lại ở đây
    theo đúng khuôn `test_embedding_gateway_meta.py` để mutation sweep P7 có 1 chỗ tập trung."""
    for name in _ROUTE_FILES:
        source = (_ROUTES_DIR / name).read_text(encoding="utf-8")
        assert "CallistoStubEmbedding" not in source
        assert "derive_vector" not in source
