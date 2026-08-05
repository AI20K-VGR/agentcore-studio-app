"""D13 flip-readiness cho `KbSearchService.search` (issue #91) — composition root, Postgres thật.

**Điều kiện lật** — ngày `studio_kb.search.KbSearchService.search` không còn `raise
NotImplementedError`: **T3** dưới đây tự chuyển XFAIL → XPASS mà AIE-1 không sửa dòng nào. Nội
dung cú lật (file/dòng cần sửa) sống **DUY NHẤT** ở
`plans/260805-1011-d13-kb-search-flip-readiness/flip-checklist.md` — SSOT, không chép lại ở đây
(chép số dòng của `packages/kb/**` vào đây sẽ tự sai đúng lúc DE sửa file đó).

**File này KHÔNG thay `packages/kb/tests/test_leak.py`** — leak-test là behavioral fence của DE,
chạy trực tiếp trong `packages/kb`. File này kiểm một tầng khác: đường **composition** ở
`apps/studio` (`EngineAgentRunner → interpreter → kb.search → citations`) mà `.importlinter` cấm
`studio_kb` tự kiểm (layer dưới không import ngược layer trên).

**Vì sao 2 test luôn-xanh (T2 seed proof/twin) + T4 (drift-guard) mang toàn bộ phần chứng minh,
T3 chỉ là dây bẫy** (chặn Goodhart "test là xfail nên CI luôn xanh"): T1 chứng minh corpus Callisto
thật nạp được vào `kb.chunks`; T2 chứng minh chuỗi composition-root chạy thật, xanh, trên
`PgKbSearch` — twin sinh đôi của T3 dùng chung một helper assert, nên seed/wiring/assertion hỏng
thì T2 đỏ ầm ĩ trước; T4 pin hình dạng seam (`__init__` + `search`) không phụ thuộc `xfail`. Bậc tự
do duy nhất còn lại của T3 đúng bằng cái seam DE chưa lấp.

Cần Postgres thật: `docker compose -f docker-compose.test.yml up -d` + 2 biến DSN
(`STUDIO_DATABASE_URL`, `STUDIO_DATABASE_URL_ADMIN`). Thiếu thì fixture `pool`/`admin_pool` ở
`conftest.py` gốc **skip**, không fail (`conftest.py:48-52`).
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterable

import pytest
import pytest_asyncio
from studio_app.core._db import Pool
from studio_app.eval_adapter import EngineAgentRunner
from studio_app.obs.trace_writer import PgTraceWriter
from studio_app.providers.fakes import ExtractiveFakeLLM
from studio_contracts import TraceEvent
from studio_engine.demo_stubs import StubEmbedding
from studio_evalhub.cli import _demo_golden_set
from studio_evalhub.harness import citations_from_trace
from studio_kb.doc_factory import TENANT_IDS, load_callisto
from studio_kb.embeddings import derive_vector
from studio_kb.postgres import KbIngest, PgKbSearch
from studio_kb.search import KbSearchService

# (số dòng ghi, {slug: {chunk_id đã seed}}) — trả về bởi fixture `seeded_corpus`.
SeededCorpus = tuple[int, dict[str, set[str]]]

_STABLE_CHUNK_ID = re.compile(r"^[\w-]+#c\d+$")
_ALLOWED_SECTION_ROLES = frozenset({"public"})


class _CallistoEmbedding:
    """Adapter 4 dòng thoả `EmbeddingService` — seed vector CÙNG không gian với vector DE dùng để
    xếp hạng khi flip (QĐ-P1).

    **Không** dùng `studio_app.providers.fakes.FakeEmbedding`: nó băm `sha256` — cùng số chiều 8
    nhưng KHÁC hoàn toàn không gian cosine so với bag-of-words của `derive_vector`, nên nạp qua
    `FakeEmbedding` sẽ khớp `EMBEDDING_DIM` mà kết quả `PgKbSearch`/`KbSearchService` trả về
    (chấm bằng cosine) trở thành vô nghĩa — test vẫn "chạy" nhưng không chứng minh gì.

    **Không** dùng `studio_engine.demo_stubs.StubEmbedding`: nó replay 2 vector cố định từ fixture
    (VCR-style), KHÔNG content-aware — mọi chunk nạp qua nó ra cùng vài vector bất kể nội dung,
    nên không seed được một corpus có cấu trúc cosine thật.

    Nguồn vector đúng là `studio_kb.embeddings.derive_vector` — `load_callisto_embeddings()` tự
    khai là đường đọc DUY NHẤT cho vector Callisto, và cả `PgKbSearch`/`KbSearchService` (khi DE
    land) đều xếp hạng trên đúng không gian đó.
    """

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [derive_vector(text) for text in texts]


@pytest_asyncio.fixture
async def seeded_corpus(pool: Pool) -> SeededCorpus:
    """Nạp corpus Callisto thật vào `kb.chunks` qua `KbIngest` đã ship (QĐ-P4) — không tự viết
    INSERT (giữ đúng idempotency `ON CONFLICT` + `_bind_tenant` của `KbIngest.ingest`).

    Trả `(written, {slug: {chunk_id}})` để test vừa kiểm được tổng số dòng ghi, vừa tra được phạm
    vi từng tenant mà không phải gọi lại `load_callisto()`/seed lần hai.
    """
    chunks = list(load_callisto())
    written = await KbIngest(pool, _CallistoEmbedding()).ingest(chunks)

    slug_of_tenant = {tenant_id: slug for slug, tenant_id in TENANT_IDS.items()}
    by_slug: dict[str, set[str]] = {slug: set() for slug in TENANT_IDS}
    for chunk in chunks:
        by_slug[slug_of_tenant[chunk.tenant_id]].add(chunk.chunk_id)
    return written, by_slug


def _retrieved_chunks(events: list[TraceEvent]) -> list[dict[str, object]]:
    """Chunk **đã truy xuất** (node `kb-retrieve`), đọc từ `event.outputs["chunks"]` — đúng cách
    `apps/studio/scripts/e2e_smoke_eval.py` đã làm. Khác `citations_from_trace`: nguồn đó gom
    `.citations` của event `llm-step` (LLM-trích ∩ retrieved); đây là TOÀN BỘ những gì `kb.search`
    trả về trước khi LLM lọc lại — hai mặt quan sát khác nhau của cùng một run."""
    chunks: list[dict[str, object]] = []
    for event in events:
        raw_chunks = event.outputs.get("chunks")
        if not isinstance(raw_chunks, list):
            continue
        for chunk in raw_chunks:
            if isinstance(chunk, dict) and "chunk_id" in chunk:
                chunks.append(chunk)
    return chunks


def _assert_scoped_to_ankor(items_or_ids: Iterable[dict[str, object] | str], seeded: SeededCorpus) -> None:
    """Helper assert dùng chung cho **T2 và T3** (QĐ-P6, cơ chế chống mù `xfail` — R2): cả hai gọi
    đúng MỘT hàm, không copy-paste. Nhận hoặc chunk-dict (từ `_retrieved_chunks`, sau
    `model_dump(mode="json")`) hoặc `chunk_id` trần (từ `citations_from_trace`).

    Kiểm đúng thứ hợp đồng hứa (QĐ-P5 — KHÔNG bám thứ hạng, `EMBEDDING_DIM=8` bag-of-words không
    chịu nổi kết luận về chất lượng retrieval):
    - khác rỗng — **răng dương**, để một impl trả `[]` không false-pass vế loại trừ dưới;
    - mọi `chunk_id` ∈ tập `ankor` đã seed;
    - **không** `chunk_id` nào ∈ tập `borea` đã seed — **răng âm** (chéo tenant);
    - `tenant_id`/`section_role` (khi có, tức đầu vào là chunk-dict) so bằng `str(...)`.

    ⚠️ **Bắt buộc `str(...)`, KHÔNG so trực tiếp với object `UUID`**: `interpreter.py` dump chunk
    qua `item.model_dump(mode="json")`, biến `UUID` thành `str`. So một `str` với một `UUID` object
    luôn `False` → vế đó thành răng giả vĩnh viễn, và test sẽ "đỏ" vì lệch kiểu chứ không phải vì
    hàng rào hoạt động (F4).
    """
    _, by_slug = seeded
    ankor_ids = by_slug["ankor"]
    borea_ids = by_slug["borea"]

    materialized = list(items_or_ids)
    chunk_ids = [str(item["chunk_id"]) if isinstance(item, dict) else str(item) for item in materialized]

    assert chunk_ids, "rỗng — 'khác rỗng' phải là răng dương thật, một impl trả [] không được false-pass"
    for chunk_id in chunk_ids:
        assert chunk_id in ankor_ids, f"{chunk_id} không thuộc tập ankor đã seed"
        assert chunk_id not in borea_ids, f"{chunk_id} rò rỉ sang tenant borea"

    for item in materialized:
        if not isinstance(item, dict):
            continue
        assert item.get("tenant_id") == str(TENANT_IDS["ankor"]), (
            f"tenant_id lệch phạm vi: {item.get('tenant_id')!r} != {str(TENANT_IDS['ankor'])!r}"
        )
        assert item.get("section_role") in _ALLOWED_SECTION_ROLES, (
            f"section_role ngoài phạm vi: {item.get('section_role')!r}"
        )


async def test_kb_ingest_seeds_callisto_corpus(seeded_corpus: SeededCorpus) -> None:
    """T1 (F1) — `KbIngest` nạp corpus Callisto thật vào `kb.chunks`, cả 2 tenant, `chunk_id` giữ
    đúng dạng bền `{doc_id}#c{n}` (`kb-search.v0.md:29-32`) — điều kiện `re_index` bắt buộc."""
    written, by_slug = seeded_corpus
    total_chunks = len(list(load_callisto()))

    assert written == total_chunks, "số dòng ghi lệch tổng chunk trên đĩa — corpus đã đổi hoặc ingest bỏ sót"
    assert by_slug["ankor"], "thiếu chunk tenant ankor sau ingest"
    assert by_slug["borea"], "thiếu chunk tenant borea sau ingest"

    for chunk_id in by_slug["ankor"] | by_slug["borea"]:
        assert _STABLE_CHUNK_ID.fullmatch(chunk_id), f"chunk_id lệch dạng bền {{doc_id}}#c{{n}}: {chunk_id!r}"


async def test_composition_root_ready_for_live_kb(pool: Pool, seeded_corpus: SeededCorpus) -> None:
    """T2 (F2) — twin luôn-xanh của T3 (QĐ-P6): chạy case SC-01 qua chuỗi composition-root thật,
    `kb_search=PgKbSearch(...)` thay vì `KbSearchService(...)`. Hỏng vì seed/wiring/assertion thì
    test NÀY đỏ ầm ĩ trước — bậc tự do còn lại của T3 (`xfail`) đúng bằng cái seam DE chưa lấp."""
    case = next(c for c in _demo_golden_set().cases if c.case_id == "SC-01")
    runner = EngineAgentRunner(
        kb_search=PgKbSearch(pool, _CallistoEmbedding()),
        llm=ExtractiveFakeLLM(),
        embedding=StubEmbedding("smoke-01"),
        trace_writer=PgTraceWriter(pool),
    )
    case_run = await runner.run_case(
        agent_id="agent-callisto-d4",
        query=case.query,
        tenant_id=TENANT_IDS["ankor"],
        section_roles=case.section_roles,
    )

    _assert_scoped_to_ankor(_retrieved_chunks(case_run.events), seeded_corpus)
    _assert_scoped_to_ankor(citations_from_trace(case_run.events), seeded_corpus)


@pytest.mark.xfail(
    strict=False,
    reason="studio_kb.search.KbSearchService.search vẫn raise NotImplementedError (spec DE, issue "
    "#91) — dây bẫy chờ DE land; xem flip-checklist.md cho nội dung cú lật",
)
async def test_engine_agent_runner_with_kb_search_service(pool: Pool, seeded_corpus: SeededCorpus) -> None:
    """T3 (F3) — giống hệt T2 trừ đúng một thứ: `kb_search=KbSearchService(pool)`. Ngày DE land,
    test này tự XPASS mà AIE-1 không sửa dòng nào (QĐ-U1). `xfail(strict=False)` để một impl land
    một phần không phạt cả suite; twin T2 + drift-guard T4 chống mù cho marker này (R2)."""
    case = next(c for c in _demo_golden_set().cases if c.case_id == "SC-01")
    runner = EngineAgentRunner(
        kb_search=KbSearchService(pool),
        llm=ExtractiveFakeLLM(),
        embedding=StubEmbedding("smoke-01"),
        trace_writer=PgTraceWriter(pool),
    )
    case_run = await runner.run_case(
        agent_id="agent-callisto-d4",
        query=case.query,
        tenant_id=TENANT_IDS["ankor"],
        section_roles=case.section_roles,
    )

    _assert_scoped_to_ankor(_retrieved_chunks(case_run.events), seeded_corpus)
    _assert_scoped_to_ankor(citations_from_trace(case_run.events), seeded_corpus)


def test_kb_search_service_seam_shape_unchanged() -> None:
    """T4 (F5) — drift-guard cho 2 hình dạng DE có thể land (plan.md R1/R1b), hai vế so khác nhau
    có chủ đích:

    - `__init__` (KHÔNG frozen) → **bind-check**, không so danh sách tham số: so danh sách sẽ báo
      động giả khi DE thêm một tham số có-default hoặc `**kwargs` (không phá call-site
      `KbSearchService(pool)` nào), mà vẫn không bắt được đường DE land bằng `classmethod` factory.
      Bind-check bắt đúng thứ thật sự phá lời gọi hiện tại: **tham số bắt buộc mới**.
    - `search` (ĐÃ frozen, `kb-search.v0.md:24-32`) → so **chính xác** danh sách tham số. Chữ ký
      frozen thì so chính xác mới đúng nghĩa; đây cũng là vế bắt được đường "DE land bằng factory,
      không đụng `__init__`" mà bind-check bỏ lọt (R1b).
    """
    sentinel_pool: object = object()
    inspect.signature(KbSearchService.__init__).bind(None, sentinel_pool)  # không raise = pass

    search_params = list(inspect.signature(KbSearchService.search).parameters)
    assert search_params == ["self", "query", "tenant_id", "section_roles", "top_k"]
