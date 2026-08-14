"""Ký/verify JWT cho identity `{tenant_id, user, roles}` (Kế hoạch 2, A1/A2).

Đây là phần "JWT THẬT" theo đúng nghĩa mật mã học — chữ ký được verify bằng `settings.jwt_secret`
(HS256), một request không có khoá bí mật đó KHÔNG THỂ tự chế ra 1 token hợp lệ, khác hẳn bản demo
cũ (tin thẳng JSON client gửi, không ký gì cả).

**Ranh giới cần nói rõ, không nhận vơ**: việc này KHÔNG phải "authentication thật" theo nghĩa đầy
đủ — `issue_token()` (gọi từ `routes/auth.py::demo_login`) vẫn KÝ bất kỳ `tenant`/`user`/`roles`
nào caller đưa vào, không có bước kiểm mật khẩu/OAuth/identity-provider nào đứng trước. Nói cách
khác: đã đóng được câu hỏi "ai đó có thể GIẢ MẠO token của người khác không?" (KHÔNG, cần
`jwt_secret`) nhưng CHƯA đóng câu hỏi "ai được phép TỰ XIN token cho tenant/user nào?" (câu đó cần
1 identity provider thật — mật khẩu, SSO, ... — không có trong phạm vi hiện tại).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import bcrypt
import jwt
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
    fail-closed giống nhau, không phải nhớ tự chặn ở từng nơi gọi."""
    if len(plain.encode("utf-8")) > 72:
        return False
    return bcrypt.checkpw(plain.encode("utf-8"), password_hash.encode("utf-8"))


# Hash cố định của 1 chuỗi không phải mật khẩu thật của ai — dùng khi email KHÔNG tồn tại, để
# `login()` vẫn tốn đúng 1 lần bcrypt.checkpw() thay vì return sớm. Không làm điều này thì thời
# gian phản hồi (~0ms return sớm vs ~200ms bcrypt thật) tự nó là oracle phân biệt email tồn tại
# hay không, độc lập với status code (review `app#17`, Chặn 2, nửa "timing" của oracle).
DUMMY_PASSWORD_HASH = hash_password("dummy-password-khong-phai-cua-ai-chi-de-can-bang-thoi-gian-bcrypt")


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
