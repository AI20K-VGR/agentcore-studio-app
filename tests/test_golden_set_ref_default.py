"""Mặc định `RunRequest.golden_set_ref` — PR-4 của cutover Callisto 2.0 (lane AIE-2).

Hai bài, hai hỏng khác nhau, không bài nào thay được bài kia:

- bài đầu ghim **giá trị**: đây là chỗ duy nhất production chọn bộ golden, và một lần trôi ngược
  về `callisto-golden-30-v1` sẽ làm mọi lượt chấm đo trên corpus 1.0 (140 chunk) trong khi
  `kb.chunks` đã là corpus 2.0 (800 chunk) — số vẫn ra, chỉ là số của một thế giới khác;
- bài sau ghim **ref mặc định phải nạp được vào DB**, và đó là bài đáng giá hơn. Cutover đổi
  `routes/publish.py` sang đọc `eval.golden_sets` thay vì đĩa, nên "ref không resolve được thành
  file" không còn là 400 ở đường ĐỌC — nhưng nó dời nguyên vẹn sang đường NẠP: `seed_golden_sets`
  chỉ nạp được thứ có file, và ref mặc định không có file thì DB không bao giờ có dòng đó, và
  `/evaluate` 400 vĩnh viễn y như trước, chỉ khác chỗ phát sinh. Lớp lỗi này đang sống thật ở
  `apps/web` (`recipe/sample.ts` gửi `callisto-smoke-5-v0`, mà file là `smoke-5.yaml`). Không có
  bài nào khác ở repo này canh nó cho phía server.
"""

from __future__ import annotations

from studio_app.core.golden_seed import GOLDEN_SET_DIR, iter_bundled_golden_sets
from studio_app.routes.publish import PublishRequest


def test_mac_dinh_la_bo_golden_2_0() -> None:
    """Bộ 2.0 là bộ khớp corpus mà `packages/kb` đang ingest (`kb#32` corpus, `kb#43` embedding).

    app#44: field này dời từ `routes/runs.py::RunRequest` (đã đổi shape, mục D) sang
    `routes/publish.py::PublishRequest` (`/evaluate`/`/publish`, không đổi scope)."""
    assert PublishRequest.model_fields["golden_set_ref"].default == "callisto-2.0-golden-30-v1"


def test_ref_mac_dinh_nap_duoc_vao_db() -> None:
    """`ref` mặc định PHẢI nằm trong số bộ mà `seed_golden_sets` nạp được.

    Khớp theo **ref khai bên trong file**, không theo tên file — cùng bất biến `DEC-D16-01` mà
    `load_golden_set` bảo vệ, và cũng là thứ `_resolve_golden_set_path` (đã xoá) từng làm. Suy từ
    tên file sẽ đúng hôm nay (tên trùng ref) và sai ngày ai đó đổi tên file, mà không dòng code nào
    phải đổi để lỗi xuất hiện.

    Bài này biến "ref mặc định không có bộ tương ứng" từ lỗi runtime của người dùng — 400 ở request
    `/evaluate` đầu tiên sau khi seed — thành lỗi CI của người sửa mặc định."""
    ref = PublishRequest.model_fields["golden_set_ref"].default
    nap_duoc = [g.golden_set_ref for g in iter_bundled_golden_sets()]
    assert ref in nap_duoc, (
        f"golden_set_ref mặc định {ref!r} không có bộ nào khai ref đó trong {GOLDEN_SET_DIR} — "
        f"`seed_golden_sets` sẽ không nạp nó, và mọi lời gọi /evaluate hay /publish không khai ref "
        f"sẽ 400. Ref nạp được hiện có: {nap_duoc}"
    )
