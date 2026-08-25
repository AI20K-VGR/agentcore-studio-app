"""`POST/PATCH/DELETE/GET /api/admin/sections` — CRUD "phòng ban" theo tenant, thay thế
`SECTION_VOCAB` toàn cục cứng (`studio_kb.doc_factory`) đã dùng trước đây ở `routes/admin.py`.

Chỉ superadmin được thêm/sửa/xoá (đúng thiết kế: mỗi công ty tự khai tên phòng ban, nhưng
superadmin là người NHẬP vào hệ thống — tránh company-admin tự đổi taxonomy làm dữ liệu/role đã
gắn theo tên cũ trở nên mồ côi). Company-admin chỉ ĐỌC (`GET`, scoped đúng tenant mình).

`core.sections` KHÔNG bật RLS (xem comment ở `core/schema.py` ngay trên `CREATE TABLE
core.sections`) — ranh giới tenant enforce TƯỜNG MINH ở đây, không nhờ DB tự lọc, đúng tiền lệ
`routes/admin.py` đã dùng cho `core.users`/`core.tenants`."""

from __future__ import annotations

from uuid import UUID

import psycopg.errors
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from studio_app.authz import fetch_fresh_identity, require_superadmin
from studio_app.middleware import get_request_connection, get_request_session
from studio_app.validators import reject_reserved_section_name

router = APIRouter(prefix="/api/admin/sections", tags=["sections"])


class SectionResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    created_at: str


class CreateSectionRequest(BaseModel):
    # `tenant_id` CÓ mặt ở đây — ngoại lệ CÓ CHỦ ĐÍCH với nguyên tắc INV-1 ("không tin tenant_id
    # client tự khai", `routes/runs.py:49-52`): người gọi (superadmin) không có "tenant của session"
    # nào để mặc định — JWT của họ trỏ `__system__` (`scripts/seed_superadmin.py`), không phải
    # business tenant nào. Đúng pattern `company_name` ở `POST /api/admin/companies` cũng do client
    # khai vì lý do y hệt — chỉ hợp lệ vì route này CHỈ superadmin gọi được (`require_superadmin`
    # bên dưới), không phải bất kỳ ai đã đăng nhập.
    tenant_id: str
    name: str

    _validate_name = field_validator("name")(reject_reserved_section_name)


class RenameSectionRequest(BaseModel):
    name: str

    _validate_name = field_validator("name")(reject_reserved_section_name)


@router.post("", response_model=SectionResponse)
async def create_section(body: CreateSectionRequest) -> SectionResponse:
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_superadmin(identity.system_roles)

    # Parse UUID TRƯỚC khi query — cùng bug đã sửa ở routes/documents.py (review app#27,
    # dholmes0207/TranBaDat2607, finding #2): tenant_id sai định dạng đi thẳng vào WHERE id = %s
    # (cột UUID) sẽ làm psycopg raise lỗi cú pháp CHƯA BẮT ⇒ 500 thay vì 400 rõ ràng.
    try:
        UUID(body.tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"tenant_id không phải UUID hợp lệ: {body.tenant_id!r}") from exc

    cur = await conn.execute(
        "SELECT 1 FROM core.tenants WHERE id = %s AND name != '__system__'",
        (body.tenant_id,),
    )
    if await cur.fetchone() is None:
        raise HTTPException(status_code=404, detail=f"tenant_id {body.tenant_id!r} không tồn tại")

    try:
        cur = await conn.execute(
            "INSERT INTO core.sections (tenant_id, name, created_by) VALUES (%s, %s, %s) "
            "RETURNING id, tenant_id, name, created_at",
            (body.tenant_id, body.name, identity.id),
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail=f"phòng ban {body.name!r} đã tồn tại cho tenant này") from exc
    row = await cur.fetchone()
    assert row is not None
    return SectionResponse(id=str(row[0]), tenant_id=str(row[1]), name=row[2], created_at=row[3].isoformat())


@router.get("", response_model=list[SectionResponse])
async def list_sections(tenant_id: str | None = None) -> list[SectionResponse]:
    """Dual-gate — superadmin xem được BẤT KỲ tenant nào (bắt buộc khai `?tenant_id=`, không có
    "tenant mặc định" nào cho họ), company-admin CHỈ xem tenant chính mình (bỏ qua `tenant_id` nếu
    khớp sẵn tenant mình; 403 nếu cố truyền tenant khác — báo lỗi rõ thay vì âm thầm phớt lờ, đỡ
    1 lớp confuse phía client khi tự debug tại sao list rỗng)."""
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)

    if "superadmin" in identity.system_roles:
        if tenant_id is None:
            raise HTTPException(
                status_code=400, detail="superadmin phải khai ?tenant_id= để xem sections của công ty nào"
            )
        # Parse UUID TRƯỚC khi query — cùng finding #2 (review app#27).
        try:
            UUID(tenant_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"tenant_id không phải UUID hợp lệ: {tenant_id!r}") from exc
        target_tenant_id = tenant_id
    elif "admin" in identity.system_roles:
        if tenant_id is not None and tenant_id != str(identity.tenant_id):
            raise HTTPException(status_code=403, detail="company-admin chỉ xem được sections của tenant mình")
        target_tenant_id = str(identity.tenant_id)
    else:
        raise HTTPException(status_code=403, detail="Cần quyền admin hoặc superadmin.")

    cur = await conn.execute(
        "SELECT id, tenant_id, name, created_at FROM core.sections WHERE tenant_id = %s ORDER BY name",
        (target_tenant_id,),
    )
    rows = await cur.fetchall()
    return [
        SectionResponse(id=str(row[0]), tenant_id=str(row[1]), name=row[2], created_at=row[3].isoformat())
        for row in rows
    ]


@router.patch("/{section_id}", response_model=SectionResponse)
async def rename_section(section_id: str, body: RenameSectionRequest) -> SectionResponse:
    """Đổi tên — 1 transaction cascade sang `core.users.system_roles` (MỌI user trong tenant đó đang gắn
    tên cũ, đổi luôn sang tên mới). KHÔNG cascade sang `kb.chunks.section_role` — bảng đó hiện chỉ
    được ghi bởi `ingest_callisto.py` (fixture, hard-bind tenant `ankor`/`borea`), không có đường
    nào ghi cho 1 tenant thật do superadmin tạo (`KbPipeline` vẫn `NotImplementedError`) — cascade
    vào đó là dead code cho tính năng hiện tại. Bổ sung khi `KbPipeline` được implement thật."""
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_superadmin(identity.system_roles)

    async with conn.transaction():
        cur = await conn.execute(
            "SELECT tenant_id, name FROM core.sections WHERE id = %s FOR UPDATE",
            (section_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"section_id {section_id!r} không tồn tại")
        tenant_id, old_name = row

        try:
            cur = await conn.execute(
                "UPDATE core.sections SET name = %s WHERE id = %s RETURNING id, tenant_id, name, created_at",
                (body.name, section_id),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise HTTPException(status_code=409, detail=f"phòng ban {body.name!r} đã tồn tại cho tenant này") from exc
        updated = await cur.fetchone()
        assert updated is not None

        await conn.execute(
            "UPDATE core.users SET system_roles = array_replace(system_roles, %s, %s) "
            "WHERE tenant_id = %s AND %s = ANY(system_roles)",
            (old_name, body.name, tenant_id, old_name),
        )

    return SectionResponse(
        id=str(updated[0]), tenant_id=str(updated[1]), name=updated[2], created_at=updated[3].isoformat()
    )


@router.delete("/{section_id}", status_code=204)
async def delete_section(section_id: str) -> None:
    """Chặn (409) nếu còn `core.users` gắn role này — fail-closed, không xoá nửa vời để lại role
    mồ côi (không ai với tới được, nhưng vẫn nằm trong `system_roles[]` của user đó mãi mãi)."""
    session = get_request_session()
    conn = get_request_connection()
    identity = await fetch_fresh_identity(conn, session.user)
    require_superadmin(identity.system_roles)

    cur = await conn.execute(
        "SELECT tenant_id, name FROM core.sections WHERE id = %s",
        (section_id,),
    )
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"section_id {section_id!r} không tồn tại")
    tenant_id, name = row

    cur = await conn.execute(
        "SELECT count(*) FROM core.users WHERE tenant_id = %s AND %s = ANY(system_roles)",
        (tenant_id, name),
    )
    user_count_row = await cur.fetchone()
    assert user_count_row is not None
    user_count = user_count_row[0]
    if user_count > 0:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"còn {user_count} user đang gắn role {name!r} — gỡ role trước khi xoá",
                "user_count": user_count,
            },
        )

    await conn.execute("DELETE FROM core.sections WHERE id = %s", (section_id,))
