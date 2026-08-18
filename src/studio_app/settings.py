"""Pydantic-settings, env prefix `STUDIO_` (Decision #3 2-DSN split + Decision #9 provider
flags). `.env.example` at the repo root documents every field this class reads."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LlmProvider(StrEnum):
    """Closed vocabulary cho provider LLM thật app#19 sẽ chọn — cùng cap cứng của `NodeType`
    (`packages/contracts/src/studio_contracts/nodes.py`): thêm giá trị thứ 3 là quyết định có
    chủ đích, không phải string tự do."""

    OPENAI = "openai"
    GEMINI = "gemini"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STUDIO_", env_file=".env", extra="ignore")

    # 2-DSN split (F1/F2) — never point database_url at the studio_owner role, that silently
    # disables the RLS fence (core/_db.py docstring explains why).
    database_url: str
    database_url_admin: str

    enable_tracing: bool = False
    use_fake_providers: bool = True

    # JWT ký/verify identity (Kế hoạch 3) — `issue_token()` giờ CHỈ được gọi từ `routes/auth.py::login`
    # SAU KHI verify mật khẩu thật khớp `core.users.password_hash` (bcrypt) — không còn giai đoạn
    # `demo-login` ký bất kỳ tenant/roles nào không qua kiểm mật khẩu (xem `jwt_auth.py` docstring).
    # Việc ký/verify chữ ký là THẬT (không ai giả mạo được token nếu không có `jwt_secret`), VÀ việc
    # "ai được phép xin token cho tenant/roles nào" giờ ĐÃ được xác thực bằng mật khẩu thật — không
    # còn là câu hỏi bỏ ngỏ (review `app#17`, "nên sửa" #2: comment cũ nói ngược, viết lúc còn
    # demo-login, chưa cập nhật theo commit xoá route đó). `jwt_secret` không có default: thiếu biến
    # môi trường phải raise rõ ràng lúc khởi động (pydantic ValidationError), không được chạy với
    # khoá rỗng/đoán được.
    #
    # `min_length=32` — kit#129 §3.3, mục #3 (VinSOC pentest). LÚC PHÁT HIỆN (trước bản vá này),
    # `.env.example` để default `STUDIO_JWT_SECRET=changeme` (7 ký tự) — app khởi động bình thường,
    # chỉ cảnh báo "HMAC key 8 bytes" từ thư viện chứ không chặn — ai deploy thật quên đổi secret
    # thì kẻ tấn công tự ký được JWT giả (biết trước default công khai trong repo). Verify JWT đã
    # đúng chuẩn (alg=none/sai-secret/hết-hạn đều chặn — mentor xác nhận) — vấn đề CHỈ ở chỗ secret
    # yếu không bị enforce. `min_length=32` chặn cả "changeme" lẫn mọi secret ngắn khác, không tách
    # dev/prod. `.env.example` đã đổi placeholder dài hơn 32 ký tự trong cùng đợt vá (kit#164) —
    # comment này giữ nguyên bối cảnh LÚC PHÁT HIỆN, không phải hiện trạng `.env.example` bây giờ
    # (review `app#17`, Chặn 3: comment cũ khẳng định nhầm "đã xác nhận sẵn" trong khi lúc viết PR
    # đó `.env.example` vẫn còn `changeme` — sửa cách diễn đạt cho khỏi tự mâu thuẫn).
    jwt_secret: str = Field(min_length=32)
    jwt_expire_minutes: int = 480

    # `rate_limit.py` keys theo IP cho `POST /api/auth/login` (review `app#17`, Important #2, đợt
    # 8→9) — mặc định đọc `request.client.host` (IP TCP thật của kết nối tới process này). Sau
    # reverse proxy/load balancer, MỌI request đều mang cùng 1 IP đó (IP của proxy), gộp MỌI người
    # dùng thật vào chung 1 bucket 10 req/phút — tự-DoS người dùng hợp lệ, không cần kẻ tấn công
    # làm gì. Mặc định `False` (an toàn cho triển khai KHÔNG có proxy đáng tin, kể cả `docker-compose
    # --profile app`) — bật `True` CHỈ khi có reverse proxy đáng tin cấu hình ghi đè/set cứng
    # `X-Forwarded-For` (không cho client tự khai giá trị đó lọt qua), nếu không bật nhầm sẽ MỞ LẠI
    # đường né rate-limit bằng cách client tự set header giả trực tiếp (KHÔNG qua proxy nào).
    trust_x_forwarded_for: bool = False

    # Discriminator provider thật (app#19) — required, KHÔNG `Field(default=...)`: cùng lý lẽ
    # `jwt_secret` ở trên (dòng 29-31), thiếu biến môi trường phải raise rõ ràng lúc khởi động,
    # không được đoán/rơi về nhánh mặc định nào. Một default (vd luôn rơi về Gemini khi thiếu
    # biến) sẽ ẩn lỗi cấu hình triển khai thật đến tận lúc gọi LLM mới lộ ra — trái với "giá trị
    # lạ phải ồn" (cùng lý lẽ `_VOCABULARY` của `agreement.py:64`, `DEC-D18-01`).
    llm_provider: LlmProvider
    openai_api_key: str | None = None
    gemini_api_key: str | None = None

    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Process-wide cached Settings — reads env once, not once per call site."""
    # pydantic-settings resolves `database_url`/`database_url_admin` (no Python default) from
    # STUDIO_* env vars / .env at call time — the pydantic.mypy plugin models BaseSettings so no
    # call-arg ignore is needed.
    return Settings()
