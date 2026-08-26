"""`scripts/backfill_kb_search_whitelist.py` spec tests — review dholmes0207 trên PR script này
(F6, và gợi ý riêng: "nếu thêm đúng 1 test thì chọn bất biến dry-run không ghi gì — rẻ, và là chỗ
bug gây thiệt hại lớn nhất"). 0/7 script khác trong thư mục này có test — file này không đi ngược
tiền lệ, chỉ thêm đúng 1 script có rủi ro ghi dữ liệu thật khi chạy tay trên production.

Không cần Postgres thật: `_parse_execute_flag` là hàm thuần; `_backfill_table` chỉ cần 1
`AsyncConnection` giả ghi lại số lần `execute()` được gọi — dry-run đúng nghĩa là "chỉ SELECT, không
bao giờ UPDATE", nên đếm số lần gọi (1 vs 2) là bằng chứng đủ, không cần parse SQL text.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID

import pytest
from studio_contracts import AgentConfig, Dag, KbBinding, Node, NodeType, Recipe, ScorecardThreshold

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
# `scripts/` không phải package trong bất kỳ `src/` layout nào (mypy `strict=true` không resolve
# được qua sys.path runtime ở trên) — cùng khuôn `# type: ignore[import-untyped]` đã dùng ở
# `packages/engine/scripts/{run_golden_batch,embed_harness}.py` cho lý do tương tự.
from backfill_kb_search_whitelist import _backfill_table, _parse_execute_flag  # type: ignore[import-not-found]  # noqa: E402, I001

_TENANT = UUID("a0000000-0000-0000-0000-000000000001")


def test_parse_execute_flag_no_args_is_dry_run() -> None:
    assert _parse_execute_flag([]) is False


def test_parse_execute_flag_execute_flag_enables_execute() -> None:
    assert _parse_execute_flag(["--execute"]) is True


def test_parse_execute_flag_unknown_flag_raises_systemexit() -> None:
    """KHÓA review F6 — cờ gõ sai/lạ PHẢI dừng script rõ ràng, không được âm thầm rơi về dry-run."""
    with pytest.raises(SystemExit):
        _parse_execute_flag(["--exec"])
    with pytest.raises(SystemExit):
        _parse_execute_flag(["--execute=true"])
    with pytest.raises(SystemExit):
        _parse_execute_flag(["-e"])


def _recipe_needing_patch() -> Recipe:
    """Recipe có node `kb-retrieve` nhưng `tool_whitelist` thiếu `kb_search` — hình dạng MỌI recipe
    published trước workbench#57, đúng đối tượng script này tồn tại để vá."""
    return Recipe(
        agent_id="agent-1",
        tenant_id=_TENANT,
        agent_config=AgentConfig(
            system_prompt="Answer from KB only.", model="gpt-4o-mini", tool_whitelist=["calculator"]
        ),
        dag=Dag(nodes=[Node(id="n1", type=NodeType.KB_RETRIEVE, params={})], edges=[]),
        kb_binding=KbBinding(kb_id="kb-1", scope="ankor/public"),
        golden_set_ref="golden-set-1",
        scorecard_threshold=ScorecardThreshold(success=0.9, citation_accuracy=0.95),
    )


class _FakeCursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


class _FakeConn:
    """`AsyncConnection` giả — chỉ ghi lại MỖI lần `execute()` được gọi (query + params), không thật
    sự chạm DB nào. Luôn trả cùng 1 hàng SELECT cho mọi lần gọi (đủ dùng: `_backfill_table` chỉ
    `fetchall()` ngay sau lần gọi ĐẦU TIÊN — lần UPDATE, nếu có, không đọc lại cursor)."""

    def __init__(self, select_rows: list[tuple[object, ...]]) -> None:
        self.calls: list[tuple[object, object]] = []
        self._select_rows = select_rows

    async def execute(self, query: object, params: object = None) -> _FakeCursor:
        self.calls.append((query, params))
        return _FakeCursor(self._select_rows)


async def test_dry_run_never_calls_execute_a_second_time() -> None:
    """KHÓA bất biến quan trọng nhất theo chính lời reviewer: dry-run (`execute=False`) chỉ được
    gọi `conn.execute()` ĐÚNG 1 LẦN (SELECT đọc dòng) — không bao giờ có lần gọi thứ 2 (UPDATE) dù
    dòng đang xét thật sự cần vá."""
    recipe = _recipe_needing_patch()
    row = (
        "row-id-1",
        recipe.agent_id,
        1,
        recipe.model_dump(mode="json", by_alias=True),
    )
    conn = _FakeConn(select_rows=[row])

    stats = await _backfill_table(conn, "wb", "recipes", _TENANT, execute=False)

    assert stats.changed == 1  # xác nhận dòng THẬT SỰ cần vá — không phải bị bỏ qua ngoài ý muốn
    assert len(conn.calls) == 1  # chỉ đúng lần SELECT, không có UPDATE nào


async def test_execute_true_calls_execute_a_second_time_for_the_update() -> None:
    """Đối chứng — `execute=True` PHẢI gọi thêm 1 lần nữa (UPDATE) cho dòng cần vá, để bài trên thật
    sự phân biệt được 2 chế độ chứ không phải luôn == 1 vì lý do khác."""
    recipe = _recipe_needing_patch()
    row = (
        "row-id-1",
        recipe.agent_id,
        1,
        recipe.model_dump(mode="json", by_alias=True),
    )
    conn = _FakeConn(select_rows=[row])

    stats = await _backfill_table(conn, "wb", "recipes", _TENANT, execute=True)

    assert stats.changed == 1
    assert len(conn.calls) == 2  # SELECT + UPDATE


async def test_row_already_patched_is_noop_in_both_modes() -> None:
    """Dòng đã có `kb_search` sẵn (đã vá từ lượt trước, hoặc publish sau workbench#57) không được
    đếm vào `changed`, và dry-run lẫn execute đều chỉ gọi `execute()` đúng 1 lần (SELECT)."""
    recipe = _recipe_needing_patch()
    already_patched = recipe.model_copy(
        update={"agent_config": recipe.agent_config.model_copy(update={"tool_whitelist": ["kb_search", "calculator"]})}
    )
    row = ("row-id-2", already_patched.agent_id, 1, already_patched.model_dump(mode="json", by_alias=True))

    for execute in (False, True):
        conn = _FakeConn(select_rows=[row])
        stats = await _backfill_table(conn, "wb", "recipes", _TENANT, execute=execute)
        assert stats.changed == 0
        assert len(conn.calls) == 1


async def test_unparseable_row_is_skipped_not_raised() -> None:
    """KHÓA review F2 — 1 dòng lịch sử không parse được thành `Recipe` hợp lệ (giá trị ngoài khoảng
    ScorecardThreshold hiện tại chấp nhận, vd) bị đếm vào `skipped`, KHÔNG raise, không chặn các
    dòng khác trong cùng bảng."""
    recipe = _recipe_needing_patch()
    good_row = ("row-id-good", recipe.agent_id, 1, recipe.model_dump(mode="json", by_alias=True))
    bad_raw = recipe.model_dump(mode="json", by_alias=True)
    bad_raw["scorecard_threshold"]["success"] = -999  # ngoài khoảng ge=0.0 hiện tại — trước kit#129 vẫn ghi được
    bad_row = ("row-id-bad", recipe.agent_id, 2, bad_raw)
    conn = _FakeConn(select_rows=[good_row, bad_row])

    stats = await _backfill_table(conn, "wb", "recipes", _TENANT, execute=False)

    assert stats.scanned == 2
    assert stats.changed == 1  # dòng tốt vẫn được xử lý bình thường
    assert stats.skipped == 1  # dòng hỏng bị đếm riêng, không làm hàm raise
