"""Bootstrap tài khoản superadmin ĐẦU TIÊN vào `core.users` — chạy tay 1 lần lúc khởi tạo hệ
thống, KHÔNG có endpoint API nào tạo được superadmin (nếu có, đó là 1 lỗ hổng leo quyền: ai xác
minh người gọi API đó có quyền phong superadmin cho chính họ?). Đây là cơ chế bootstrap NGOÀI
luồng API, giống tinh thần `seed_demo_tenants.py`.

Email/mật khẩu đọc từ BIẾN MÔI TRƯỜNG, KHÔNG hardcode trong file này — khác `seed_demo_tenants.py`
(UUID tenant không phải bí mật, hardcode vô hại), mật khẩu superadmin hardcode + commit lên git
sẽ tự nó thành 1 lỗ hổng mới (ai đọc được repo cũng biết mật khẩu).

`tenant_id` của superadmin trỏ vào 1 tenant HỆ THỐNG đặc biệt (`core.tenants.name = '__system__'`,
tự tạo nếu chưa có) — KHÔNG phải công ty thật. Xem giải thích đầy đủ ở `core/schema.py` (comment
ngay trên `CREATE TABLE core.users`): tenant_id CỐ Ý không nullable để không phải sửa dây chuyền
`ResolvedContext`/`tenant_wall.py`/RLS ở mọi bảng khác.

Chạy:
    uv run python apps/studio/scripts/seed_superadmin.py

Idempotent — chạy lại nhiều lần không tạo trùng (`ON CONFLICT (email) DO NOTHING`).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from studio_app.core._db import Pool, close_pools, get_admin_pool
from studio_app.core.schema import ensure_all_schemas
from studio_app.jwt_auth import hash_password, normalize_email

# Tự động tìm nạp file .env ở thư mục gốc (lùi 3 cấp thư mục từ apps/studio/scripts)
_ROOT_DIR = Path(__file__).resolve().parents[3]
load_dotenv(_ROOT_DIR / ".env")

_SYSTEM_TENANT_NAME = "__system__"

async def _ensure_system_tenant(admin: Pool) -> str:
    """Trả `id` (text) của tenant hệ thống, tự tạo nếu chưa có. Không dùng UUID hardcode như
    `seed_demo_tenants.py` (ankor/borea) vì tenant này không cần khớp hằng số ở quadrant nào khác
    — không package nào khác biết/cần biết tới `__system__`."""
    async with admin.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO core.tenants (name) VALUES (%s) "
            "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name "  # ép RETURNING chạy cả khi đã tồn tại
            "RETURNING id",
            (_SYSTEM_TENANT_NAME,),
        )
        row = await cur.fetchone()
    assert row is not None
    return str(row[0])


async def seed_superadmin() -> None:
    email = os.environ.get("STUDIO_SUPERADMIN_EMAIL")
    password = os.environ.get("STUDIO_SUPERADMIN_PASSWORD")
    if not email or not password:
        raise SystemExit(
            "STUDIO_SUPERADMIN_EMAIL và STUDIO_SUPERADMIN_PASSWORD chưa đặt — cần cả hai. "
            "Không có default (fail-loud): superadmin không được phép tạo bằng giá trị đoán được."
        )
    # Cùng `normalize_email()` dùng ở `routes/admin.py`/`routes/auth.py::login` — nếu ghi email
    # không chuẩn hoá ở đây, `login()` (tra bằng email đã `.strip().lower()`) sẽ không khớp được
    # dòng superadmin (review `app#17`, "nên sửa" #1).
    try:
        email = normalize_email(email)
    except ValueError as exc:
        raise SystemExit(f"STUDIO_SUPERADMIN_EMAIL không hợp lệ: {exc}") from exc
    # Cùng chính sách `CreateCompanyRequest.admin_password`/`CreateUserRequest.password`
    # (`routes/admin.py`, `Field(min_length=8)` + guard 72 byte) — trước bản vá, script này KHÔNG
    # kiểm gì cả, nên tài khoản quyền CAO NHẤT hệ thống lại có chính sách mật khẩu YẾU NHẤT (review
    # `app#17` đợt 2, "nhẹ hơn nên biết"). >72 byte sẽ raise `ValueError` bên trong `hash_password`
    # nếu để lọt xuống dưới — chặn ở đây, thông điệp rõ ràng, thay vì traceback khó hiểu.
    password_bytes = len(password.encode("utf-8"))
    if password_bytes < 8:
        raise SystemExit("STUDIO_SUPERADMIN_PASSWORD tối thiểu 8 byte.")
    if password_bytes > 72:
        raise SystemExit("STUDIO_SUPERADMIN_PASSWORD tối đa 72 byte (giới hạn bcrypt).")

    admin = await get_admin_pool()
    await ensure_all_schemas(admin)  # đảm bảo core.users + core.tenants tồn tại trước khi INSERT
    system_tenant_id = await _ensure_system_tenant(admin)

    async with admin.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, roles, created_by) "
            "VALUES (%s, %s, %s, %s, NULL) "
            "ON CONFLICT (email) DO NOTHING "
            "RETURNING id",
            (system_tenant_id, email, hash_password(password), ["superadmin"]),
        )
        row = await cur.fetchone()

    # `ON CONFLICT ... DO NOTHING` không trả dòng nào khi email đã tồn tại — nếu vẫn in "Seeded ...",
    # ai chạy lại script để XOAY mật khẩu superadmin sẽ nhận thông báo thành công trong khi mật khẩu
    # KHÔNG đổi (review `app#17`, "nên sửa" #4: thực nghiệm xác nhận mật khẩu cũ vẫn còn đúng, mật
    # khẩu mới không được ghi). `RETURNING id` phân biệt 2 ca — không có dòng nào nghĩa là hàng đã
    # tồn tại từ trước, script không đổi gì cả, phải nói rõ thay vì báo giả.
    if row is None:
        print(f"Superadmin {email!r} đã tồn tại trong core.users — không đổi gì (mật khẩu giữ nguyên).")
    else:
        print(f"Seeded superadmin {email!r} (tenant hệ thống {system_tenant_id}) vào core.users.")


async def main() -> None:
    try:
        await seed_superadmin()
    finally:
        await close_pools()


if __name__ == "__main__":
    if sys.platform == "win32":
        # Windows mặc định ProactorEventLoop; psycopg async từ chối thẳng ("Psycopg cannot use
        # the 'ProactorEventLoop' to run in async mode"). Selector* là loop chính lỗi đó khuyến
        # nghị, có sẵn trong stdlib mọi hệ điều hành script này chạy — không thêm dependency nào.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
