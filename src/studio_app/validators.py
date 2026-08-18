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


def reject_oversized_password(v: str) -> str:
    """bcrypt chỉ băm được tối đa 72 BYTE (không phải 72 ký tự — 1 ký tự có dấu tiếng Việt có thể
    chiếm 2-3 byte UTF-8), vượt quá sẽ raise `ValueError` bên trong `hash_password` -> 500 không
    bắt được ở tầng route. Chặn ở Pydantic validator (422, không phải 500) — review `app#17`, nửa
    "create" của Chặn 2 (nửa "login" đã chặn riêng ở `jwt_auth.verify_password`). Dùng chung cho
    mọi request mang field mật khẩu mới (tạo company/user, đổi mật khẩu tự phục vụ)."""
    if len(v.encode("utf-8")) > 72:
        raise ValueError("mật khẩu tối đa 72 byte (giới hạn bcrypt)")
    return v
