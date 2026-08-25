"""`ExtractiveFakeLLM` (`providers/fakes.py`) — hành vi TOOL_CALL cho vòng lặp agent mới (app#44).

Trước app#44, `ExtractiveFakeLLM` chỉ đọc chunk ĐÃ CÓ SẴN trong prompt (kiến trúc DAG cũ chạy
`kb-retrieve` TRƯỚC `llm-step` vô điều kiện). Vòng lặp mới (`run_agent_loop`, engine#33) đòi LLM TỰ
PHÁT `TOOL_CALL:` để trigger `kb_search` — nếu double không phát tín hiệu đó, `kb_search` không bao
giờ chạy, mọi câu hỏi refuse dưới `STUDIO_USE_FAKE_PROVIDERS=true` (mặc định CI/demo), phá MONEY-SHOT.

3 bài dưới đây pin đúng 3 trạng thái `agent_loop.run_agent_loop()` thật sự đưa `complete()` vào,
dựng prompt bằng CHÍNH `build_agent_prompt`/`render_kb_observation` của engine (không tự chế format
tay — đúng nguyên tắc "đo bằng đường sinh thật", `test_stable_recipe_per_run.py` đã dùng)."""

from __future__ import annotations

from uuid import UUID

from studio_app.providers.fakes import ExtractiveFakeLLM
from studio_contracts import KbSearchResultItem
from studio_engine.agent_protocol import (
    KB_SEARCH_TOOL,
    FinalAnswer,
    Observation,
    ToolCall,
    build_agent_prompt,
    parse_agent_signal,
    render_kb_observation,
)

TENANT_ID = UUID("a0000000-0000-0000-0000-000000000001")


def _item(chunk_id: str, text: str) -> KbSearchResultItem:
    return KbSearchResultItem(chunk_id=chunk_id, text=text, score=0.9, tenant_id=TENANT_ID, section_role="public")


async def test_first_turn_emits_tool_call_kb_search_with_the_question_as_query() -> None:
    """Lượt 1 — chưa có observation nào trong prompt (`[Kết quả kb_search]` chưa xuất hiện) — phải
    phát ĐÚNG tín hiệu `TOOL_CALL:` mà `agent_protocol.parse_agent_signal` nhận ra, `tool="kb_search"`,
    `params["query"]` bằng đúng câu hỏi đã đưa vào `build_agent_prompt`."""
    question = "nghỉ phép cần báo trước bao lâu?"
    prompt = build_agent_prompt(system_prompt="", question=question, tool_names=[KB_SEARCH_TOOL], observations=[])

    raw = await ExtractiveFakeLLM().complete(prompt)
    signal = parse_agent_signal(raw)

    assert isinstance(signal, ToolCall), f"lượt 1 phải là ToolCall, thực tế: {signal!r}"
    assert signal.tool == KB_SEARCH_TOOL
    assert signal.params.get("query") == question


async def test_second_turn_answers_from_the_kb_observation_already_in_prompt() -> None:
    """Lượt 2 — sau khi `kb_search` đã trả về chunk thật (observation có mặt trong prompt), double
    phải đọc lại ĐÚNG chunk đó và trả lời trích dẫn — cùng chính sách cũ (chép đoạn trích ĐẦU TIÊN,
    trích đúng chunk_id của nó), chỉ khác ở CHỖ nó đọc (từ block `[Kết quả kb_search]`, không phải
    từ đầu prompt như trước app#44)."""
    question = "nghỉ phép cần báo trước bao lâu?"
    chunk = _item("ankor-leave-001#c1", "Báo trước 3 ngày làm việc.")
    obs = Observation(tool=KB_SEARCH_TOOL, params={"query": question}, result_text=render_kb_observation([chunk]))
    prompt = build_agent_prompt(system_prompt="", question=question, tool_names=[KB_SEARCH_TOOL], observations=[obs])

    raw = await ExtractiveFakeLLM().complete(prompt)
    signal = parse_agent_signal(raw)

    assert isinstance(signal, FinalAnswer), f"lượt 2 có chunk phải là FinalAnswer, thực tế: {signal!r}"
    assert "Báo trước 3 ngày làm việc." in signal.text
    assert "[ankor-leave-001#c1]" in signal.text


async def test_refuses_without_researching_again_when_kb_search_already_returned_empty() -> None:
    """Lượt 2 — `kb_search` đã chạy nhưng trả về 0 chunk (fence chặn sạch, đúng MONEY-SHOT) — double
    PHẢI trả lời từ chối NGAY, không được phát thêm `TOOL_CALL: kb_search` lần nữa (sẽ vòng lặp vô
    ích/không bao giờ dừng nếu mọi double đều làm vậy). Phân biệt với lượt 1 (chưa search) bằng sự
    HIỆN DIỆN của block `[Kết quả kb_search]` trong prompt, không chỉ bằng việc thiếu `[chunk_id]`."""
    question = "câu hỏi thuộc tenant khác?"
    obs = Observation(tool=KB_SEARCH_TOOL, params={"query": question}, result_text=render_kb_observation([]))
    prompt = build_agent_prompt(system_prompt="", question=question, tool_names=[KB_SEARCH_TOOL], observations=[obs])

    raw = await ExtractiveFakeLLM().complete(prompt)
    signal = parse_agent_signal(raw)

    assert isinstance(signal, FinalAnswer), (
        f"0 chunk sau khi đã search phải trả lời ngay (refuse), không search lại — thực tế: {signal!r}"
    )
    assert signal.text == "Không có đoạn trích nào để trả lời."
