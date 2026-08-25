"""`POST /api/admin/golden-sets` — người dùng tự đưa bộ golden case vào hệ thống.

Đường thứ hai vào `eval.golden_sets`, cạnh bộ sinh máy (`studio_kb.golden_from_kb`). Bài ở đây chạy
trên Postgres thật vì thứ được chứng minh nằm ở tầng DB: RLS `FORCE` theo tenant, và cổng scope của
`golden_store` mà route phải **thoả** chứ không được vượt qua.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from io import BytesIO
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException, UploadFile
from studio_app import middleware
from studio_app.core._db import Pool, close_pools, get_pool
from studio_app.routes.documents import upload_document
from studio_app.routes.golden_sets import (
    RegenerateRequest,
    UploadGoldenSetRequest,
    overlay_golden_set,
    regenerate_golden_set,
    upload_golden_set,
)
from studio_evalhub.golden_case import GoldenCase, GoldenSet
from studio_evalhub.golden_store import read_golden_set
from studio_workbench.tenant_wall import ResolvedContext


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    yield
    await close_pools()


def _set_session(*, tenant_id: UUID, user: str, system_roles: list[str]) -> object:
    return middleware._request_session.set(ResolvedContext(tenant_id=tenant_id, user=user, system_roles=system_roles))


@asynccontextmanager
async def _simulate_request_connection() -> AsyncIterator[None]:
    pool = await get_pool()
    async with pool.connection() as conn:
        await conn.execute("SELECT set_config('app.tenant_id', %s, true)", ("",))
        token = middleware._request_conn.set(conn)
        try:
            yield
        finally:
            middleware._request_conn.reset(token)


async def _seed_tenant(admin_pool: Pool, name: str) -> UUID:
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", (name,))
        row = await cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


_VALID_MD = b"# Chinh sach\n\n## Nghi phep\nNhan vien chinh thuc duoc 12 ngay phep nam moi nam.\n"


async def _seed_section(admin_pool: Pool, tenant_id: UUID, name: str, created_by: UUID) -> None:
    async with admin_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO core.sections (tenant_id, name, created_by) VALUES (%s, %s, %s)",
            (str(tenant_id), name, str(created_by)),
        )


def _md_upload_file(filename: str, content: bytes) -> UploadFile:
    return UploadFile(filename=filename, file=BytesIO(content))


async def _seed_user(admin_pool: Pool, tenant_id: UUID, email: str, system_roles: list[str]) -> UUID:
    async with admin_pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, system_roles) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (str(tenant_id), email, "not-a-real-hash", system_roles),
        )
        row = await cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _case(case_id: str, *, bay: bool = False, **thay_doi: Any) -> dict[str, Any]:
    """Case tối thiểu hợp lệ. `bay=True` dựng case bẫy chéo-tenant (đáp án ở tenant khác)."""
    base: dict[str, Any] = {
        "case_id": case_id,
        "query": "Nghỉ phép năm được bao nhiêu ngày?",
        "tenant": "ankor",
        "section_roles": ["hr"],
        "expected_tenant": "borea" if bay else "ankor",
        "expected_section_role": "hr",
        "expected": "refusal" if bay else "12 ngày",
        "expected_citation": [] if bay else ["ankor-leave-001#c1"],
    }
    base.update(thay_doi)
    return base


async def test_company_admin_nap_bo_roi_doc_lai_ra_dung(admin_pool: Pool) -> None:
    """Vòng tròn: route ghi → `read_golden_set` đọc lại ra đúng bộ, đúng tenant."""
    tenant_id = await _seed_tenant(admin_pool, "golden-probe-a")
    await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", system_roles=["admin"])
    try:
        async with _simulate_request_connection():
            result = await upload_golden_set(
                UploadGoldenSetRequest(golden_set_ref="bo-cua-toi", cases=[_case("U-01"), _case("U-02", bay=True)])
            )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert result.n_case == 2
    assert result.n_traps == 1
    assert result.tenant_id == str(tenant_id)

    pool = await get_pool()
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
        doc_lai = await read_golden_set(conn, "bo-cua-toi", tenant_id)
    assert [c.case_id for c in doc_lai.cases] == ["U-01", "U-02"]


async def test_n_traps_derived_from_expects_refusal_not_from_payload(admin_pool: Pool) -> None:
    """`n_traps` suy từ **hai trục tenant/vai**, không từ một cờ người nạp tự khai.

    Case dưới đây khai `expected_citation` **không rỗng** (trông như case trả-lời-được) nhưng
    `expected_section_role` nằm **ngoài** `section_roles` — tức nó là bẫy T6 theo luật của
    `GoldenCase.expects_refusal`. Một bản cài đặt đếm bằng `not expected_citation` sẽ trả `0` và
    người nạp tin rằng bộ của họ không có nhánh hàng rào."""
    tenant_id = await _seed_tenant(admin_pool, "golden-probe-b")
    await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", system_roles=["admin"])
    try:
        async with _simulate_request_connection():
            result = await upload_golden_set(
                UploadGoldenSetRequest(
                    golden_set_ref="bay-t6",
                    cases=[
                        _case(
                            "T6-01",
                            expected_section_role="finance",
                            expected_citation=["ankor-fin-001#c1"],
                        )
                    ],
                )
            )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert result.n_traps == 1, "case chéo-vai phải được đếm là bẫy dù expected_citation không rỗng"


async def test_route_KHONG_khai_ho_source(admin_pool: Pool) -> None:
    """**Route không suy nguồn gốc hộ người dùng.** `source` vắng ⇒ đọc lại vẫn `None`.

    Cám dỗ là ép `source="human"` cho mọi case vì *"người upload mà"*. Nhưng route chỉ biết một sự
    thật: **một người đã nạp tệp này** — không biết từng case do người viết hay do máy sinh rồi
    người sửa. Ép giá trị là khai hộ, đúng thứ mặc định `None` tồn tại để tránh. Và nó không vô hại:
    dedup lúc hợp nhất dùng `source` để quyết *"human wins"*, nên một nhãn `human` bịa ra sẽ **ghi
    đè** một case người thật viết."""
    tenant_id = await _seed_tenant(admin_pool, "golden-probe-c")
    await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", system_roles=["admin"])
    try:
        async with _simulate_request_connection():
            await upload_golden_set(
                UploadGoldenSetRequest(
                    golden_set_ref="khong-khai-source",
                    cases=[_case("U-01"), _case("U-02", source="human")],
                )
            )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    pool = await get_pool()
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
        doc_lai = await read_golden_set(conn, "khong-khai-source", tenant_id)

    theo_id = {c.case_id: c.source for c in doc_lai.cases}
    assert theo_id["U-01"] is None, "case không khai source phải giữ None, không bị ép 'human'"
    assert theo_id["U-02"] == "human", "case tự khai source phải được giữ nguyên"


async def test_field_go_sai_tra_422_va_neu_dich_danh_field(admin_pool: Pool) -> None:
    """`extra="forbid"` biến một field gõ sai thành lỗi **ngay tại request**, nêu đích danh field.

    Nhận dict thô rồi ghi thẳng JSONB sẽ để `expected_citaion` (thiếu `t`) nằm im trong DB tới lần
    chấm đầu tiên — và lúc đó nó biểu hiện thành `citation_accuracy = 0`, tức một **con số**, không
    phải một lỗi."""
    tenant_id = await _seed_tenant(admin_pool, "golden-probe-d")
    await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])
    sai = _case("U-01")
    sai["expected_citaion"] = sai.pop("expected_citation")

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", system_roles=["admin"])
    try:
        with pytest.raises(HTTPException) as bat:
            async with _simulate_request_connection():
                await upload_golden_set(UploadGoldenSetRequest(golden_set_ref="go-sai", cases=[sai]))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert bat.value.status_code == 422
    assert "expected_citaion" in str(bat.value.detail)


async def test_bo_rong_tra_422(admin_pool: Pool) -> None:
    """Bộ 0 case ⇒ 422. Một bộ rỗng ghi được sẽ cho `success_rate` trên mẫu số 0 ở lần chấm đầu."""
    tenant_id = await _seed_tenant(admin_pool, "golden-probe-e")
    await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", system_roles=["admin"])
    try:
        with pytest.raises(HTTPException) as bat:
            async with _simulate_request_connection():
                await upload_golden_set(UploadGoldenSetRequest(golden_set_ref="rong", cases=[]))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert bat.value.status_code == 422


async def test_company_admin_khai_tenant_khac_tra_403(admin_pool: Pool) -> None:
    """Dual-gate y hệt `routes/documents.py`: khai tenant khác ⇒ **403**, không phải bị bỏ qua.

    Im lặng bỏ qua một tham số quyền hạn là cách một client tưởng mình làm được việc mà thực ra
    không — và ở đây "việc" là ghi bộ chấm cho công ty khác."""
    tenant_id = await _seed_tenant(admin_pool, "golden-probe-f")
    khac = await _seed_tenant(admin_pool, "golden-probe-f-khac")
    await _seed_user(admin_pool, tenant_id, "admin@acme.com", ["admin"])

    token = _set_session(tenant_id=tenant_id, user="admin@acme.com", system_roles=["admin"])
    try:
        with pytest.raises(HTTPException) as bat:
            async with _simulate_request_connection():
                await upload_golden_set(
                    UploadGoldenSetRequest(golden_set_ref="cua-nguoi-khac", cases=[_case("U-01")], tenant_id=str(khac))
                )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert bat.value.status_code == 403


async def test_superadmin_khong_khai_tenant_tra_400(admin_pool: Pool) -> None:
    """Superadmin **bắt buộc** khai `tenant_id` — JWT của họ trỏ `__system__`, không có tenant mặc
    định. Không có phép kiểm này thì bộ sẽ được ghi cho tenant `__system__`, một chỗ không ai chấm."""
    sys_tenant = await _seed_tenant(admin_pool, "golden-probe-g-system")
    await _seed_user(admin_pool, sys_tenant, "root@system.com", ["superadmin"])

    token = _set_session(tenant_id=sys_tenant, user="root@system.com", system_roles=["superadmin"])
    try:
        with pytest.raises(HTTPException) as bat:
            async with _simulate_request_connection():
                await upload_golden_set(UploadGoldenSetRequest(golden_set_ref="thieu-tenant", cases=[_case("U-01")]))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert bat.value.status_code == 400


async def test_superadmin_khai_tenant_thi_ghi_duoc_cho_tenant_do(admin_pool: Pool) -> None:
    """Superadmin nạp hộ tenant khác — đường này **thoả** cổng scope của `golden_store`, không vượt.

    `write_golden_set` từ chối khi `app.tenant_id` của phiên khác `tenant_id` được truyền
    (`GoldenSetScopeError`). Phiên superadmin trỏ `__system__`, nên route bind lại **tường minh**
    bằng `SET LOCAL` trong đúng transaction ghi: connection thật sự được scope về tenant đích, và
    `SET LOCAL` hết hiệu lực khi transaction đóng nên phần còn lại của request không thừa hưởng.

    Bài này chứng minh đường đó **hoạt động**; bài `test_company_admin_khai_tenant_khac_tra_403`
    chứng minh nó **không mở cho người không có quyền**. Thiếu một trong hai thì hoặc superadmin
    không nạp hộ được, hoặc ai cũng nạp hộ được."""
    sys_tenant = await _seed_tenant(admin_pool, "golden-probe-h-system")
    dich = await _seed_tenant(admin_pool, "golden-probe-h-dich")
    await _seed_user(admin_pool, sys_tenant, "root@system.com", ["superadmin"])

    token = _set_session(tenant_id=sys_tenant, user="root@system.com", system_roles=["superadmin"])
    try:
        async with _simulate_request_connection():
            result = await upload_golden_set(
                UploadGoldenSetRequest(golden_set_ref="nap-ho", cases=[_case("U-01")], tenant_id=str(dich))
            )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert result.tenant_id == str(dich)

    pool = await get_pool()
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(dich),))
        assert len((await read_golden_set(conn, "nap-ho", dich)).cases) == 1


def _overlay_case(case_id: str, query: str, source: str | None = None) -> GoldenCase:
    return GoldenCase(
        case_id=case_id,
        query=query,
        tenant="ankor",
        section_roles=["hr"],
        expected_tenant="ankor",
        expected_section_role="hr",
        expected="12 ngày",
        expected_citation=["d#c1"],
        source=source,
    )


def test_overlay_thay_case_trung_khoa_va_giu_phan_con_lai() -> None:
    """Luật phủ: case vừa nạp thắng trên khoá của nó, case cũ khác khoá giữ nguyên.

    Khoá là `(tenant, câu hỏi chuẩn hoá, phòng ban)` — KHÔNG phải `case_id`. Nên `U-01` thay được
    `AI-1` dù hai id chẳng liên quan gì nhau: chúng hỏi cùng một câu."""
    uploaded = GoldenSet(golden_set_ref="x", cases=[_overlay_case("U-01", "Nghỉ phép bao nhiêu ngày?")])
    existing = GoldenSet(
        golden_set_ref="x",
        cases=[
            _overlay_case("AI-1", "nghỉ phép bao nhiêu ngày?", "ai"),
            _overlay_case("AI-2", "Bảo hiểm ra sao?", "ai"),
        ],
    )

    merged, n_kept = overlay_golden_set(uploaded, existing)

    assert [c.case_id for c in merged.cases] == ["U-01", "AI-2"]
    assert n_kept == 1, "case cũ không trùng khoá phải được giữ — nếu không là xoá trắng bộ máy sinh"


def test_overlay_nap_lai_y_het_khong_doi_gi() -> None:
    """Hồi quy DE bắt ở review app#68. Bản trước dùng `merge_golden_sets`, mà hàm đó phân xử va
    chạm bằng luật `source` (chỉ `human` thắng `ai`) nên **mọi** cặp khác đều ném — kể cả hai case
    giống hệt nhau. Hệ quả: chạy lại script, thử lại sau lỗi mạng, hay sửa một case rồi gửi cả bộ
    đều trả 409, trong khi `write_golden_set` vốn là upsert idempotent."""
    uploaded = GoldenSet(golden_set_ref="x", cases=[_overlay_case("U-01", "Nghỉ phép bao nhiêu ngày?")])
    existing = GoldenSet(golden_set_ref="x", cases=[_overlay_case("AI-2", "Bảo hiểm ra sao?", "ai")])

    first, _ = overlay_golden_set(uploaded, existing)
    second, _ = overlay_golden_set(uploaded, first)

    assert [c.case_id for c in first.cases] == [c.case_id for c in second.cases]


def test_overlay_bo_cu_rong_thi_ra_dung_bo_vua_nap() -> None:
    merged, n_kept = overlay_golden_set(
        GoldenSet(golden_set_ref="x", cases=[_overlay_case("U-01", "q?")]),
        GoldenSet(golden_set_ref="x", cases=[]),
    )
    assert [c.case_id for c in merged.cases] == ["U-01"]
    assert n_kept == 0


async def test_regenerate_rebuilds_without_uploading_a_document(admin_pool: Pool) -> None:
    """`POST /regenerate` — dựng lại bộ của một phòng ban từ tài liệu ĐANG CÓ.

    Trước route này, đường sinh máy chỉ chạy như tác dụng phụ của việc nạp tài liệu. Một tenant đã
    nạp đủ tài liệu mà muốn dựng lại bộ không có cách nào ngoài upload lại một file bất kỳ — bắt
    người dùng làm một việc không liên quan để đạt việc họ cần.
    """
    tenant_id = await _seed_tenant(admin_pool, f"regen-{uuid4().hex[:8]}")
    admin_id = await _seed_user(admin_pool, tenant_id, "admin@regen.test", ["admin"])
    await _seed_section(admin_pool, tenant_id, "hr", admin_id)

    token = _set_session(tenant_id=tenant_id, user="admin@regen.test", system_roles=["admin"])
    try:
        async with _simulate_request_connection():
            await upload_document(file=_md_upload_file("Chinh sach.md", _VALID_MD), section_role="hr", tenant_id=None)
        async with _simulate_request_connection():
            result = await regenerate_golden_set(RegenerateRequest(section_role="hr"))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert result.golden_set_ref == "kb-hr-auto-v1"
    assert result.n_cases > 0, "dựng lại từ tài liệu đang có mà ra bộ rỗng"
    assert result.n_ai == result.n_cases and result.n_human == 0


async def test_regenerate_KEEPS_user_written_cases(admin_pool: Pool) -> None:
    """Vế đắt, và là hợp đồng mà cả form nhập tay lẫn file mẫu đứng lên trên.

    `golden_autogen` sinh lại phần máy và **chỉ giữ** case `source="human"`. Nếu vế này hỏng thì mọi
    câu người dùng gõ tay biến mất ở lần dựng lại kế tiếp — im lặng, không cảnh báo, và họ chỉ phát
    hiện khi mở bộ ra xem."""
    tenant_id = await _seed_tenant(admin_pool, f"regen-h-{uuid4().hex[:8]}")
    admin_id = await _seed_user(admin_pool, tenant_id, "admin@regenh.test", ["admin"])
    await _seed_section(admin_pool, tenant_id, "hr", admin_id)
    tenant_slug = ""

    token = _set_session(tenant_id=tenant_id, user="admin@regenh.test", system_roles=["admin"])
    try:
        async with _simulate_request_connection():
            await upload_document(file=_md_upload_file("Chinh sach.md", _VALID_MD), section_role="hr", tenant_id=None)
        async with _simulate_request_connection():
            conn = middleware.get_request_connection()
            cur = await conn.execute("SELECT name FROM core.tenants WHERE id = %s", (tenant_id,))
            row = await cur.fetchone()
            assert row is not None
            tenant_slug = str(row[0])
            await upload_golden_set(
                UploadGoldenSetRequest(
                    golden_set_ref="kb-hr-auto-v1",
                    cases=[
                        {
                            "case_id": "HUMAN-001",
                            "query": "Câu người dùng tự viết, không trùng câu máy sinh",
                            "tenant": tenant_slug,
                            "section_roles": ["hr"],
                            "expected_tenant": tenant_slug,
                            "expected_section_role": "hr",
                            "expected": "đáp án tay",
                            "expected_citation": [],
                            "source": "human",
                        }
                    ],
                )
            )
        async with _simulate_request_connection():
            after = await regenerate_golden_set(RegenerateRequest(section_role="hr"))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert after.n_human == 1, "case người dùng tự viết bị xoá khi dựng lại"


async def test_regenerate_rejects_unknown_section(admin_pool: Pool) -> None:
    """400 kèm danh sách phòng ban hợp lệ, không phải 500 — người gõ sai tên phòng ban tự sửa được."""
    tenant_id = await _seed_tenant(admin_pool, f"regen-x-{uuid4().hex[:8]}")
    admin_id = await _seed_user(admin_pool, tenant_id, "admin@regenx.test", ["admin"])
    await _seed_section(admin_pool, tenant_id, "hr", admin_id)

    token = _set_session(tenant_id=tenant_id, user="admin@regenx.test", system_roles=["admin"])
    try:
        async with _simulate_request_connection():
            with pytest.raises(HTTPException) as raised:
                await regenerate_golden_set(RegenerateRequest(section_role="khong-co-that"))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert raised.value.status_code == 400
    assert "hr" in str(raised.value.detail)


async def test_regenerate_superadmin_without_tenant_id_returns_400(admin_pool: Pool) -> None:
    """Superadmin không có tenant mặc định — JWT của họ trỏ `__system__`, không phải một công ty
    thật. Thiếu `tenant_id` phải là 400 chứ không phải im lặng dựng lại cho tenant hệ thống."""
    tenant_id = await _seed_tenant(admin_pool, f"regen-sa-{uuid4().hex[:8]}")
    await _seed_user(admin_pool, tenant_id, "root@sys.test", ["superadmin"])

    token = _set_session(tenant_id=tenant_id, user="root@sys.test", system_roles=["superadmin"])
    try:
        async with _simulate_request_connection():
            with pytest.raises(HTTPException) as raised:
                await regenerate_golden_set(RegenerateRequest(section_role="hr"))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert raised.value.status_code == 400
    assert "tenant_id" in str(raised.value.detail)


async def test_regenerate_company_admin_declaring_other_tenant_returns_403(admin_pool: Pool) -> None:
    """Hàng rào tenant trên đường DỰNG LẠI. Route này ghi đè cả bộ golden của một phòng ban, nên
    một company-admin dựng lại hộ tenant khác là xoá dữ liệu chấm điểm của công ty đó."""
    tenant_a = await _seed_tenant(admin_pool, f"regen-a-{uuid4().hex[:8]}")
    tenant_b = await _seed_tenant(admin_pool, f"regen-b-{uuid4().hex[:8]}")
    admin_a = await _seed_user(admin_pool, tenant_a, "admin@a.test", ["admin"])
    await _seed_section(admin_pool, tenant_a, "hr", admin_a)

    token = _set_session(tenant_id=tenant_a, user="admin@a.test", system_roles=["admin"])
    try:
        async with _simulate_request_connection():
            with pytest.raises(HTTPException) as raised:
                await regenerate_golden_set(RegenerateRequest(section_role="hr", tenant_id=str(tenant_b)))
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert raised.value.status_code == 403


async def test_regenerate_unknown_tenant_returns_404_not_400_about_section(admin_pool: Pool) -> None:
    """Review app#71: thứ tự hai phép kiểm quyết định người đọc lỗi đi sửa chỗ nào.

    Tenant không tồn tại thì MỌI phòng ban đều "không hợp lệ", nên kiểm phòng ban trước sẽ trả một
    câu 400 chỉ sai chỗ — người dùng đi sửa tên phòng ban trong khi lỗi nằm ở `tenant_id`."""
    tenant_id = await _seed_tenant(admin_pool, f"regen-404-{uuid4().hex[:8]}")
    await _seed_user(admin_pool, tenant_id, "root404@sys.test", ["superadmin"])

    token = _set_session(tenant_id=tenant_id, user="root404@sys.test", system_roles=["superadmin"])
    try:
        async with _simulate_request_connection():
            with pytest.raises(HTTPException) as raised:
                await regenerate_golden_set(
                    RegenerateRequest(section_role="hr", tenant_id="00000000-0000-0000-0000-000000000000")
                )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert raised.value.status_code == 404, f"phải 404 về tenant, thấy {raised.value.status_code}"


async def test_read_only_paths_still_work_when_embedding_provider_is_BROKEN(
    admin_pool: Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Đường dựng-lại/liệt-kê/xoá phải sống được khi provider embedding hỏng hoặc thiếu key.

    Đây là bài **còn thiếu** khiến defect tái phát: `documents.py` đã đổi sang `ReadOnlyEmbedding`
    từ app#68, nhưng không bài nào phủ đường đó, nên route `regenerate` viết sau đó dùng lại
    `build_embedding` mà mọi thứ vẫn xanh (review app#71, Dozyboy). Tôi gieo mutant để chắc: đổi
    ngược về `build_embedding` → 17 bài **vẫn xanh**.

    Phát biểu bằng HÀNH VI, không bằng "route phải dùng class X": *nếu `build_embedding` ném thì ba
    đường này vẫn phải chạy*. Đó mới là điều người vận hành cần — và nó đúng vào lúc cần nhất, khi
    provider đang hỏng thì dựng lại bộ hoặc xoá tài liệu là đường thoát hiểm."""
    from studio_app.providers import factory as factory_module
    from studio_app.routes import documents as documents_module
    from studio_app.routes import golden_sets as golden_sets_module

    def _boom() -> object:
        raise HTTPException(status_code=503, detail="STUDIO_OPENROUTER_API_KEY thiếu (mô phỏng)")

    # Vá ở CẢ ba chỗ tên có thể được tra: module gốc và hai route đã `from … import` vào namespace
    # riêng. Chỉ vá module gốc thì một route đã bind sẵn tên sẽ lọt qua bài này.
    for mod in (factory_module, documents_module, golden_sets_module):
        monkeypatch.setattr(mod, "build_embedding", _boom, raising=False)

    tenant_id = await _seed_tenant(admin_pool, f"noembed-{uuid4().hex[:8]}")
    admin_id = await _seed_user(admin_pool, tenant_id, "admin@noembed.test", ["admin"])
    await _seed_section(admin_pool, tenant_id, "hr", admin_id)

    token = _set_session(tenant_id=tenant_id, user="admin@noembed.test", system_roles=["admin"])
    try:
        async with _simulate_request_connection():
            listed = await documents_module.list_documents(tenant_id=None)
            regenerated = await golden_sets_module.regenerate_golden_set(RegenerateRequest(section_role="hr"))
            deleted = await documents_module.delete_documents(
                documents_module.DeleteDocumentsRequest(ids=["khong-co-that"]), tenant_id=None
            )
    finally:
        middleware._request_session.reset(token)  # type: ignore[arg-type]

    assert listed.documents == []
    assert regenerated.n_cases == 0, "tenant chưa có tài liệu nên bộ rỗng — điều cần đo là KHÔNG ném 503"
    assert deleted.not_found == ["khong-co-that"]
