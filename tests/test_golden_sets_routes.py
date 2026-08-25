"""`POST /api/admin/golden-sets` — người dùng tự đưa bộ golden case vào hệ thống.

Đường thứ hai vào `eval.golden_sets`, cạnh bộ sinh máy (`studio_kb.golden_from_kb`). Bài ở đây chạy
trên Postgres thật vì thứ được chứng minh nằm ở tầng DB: RLS `FORCE` theo tenant, và cổng scope của
`golden_store` mà route phải **thoả** chứ không được vượt qua.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi import HTTPException
from studio_app import middleware
from studio_app.core._db import Pool, close_pools, get_pool
from studio_app.routes.golden_sets import UploadGoldenSetRequest, overlay_golden_set, upload_golden_set
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
