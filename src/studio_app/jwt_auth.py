"""Ký/verify JWT cho identity `{tenant_id, user, roles}`, cộng `hash_password()`/`verify_password()`
(bcrypt) dùng chung cho cả login lẫn tạo tài khoản (Kế hoạch 3).

Chữ ký được verify bằng `settings.jwt_secret` (HS256) — một request không có khoá bí mật đó KHÔNG
THỂ tự chế ra 1 token hợp lệ.

`issue_token()` giờ CHỈ được gọi từ `routes/auth.py::login`, SAU KHI đã verify mật khẩu thật khớp
`core.users.password_hash` — khác giai đoạn trước (`routes/auth.py::demo_login`, đã bị xoá hẳn),
lúc đó `issue_token()` ký bất kỳ `tenant`/`user`/`roles` nào caller đưa vào, không có bước kiểm mật
khẩu nào đứng trước. Câu hỏi "ai được phép TỰ XIN token cho tenant/user nào?" giờ đã đóng bằng mật
khẩu thật, không còn là identity provider để trống như giai đoạn demo-login.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import bcrypt
import jwt
from loguru import logger
from studio_workbench.tenant_wall import ResolvedContext

from studio_app.settings import get_settings

_ALGORITHM = "HS256"


def hash_password(plain: str) -> str:
    """Băm mật khẩu bằng bcrypt (cost mặc định 12, đủ chậm chống brute-force mà không làm login
    chậm cảm nhận được). Kết quả là chuỗi tự chứa salt — không cần lưu salt riêng."""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, password_hash: str) -> bool:
    """So mật khẩu client gửi với hash đã lưu. Trả `bool` thuần (không raise) — sai mật khẩu là kết
    quả BÌNH THƯỜNG của việc gõ nhầm, không phải lỗi hệ thống, khác hẳn `verify_token` (JWT sai/hết
    hạn luôn là bất thường, phải raise `InvalidTokenError`).

    `bcrypt.checkpw` raise `ValueError` cho mật khẩu >72 byte thay vì trả `False` — nếu để lọt,
    `routes/auth.py::login` sẽ 500 cho email tồn tại nhưng 401 cho email không tồn tại (short-circuit
    trước khi gọi hàm này), lộ ra sự tồn tại của email qua status code (review `app#17`, Chặn 2).
    Chặn ở đây — trước bcrypt, không phải trong route — để MỌI call site (login lẫn tương lai) đều
    fail-closed giống nhau, không phải nhớ tự chặn ở từng nơi gọi.

    `bcrypt.checkpw` cũng raise `ValueError` (không phải trả `False`) nếu `password_hash` KHÔNG
    đúng định dạng bcrypt (VD `"Invalid salt"` — thực nghiệm xác nhận). `core.users.password_hash`
    là `TEXT` thuần, không có CHECK constraint ràng định dạng ở DB — nếu 1 dòng nào đó có hash hỏng
    (lỗi ghi dữ liệu, sửa tay, migration tương lai), request login đúng email đó sẽ 500 thay vì
    401, lại là 1 oracle khác biệt account đó tồn tại (review `app#17` đợt 3, Important). Bắt luôn
    `ValueError` ở đây — cùng chỗ đã chặn ca >72 byte, cùng lý do fail-closed cho mọi call site.

    Log `warning` khi rơi vào nhánh hash hỏng (review `app#17` đợt 3, "log khi hash hỏng"): fail-
    closed (trả `False`, account đó không login được) là đúng, nhưng NẾU không log gì, dữ liệu
    hỏng này sẽ nằm im vô thời hạn — không ai biết để sửa, account đó coi như "quên mật khẩu" mãi
    mãi mà không có dấu vết nào để debug. KHÔNG log `plain`/`password_hash` (chính giá trị mật
    khẩu/hash thật) — chỉ log SỰ KIỆN "gặp hash hỏng", đủ để vận hành biết có dữ liệu cần sửa mà
    không tự nó lại thành 1 chỗ rò rỉ mới."""
    if len(plain.encode("utf-8")) > 72:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        logger.warning("[AUTH] verify_password: password_hash không đúng định dạng bcrypt — coi như sai mật khẩu")
        return False


# Hash cố định của 1 chuỗi không phải mật khẩu thật của ai — dùng khi email KHÔNG tồn tại, để
# `login()` vẫn tốn đúng 1 lần bcrypt.checkpw() thay vì return sớm. Không làm điều này thì thời
# gian phản hồi (~0ms return sớm vs ~200-370ms bcrypt thật) tự nó là oracle phân biệt email tồn
# tại hay không, độc lập với status code (review `app#17`, Chặn 2, nửa "timing" của oracle).
#
# Giá trị VIẾT TAY sẵn (không gọi `hash_password()` lúc import module) — review đợt 2 của `app#17`
# chỉ ra: gọi `hash_password()` ở top-level module tốn ~200-370ms bcrypt THẬT ngay lúc import (mỗi
# lần process khởi động/mỗi lần pytest collect module này), không cần thiết vì giá trị không đổi
# giữa các lần chạy — chỉ cần MỘT hash bcrypt hợp lệ bất kỳ, không cần khớp lại plaintext nào.
DUMMY_PASSWORD_HASH = "$2b$12$Ga5KFrUJZMsc8/kd1gTe/u9XnMi3uDM5tWUSbZUaM2V5qPr0paoAa"


class InvalidTokenError(Exception):
    """Token thiếu/sai chữ ký/hết hạn/thiếu claim bắt buộc — luôn fail-closed, không có nhánh
    "coi như hợp lệ" nào cho lỗi loại này."""


def issue_token(session: ResolvedContext) -> str:
    """Ký `session` (đã qua `resolve_session()`, đã fail-closed đúng shape) thành 1 JWT, hạn dùng
    `settings.jwt_expire_minutes` phút kể từ lúc ký."""
    settings = get_settings()
    now = datetime.now(UTC)
    claims = {
        "tenant_id": str(session.tenant_id),
        "user": session.user,
        "roles": session.roles,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expire_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=_ALGORITHM)


def verify_token(token: str) -> ResolvedContext:
    """Verify chữ ký + hạn dùng, decode ra `ResolvedContext`. Raise `InvalidTokenError` cho MỌI
    lỗi (chữ ký sai, hết hạn, thiếu claim, `tenant_id` không phải UUID) — không có nhánh "đoán giá
    trị mặc định", đúng nguyên tắc fail-closed đã dùng xuyên suốt (`tenant_wall.resolve_session`).
    """
    settings = get_settings()
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(f"JWT không hợp lệ: {exc}") from exc

    try:
        tenant_id = UUID(str(claims["tenant_id"]))
        user = str(claims["user"])
        roles = [str(r) for r in claims.get("roles", [])]
    except (KeyError, ValueError) as exc:
        raise InvalidTokenError(f"JWT thiếu/sai claim bắt buộc: {exc}") from exc

    return ResolvedContext(tenant_id=tenant_id, user=user, roles=roles)
