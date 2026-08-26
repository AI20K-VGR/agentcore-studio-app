"""GATE-2 money-shot — publish chặn **vì `verdict`**, không vì `recipe_hash`, trên Postgres THẬT.

`umbrella-contract` bước 6/7 là ô demo của cả gate giữa kỳ:

    6 · verdict PASS → publish thành công
    7 · verdict FAIL → chặn publish + rollback

Đọc thứ tự 4 cổng trong `publish()` (`studio_workbench.publish`, neo bằng TÊN ĐIỀU KIỆN chứ không
phải số dòng — số dòng trôi mỗi lần file đó đổi, và đã từng trôi thật ở chính docstring này, xem
review `app#26` 🟡):

    1. enforce_agent_shape(recipe) + enforce_agent_topology(recipe)  raise ValueError(… lỗi lint …)
       (app#78/workbench#48 — trước đây 1 `graph_lint(recipe)`, nay tách 2 hàm)
    2. scorecard.recipe_hash is None                    raise ValueError(… "recipe_hash" …)
    3. scorecard.recipe_hash != recipe_hash(recipe)     raise ValueError(… "chứng nhận một recipe KHÁC" …)
    4. scorecard.gate.verdict == "FAIL"                 await _reassert_last_published(…) rồi
                                                          raise ValueError(… "verdict" …)

**`DEC-03` giờ ĐÃ có producer** (`studio_workbench.publish.recipe_hash()`, review `workbench#27`) —
3 bài dưới đây từng ghim trạng thái "trước-producer" (mọi `publish()` đều bị chặn ở cổng 2 vì
`compute_scorecard` mặc định `recipe_hash=None`, nên bước 6/7 của money-shot chưa từng chạy tới
`gate.verdict` thật). Giờ caller (bài test, và thật hơn là `apps/studio/routes/publish.py`) TỰ băm
`recipe_hash(recipe)` rồi truyền vào — cổng 2/3 đi qua được, và cổng 4 (verdict) mới là thứ quyết
định bước 6/7, đúng ý nghĩa gốc của money-shot.

Bước 7 là ô dễ tuyên bố nhất của cả gate (*"đã chặn đấy thôi"*), và trước bản vá này nó xanh vì một
lý do khác với lý do được demo (chặn ở cổng 2, không phải cổng 4). File này là chỗ phân biệt hai lý
do đó bằng phép đo — vẫn giữ nguyên giá trị dù cổng 2 giờ đã đi qua được, vì nó khoá đúng **cổng nào
thật sự quyết định** bước 7, không phải chỉ "có bị chặn hay không".

## Vì sao ba bài này KHÔNG trùng `workbench/tests/test_publish.py`

SWE đã khoá **logic nhánh** ở đó, và khoá đúng — kể cả `match=` để hai `ValueError` phân biệt được.
Nhưng 6/8 bài cổng của SWE chạy trên `FakeConn` (double in-memory), còn hai bài dùng `pool` chỉ đo
**RLS**. File này thêm ba thứ mà một double không dựng được:

1. **`Scorecard` đến từ `compute_scorecard` thật**, không dựng tay — nên bài 1 ghim đúng **hành vi
   thật** của đường sinh khi caller KHÔNG truyền `recipe_hash` (mặc định vẫn `None`, `DEC-D20-02`
   không đổi bởi việc `DEC-03` có producer — producer là 1 hàm caller CÓ THỂ gọi, không phải giá
   trị mặc định mới của `compute_scorecard`), chứ không phải một giả định về nó.
2. **Rollback để lại row thật trong `wb.recipes`/`wb.recipe_versions`** — `_reassert_last_published`
   chạy hay không đọc được từ dữ liệu, không từ trạng thái của một object trong RAM.
3. **RLS đang bật trên `wb.recipes`** ⇒ `publish()` phải chạy trong transaction có
   `app.tenant_id`. Một double bỏ qua tầng đó.

## `DEC-D20-02` — hash ở đây DÙNG PRODUCER THẬT từ workbench

Đã có producer `recipe_hash` thật từ `studio_workbench.publish`, cổng 2 (`is None`) và cổng 3
(`!= recipe_hash(recipe)`) bây giờ sẽ đối chiếu hash thật sự. Ta dùng `_recipe_hash` để băm trực
tiếp `Recipe` ở bài test để qua được 2 cổng đó, chứng minh đường publish chạy trót lọt khi hash khớp.

**Không uốn run thật của T3 để ra `PASS`** (`DEC-D20-03` ranh giới 4): nhánh `PASS` ở bài 3 là một
`Scorecard` dựng từ `CaseResult` fixture có nhãn, không phải một run được hiệu chỉnh cho đẹp.

Cần Postgres — thiếu DSN ⇒ fixture `pool` skip, cùng hành vi mọi bài DB khác.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from studio_app.eval_adapter import _CERTIFIED_EDGES, _CERTIFIED_NODES
from studio_contracts import CaseResult, Recipe, Scorecard
from studio_evalhub.compute import compute_scorecard
from studio_workbench import create_recipe
from studio_workbench.publish import publish
from studio_workbench.publish import recipe_hash as _recipe_hash

ANKOR_ID = UUID("a0000000-0000-0000-0000-000000000001")

_THRESHOLD_SUCCESS = 0.9
_THRESHOLD_CITATION = 0.95


def _recipe(agent_id: str) -> Recipe:
    """Recipe thật qua `create_recipe`, DAG **import trực tiếp** từ `eval_adapter.py::
    _CERTIFIED_NODES`/`_CERTIFIED_EDGES` — cùng hằng số `EngineAgentRunner.certified_recipe` tự
    dựng ở nhánh không tiêm `recipe=` (workbench#41 — `create_recipe_d4` đã bị xoá). Import thay vì
    chép tay: khẳng định "cùng DAG với đường thật" được `import` ép buộc, không chỉ là lời hứa
    trong docstring — ngày `certified_recipe()` đổi DAG, bài này tự động đổi theo, không cần ai
    nhớ sửa 2 chỗ. Nhờ vậy bài này đi qua `enforce_agent_shape`/`enforce_agent_topology` y như đường
    thật (app#78/workbench#48 — trước đây `graph_lint`)."""
    return create_recipe(
        agent_id=agent_id,
        tenant_id=ANKOR_ID,
        system_prompt="Tra cứu quy trình và bảo mật Callisto.",
        tool_whitelist=[],
        nodes=_CERTIFIED_NODES,
        edges=_CERTIFIED_EDGES,
    )


def _scorecard(*, verdict: str, recipe_hash: str | None, agent_id: str) -> Scorecard:
    """`Scorecard` sinh bằng **`compute_scorecard` thật**, không dựng tay.

    Đây là điểm khác biệt so với `test_publish.py` của SWE: nối bài này vào **đường sinh** nên nó đo
    được cả `DEC-D20-02` (giá trị caller đưa đi thẳng qua) lẫn hành vi cổng, thay vì hai thứ tách rời.

    `verdict` điều khiển bằng **dữ liệu**, không bằng một field gán tay: `success`/`citation_accuracy`
    `1.0` ⇒ vượt cả hai ngưỡng ⇒ `PASS`; `0.0` ⇒ `FAIL`. Nếu `compute_scorecard` ngừng là một hàm của
    dữ liệu, bài này đỏ — đúng thứ `M-G1` đi tìm."""
    tot = verdict == "PASS"
    diem = 1.0 if tot else 0.0
    sc = compute_scorecard(
        agent_id,
        "callisto-golden-30-v1",
        [CaseResult(case_id="c1", expected="x", actual="x", success=tot, citation_accuracy=diem)],
        _THRESHOLD_SUCCESS,
        _THRESHOLD_CITATION,
        scored_case_ids={"c1"},
        recipe_hash=recipe_hash,
    )
    assert sc.gate.verdict == verdict, f"fixture tự mâu thuẫn: muốn {verdict}, compute ra {sc.gate.verdict}"
    return sc


async def _bind_tenant(conn: Any, tenant_id: UUID) -> None:
    await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))


async def _rows(pool: Any, agent_id: str) -> list[tuple[int, str]]:
    async with pool.connection() as conn, conn.transaction():
        await _bind_tenant(conn, ANKOR_ID)
        cur = await conn.execute(
            "SELECT version, status FROM wb.recipes WHERE agent_id = %s AND tenant_id = %s ORDER BY version",
            (agent_id, ANKOR_ID),
        )
        return [(int(v), str(s)) for v, s in await cur.fetchall()]


async def test_bai1_scorecard_hom_nay_bi_chan_o_cong_recipe_hash(pool: Any) -> None:
    """**Bài 1 — ghim trạng thái hôm nay để nó không trôi trong im lặng.**

    `compute_scorecard` **không truyền** `recipe_hash` ⇒ `None` ⇒ `publish()` từ chối ở cổng
    `recipe_hash is None`, **trước khi đọc verdict**. Đây là lý do bước 6 của money-shot không chạy
    được hôm nay.

    Assert **chuỗi thông điệp**, không chỉ `pytest.raises(ValueError)`: cả hai cổng raise **cùng một
    kiểu**, nên một bài chỉ bắt kiểu sẽ **xanh với cả hai lý do** — đúng lớp lỗi cả tuần đi tìm, và
    đúng chỗ ô demo bước 7 đang xanh vì lý do sai.

    `verdict="PASS"` có chủ đích: nó chứng minh cổng `recipe_hash is None` chặn **kể cả khi verdict hoàn toàn đạt**,
    tức thứ chặn thật sự là `recipe_hash` chứ không phải chất lượng bản build. Bài này KHÔNG bị ảnh
    hưởng bởi việc `DEC-03` có producer hay không — nó cố tình gọi `_scorecard(recipe_hash=None,
    ...)`, tức tự khai KHÔNG truyền giá trị, nên vẫn ghim đúng gate `is None` bất kể producer đã
    tồn tại ở nơi khác trong hệ thống.

    **`agent_id` cố tình không chứa chuỗi `hash` hay `verdict`**, và `match=` neo vào cụm **chỉ có ở
    thông điệp của cổng** (`scorecard.recipe_hash is None`), không phải một từ đơn. Lý do đo được:
    `publish()` nội suy `agent_id` vào **cả hai** thông điệp, nên một `agent_id` mang tên nhánh sẽ làm
    `match=` khớp vào chính cái tên mình đặt thay vì vào lý do chặn — bài xanh, và xanh vì đọc nhầm
    chỗ. Đúng lớp lỗi D19 số 2."""
    agent_id = "gate2-case1-no-provenance"
    scorecard = _scorecard(verdict="PASS", recipe_hash=None, agent_id=agent_id)
    assert scorecard.recipe_hash is None, "compute_scorecard mặc định phải là None (DEC-D20-02)"

    with pytest.raises(ValueError, match=r"scorecard\.recipe_hash is None") as bat:
        async with pool.connection() as conn, conn.transaction():
            await _bind_tenant(conn, ANKOR_ID)
            await publish(_recipe(agent_id), scorecard, conn)

    # Thông điệp KHÔNG được nhắc verdict — nếu nó nhắc, hai lý do không còn phân biệt được và bài 2
    # mất hết giá trị.
    assert "verdict" not in str(bat.value)
    assert await _rows(pool, agent_id) == []


async def test_bai2_verdict_fail_chan_va_rollback_that_su_chay(pool: Any) -> None:
    """**Bài 2 — bước 7 money-shot, lần đầu chạy ĐÚNG LÝ DO.**

    `recipe_hash(recipe)` băm THẬT, khớp đúng recipe đang publish ⇒ qua được cổng `recipe_hash is
    None`/`recipe_hash != recipe_hash(recipe)` ⇒ cổng `verdict == "FAIL"` mới là thứ chặn, và
    `_reassert_last_published` **thật sự chạy**.

    Đo rollback bằng **dữ liệu trong `wb.recipes`**, không bằng *"không có row mới"*: seed v1
    `published` trước, rồi thử publish một bản `FAIL`. Nếu chỉ assert *"không có v2"* thì một cài đặt
    `return` sớm trước `_reassert_last_published` cũng xanh — mà đó chính xác là hành vi hôm nay đang
    xảy ra vì cổng `recipe_hash is None` bắn trước. Assert v1 vẫn `published` là thứ phân biệt
    *"chặn rồi bỏ đấy"* với *"chặn rồi khôi phục bản đã biết là tốt"*.

    Hai assert chuỗi, đối xứng với bài 1: thông điệp phải nhắc **lý do verdict** và **không** được
    nhắc `recipe_hash`. `match=` neo vào `gate.verdict='FAIL'` — cụm chỉ có ở thông điệp cổng verdict —
    chứ không vào từ `verdict` trần: `publish()` nội suy `agent_id` vào thông điệp, nên một
    `agent_id` mang tên nhánh sẽ làm `match=` khớp vào chính cái tên mình đặt. Đo được: bản đầu của
    bài này đặt `agent_id="gate2-bai2-verdict-fail"` và `match="verdict"` **vẫn khớp** khi gieo
    `M-G4`, tức cổng đã chặn sai chỗ mà `pytest.raises` không hề thấy. Thứ giết `M-G4` là assert phủ
    định bên dưới, không phải `match=`."""
    agent_id = "gate2-case2-blocked-branch"
    recipe = _recipe(agent_id)
    r_hash = _recipe_hash(recipe)

    # Seed: một bản PASS đã publish — rollback cần một thứ để khôi phục VỀ.
    async with pool.connection() as conn, conn.transaction():
        await _bind_tenant(conn, ANKOR_ID)
        await publish(recipe, _scorecard(verdict="PASS", recipe_hash=r_hash, agent_id=agent_id), conn)
    assert await _rows(pool, agent_id) == [(1, "published")]

    with pytest.raises(ValueError, match=r"gate\.verdict='FAIL'") as bat:
        async with pool.connection() as conn, conn.transaction():
            await _bind_tenant(conn, ANKOR_ID)
            await publish(
                recipe,
                _scorecard(verdict="FAIL", recipe_hash=r_hash, agent_id=agent_id),
                conn,
            )

    assert "recipe_hash" not in str(bat.value), (
        "Thông điệp chặn phải nói VERDICT. Nếu nó nói recipe_hash thì bước 7 vẫn đang chặn vì lý do sai."
    )
    # v2 không bao giờ được ghi, và v1 vẫn đứng `published` ⇒ rollback đã chạy.
    assert await _rows(pool, agent_id) == [(1, "published")]


async def test_bai3_verdict_pass_publish_thanh_cong(pool: Any) -> None:
    """**Bài 3 — bước 6 money-shot.** `recipe_hash` thật (khớp đúng recipe) + `verdict="PASS"` ⇒
    publish thành công, `wb.recipes` có row `status='published'`.

    Đây là **đối chứng dương** của hai bài trên, và nó là thứ làm chúng có nghĩa: một `publish()` từ
    chối **mọi thứ** cũng làm bài 1 và bài 2 xanh. Không có bài này thì hai bài kia không phân biệt
    được *"cổng chặn đúng"* với *"cổng chặn tất"*.

    Cũng đo được `DEC-D20-02` đi trọn vòng: chuỗi hash do caller đưa vào `compute_scorecard` là
    đúng chuỗi `publish()` đọc ở 2 cổng `recipe_hash` — không có ai băm lại ở giữa."""
    agent_id = "gate2-case3-published"
    recipe = _recipe(agent_id)
    r_hash = _recipe_hash(recipe)
    scorecard = _scorecard(verdict="PASS", recipe_hash=r_hash, agent_id=agent_id)
    assert scorecard.recipe_hash == r_hash, "giá trị caller đưa phải đi thẳng, không bị dẫn xuất lại"

    async with pool.connection() as conn, conn.transaction():
        await _bind_tenant(conn, ANKOR_ID)
        await publish(recipe, scorecard, conn)

    assert await _rows(pool, agent_id) == [(1, "published")]
