"""Mặc định `RunRequest.golden_set_ref` — PR-4 của cutover Callisto 2.0 (lane AIE-2).

Hai bài, hai hỏng khác nhau, không bài nào thay được bài kia:

- bài đầu ghim **giá trị**: đây là chỗ duy nhất production chọn bộ golden, và một lần trôi ngược
  về `callisto-golden-30-v1` sẽ làm mọi lượt chấm đo trên corpus 1.0 (140 chunk) trong khi
  `kb.chunks` đã là corpus 2.0 (800 chunk) — số vẫn ra, chỉ là số của một thế giới khác;
- bài sau ghim **ref phải resolve được thành file**, và đó là bài đáng giá hơn: `routes/publish.py`
  dựng đường dẫn bằng `_GOLDEN_SET_DIR / f"{ref}.yaml"`, nên bất kỳ ref nào không trùng TÊN FILE
  là 400 vĩnh viễn. Lớp lỗi này đang sống thật ở `apps/web` (`recipe/sample.ts` gửi
  `callisto-smoke-5-v0`, mà file là `smoke-5.yaml` — nút "Chấm điểm" 400 ngay lần bấm đầu). Không
  có bài nào ở repo này canh nó cho phía server.
"""

from __future__ import annotations

from studio_app.routes.publish import _GOLDEN_SET_DIR, PublishRequest


def test_mac_dinh_la_bo_golden_2_0() -> None:
    """Bộ 2.0 là bộ khớp corpus mà `packages/kb` đang ingest (`kb#32` corpus, `kb#43` embedding).

    app#44: field này dời từ `routes/runs.py::RunRequest` (đã đổi shape, mục D) sang
    `routes/publish.py::PublishRequest` (`/evaluate`/`/publish`, không đổi scope)."""
    assert PublishRequest.model_fields["golden_set_ref"].default == "callisto-2.0-golden-30-v1"


def test_ref_mac_dinh_resolve_duoc_thanh_file_that() -> None:
    """`ref` mặc định PHẢI có file tương ứng trong thư mục golden đã đóng gói.

    `_evaluate` (`routes/publish.py`) trả 400 khi `_GOLDEN_SET_DIR / f"{ref}.yaml"` không tồn tại —
    không phải 500, không phải fallback: route đơn giản là không chạy được. Bài này biến "ref gõ sai
    / ref không phải tên file" từ lỗi runtime của người dùng thành lỗi CI của người sửa mặc định.
    """
    ref = PublishRequest.model_fields["golden_set_ref"].default
    path = _GOLDEN_SET_DIR / f"{ref}.yaml"
    assert path.is_file(), (
        f"golden_set_ref mặc định {ref!r} không có file tương ứng ở {path} — "
        f"mọi lời gọi /evaluate và /publish không khai ref sẽ 400. "
        f"File hiện có: {sorted(p.name for p in _GOLDEN_SET_DIR.glob('*.yaml'))}"
    )
