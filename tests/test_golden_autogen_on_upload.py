"""Upload tài liệu ⇒ golden set của phòng ban được SINH LẠI, và bản người sửa sống sót.

Golden set không còn phải có sẵn: nó sinh ra từ chunk đã index, mỗi phòng ban một bộ
(`kb-<section_role>-auto-v1`). Bài ở đây chạy qua **đúng route thật** (`upload_document`), không
gọi thẳng `regenerate_for_section` — vì thứ dễ hỏng nhất không phải hàm sinh mà là **chỗ nối**:
bind `app.tenant_id` cho tenant ĐÍCH, và transaction bao quanh lệnh ghi.

Bài đắt nhất trong file là `..._giu_case_nguoi_sua_qua_lan_sinh_lai`. Ba bài còn lại đều xanh với
một bản cài đặt ngây thơ *"sinh xong ghi đè"*; chỉ bài đó phân biệt được.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from psycopg import sql
from studio_app import middleware
from studio_app.core._db import Pool, close_pools, get_pool
from studio_app.core.golden_autogen import auto_golden_set_ref, regenerate_for_section
from studio_app.routes.documents import DeleteDocumentsRequest, delete_documents, upload_document
from studio_contracts import Aggregate, CaseResult, Gate, GateThreshold, Scorecard
from studio_evalhub.golden_case import GoldenCase, GoldenSet
from studio_evalhub.golden_store import GoldenSetNotFound, read_golden_set, write_golden_set
from studio_evalhub.scorecard_store import read_pending_scorecard, write_pending_scorecard
from studio_kb.pipeline import KbPipeline
from test_documents_routes import (
    _md_upload_file,
    _seed_section,
    _seed_tenant,
    _seed_user,
    _set_session,
    _simulate_request_connection,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    """Đóng pool singleton sau MỖI bài — bắt buộc, không phải dọn dẹp cho gọn.

    `get_pool()` là singleton toàn tiến trình, còn `pytest-asyncio` dựng event loop mới cho mỗi
    bài. Pool tạo ở loop của bài này mà bị bài sau dùng lại sẽ ném
    `ValueError: The future belongs to a different loop` — và nó nổ ở **bài sau**, không phải bài
    gây ra. Đã đo: thiếu fixture này thì `test_golden_sets_routes.py` đỏ 1 bài + 1 error trong
    lượt chạy cả thư mục, còn chạy riêng thì xanh.

    Cùng fixture `test_documents_routes.py` đã có. Fixture KHÔNG đi theo `import` helper — nó phải
    khai lại ở từng module.
    """
    yield
    await close_pools()


# Hình dạng Markdown THẬT — tiêu đề nêu chủ đề, thân bài mang đại lượng. Đây là thứ bộ soạn mặc
# định (`TemplateQuestionWriter`) đọc được: nó CỐ Ý từ chối văn xuôi phẳng không tiêu đề, vì dựng câu
# hỏi từ một đoạn văn chỉ ra được câu bắt agent chép lại nguyên văn tài liệu. Fixture cũ ở đây là
# văn xuôi phẳng, nên sau khi đổi bộ soạn nó sinh ra 0 case — bài đỏ là ĐÚNG, và cách sửa là cho
# fixture giống tài liệu thật chứ không phải nới bộ soạn.
_DOC_HR = (
    "## Nghỉ phép năm\n"
    "Nhân viên chính thức được nghỉ phép 12 ngày mỗi năm.\n\n"
    "## Thời hạn nộp đơn\n"
    "Đơn xin nghỉ phải nộp trước 3 ngày làm việc.\n"
).encode()
_DOC_FINANCE = (
    "## Phụ cấp ăn trưa\n"
    "Mức chi là 40.000 đồng mỗi ngày công.\n\n"
    "## Phụ cấp đi lại\n"
    "Công ty trả 500.000 đồng mỗi tháng, cùng kỳ lương.\n"
).encode()


class _UnusedEmbedding:
    """`EmbeddingService` double — `regenerate_for_section` chỉ ĐỌC chunk, không nhúng gì.

    Gọi tới nó nghĩa là đường đọc đã âm thầm đổi thành đường ghi, nên nó ném thay vì trả vector."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError(f"regenerate_for_section không được nhúng gì; bị gọi với {len(texts)} text")


async def _bind(conn: Any, tenant_id: UUID) -> None:
    await conn.execute(sql.SQL("SET LOCAL app.tenant_id = {}").format(sql.Literal(str(tenant_id))))


async def _make_tenant(admin_pool: Pool, name: str, *section_roles: str) -> UUID:
    tenant_id = await _seed_tenant(admin_pool, name)
    admin_id = await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])
    for role in section_roles:
        await _seed_section(admin_pool, tenant_id, role, admin_id)
    return tenant_id


async def _upload(tenant_id: UUID, filename: str, content: bytes, section_role: str) -> Any:
    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", system_roles=["admin"])
    try:
        async with _simulate_request_connection():
            return await upload_document(
                file=_md_upload_file(filename, content), section_role=section_role, tenant_id=None
            )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


def _pending_scorecard() -> Scorecard:
    """Một Scorecard chờ tối thiểu — chỉ để chứng minh nó SỐNG SÓT qua một lượt xoá hụt."""
    return Scorecard(
        agent_id="agent-noop",
        golden_set_ref="kb-hr-auto-v1",
        results=[CaseResult(case_id="c1", expected="x", actual="x", success=True, citation_accuracy=1.0)],
        aggregate=Aggregate(success_rate=1.0, citation_accuracy=1.0, n_scored_citation=1),
        gate=Gate(threshold=GateThreshold(success=0.9, citation_accuracy=0.95), verdict="PASS"),
        recipe_hash="hash-noop",
    )


async def _delete(tenant_id: UUID, doc_ids: list[str]) -> Any:
    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", system_roles=["admin"])
    try:
        async with _simulate_request_connection():
            return await delete_documents(DeleteDocumentsRequest(ids=doc_ids), tenant_id=None)
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]


async def _read_set(admin_pool: Pool, tenant_id: UUID, ref: str) -> GoldenSet:
    async with admin_pool.connection() as conn, conn.transaction():
        await _bind(conn, tenant_id)
        return await read_golden_set(conn, ref, tenant_id)


async def test_upload_generates_the_golden_set_for_that_section(admin_pool: Pool) -> None:
    """Trước upload: chưa có bộ nào. Sau upload: có bộ, toàn case `source="ai"`.

    Vế "trước upload" là phần chống rỗng-nghĩa: không có nó, bài vẫn xanh nếu bộ đã tồn tại sẵn từ
    một đường khác và upload chẳng làm gì."""
    tenant_id = await _make_tenant(admin_pool, "autogen-a", "hr")
    ref = auto_golden_set_ref("hr")

    with pytest.raises(GoldenSetNotFound):
        await _read_set(admin_pool, tenant_id, ref)

    result = await _upload(tenant_id, "nghi-phep.md", _DOC_HR, "hr")

    assert result.golden_set_ref == ref
    assert result.golden_n_cases > 0
    assert result.golden_n_human == 0

    bo = await _read_set(admin_pool, tenant_id, ref)
    assert len(bo.cases) == result.golden_n_cases
    assert {c.source for c in bo.cases} == {"ai"}, "bộ vừa sinh phải khai đúng nguồn là máy"


async def test_human_edited_cases_survive_regeneration(admin_pool: Pool) -> None:
    """**Bài đắt nhất file.** Người sửa một case, upload lại tài liệu ⇒ bản người sửa PHẢI còn.

    Đây là điểm khác biệt duy nhất giữa bản cài đặt đúng và bản *"sinh xong ghi đè"* — ba bài còn
    lại trong file đều xanh với bản ghi đè. Nếu bài này đỏ thì công sức người dùng bỏ ra để sửa
    ground truth bị một lần upload xoá sạch, và không có gì báo."""
    tenant_id = await _make_tenant(admin_pool, "autogen-b", "hr")
    ref = auto_golden_set_ref("hr")
    await _upload(tenant_id, "nghi-phep.md", _DOC_HR, "hr")

    human_case = GoldenCase(
        case_id="HUMAN-01",
        query="Đơn xin nghỉ phép nộp trước bao lâu?",
        tenant="autogen-b",
        section_roles=["hr"],
        expected_tenant="autogen-b",
        expected_section_role="hr",
        expected="Trước ba ngày làm việc.",
        source="human",
    )
    before = await _read_set(admin_pool, tenant_id, ref)
    async with admin_pool.connection() as conn, conn.transaction():
        await _bind(conn, tenant_id)
        await write_golden_set(conn, GoldenSet(golden_set_ref=ref, cases=[*before.cases, human_case]), tenant_id)

    result = await _upload(tenant_id, "nghi-phep.md", _DOC_HR, "hr")

    assert result.golden_n_human == 1, "case người sửa phải được ĐẾM là còn giữ"
    after = await _read_set(admin_pool, tenant_id, ref)
    kept = [c for c in after.cases if c.case_id == "HUMAN-01"]
    assert len(kept) == 1, f"case người sửa bị mất after khi sinh lại — còn {[c.case_id for c in after.cases]}"
    assert kept[0].expected == "Trước ba ngày làm việc."
    assert kept[0].source == "human"


async def test_regeneration_is_deterministic_across_identical_uploads(admin_pool: Pool) -> None:
    """Upload lại **cùng** file ⇒ bộ case không đổi. Nếu không tất định, mỗi lần upload lại đổi
    thứ agent đang bị chấm mà không ai đụng vào golden set."""
    tenant_id = await _make_tenant(admin_pool, "autogen-c", "hr")
    ref = auto_golden_set_ref("hr")

    await _upload(tenant_id, "nghi-phep.md", _DOC_HR, "hr")
    first = await _read_set(admin_pool, tenant_id, ref)
    await _upload(tenant_id, "nghi-phep.md", _DOC_HR, "hr")
    second = await _read_set(admin_pool, tenant_id, ref)

    assert [c.case_id for c in first.cases] == [c.case_id for c in second.cases]
    assert [c.query for c in first.cases] == [c.query for c in second.cases]


async def test_uploading_one_section_leaves_another_sections_set_untouched(admin_pool: Pool) -> None:
    """Một bộ cho mỗi phòng ban — upload vào `finance` không đụng bộ của `hr`.

    Fixture bất đối xứng: nạp `hr` trước, ghi số case của nó, rồi nạp `finance` bằng nội dung KHÁC.
    Nếu ref không mang `section_role` thì lần nạp after ghi đè lần trước và số case đổi."""
    tenant_id = await _make_tenant(admin_pool, "autogen-d", "hr", "finance")

    await _upload(tenant_id, "nghi-phep.md", _DOC_HR, "hr")
    hr_before = await _read_set(admin_pool, tenant_id, auto_golden_set_ref("hr"))

    await _upload(tenant_id, "phu-cap.md", _DOC_FINANCE, "finance")
    hr_after = await _read_set(admin_pool, tenant_id, auto_golden_set_ref("hr"))
    fin = await _read_set(admin_pool, tenant_id, auto_golden_set_ref("finance"))

    assert [c.query for c in hr_after.cases] == [c.query for c in hr_before.cases], "bộ hr bị đụng"
    assert fin.golden_set_ref == "kb-finance-auto-v1"
    assert {c.section_roles[0] for c in fin.cases} == {"finance"}


async def test_source_none_is_not_treated_as_human(admin_pool: Pool) -> None:
    """`source=None` = *"chưa khai nguồn"*, KHÔNG phải *"người viết"* — nên nó bị sinh lại thay.

    Khoá chiều NGƯỢC của bài giữ-case-người-sửa. Thiếu bài này, một bản cài đặt giữ luôn cả case
    `None` (vd `c.source != "ai"`) vẫn xanh — và nó sẽ khai hộ nguồn gốc cho mọi case seed sẵn,
    đúng thứ `DEC-D16-03` cấm."""
    tenant_id = await _make_tenant(admin_pool, "autogen-e", "hr")
    ref = auto_golden_set_ref("hr")
    await _upload(tenant_id, "nghi-phep.md", _DOC_HR, "hr")

    undeclared = GoldenCase(
        case_id="UNDECLARED-01",
        query="Câu hỏi chưa khai nguồn.",
        tenant="autogen-e",
        section_roles=["hr"],
        expected_tenant="autogen-e",
        expected_section_role="hr",
        expected="x",
    )
    assert undeclared.source is None, "fixture hỏng: case này phải THẬT SỰ chưa khai nguồn"
    before = await _read_set(admin_pool, tenant_id, ref)
    async with admin_pool.connection() as conn, conn.transaction():
        await _bind(conn, tenant_id)
        await write_golden_set(conn, GoldenSet(golden_set_ref=ref, cases=[*before.cases, undeclared]), tenant_id)

    result = await _upload(tenant_id, "nghi-phep.md", _DOC_HR, "hr")

    assert result.golden_n_human == 0
    after = await _read_set(admin_pool, tenant_id, ref)
    assert "UNDECLARED-01" not in [c.case_id for c in after.cases]


async def test_empty_result_does_not_overwrite_the_existing_set(admin_pool: Pool) -> None:
    """Phòng ban 0 chunk ⇒ 0 case sinh ra ⇒ **không ghi**, bộ cũ còn nguyên.

    Bài này gọi thẳng `regenerate_for_section`, không qua route — cố ý. Guard *"không ghi khi rỗng"*
    **không với tới được từ route**: mọi upload thành công đều tạo ≥1 chunk, mà `build_cases` cho
    ≥1 case từ 1 chunk, nên bộ hợp nhất không bao giờ rỗng trên đường đó. Đã đo: mutant bỏ guard
    (`if True:`) sống sót toàn bộ 5 bài đi qua route.

    Nhưng `regenerate_for_section` là hàm công khai, và một bộ 0 case đi tiếp vào `EvalHarness.run()`
    cho `success_rate` trên mẫu số 0. Guard là thật; chỗ canh nó phải là tầng hàm.
    """
    tenant_id = await _make_tenant(admin_pool, "autogen-f", "hr")
    ref = auto_golden_set_ref("hr")
    existing = GoldenSet(
        golden_set_ref=ref,
        cases=[
            GoldenCase(
                case_id="AI-OLD-01",
                query="Bộ cũ, phải còn nguyên.",
                tenant="autogen-f",
                section_roles=["hr"],
                expected_tenant="autogen-f",
                expected_section_role="hr",
                expected="x",
                source="ai",
            )
        ],
    )
    async with admin_pool.connection() as conn, conn.transaction():
        await _bind(conn, tenant_id)
        await write_golden_set(conn, existing, tenant_id)

    pipeline = KbPipeline(await get_pool(), _UnusedEmbedding())
    async with admin_pool.connection() as conn, conn.transaction():
        await _bind(conn, tenant_id)
        report = await regenerate_for_section(
            conn, pipeline, tenant_id=tenant_id, tenant_slug="autogen-f", section_role="hr"
        )

    assert report.n_chunks == 0, "fixture hỏng: phòng ban này phải THẬT SỰ chưa có chunk nào"
    assert report.n_cases == 0
    # `written=False` là nửa còn lại của guard (review app#71 đợt 2, mục 1). Bản trước chỉ trả
    # `n_cases=0`, không phân biệt được với ca "bộ vừa ghi và đúng là rỗng" — người bấm nút tin bộ
    # cũ đã biến mất, trong khi cổng publish vẫn chấm bằng đúng bộ cũ đang nằm dưới kia.
    assert report.written is False
    after = await _read_set(admin_pool, tenant_id, ref)
    assert [c.case_id for c in after.cases] == ["AI-OLD-01"], "bộ cũ bị ghi đè bằng một bộ rỗng"


@pytest.mark.asyncio
async def test_generated_set_contains_fence_cases_when_tenant_has_two_sections(admin_pool: Pool) -> None:
    """**Bài mang toàn bộ giá trị của bản vá phạm vi.** Bộ sinh phải CÓ case hàng rào.

    `build_cases` dựng case bẫy bằng cách ghép chéo vai. Bản đầu của `regenerate_for_section` đọc
    `chunks_for_section` — chunk của đúng một phòng ban — nên không còn gì để ghép chéo, và **mọi
    bộ nó sinh ra có 0 case hàng rào**. Cổng khi đó chấm chất lượng trả lời mà không bao giờ chấm
    hàng rào tenant, tức mất đúng trục duy nhất bắt được lỗi bịa-xuyên-chủ-thể (`engine#43`) —
    trục citation không bắt được, vì câu trả lời sai vẫn cho `citation_accuracy = 1.0` khi nó trích
    một chunk có thật.

    Assert trên TỔNG hai phòng ban chứ không một: `_dung_case_bay` rải case bẫy theo
    `khoa[i % len(khoa)]`, nên với bộ nhỏ thì bẫy có thể rơi trọn vào một vai. Điều bài này khoá là
    *"bộ sinh máy CÓ trục hàng rào"*, không phải *"vai nào cũng có"* — vế sau là chuyện của
    `sample_report`."""
    tenant_id = await _make_tenant(admin_pool, "autogen-fence", "hr", "finance")
    await _upload(tenant_id, "nghi-phep.md", _DOC_HR, "hr")
    await _upload(tenant_id, "phu-cap.md", _DOC_FINANCE, "finance")

    hr = await _read_set(admin_pool, tenant_id, auto_golden_set_ref("hr"))
    fin = await _read_set(admin_pool, tenant_id, auto_golden_set_ref("finance"))
    moi = [*hr.cases, *fin.cases]

    hang_rao = [c for c in moi if c.expects_refusal]
    assert hang_rao, (
        "bộ sinh máy KHÔNG có case hàng rào nào — cổng sẽ không bao giờ chấm trục từ chối. "
        f"Tổng {len(moi)} case: hr={len(hr.cases)} finance={len(fin.cases)}"
    )
    assert all(c.is_critical for c in hang_rao), "case hàng rào phải khai is_critical"
    assert all(c.tier == "core" for c in hang_rao), "case hàng rào phải thuộc tier core"

    # Chống rỗng-nghĩa: bộ phải có CẢ hai loại, không phải toàn bẫy.
    assert [c for c in moi if not c.expects_refusal], "bộ chỉ có bẫy thì cũng là bộ hỏng"


# MỘT mục dài hơn một cửa sổ chunk (850 từ/overlap 170): tiêu đề ở đầu, con số ở cuối. Đo được:
# cắt ra 2 chunk, chunk 1 có tiêu đề mà không có đáp án, chunk 2 ngược lại — KHÔNG chunk nào có cả
# hai, kể cả sau overlap.
#
# Cỡ này không phải dựng cho vừa bài test: tài liệu nội quy thật đo được ~26.000 từ chia ~30 mục,
# tức trung bình ~870 từ một mục — đúng ngay ngưỡng cửa sổ.
_DOC_LONG = (
    "## Nghỉ phép năm\n"
    + " ".join(["Nội dung diễn giải chi tiết về chính sách nghỉ phép của công ty."] * 90)
    + "\nNhân viên chính thức được nghỉ 12 ngày phép có lương.\n"
).encode()


async def test_a_long_document_generates_from_its_full_text_not_from_windows(admin_pool: Pool) -> None:
    """**Bài trung tâm của tầng tài liệu.** Tài liệu dài hơn một cửa sổ chunk vẫn ra câu hỏi của mục
    cuối — mục nằm sau ranh giới chunk.

    `cut_window` cắt theo SỐ TỪ (850/overlap 170), không theo mục. Trước bản vá này bộ sinh đọc từng
    chunk như thể đó là một tài liệu, nên nó dựng câu hỏi từ mảnh vụn: đo được trên hệ thật, tài
    liệu 31 chunk × 835 từ cho ra *"Xuất bản: Hà Nội & TP. Hồ Chí Minh là bao nhiêu năm?"*.

    Bài này khẳng định đường ghi (`save_document_text` lúc upload) và đường đọc
    (`document_texts_for_tenant` lúc sinh) NỐI được với nhau — hai nửa đó ở hai repo khác nhau, và
    mỗi nửa chạy đúng một mình vẫn không đảm bảo chúng gặp nhau."""
    tenant_id = await _make_tenant(admin_pool, "autogen-long", "hr")
    result = await _upload(tenant_id, "noi-quy-dai.md", _DOC_LONG, "hr")

    assert result.chunk_count > 1, "fixture phải dài hơn một cửa sổ chunk thì bài mới đo được"

    golden = await _read_set(admin_pool, tenant_id, result.golden_set_ref)
    queries = [c.query for c in golden.cases]
    assert any("Nghỉ phép năm" in q for q in queries), (
        f"mục nằm sau ranh giới chunk không ra câu hỏi — bộ sinh vẫn đọc ở tầng chunk: {queries}"
    )


async def test_deleting_a_document_regenerates_its_section_set(admin_pool: Pool) -> None:
    """**Xoá tài liệu phải sinh lại bộ**, đúng như đường upload.

    Trước bản vá này hai đường bất đối xứng: upload thì `delete_by_doc_id` + `index` +
    `regenerate_for_section` + `drop_pending_scorecards`, còn xoá thì CHỈ `delete_by_doc_id`.

    Hệ quả: case sinh từ tài liệu vừa xoá vẫn ở lại, mang `expected_citation` trỏ vào những
    `chunk_id` không còn tồn tại. Mỗi case như vậy chấm ra `citation_accuracy = 0` vĩnh viễn, và
    nhìn từ bảng điểm thì giống hệt *"agent trích sai"*. Đo được trên một DB thật: **12/84 trích
    dẫn mồ côi** sau vài lần xoá tài liệu."""
    tenant_id = await _make_tenant(admin_pool, "autogen-del", "hr")
    result = await _upload(tenant_id, "nghi-phep.md", _DOC_HR, "hr")
    assert result.golden_n_cases > 0

    deleted = await _delete(tenant_id, [result.doc_id])

    assert deleted.deleted_documents == [result.doc_id]
    # Đây là tài liệu DUY NHẤT của phòng ban ⇒ xoá HẲN bộ, không sinh lại.
    #
    # `regenerate_for_section` mang guard "rỗng thì không ghi": lượt sinh ra 0 case thì giữ nguyên
    # bộ cũ. Guard đó đúng cho ca *sinh hụt*, nhưng sai cho ca *không còn gì để chấm* — bộ cũ ở lại
    # với `expected_citation` trỏ vào những `chunk_id` đã biến mất, và cổng publish vẫn chấm bằng
    # đúng bộ đó.
    assert deleted.deleted_golden_sets == ["hr"]
    assert deleted.regenerated_sections == []

    with pytest.raises(GoldenSetNotFound):
        await _read_set(admin_pool, tenant_id, result.golden_set_ref)


async def test_deleting_one_of_two_documents_regenerates_instead_of_deleting(admin_pool: Pool) -> None:
    """Phòng ban còn tài liệu khác ⇒ SINH LẠI, không xoá bộ.

    Đối trọng bắt buộc của bài trên: "xoá tài liệu thì xoá bộ" nới thành vô điều kiện sẽ làm một
    lần dọn một tài liệu cũ xoá sạch bộ chấm của cả phòng ban còn đang dùng."""
    tenant_id = await _make_tenant(admin_pool, "autogen-del-2", "hr")
    first = await _upload(tenant_id, "nghi-phep.md", _DOC_HR, "hr")
    await _upload(tenant_id, "noi-quy-dai.md", _DOC_LONG, "hr")

    deleted = await _delete(tenant_id, [first.doc_id])

    assert deleted.deleted_golden_sets == []
    assert deleted.regenerated_sections == ["hr"]

    golden = await _read_set(admin_pool, tenant_id, first.golden_set_ref)
    orphans = [c for c in golden.cases for cid in c.expected_citation if cid.startswith(first.doc_id)]
    assert orphans == [], f"còn case trỏ vào tài liệu đã xoá: {[c.case_id for c in orphans]}"


async def test_deleting_nothing_does_not_touch_the_golden_set(admin_pool: Pool) -> None:
    """Đối trọng: một lượt xoá KHÔNG xoá được gì thì không sinh lại.

    Tác hại thật không nằm ở `regenerated_sections` (nó rỗng dù thế nào, vì không tài liệu nào khớp)
    mà ở `drop_pending_scorecards`: chạy vô điều kiện thì mỗi lần bấm nhầm id sẽ **huỷ sạch điểm
    Chấm-điểm-chưa-publish** của người dùng, và họ phải chấm lại từ đầu mà không hiểu vì sao.

    Đo đúng chỗ đó — mutation xác nhận cần: đổi `if deleted_documents:` thành `if True:` mà bài chỉ
    kiểm `regenerated_sections` vẫn xanh."""
    tenant_id = await _make_tenant(admin_pool, "autogen-del-noop", "hr")
    await _upload(tenant_id, "nghi-phep.md", _DOC_HR, "hr")
    async with admin_pool.connection() as conn, conn.transaction():
        await _bind(conn, tenant_id)
        await write_pending_scorecard(conn, _pending_scorecard(), tenant_id)

    deleted = await _delete(tenant_id, ["khong-ton-tai"])

    assert deleted.not_found == ["khong-ton-tai"]
    assert deleted.regenerated_sections == []
    async with admin_pool.connection() as conn, conn.transaction():
        await _bind(conn, tenant_id)
        still_there = await read_pending_scorecard(conn, "agent-noop", "hash-noop")
    assert still_there is not None, "xoá hụt không được huỷ điểm chấm đang chờ của người dùng"
