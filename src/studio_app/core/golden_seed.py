"""Nạp các bộ golden đóng gói sẵn (`studio_kb/golden/*.yaml`) vào `eval.golden_sets`.

## Vì sao module này tồn tại

`routes/publish.py` trước đây đọc bộ case **thẳng từ đĩa** mỗi lần chấm (`_resolve_golden_set_path`
glob `studio_kb/golden/`, rồi `load_golden_set(path, expect_ref=…)`). Đường đó không có khái niệm
tenant: mọi tenant bị chấm bằng đúng một file, và một tenant tự nạp bộ case của mình qua
`POST /api/admin/golden-sets` (app#56) thì bộ đó **không bao giờ được dùng** để chấm.

Cutover đổi đường đọc sang `eval.golden_sets` (RLS theo tenant). Nhưng đổi mỗi đường đọc là chưa
đủ — bảng rỗng thì mọi tenant nhận `GoldenSetNotFound` và `/evaluate`/`/publish` vỡ ngay request
đầu tiên. Module này là **đường nạp** đóng nốt vòng đó: đưa các bộ đóng gói sẵn vào DB cho một
tenant cụ thể.

## Vì sao ở `apps/studio`, không phải `packages/kb` hay `packages/evalhub`

`DEC-D16-01`: composition root là nơi **duy nhất** được phép biết golden-set nằm ở đâu trên đĩa.
`packages/kb` sở hữu nội dung file, `packages/evalhub` sở hữu bảng và hàm ghi — nhưng chỗ biết
**cả hai** chỉ có thể là composition root. Hằng số `GOLDEN_SET_DIR` vì vậy dời từ
`routes/publish.py` sang đây: sau cutover, route không còn cần biết thư mục đó, chỉ đường nạp cần.

## Vì sao là hàm cho script gọi, không phải seed lúc boot

`write_golden_set` là **upsert** theo `(tenant_id, golden_set_ref)`. Nếu gọi trong lifespan, mỗi
lần backend khởi động lại sẽ ghi đè bộ case mà tenant đã tự nạp qua `POST /api/admin/golden-sets`
nếu ref trùng — dữ liệu người dùng biến mất vì một lần restart, không có thao tác nào của họ. Nạp
là việc **có chủ đích, chạy tay**, cùng khuôn `scripts/seed_demo_tenants.py`/`seed_superadmin.py`
vốn cũng ghi dữ liệu nghiệp vụ.

Hệ quả phải nói rõ: DB chưa seed thì `/evaluate`/`/publish` trả 400 kèm tên script cần chạy. Đó là
fail-closed có chỉ dẫn, không phải fallback im lặng về file — một fallback như vậy sẽ làm cutover
này vô nghĩa (đường đĩa vẫn sống) và giấu mất đúng trạng thái cần thấy.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import studio_kb
import yaml
from studio_evalhub.golden_case import GoldenSet
from studio_evalhub.golden_loader import load_golden_set
from studio_evalhub.golden_store import write_golden_set

# Cùng công thức `routes/publish.py::_GOLDEN_SET_DIR` đã dùng (kb#37/kit#181): đi ngược ĐÚNG 1 cấp
# từ `studio_kb/__init__.py`, cho cùng kết quả ở cả editable-install (`src/studio_kb/golden`) lẫn
# wheel (`site-packages/studio_kb/golden`) vì `golden/` là con trực tiếp của thư mục chứa
# `__init__.py` ở cả hai layout.
GOLDEN_SET_DIR = Path(studio_kb.__file__).resolve().parent / "golden"


def iter_bundled_golden_sets(golden_dir: Path | None = None) -> Iterator[GoldenSet]:
    """Duyệt mọi `*.yaml` trong thư mục golden, yield `GoldenSet` đã validate.

    File không parse được, hoặc parse ra thứ không phải mapping, hoặc không khai `golden_set_ref`
    dạng chuỗi — **bỏ qua**, không raise: thư mục này còn chứa file không phải bộ case
    (`embeddings-callisto-v0.json` là `.json` nên không dính glob, nhưng ràng buộc "chỉ có file bộ
    case" không phải bất biến được ai cưỡng chế). Ngược lại, một file *đúng là* bộ case nhưng nội
    dung hỏng thì `load_golden_set` ném nguyên `ValidationError` lên — nạp nửa vời một bộ case hỏng
    còn tệ hơn không nạp.

    **`expect_ref` ở đây cố ý vô hiệu** — nó lấy chính giá trị vừa đọc từ file làm kỳ vọng, nên phép
    đối chiếu trong `load_golden_set` không thể đỏ. Không phải sơ suất: đường nạp **không có** kỳ
    vọng độc lập nào về ref (việc của nó là "đưa những gì đóng gói sẵn vào DB", không phải "kiểm
    xem file có đúng bộ tôi định chấm không"). Phép đối chiếu đó kiếm được cơm ở **đường đọc** —
    `_evaluate` đọc theo `recipe.golden_set_ref`, và đó mới là một kỳ vọng thật sự độc lập với dữ
    liệu. Gọi `load_golden_set` thay vì `GoldenSet(**raw)` là để dùng phần validate còn lại của nó.
    """
    directory = GOLDEN_SET_DIR if golden_dir is None else golden_dir
    for path in sorted(directory.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        if not isinstance(raw, dict):
            continue
        ref = raw.get("golden_set_ref")
        if not isinstance(ref, str) or not ref:
            continue
        yield load_golden_set(path, expect_ref=ref)


async def seed_golden_sets(conn: Any, tenant_id: UUID, *, golden_dir: Path | None = None) -> tuple[str, ...]:
    """Nạp mọi bộ đóng gói sẵn vào `eval.golden_sets` cho `tenant_id`; trả về các ref đã nạp.

    Nhận `conn` chứ không phải `Pool` — cùng lý do `golden_store` nhận `conn`: `eval.golden_sets`
    bật RLS `FORCE`, nên connection phải đã bind `app.tenant_id` khớp `tenant_id`. Một connection
    mới lấy từ pool thì **chưa** bind, và dưới `FORCE` mọi câu sẽ lọc sạch trong im lặng. Bind là
    việc của caller (transaction-scoped `SET LOCAL`), đúng như route và middleware đang làm.

    Thứ tự ổn định theo tên file (`sorted` trong `iter_bundled_golden_sets`) để hai lần chạy trả về
    cùng một danh sách — script in ra danh sách này, và một thứ tự đổi ngẫu nhiên sẽ đọc như thể có
    gì đó vừa thay đổi.
    """
    refs: list[str] = []
    for golden in iter_bundled_golden_sets(golden_dir):
        await write_golden_set(conn, golden, tenant_id)
        refs.append(golden.golden_set_ref)
    return tuple(refs)
