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
import re


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

    def __init__(self) -> None:
        self.prompts_seen: list[str] = []

    async def complete(self, prompt: str, **kwargs: object) -> str:
        del kwargs  # accepted for Protocol-shape parity
        self.prompts_seen.append(prompt)

        marks = list(self._EXCERPT_RE.finditer(prompt))
        if not marks:
            # Không đoạn trích nào (retrieval rỗng vì fence, hoặc prompt không phải khuôn
            # `build_prompt`) → không có gì để chép và không có gì để trích. Không trích dẫn ⇒
            # `LlmStepExecutor` đọc thành `refused=True`.
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
