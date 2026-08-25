"""Sinh lại golden set của một phòng ban từ chunk đã index — chạy sau mỗi lần upload tài liệu.

## Vì sao ở `apps/studio`

Việc này cần **cả hai** quadrant cùng lúc: `studio_kb.golden_from_kb` (sinh case) và
`studio_evalhub.golden_store`/`golden_merge` (đọc-ghi-hợp nhất). `.importlinter` xếp bốn quadrant
là sibling **cấm import nhau**, nên chỗ duy nhất nhìn được cả hai là composition root. Cùng lý lẽ
`core/golden_seed.py` (đường nạp file → DB) đã dùng.

## Khoá: một bộ cho mỗi knowledge base

`golden_set_ref = f"kb-{section_role}-auto-v1"`. Theo docstring `kb.knowledge_bases`
(`studio_kb/schema.py`), một KB **là** cặp `(tenant, section_role)` — *"1 tenant có nhiều KB theo
phòng ban"*. Tenant không cần vào ref vì `eval.golden_sets` đã `UNIQUE (tenant_id, golden_set_ref)`
(`evalhub#46`), nên hai tenant cùng phòng ban `hr` giữ hai bộ tách biệt dưới cùng một ref.

**Cột `eval.golden_sets.kb_id` để `NULL`, có chủ đích.** Nó là `UUID` trỏ sang `kb.knowledge_bases`,
mà bảng đó hiện là **shell rỗng — chưa ai ghi dòng nào** (chính docstring cột đó ghi vậy). Bịa một
UUID để lấp cho đủ là dựng một tham chiếu trỏ vào hư không. Cột được thiết kế nullable đúng cho giai
đoạn này; ngày `kb.knowledge_bases` có dòng thật thì nối lại, và ref ở trên **không phải đổi**.

## Vì sao sinh trên CẢ TENANT rồi mới lọc theo phòng ban

`build_cases` dựng case **bẫy** bằng cách **ghép chéo vai** (`_chon_nguon_bay`): hỏi dưới vai A
trong khi đáp án nằm ở vai B, kỳ vọng agent **từ chối**. Đưa nó chunk của đúng một phòng ban thì
không còn gì để ghép chéo — đo được:

| đầu vào | case | case bẫy | `is_critical` | `tier="core"` |
|---|---|---|---|---|
| 400 chunk, **1 vai** | 58 | **0** | 0 | 0 |
| 400 chunk, 2 vai (lọc `hr`) | 38 | **9** | 9 | 9 |

Bản đầu của module này đọc `chunks_for_section(tenant, section_role)` và vì thế **mọi bộ nó sinh ra
đều có 0 case hàng rào** — cổng sẽ chấm chất lượng trả lời mà **không bao giờ** chấm hàng rào
tenant. Đó đúng là trục duy nhất bắt được lỗi bịa-xuyên-chủ-thể (`engine#43`), vì trục citation
không bắt được: case trả lời sai vẫn cho `citation_accuracy = 1.0` khi nó trích một chunk có thật.

Nên: đọc **cả tenant** (`chunks_for_tenant`), sinh, rồi **lọc theo `section_roles`**. Mọi case —
cả trả-lời lẫn bẫy — mang `section_roles=(vai_hỏi,)` đúng một phần tử, nên phép lọc phủ hết và
không case nào rơi vào hai bộ.

**Trục T1 (chéo-tenant) vẫn không sinh được, và không nên.** Nó đòi chunk của tenant khác — thứ RLS
chặn và cũng không phải thứ đường này nên đọc. Bộ sinh máy chỉ phủ T6 (chéo-vai); case T1 phải do
người viết, và `sample_report` khai ra khoảng trống đó thay vì im lặng.

## Vì sao SINH LẠI TOÀN BỘ, không thêm dần

Upload tài liệu thứ hai vào cùng phòng ban thì sinh lại case cho **cả phòng ban**, không chỉ tài
liệu vừa nạp. Ba lý do, và lý do thứ ba là lý do bắt buộc:

1. `build_cases` phân tầng case theo vai và chọn chunk bẫy từ **toàn bộ** mẫu — chạy nó trên một
   tài liệu lẻ cho ra một bộ khác hẳn chạy trên cả phòng ban.
2. Sinh lại là **idempotent**: cùng chunk ⇒ cùng bộ case (`build_cases` tất định, và
   `chunks_for_tenant` `ORDER BY chunk_id` để đầu vào cũng tất định).
3. `case_id` do `build_cases` sinh là `AI-001`/`AI-BAY-001` — **không mang dấu vết tài liệu nào**.
   Nên không có cách nào biết case cũ nào thuộc tài liệu vừa được nạp lại để thay đúng phần đó.
   Thêm dần sẽ đụng `merge_golden_sets` với hai bản `source="ai"` cùng khoá ⇒
   `GoldenSetMergeConflict`, đúng theo thiết kế của nó (luật `source` chỉ phân xử `human` thắng
   `ai`). Sinh lại toàn bộ né được chuyện đó mà không phải nới luật.

## Bản người sửa KHÔNG bị sinh lại đè mất

Đây là điểm chính của module. Trước khi ghi, bộ cũ được đọc lại và **giữ nguyên mọi case
`source="human"`**; bộ máy vừa sinh chỉ đóng góp phần `source="ai"`. Hai bộ đi qua
`merge_golden_sets`, nơi luật *"human ground-truth always wins"* quyết bản nào thắng khi trùng khoá
— không phải thứ tự tham số. Đó chính là caller production đầu tiên của `evalhub#49`, và nó đóng nợ
`DEC-D28-06` (*"module đúng, chưa ai gọi"*).

Case `source=None` (chưa khai nguồn — vd bộ Callisto đóng gói sẵn được seed vào cùng ref) **bị coi
là không phải của người**, nên chúng sẽ bị bộ máy thay. Đó là hệ quả trực tiếp của luật `None` =
*"chưa khai"* ≠ *"người viết"* (`DEC-D16-03`): coi `None` là `human` sẽ khai hộ nguồn gốc cho một bộ
mà không ai kiểm. Ai muốn giữ case cũ qua các lần sinh lại thì phải khai `source: "human"` tường
minh — qua `POST /api/admin/golden-sets`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from studio_evalhub.golden_case import GoldenCase as RuntimeCase
from studio_evalhub.golden_case import GoldenSet
from studio_evalhub.golden_merge import merge_golden_sets
from studio_evalhub.golden_store import GoldenSetNotFound, read_golden_set, write_golden_set
from studio_kb.golden_from_kb import SourceChunk, build_cases, sample_report
from studio_kb.golden_set_core import GoldenCase as AuthoringCase
from studio_kb.pipeline import KbPipeline


def auto_golden_set_ref(section_role: str) -> str:
    """Ref của bộ sinh-máy cho một phòng ban. Xem mục *"Khoá"* ở docstring module."""
    return f"kb-{section_role}-auto-v1"


def _to_runtime_case(case: AuthoringCase) -> RuntimeCase:
    """`studio_kb` `GoldenCase` (dataclass soạn thảo) → `studio_evalhub` `GoldenCase` (model chạy).

    **Hai kiểu cùng tên, khác việc, và đây là chỗ chúng gặp nhau.** Bản `kb` là thứ `render_cases`
    ghi ra YAML cho người đọc/sửa; bản `evalhub` là thứ `EvalHarness` chấm. Đường chuyển sẵn có đi
    vòng qua đĩa (`render_cases` → file → `load_golden_set`), hợp lý cho luồng soạn thảo nhưng vô
    nghĩa ở đây: ta đang đi thẳng vào DB, không có file nào ở giữa.

    Ánh xạ **tường minh từng field**, không `asdict()`:

    - `note` **bị bỏ** — bản `evalhub` không có field đó, và nó bật `extra="forbid"` (`DEC-D18-01`).
      Nên `GoldenCase(**asdict(case))` sẽ ném `ValidationError`. Đó là tripwire hoạt động đúng, chứ
      không phải chỗ để nới `extra`: `note` là ghi chú cho người soạn, không phải dữ liệu chấm.
    - `tuple` → `list`: bản `kb` frozen nên dùng tuple, bản `evalhub` khai `list[str]`.
    - `source`/`is_critical`/`tier` chuyển **nguyên trạng**, gồm cả `None`. Không điền hộ giá trị
      nào (`DEC-D16-03`) — `build_cases` đã tự khai `source="ai"` cho mọi case nó sinh, nên `None`
      lọt tới đây nghĩa là bộ sinh chưa khai, và bịa ở tầng này sẽ giấu mất điều đó.
    """
    return RuntimeCase(
        case_id=case.case_id,
        query=case.query,
        tenant=case.tenant,
        section_roles=list(case.section_roles),
        expected_tenant=case.expected_tenant,
        expected_section_role=case.expected_section_role,
        expected=case.expected,
        expected_citation=list(case.expected_citation),
        source=case.source,
        is_critical=case.is_critical,
        tier=case.tier,
    )


@dataclass(frozen=True, slots=True)
class AutogenReport:
    """Kết quả một lần sinh lại — thứ route trả về cho người upload.

    Tách `n_ai` và `n_human` chứ không chỉ tổng: người vừa upload cần biết bản họ đã sửa tay còn
    hay không. Một con số tổng không phân biệt được *"máy sinh 20"* với *"máy sinh 15 + người giữ
    5"*, mà đó đúng là câu hỏi họ sẽ hỏi sau lần sửa đầu tiên.
    """

    golden_set_ref: str
    n_cases: int
    n_ai: int
    n_human: int
    n_chunks: int
    roles_below_minimum: tuple[str, ...]


async def regenerate_for_section(
    conn: Any,
    pipeline: KbPipeline,
    *,
    tenant_id: UUID,
    tenant_slug: str,
    section_role: str,
) -> AutogenReport:
    """Sinh lại bộ golden của `(tenant_id, section_role)` từ chunk đang có; giữ case người sửa.

    Nhận `conn` chứ không phải `Pool` — cùng lý do `golden_store` nhận `conn`: `eval.golden_sets`
    bật RLS `FORCE`, nên connection phải đã bind `app.tenant_id` khớp `tenant_id`. Bind là việc của
    caller, trong đúng transaction ghi.

    **Không ghi khi bộ hợp nhất rỗng.** Một `GoldenSet` 0 case đi tiếp vào `EvalHarness.run()` cho
    `success_rate` trên mẫu số 0 — hoặc `ZeroDivisionError`, hoặc tệ hơn, một con số. Phòng ban quá
    ít chunk để `build_cases` dựng nổi case nào là trạng thái **hợp lệ** (vừa upload tài liệu đầu
    tiên, ngắn), nên nó không phải lỗi — nhưng ghi một bộ rỗng đè lên bộ cũ thì là. Report vẫn trả
    về với `n_cases=0` để caller nói được điều đó ra.
    """
    # Đọc chunk của CẢ TENANT, mọi phòng ban — không chỉ phòng ban vừa upload. Xem mục
    # *"Vì sao sinh trên cả tenant rồi mới lọc"* ở docstring module: lọc chunk trước khi sinh sẽ
    # tước mất khả năng ghép chéo vai của `build_cases`, và bộ ra có 0 case hàng rào.
    chunks = await pipeline.chunks_for_tenant(tenant_id)
    sources = tuple(
        SourceChunk(chunk_id=c.chunk_id, text=c.text, tenant=tenant_slug, section_role=c.section_role) for c in chunks
    )
    sinh_ca_tenant = build_cases(sources)
    # Lọc SAU khi sinh. Mọi case — cả trả-lời lẫn bẫy — mang `section_roles=(vai_hỏi,)` đúng một
    # phần tử (`golden_from_kb`), nên phép lọc này phủ hết và không case nào thuộc hai bộ.
    # Case bẫy của phòng ban này là case HỎI dưới vai này mà đáp án nằm ở vai khác — đúng thứ bộ
    # golden của phòng ban này cần để đo hàng rào.
    generated = tuple(c for c in sinh_ca_tenant if tuple(c.section_roles) == (section_role,))
    # Đo mẫu trên phần MÁY SINH của CHÍNH phòng ban này, sau khi lọc — `sample_report` nhận
    # `GoldenCase` của `kb`. Đo trên `sinh_ca_tenant` sẽ báo tỷ lệ bẫy của cả tenant, không phải
    # của bộ thật sự được ghi.
    report = sample_report(generated)

    ref = auto_golden_set_ref(section_role)
    try:
        existing = await read_golden_set(conn, ref, tenant_id)
    except GoldenSetNotFound:
        human_kept: tuple[RuntimeCase, ...] = ()
    else:
        human_kept = tuple(c for c in existing.cases if c.source == "human")

    merged = merge_golden_sets(
        GoldenSet(golden_set_ref=ref, cases=[_to_runtime_case(c) for c in generated]),
        GoldenSet(golden_set_ref=ref, cases=list(human_kept)),
        golden_set_ref=ref,
    )

    if merged.cases:
        await write_golden_set(conn, merged, tenant_id)

    return AutogenReport(
        golden_set_ref=ref,
        n_cases=len(merged.cases),
        n_ai=sum(1 for c in merged.cases if c.source == "ai"),
        n_human=sum(1 for c in merged.cases if c.source == "human"),
        n_chunks=len(chunks),
        roles_below_minimum=tuple(getattr(report, "roles_below_minimum", getattr(report, "vai_thieu_case", ()))),
    )
