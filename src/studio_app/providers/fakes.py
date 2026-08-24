"""CI-fixture provider doubles — deterministic, hash-seeded. NO class here is a deliverable.

`FakeLLM` — CI fixture — KHÔNG phải deliverable AIE-1. A throwaway `LLM` double so `llm-step`
wiring can be exercised in CI without a live Gemini key.

`ExtractiveFakeLLM` — CI fixture — reads the PROMPT and nothing else. Same family as `FakeLLM`,
but useful where `FakeLLM`'s hash output is too blind to score (see its own docstring).

`FakeEmbedding` — CI fixture — KHÔNG phải deliverable AIE-1. A test-double for `EmbeddingService`
ONLY (F3) — the graded 2-impl `EmbeddingService` (stub-local + gateway, R-SPEC A1#5/A4) is AIE-1's
own deliverable; this class does NOT count toward that 2-impl requirement and must never be
mistaken for one.

Both are deterministic (same input -> same output, hash-seeded `[ASSUMED]`) so CI stays
reproducible — no wall-clock, no `random` module anywhere in this file.
"""

from __future__ import annotations

import hashlib
import json
import re

from studio_engine.agent_protocol import KB_SEARCH_TOOL, TOOL_CALL_PREFIX


class FakeLLM:
    """CI fixture — KHÔNG phải deliverable AIE-1."""

    async def complete(self, prompt: str, **kwargs: object) -> str:
        del kwargs  # accepted for Protocol-shape parity; the fake ignores generation params
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        return f"fake-completion:{digest}"


class ExtractiveFakeLLM:
    """CI fixture — an `LLM` double that reads ONLY the prompt and never the golden set.

    Ý gốc của **DE (@DongAnh2704)** trong `scripts/smoke_eval_d6.py` (`_NaiveExtractiveLLM`), đưa
    vào đây để cả test lẫn script e2e dùng chung một bản, thay vì mỗi chỗ chép một lần.

    **app#44 — tool-calling awareness cho `run_agent_loop()` (engine#33).** Trước app#44, class này
    chỉ đọc chunk ĐÃ CÓ SẴN trong prompt: kiến trúc DAG cũ (`interpreter.run()`) chạy `kb-retrieve`
    TRƯỚC `llm-step` vô điều kiện nên `complete()` không bao giờ cần TỰ quyết định có tra cứu hay
    không. Vòng lặp mới đòi chính LLM phát `TOOL_CALL: {"tool":"kb_search",...}` để trigger tra cứu
    — không phát thì `kb_search` không bao giờ chạy. `complete()` giờ tự phân biệt 3 trạng thái bằng
    CHÍNH prompt nhận được (không giữ state ngoài lời gọi — mỗi lượt của vòng lặp gọi lại với TOÀN
    BỘ prompt tích luỹ, đúng hợp đồng `LLM.complete`):

    1. **Chưa search** (block `[Kết quả kb_search]` — do `agent_protocol.build_agent_prompt` render
       cho mỗi `Observation` — chưa xuất hiện trong prompt): phát `TOOL_CALL: {"tool": "kb_search",
       "params": {"query": <câu hỏi>}}`, câu hỏi lấy từ dòng `"Câu hỏi: "` cuối prompt (marker
       `_QUESTION_MARKER` đã có sẵn).
    2. **Đã search, có chunk** (block đó hiện diện VÀ chứa ít nhất 1 dòng `[chunk_id]`): hành vi CŨ
       nguyên vẹn — chép đoạn trích đầu tiên tìm thấy, trích đúng `chunk_id` của nó. Vẫn dùng đúng
       `_EXCERPT_RE` hiện có: regex đó không khớp `[Kết quả kb_search]` (có khoảng trắng, ngoài lớp
       `[\\w#-]+`) nên không lẫn header với chunk thật — xác nhận bằng test
       `test_fakes_extractive_tool_calling.py`.
    3. **Đã search, 0 chunk** (block hiện diện nhưng KHÔNG có dòng `[chunk_id]` nào — fence chặn
       sạch, đúng ca MONEY-SHOT cross-tenant): trả lời từ chối NGAY (câu cũ, không đổi), KHÔNG phát
       `TOOL_CALL:` lần 2 — nếu không phân biệt được (1) và (3), double sẽ search lại vô hạn cho tới
       khi `run_agent_loop` hết `max_turns` (`AgentLoopExhausted`) thay vì trả lời/từ chối gọn.

    An toàn cho MỌI caller hiện có: đã grep xác nhận toàn bộ nơi dùng class này trong `apps/studio`
    (11 file, kể cả `scripts/e2e_smoke_eval.py`) đều đi qua `EngineAgentRunner.run_case()`
    (`eval_adapter.py`, giờ gọi `run_agent_loop()`), KHÔNG nơi nào gọi `interpreter.run()` (DAG-walk
    cũ) trực tiếp với double này — nên sửa hành vi ngay trong class thay vì tạo class song song
    không phá đường DAG-walk cũ (đường đó không dùng `ExtractiveFakeLLM`).

    Vì sao nó cần tồn tại cạnh `FakeLLM` và cạnh câu trả lời recorded — cả hai đầu kia đều **không
    đo được chất lượng**, theo hai chiều ngược nhau:

    - `FakeLLM` trả `fake-completion:<sha256>`. Không bao giờ có `[chunk_id]`, nên
      `LlmStepExecutor` luôn thấy `citations == []` và (từ Day 6, `refused = not citations`) luôn
      kết luận `refused=True`. Mọi case đều rơi vào nhánh từ-chối — đo được dây nối, không đo được
      câu trả lời.
    - Câu trả lời **recorded** per-case thì ngược lại: nó được soạn cho khớp `expected`, nên điểm
      luôn tuyệt đối. Đó là điều DE phê bình đúng — *"5 câu trả lời soạn cho khớp `expected`, luôn
      ra điểm tuyệt đối và chứng minh số không"*.

    Chính sách của class này: **lấy đoạn trích ĐẦU TIÊN trong prompt, chép lại, trích đúng
    `chunk_id` của nó.** Hành vi của một model ngoan mà kém — đọc thứ được đưa, không suy luận,
    không xếp hạng lại, không tự quyết có nên từ chối.

    Vì sao đây KHÔNG phải gian lận: nó không hề thấy `expected` hay `expected_citation`. `chunk_id`
    nó trích đến từ **dữ liệu KB vừa truy xuất** (do `studio_engine.executors.build_prompt` render
    vào prompt), không từ người viết fixture. Đổi kho tài liệu hoặc đổi công thức xếp hạng thì câu
    trả lời tự đi theo.

    Hai chỗ nó kém là **cố ý**, và chỗ nó fail chỉ ra đúng giới hạn thật của hệ thống:

    - **Chỉ đọc top-1.** Đáp án nằm ở hạng 2–3 thì trượt. Một model thật đọc cả `top_k` đoạn.
    - **Không biết từ chối.** Truy xuất ra chunk nào là chép chunk đó, kể cả khi không liên quan
      tới câu hỏi — nên case đòi từ-chối mà retrieval KHÔNG rỗng sẽ đỏ. Một model thật đọc header
      prompt, nơi đã dặn *"nếu các đoạn trích không chứa câu trả lời … KHÔNG trích dẫn gì"*.

    Nói cách khác điểm của nó là **cận dưới** của hệ thống với một model tệ nhất còn đọc được,
    không phải điểm của hệ thống. Tất định (thuần hàm của prompt) nên CI tái lập được — không
    wall-clock, không `random`.
    """

    # `[chunk_id]` đứng MỘT MÌNH trên một dòng — đúng khuôn `build_prompt` render ra. Bám khuôn
    # này là một phụ thuộc có thật vào engine: nếu `build_prompt` đổi layout thì class này mù trở
    # lại (trả câu "không có đoạn trích"), và test `test_prompt_carries_retrieved_chunk_ids` là
    # chỗ sẽ đỏ để báo.
    _EXCERPT_RE = re.compile(r"^\[([\w#-]+)\]\n", re.MULTILINE)
    _QUESTION_MARKER = "\n\nCâu hỏi:"
    # app#44 — nhãn header block quan sát `agent_protocol.build_agent_prompt` render cho MỖI
    # `Observation` (`f"[Kết quả {obs.tool}]\n{obs.result_text}"`). Có khoảng trắng nên KHÔNG khớp
    # `_EXCERPT_RE` (`[\w#-]+` không chứa khoảng trắng) — dùng làm cờ phân biệt "đã search" (1) khỏi
    # "chưa search" (2)/(3) trong module docstring của class này.
    _KB_OBSERVATION_HEADER = "[Kết quả kb_search]"

    def __init__(self) -> None:
        self.prompts_seen: list[str] = []

    def _question_from_prompt(self, prompt: str) -> str:
        """Lấy lại câu hỏi từ dòng `"Câu hỏi: "` cuối prompt (marker đã có sẵn cho việc cắt đoạn
        trích ở dưới) — dùng làm `params["query"]` khi phát `TOOL_CALL: kb_search`. Không tìm thấy
        marker (prompt không đúng khuôn `build_agent_prompt`) → trả nguyên `prompt`, fail-soft: một
        query rỗng/sai vẫn tốt hơn để `TOOL_CALL:` vỡ JSON."""
        marker_start = prompt.rfind(self._QUESTION_MARKER)
        if marker_start == -1:
            return prompt
        return prompt[marker_start + len(self._QUESTION_MARKER) :].strip()

    async def complete(self, prompt: str, **kwargs: object) -> str:
        del kwargs  # accepted for Protocol-shape parity
        self.prompts_seen.append(prompt)

        already_searched = self._KB_OBSERVATION_HEADER in prompt
        marks = list(self._EXCERPT_RE.finditer(prompt))
        if not marks:
            if not already_searched:
                # app#44 (1) — vòng lặp mới (run_agent_loop) đòi LLM TỰ phát tín hiệu để trigger
                # kb_search; kiến trúc DAG cũ chạy nó vô điều kiện nên trước app#44 chỗ này luôn là
                # "0 đoạn trích ⇒ từ chối". Câu hỏi lấy lại từ chính prompt, không giữ state riêng.
                # `json.dumps` (không f-string tay) — payload phải là JSON hợp lệ mà
                # `agent_protocol.parse_agent_signal` parse được (P1/P3), không phải Python repr.
                query = self._question_from_prompt(prompt)
                payload = json.dumps({"tool": KB_SEARCH_TOOL, "params": {"query": query}})
                return f"{TOOL_CALL_PREFIX} {payload}"
            # app#44 (3) — đã search (block hiện diện) nhưng 0 chunk (fence chặn sạch, MONEY-SHOT) ⇒
            # từ chối NGAY, KHÔNG phát TOOL_CALL lần 2 (sẽ lặp tới khi hết max_turns nếu không chặn
            # ở đây). Không đoạn trích nào để chép/trích ⇒ `refused=True` phía `run_agent_loop`
            # (A5, DEC-4: `not citations and not used_non_kb_tool`).
            return "Không có đoạn trích nào để trả lời."

        first = marks[0]
        # Đoạn trích đầu kết thúc ở đoạn kế tiếp, hoặc ở mốc "Câu hỏi:" nếu chỉ có một đoạn.
        if len(marks) > 1:
            end = marks[1].start()
        else:
            marker = prompt.rfind(self._QUESTION_MARKER)
            end = marker if marker != -1 else len(prompt)
        body = prompt[first.end() : end].strip()
        return f"{body}\n\n[{first.group(1)}]"


class FakeEmbedding:
    """CI fixture — KHÔNG phải deliverable AIE-1. Test-double for EmbeddingService's shape
    only; NOT one of AIE-1's 2 graded impls (stub-local + gateway)."""

    dim: int = 8

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [digest[i % len(digest)] / 255.0 for i in range(self.dim)]
