"""Pydantic field-validator dùng chung nhiều route (`routes/admin.py`, `routes/sections.py`) —
tách riêng thay vì import symbol private xuyên module."""

from __future__ import annotations


def reject_blank(v: str) -> str:
    """`.strip()` rồi chặn rỗng — `name=""`/`"   "` không được coi là hợp lệ ở bất kỳ field tên
    (company_name, section name...) nào dùng validator này."""
    stripped = v.strip()
    if not stripped:
        raise ValueError("không được để trống")
    return stripped


RESERVED_ROLE_NAMES = frozenset({"admin", "superadmin"})
"""2 role hệ thống KHÔNG được phép trùng tên với 1 "phòng ban" người dùng tự đặt (`routes/
sections.py`) — `routes/admin.py::create_user` ghép `section_names | {"admin"}` thành vocab role
hợp lệ, nên 1 section tên `"superadmin"` sẽ tự động cấp cho MỌI company-admin của tenant đó khả
năng tạo user với role `"superadmin"` (review `app#21` ⛔ — dựng lại được trên Postgres thật:
company-admin tạo user roles=["superadmin"] thành công, user đó gọi lọt route superadmin-only cho
tenant KHÁC). `require_superadmin` đọc roles TƯƠI từ DB (`authz.fetch_fresh_identity`) nên không
cứu được ca này — lỗ nằm ở "cái gì được phép TRỞ THÀNH role", không ở chỗ đọc role từ đâu."""


def reject_reserved_section_name(v: str) -> str:
    """`reject_blank` rồi chặn thêm tên trùng `RESERVED_ROLE_NAMES` — tầng 1 của bản vá 2 tầng
    (app#21 ⛔): chặn NGAY LÚC TẠO/ĐỔI TÊN phòng ban, trước khi nó có cơ hội lọt vào `valid_role_
    vocab`. Tầng 2 (phòng DB đã có sẵn dòng cũ từ TRƯỚC bản vá này) nằm ở `routes/admin.py::
    create_user` — trừ `RESERVED_ROLE_NAMES` khỏi `section_names` trước khi hợp với `{"admin"}`,
    nên dù 1 dòng `core.sections` cũ nào đó vẫn mang tên `"superadmin"`, nó không bao giờ leo được
    vào vocab role hợp lệ."""
    stripped = reject_blank(v)
    if stripped in RESERVED_ROLE_NAMES:
        raise ValueError(f"tên phòng ban không được trùng role hệ thống ({sorted(RESERVED_ROLE_NAMES)})")
    return stripped


def reject_oversized_password(v: str) -> str:
    """bcrypt chỉ băm được tối đa 72 BYTE (không phải 72 ký tự — 1 ký tự có dấu tiếng Việt có thể
    chiếm 2-3 byte UTF-8), vượt quá sẽ raise `ValueError` bên trong `hash_password` -> 500 không
    bắt được ở tầng route. Chặn ở Pydantic validator (422, không phải 500) — review `app#17`, nửa
    "create" của Chặn 2 (nửa "login" đã chặn riêng ở `jwt_auth.verify_password`). Dùng chung cho
    mọi request mang field mật khẩu mới (tạo company/user, đổi mật khẩu tự phục vụ)."""
    if len(v.encode("utf-8")) > 72:
        raise ValueError("mật khẩu tối đa 72 byte (giới hạn bcrypt)")
    return v
