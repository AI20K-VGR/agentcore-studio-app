"""Test HTTP-level THẬT — qua ASGI (`httpx.AsyncClient` + `ASGITransport(app=create_app())`), gửi
request qua toàn bộ chồng middleware + FastAPI routing thật, KHÔNG gọi thẳng hàm route + set
ContextVar tay như mọi test khác trong repo (review `app#17` đợt 2, mục 5 — xác nhận: "chưa có tiền
lệ TestClient trong repo", `test_middleware.py:43`).

Lỗ cụ thể được nêu, giờ có test bảo vệ: thứ tự `tenant_context_middleware`/`CORSMiddleware` trong
`app.py:60-64` (đã fix 1 lần, review PR#5 DE M2) không có test nào khoá lại — ai đảo ngược thứ tự
đó, response lỗi (401 phát sinh TRONG tenant_context_middleware qua `get_request_session()`) sẽ
không đi qua CORS để được gắn header, và cả bộ test cũ (gọi thẳng hàm route) vẫn xanh vì không bài
nào build request HTTP thật để CORSMiddleware có cơ hội chạy.

`create_app()`'s lifespan (DDL + grant) KHÔNG được trigger ở đây — `admin_pool` fixture (root
`conftest.py`) đã tự `ensure_all_schemas`/`grant_app_privileges` trước khi yield, cùng DSN với
`get_pool()`/`get_admin_pool()` (singleton `studio_app.core._db`) routes dùng — 2 pool khác object,
cùng dữ liệu, đúng pattern `test_routes_auth.py` đã dùng.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from studio_app import jwt_auth, rate_limit
from studio_app.app import create_app
from studio_app.core._db import Pool, close_pools
from studio_app.jwt_auth import hash_password
from studio_app.settings import Settings


@pytest_asyncio.fixture(autouse=True)
async def _close_singleton_pools_after_test() -> AsyncIterator[None]:
    yield
    await close_pools()


@pytest_asyncio.fixture(autouse=True)
async def _reset_rate_limiter() -> AsyncIterator[None]:
    """`rate_limit._buckets` là state module-level, KHÔNG tự reset giữa các bài — mọi request qua
    `ASGITransport` mặc định cùng 1 `client.host` giả (`127.0.0.1`, xem `httpx.ASGITransport`), nên
    không reset sẽ làm bài sau ăn hết token bucket bài trước để lại (review `app#17`, Important #2,
    đợt 8)."""
    rate_limit.reset_all()
    yield


def _settings() -> Settings:
    return Settings(
        database_url="postgresql://unused/unused",
        database_url_admin="postgresql://unused/unused",
        jwt_secret="test-secret-http-asgi-at-least-32-bytes",
    )


@pytest_asyncio.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_unauthenticated_admin_route_returns_401(client: AsyncClient, admin_pool: Pool) -> None:
    """`POST /api/admin/companies` KHÔNG kèm `Authorization` -> 401 thật qua ASGI, không phải suy
    diễn từ đọc `get_request_session()`. `admin_pool` chỉ để đảm bảo schema đã ensure trước khi
    request chạm route (route không cần dữ liệu gì cho ca này)."""
    del admin_pool
    res = await client.post(
        "/api/admin/companies",
        json={"company_name": "unauth-co", "admin_email": "x@x.com", "admin_password": "password123"},
    )
    assert res.status_code == 401


async def test_unauthenticated_users_route_returns_401(client: AsyncClient, admin_pool: Pool) -> None:
    """`POST /api/admin/users` KHÔNG kèm `Authorization` -> 401 thật qua ASGI (review `app#17`,
    "nên sửa" #2: trước bản vá này chỉ `/api/admin/companies` có test HTTP thật — route thứ hai,
    `/api/admin/users`, chưa từng đi qua `ASGITransport`, chỉ được test bằng cách gọi thẳng hàm +
    set ContextVar tay ở `test_admin_routes.py`)."""
    del admin_pool
    res = await client.post(
        "/api/admin/users",
        json={"email": "x@x.com", "password": "password123", "roles": ["public"]},
    )
    assert res.status_code == 401


async def test_cors_header_present_on_error_response(client: AsyncClient, admin_pool: Pool) -> None:
    """Khoá đúng thứ tự middleware (`app.py:60-64`, review PR#5 DE M2). Dùng token HỎNG (không
    phải thiếu hẳn header) mới đúng kịch bản comment mô tả: `tenant_context_middleware` bắt
    `InvalidTokenError` và tự trả `JSONResponse(401)` NGAY, KHÔNG gọi `call_next()` — response đó
    chỉ có cơ hội được gắn header CORS nếu CORS nằm NGOÀI (bọc) `tenant_context_middleware`. Ca
    "thiếu hẳn Authorization" (như `test_unauthenticated_admin_route_returns_401`) KHÔNG phân biệt
    được 2 thứ tự — request đó luôn đi qua `call_next()` bình thường (session=None, không raise),
    401 phát sinh SAU, sâu bên trong FastAPI's exception handling, nên vẫn được gắn CORS dù đảo
    thứ tự — đã tự mutation-verify: đảo thứ tự middleware, phiên bản "thiếu header" KHÔNG bắt được
    gì (dương tính giả về độ phủ), chỉ phiên bản "token hỏng" dưới đây mới đỏ đúng chỗ."""
    del admin_pool
    res = await client.post(
        "/api/admin/companies",
        json={"company_name": "cors-co", "admin_email": "x@x.com", "admin_password": "password123"},
        headers={"Origin": "http://localhost:5173", "Authorization": "Bearer this-is-not-a-valid-jwt"},
    )
    assert res.status_code == 401
    assert res.headers.get("access-control-allow-origin") == "*"


async def test_oversized_password_422_does_not_echo_plaintext(client: AsyncClient, admin_pool: Pool) -> None:
    """Critical #1, review `app#17` đợt 2: Pydantic `ValidationError.errors()` echo NGUYÊN GIÁ TRỊ
    client gửi vào `input` (và `.ctx.error` cho validator tự viết) — FastAPI's mặc định handler
    cho `RequestValidationError` serialize thẳng list đó vào response 422, nghĩa là mật khẩu THẬT
    client vừa gõ lộ vào body response (dễ bị log lại ở reverse-proxy/APM/HAR). Request KHÔNG kèm
    Authorization — validation body chạy TRƯỚC khi FastAPI gọi vào hàm route (nên chưa chạm
    `get_request_session()`/`require_superadmin`), nên không cần token hợp lệ để trigger 422 này."""
    del admin_pool
    oversized_password = "a" * 73
    res = await client.post(
        "/api/admin/companies",
        json={"company_name": "leak-co", "admin_email": "x@x.com", "admin_password": oversized_password},
    )
    assert res.status_code == 422
    body_text = res.text
    assert oversized_password not in body_text
    detail = res.json()["detail"]
    password_errors = [e for e in detail if "password" in str(e.get("loc", ()))]
    assert password_errors, "phải có ít nhất 1 error item cho field password"
    for error in password_errors:
        assert "input" not in error
        assert "ctx" not in error


async def test_login_missing_email_422_does_not_echo_sibling_password(client: AsyncClient, admin_pool: Pool) -> None:
    """Critical, review `app#17` đợt 3: khi 1 field BẮT BUỘC bị thiếu hẳn (ở đây: `email`),
    Pydantic v2's `ValidationError.errors()` gắn NGUYÊN dict input gốc — MỌI field anh em khác đã
    gửi, kể cả `password` hợp lệ — vào `input` của error đó, không phải `input` của riêng field bị
    thiếu. Thực nghiệm xác nhận: `LoginRequest(password="...")` (thiếu email) ra
    `errors()[0] == {"loc": ("email",), "input": {"password": "<mật khẩu thật>"}, ...}` — lỗi có
    `loc=("email",)`, KHÔNG chứa "password"/"admin_password", nên bản `_redact_sensitive_validation_errors`
    cũ (chỉ soi `loc` của chính error) bỏ lọt thẳng: mật khẩu thật lộ nguyên trong response 422 dù
    chính field password không hề sai. Fix: `_error_touches_sensitive_field` (`app.py`) soi thêm
    `input` (khi là dict) cho key nhạy cảm, không chỉ `loc`."""
    del admin_pool
    real_password = "this-is-the-real-password-do-not-leak"
    res = await client.post("/api/auth/login", json={"password": real_password})
    assert res.status_code == 422
    assert real_password not in res.text
    detail = res.json()["detail"]
    email_errors = [e for e in detail if "email" in str(e.get("loc", ()))]
    assert email_errors, "phải có ít nhất 1 error item cho field email"
    for error in email_errors:
        assert "input" not in error
        assert "ctx" not in error


async def test_login_array_body_422_does_not_echo_password(client: AsyncClient, admin_pool: Pool) -> None:
    """Critical, review `app#17` đợt 3 (lượt 2 — bản vá lượt 1 vẫn lọt): gửi THẲNG 1 JSON array
    làm body thay vì object — mật khẩu thật lộ nguyên trong `input` của error.

    Docstring bài này TỪNG ghi sai (đợt 3): gọi thẳng `LoginRequest.model_validate([...])` (NGOÀI
    FastAPI) cho `loc=[]` (rỗng hẳn), nên bản vá lượt 1 dùng tiêu chí `len(loc) == 0`. Nhưng qua
    ĐƯỜNG HTTP THẬT (bài test này, `ASGITransport`, không gọi thẳng Pydantic) thực nghiệm cho kết
    quả KHÁC hẳn — review `app#17` đợt 4 tự verify lại bằng code Python thật: FastAPI tự chèn
    `"body"` làm phần tử ĐẦU của `loc` cho mọi lỗi validate body -> `loc=["body"]` (KHÔNG rỗng).
    Nếu ai đó sửa `_error_touches_sensitive_field` (`app.py`) lại về đúng `len(loc) == 0` theo
    docstring cũ, bài test NÀY vẫn xanh giả (vì assert chỉ check status+password, không tự ghim
    lại hình dạng `loc`) trong khi lỗ mở lại — nên bài dưới đây ghim THÊM đúng hình dạng `loc` thật
    (`["body"]`, 1 phần tử) để khoá lại, không chỉ khoá hành vi cuối (không lộ password)."""
    del admin_pool
    real_password = "array-body-real-password-do-not-leak"
    res = await client.post("/api/auth/login", json=["attacker@example.com", real_password])
    assert res.status_code == 422
    assert real_password not in res.text
    detail = res.json()["detail"]
    assert detail, "phải có ít nhất 1 error item"
    for error in detail:
        assert error["loc"] == ["body"], f"loc thật qua HTTP là ['body'], không phải [] — thấy {error['loc']!r}"
        assert "input" not in error
        assert "ctx" not in error


async def test_create_company_array_body_422_does_not_echo_admin_password(
    client: AsyncClient, admin_pool: Pool
) -> None:
    """Important, review `app#17` đợt 4: biến thể body-dạng-array của Critical trên, nhưng cho
    `/api/admin/companies` (chính route có `admin_password`, ví dụ động lực GỐC của cả lỗ này) —
    trước bài này, chỉ `/api/auth/login` có test cho biến thể array, route kia chưa từng được thử."""
    del admin_pool
    real_password = "create-company-array-body-real-password-do-not-leak"
    res = await client.post("/api/admin/companies", json=["acme-co", "x@x.com", real_password])
    assert res.status_code == 422
    assert real_password not in res.text
    for error in res.json()["detail"]:
        assert "input" not in error
        assert "ctx" not in error


async def test_login_deeply_nested_junk_field_422_not_500_does_not_leak_password_to_traceback(
    client: AsyncClient, admin_pool: Pool
) -> None:
    """Critical, review `app#17` đợt 4 — do CHÍNH bản vá đợt 3 tạo ra: `_value_contains_sensitive_key`
    (`app.py`) bản đệ quy bị `RecursionError` khi body chứa 1 field lồng đủ sâu (reviewer thực
    nghiệm: ~600 tầng đã vỡ). `RecursionError` không bắt được ở đâu -> FastAPI trả 500 -> traceback
    (chứa NGUYÊN payload lỗi, gồm `password` thật) đi thẳng vào log server dưới uvicorn thật — lộ
    mật khẩu vào LOG, không cần đăng nhập/request hợp lệ, TỆ HƠN lỗ gốc PR này đóng (lỗ gốc lộ qua
    RESPONSE, cái này lộ vào log, và attacker không cần biết gì về email/mật khẩu ai cả). Fix: đổi
    đệ quy thành vòng lặp (stack tường minh, không tốn call-frame theo độ sâu) + trần
    `_MAX_SCAN_NODES`.

    Nitpick round 5 (đã sửa): bản đầu của bài này để `password` làm SIBLING-KEY cùng cấp với field
    rác lồng sâu (`{"aaa_junk": <lồng 2000 tầng>, "password": "..."}`) — vòng `for key, v in
    current.items()` ở tầng dict NGOÀI CÙNG gặp `"password"` (return `True` NGAY) trước khi vòng
    `while` chính kịp pop phần tử `aaa_junk` đã đẩy vào stack ra để duyệt xuống — bài test XANH
    ĐÚNG kết luận (không 500, không lộ) nhưng KHÔNG thực sự đi qua nhánh duyệt list lồng sâu, chỉ
    test lại đúng case sibling-key đã có bài riêng (`test_login_missing_email_422...`). Sửa: chôn
    `password` THẲNG Ở ĐÁY cấu trúc lồng 2000 tầng (`{"payload": [[[...{"password": "..."}...]]]}`)
    — không còn sibling-key nào ở tầng ngoài để thoát sớm, buộc vòng `while` phải pop hết 1500 tầng
    `list` trong stack rồi mới chạm `dict` chứa `password` ở đáy và trả `True` (1500 CỐ Ý dưới
    `_MAX_SCAN_NODES=2000` — để lần trả `True` này chắc chắn là do TÌM THẤY thật, không lẫn với
    nhánh chạm trần fail-closed; ca chạm trần đã có unit test riêng,
    `test_value_contains_sensitive_key_handles_extreme_depth_without_recursion_error`) — verify:
    không 500, không lộ mật khẩu qua response."""
    del admin_pool
    real_password = "deeply-nested-junk-real-password-do-not-leak"
    nested: object = {"password": real_password}
    for _ in range(1500):
        nested = [nested]
    res = await client.post("/api/auth/login", json={"payload": nested})
    assert res.status_code == 422, f"phải là 422 (redact), không phải 500 (RecursionError) — thấy {res.status_code}"
    assert real_password not in res.text
    for error in res.json()["detail"]:
        assert "input" not in error
        assert "ctx" not in error


def test_value_contains_sensitive_key_handles_extreme_depth_without_recursion_error() -> None:
    """Unit test trực tiếp, không qua HTTP — tái tạo ĐÚNG cách reviewer round 5 tự verify thủ công
    (`app#17` đợt 4→5): stress `_value_contains_sensitive_key` (`app.py`) ở độ sâu vượt XA giới hạn
    đệ quy mặc định của Python (~1000), để chứng minh thuật toán THỰC SỰ không tốn call-frame theo
    độ sâu dữ liệu (vòng lặp, không đệ quy) — ghim lại bằng test tự động thay vì chỉ dựa vào script
    Python reviewer chạy tay 1 lần rồi bỏ.

    3 ca, cố ý tách riêng để không lẫn 2 lý do khác nhau ra cùng 1 kết quả `True`:
    - `depth=50_000` (vượt hẳn `_MAX_SCAN_NODES=2000`), KHÔNG có key nhạy cảm nào -> `True` — nhưng
      lý do là chạm TRẦN số node (fail-closed), KHÔNG PHẢI vì tìm thấy key nào — trần chặn trước
      khi thuật toán đi hết được xuống đáy.
    - `depth=1500` (DƯỚI trần 2000, nhưng vẫn sâu hơn giới hạn đệ quy Python) với `password` chôn
      Ở ĐÁY -> `True` — lần này chắc chắn là do THẬT SỰ đi hết 1500 tầng rồi TÌM THẤY key, không
      phải chạm trần (1500 < 2000) — ca này mới thật sự chứng minh vòng lặp duyệt đúng, không chỉ
      "không crash".
    - `depth=1500`, KHÔNG có key nhạy cảm -> `False` — chứng minh không over-redact tuỳ tiện: đi
      hết 1500 tầng (dưới trần, không bị fail-closed chặn giữa chừng), không thấy gì thì phải kết
      luận đúng là không có, không phải cứ sâu là redact."""
    from studio_app.app import _value_contains_sensitive_key

    beyond_cap_no_sensitive_key: object = "bottom"
    for _ in range(50_000):
        beyond_cap_no_sensitive_key = [beyond_cap_no_sensitive_key]
    assert _value_contains_sensitive_key(beyond_cap_no_sensitive_key) is True  # chạm trần, fail-closed

    under_cap_sensitive_key_at_bottom: object = {"password": "x"}
    for _ in range(1500):
        under_cap_sensitive_key_at_bottom = [under_cap_sensitive_key_at_bottom]
    assert _value_contains_sensitive_key(under_cap_sensitive_key_at_bottom) is True  # tìm thấy thật, không phải trần

    under_cap_no_sensitive_key: object = "bottom"
    for _ in range(1500):
        under_cap_no_sensitive_key = [under_cap_no_sensitive_key]
    assert _value_contains_sensitive_key(under_cap_no_sensitive_key) is False  # đi hết, không thấy -> không redact


async def test_login_end_to_end_through_real_http(client: AsyncClient, admin_pool: Pool) -> None:
    """Đường thật, đầu-cuối qua HTTP: seed 1 dòng `core.users`, `POST /api/auth/login` bằng
    `httpx`, verify token trả về xài được để gọi tiếp 1 route cần auth (`/api/admin/companies`,
    kỳ vọng 403 vì role không phải superadmin — chứng minh token THẬT được middleware chấp nhận
    và giải mã đúng roles, không phải 401 vì thiếu/sai token)."""
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", ("http-e2e-co",))
        row = await cur.fetchone()
        assert row is not None
        tenant_id = row[0]
        await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, roles) VALUES (%s, %s, %s, %s)",
            (tenant_id, "http-e2e@acme.com", hash_password("http-e2e-password-123"), ["admin", "public"]),
        )

    login_res = await client.post(
        "/api/auth/login", json={"email": "http-e2e@acme.com", "password": "http-e2e-password-123"}
    )
    assert login_res.status_code == 200
    token = login_res.json()["access_token"]

    admin_res = await client.post(
        "/api/admin/companies",
        json={"company_name": "should-fail-co", "admin_email": "x@x.com", "admin_password": "password123"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert admin_res.status_code == 403  # role "admin", không phải "superadmin" — đúng, không phải 401


async def test_create_company_partial_failure_does_not_orphan_tenant_through_real_http(
    client: AsyncClient, admin_pool: Pool
) -> None:
    """Critical, phát hiện độc lập bởi 4 review khác nhau SAU đợt 6 (bug thật, không phải nitpick):
    `create_company` (`routes/admin.py`) trước bản vá này bọc 2 INSERT (tenant, rồi admin user)
    trong 2 `conn.transaction()` (SAVEPOINT) RIÊNG. Nếu insert tenant thành công (savepoint 1 đã
    RELEASE — merge vào transaction ngoài của middleware) rồi insert admin user thất bại
    (`UniqueViolation`, savepoint 2 rollback, route raise `HTTPException(409)`): `HTTPException` đó
    bị FastAPI's `ExceptionMiddleware` bắt và CHUYỂN THÀNH RESPONSE ngay TRONG lời gọi `call_next()`
    (`middleware.py::tenant_context_middleware`) — không BAO GIỜ propagate lên tới `try/finally`
    bọc `async with pool.connection()` của middleware. Middleware thoát khối `async with` KHÔNG có
    exception nào cả -> COMMIT nguyên transaction ngoài -> tenant vừa insert (savepoint đã release)
    được commit thật dù client nhận 409. `core.tenants.name` UNIQUE nên tên công ty đó kẹt VĨNH VIỄN
    — không endpoint nào tạo lại được qua API.

    Bài test gọi thẳng hàm (`test_admin_routes.py`, không qua ASGI) KHÔNG bắt được lỗ này: đặt
    `pytest.raises(HTTPException)` bọc NGOÀI khối tự dựng connection khiến exception propagate qua
    `pool.connection()` context manager thật của psycopg -> psycopg TỰ rollback TOÀN BỘ connection
    (kể cả savepoint đã release) -> test đó thấy "sạch", nhưng đường rollback đó KHÔNG xảy ra được
    qua HTTP thật (FastAPI đã nuốt exception trước khi nó tới được `pool.connection()`). Bài này đi
    qua `ASGITransport` thật — đúng đường thật, không mô phỏng sai.

    Sửa: gộp 2 SAVEPOINT thành 1 (bọc CẢ HAI insert), disambiguate qua `exc.diag.constraint_name`.
    Verify ở đây theo 2 lớp: (1) `core.tenants` KHÔNG có dòng mồ côi tên `partial-fail-co` sau 409;
    (2) tên công ty đó có thể tạo LẠI thành công ngay sau — chứng minh không kẹt vĩnh viễn."""
    async with admin_pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", ("partial-fail-superadmin-tenant",)
        )
        row = await cur.fetchone()
        assert row is not None
        su_tenant_id = row[0]
        await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, roles) VALUES (%s, %s, %s, %s)",
            (su_tenant_id, "su-partial-fail@sys", hash_password("superadmin-password-123"), ["superadmin"]),
        )
        # Email đã tồn tại SẴN — dùng làm admin_email cho lần gọi bên dưới để buộc INSERT thứ 2 vỡ.
        await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, roles) VALUES (%s, %s, %s, %s)",
            (su_tenant_id, "already-taken@acme.com", hash_password("whatever-password"), ["admin"]),
        )

    login_res = await client.post(
        "/api/auth/login", json={"email": "su-partial-fail@sys", "password": "superadmin-password-123"}
    )
    assert login_res.status_code == 200
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    company_name = "partial-fail-co"
    res = await client.post(
        "/api/admin/companies",
        json={"company_name": company_name, "admin_email": "already-taken@acme.com", "admin_password": "password123"},
        headers=headers,
    )
    assert res.status_code == 409, f"admin_email trùng phải 409 — thấy {res.status_code}: {res.text}"

    async with admin_pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM core.tenants WHERE name = %s", (company_name,))
        count_row = await cur.fetchone()
    assert count_row is not None
    assert count_row[0] == 0, (
        "tenant mồ côi: insert tenant đã commit dù insert admin user thất bại — "
        f"{company_name!r} kẹt vĩnh viễn vì core.tenants.name UNIQUE"
    )

    retry_res = await client.post(
        "/api/admin/companies",
        json={"company_name": company_name, "admin_email": "retry-admin@acme.com", "admin_password": "password123"},
        headers=headers,
    )
    assert retry_res.status_code == 200, (
        f"tên công ty {company_name!r} phải tạo lại được sau lần lỗi trước — thấy {retry_res.status_code}: "
        f"{retry_res.text}"
    )


async def test_demoted_admin_stale_jwt_cannot_create_user_through_real_http(
    client: AsyncClient, admin_pool: Pool
) -> None:
    """Important #1, review `app#17` đợt 8: trước bản vá, `create_user`/`create_company` phân
    quyền bằng `session.roles` — claim JWT, ảnh chụp lúc đăng nhập, sống tới `jwt_expire_minutes`
    (mặc định 480 phút = 8 tiếng). 1 admin bị thu hồi quyền giữa chừng (`core.users.roles` đổi
    trong DB, vd HR khoá tài khoản sau khi nhân viên nghỉ/vi phạm) vẫn còn "admin" trong JWT CŨ tới
    lúc hết hạn tự nhiên nếu route tin thẳng claim đó — route ĐÃ SẴN 1 lượt round-trip DB (tra
    `created_by`) nên sửa rẻ: tra thêm cột `roles` ở CÙNG query, dùng giá trị TƯƠI đó để phân quyền
    thay vì JWT.

    Kịch bản thật qua HTTP: đăng nhập lấy JWT với "admin" -> UPDATE thẳng `core.users.roles` xoá
    "admin" (mô phỏng demote, KHÔNG qua API — hệ thống hiện chưa có route demote, đây là hành động
    admin DB trực tiếp) -> DÙNG LẠI đúng JWT cũ (chưa hết hạn) gọi `/api/admin/users` -> PHẢI 403,
    không phải 200."""
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", ("stale-jwt-tenant",))
        row = await cur.fetchone()
        assert row is not None
        tenant_id = row[0]
        await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, roles) VALUES (%s, %s, %s, %s)",
            (tenant_id, "soon-demoted@acme.com", hash_password("demoted-admin-password"), ["admin", "public"]),
        )

    login_res = await client.post(
        "/api/auth/login", json={"email": "soon-demoted@acme.com", "password": "demoted-admin-password"}
    )
    assert login_res.status_code == 200
    stale_admin_token = login_res.json()["access_token"]

    # Demote NGOÀI luồng API — JWT vừa phát ở trên KHÔNG hề biết chuyện này xảy ra, vẫn mang
    # "admin" trong chữ ký của nó tới lúc hết hạn tự nhiên.
    async with admin_pool.connection() as conn:
        await conn.execute("UPDATE core.users SET roles = %s WHERE email = %s", (["public"], "soon-demoted@acme.com"))

    res = await client.post(
        "/api/admin/users",
        json={"email": "should-not-exist@acme.com", "password": "password123", "roles": ["public"]},
        headers={"Authorization": f"Bearer {stale_admin_token}"},
    )
    assert res.status_code == 403, (
        f"JWT cũ (còn 'admin') phải bị chặn sau khi DB đã demote — thấy {res.status_code}: {res.text}"
    )

    async with admin_pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM core.users WHERE email = %s", ("should-not-exist@acme.com",))
        count_row = await cur.fetchone()
    assert count_row is not None
    assert count_row[0] == 0, "user không được tạo dù JWT cũ còn 'admin' — phải tra roles tươi từ DB"


async def test_rehomed_admin_stale_jwt_creates_user_in_fresh_tenant_not_stale_one(
    client: AsyncClient, admin_pool: Pool
) -> None:
    """Nửa THỨ HAI của Important #1 (đợt 8), review đợt 9 chỉ ra chưa có bài nào ghim riêng: mọi
    bài test trong suite (kể cả bài demote roles ở trên) đều seed session/DB CÙNG 1 tenant, nên nếu
    revert nguồn `tenant_id` của `create_user` về lại `str(session.tenant_id)` (JWT) thay vì
    `str(creator_tenant_id)` (tra tươi), TOÀN BỘ suite vẫn xanh — không bài nào phân biệt được 2
    nguồn đó vì chúng luôn TRÙNG giá trị trong mọi kịch bản khác. Bài này CỐ Ý làm chúng LỆCH nhau:
    admin đăng nhập ở tenant A (JWT mang tenant A) -> "re-home" ngoài luồng API (UPDATE thẳng
    `core.users.tenant_id` sang tenant B, mô phỏng tách/sáp nhập công ty) -> DÙNG LẠI JWT cũ (còn
    mang tenant A) gọi `/api/admin/users` -> user mới PHẢI rơi vào tenant B (tươi), KHÔNG PHẢI
    tenant A (JWT cũ)."""
    async with admin_pool.connection() as conn:
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", ("rehome-tenant-a",))
        row = await cur.fetchone()
        assert row is not None
        tenant_a = row[0]
        cur = await conn.execute("INSERT INTO core.tenants (name) VALUES (%s) RETURNING id", ("rehome-tenant-b",))
        row = await cur.fetchone()
        assert row is not None
        tenant_b = row[0]
        cur = await conn.execute(
            "INSERT INTO core.users (tenant_id, email, password_hash, roles) VALUES (%s, %s, %s, %s) RETURNING id",
            (tenant_a, "soon-rehomed@acme.com", hash_password("rehomed-admin-password"), ["admin", "public"]),
        )
        row = await cur.fetchone()
        assert row is not None
        rehomed_admin_id = row[0]
        # Vocab roles hợp lệ giờ tra động theo `core.sections` CỦA TENANT ĐÍCH (tenant_b, tenant
        # TƯƠI sau re-home) — seed sẵn "public" cho tenant_b, không phải tenant_a, để request cuối
        # bài (tạo user role=["public"] SAU khi re-home) qua được vocab check.
        await conn.execute(
            "INSERT INTO core.sections (tenant_id, name, created_by) VALUES (%s, %s, %s)",
            (tenant_b, "public", rehomed_admin_id),
        )

    login_res = await client.post(
        "/api/auth/login", json={"email": "soon-rehomed@acme.com", "password": "rehomed-admin-password"}
    )
    assert login_res.status_code == 200
    stale_tenant_a_token = login_res.json()["access_token"]
    assert login_res.json()["tenant_id"] == str(tenant_a)

    # Re-home NGOÀI luồng API — JWT vừa phát vẫn mang tenant A, không hề biết chuyện này xảy ra.
    async with admin_pool.connection() as conn:
        await conn.execute("UPDATE core.users SET tenant_id = %s WHERE email = %s", (tenant_b, "soon-rehomed@acme.com"))

    res = await client.post(
        "/api/admin/users",
        json={"email": "landed-somewhere@acme.com", "password": "password123", "roles": ["public"]},
        headers={"Authorization": f"Bearer {stale_tenant_a_token}"},
    )
    assert res.status_code == 200, f"admin re-home rồi vẫn phải tạo được user (đúng tenant MỚI) — thấy {res.text}"
    assert res.json()["tenant_id"] == str(tenant_b), (
        f"user mới phải rơi vào tenant TƯƠI ({tenant_b}), không phải tenant JWT cũ ({tenant_a}) — "
        f"thấy {res.json()['tenant_id']}"
    )

    async with admin_pool.connection() as conn:
        cur = await conn.execute("SELECT tenant_id FROM core.users WHERE email = %s", ("landed-somewhere@acme.com",))
        db_row = await cur.fetchone()
    assert db_row is not None
    assert str(db_row[0]) == str(tenant_b), "dòng thật trong DB cũng phải thuộc tenant B, không phải A"


async def test_login_rate_limited_per_ip_through_real_http(client: AsyncClient) -> None:
    """Important #2, review `app#17` đợt 8: `bcrypt` cost-12 chạy KHÔNG ĐIỀU KIỆN ở `login()`, kể
    cả nhánh `DUMMY_PASSWORD_HASH` (email không tồn tại) — route KHÔNG cần đăng nhập nên không có
    gì chặn TRƯỚC nó, ~110 req/s từ 1 client ẩn danh đủ giữ bận toàn bộ AnyIO threadpool (mặc định
    40 thread, dùng chung MỌI request của process). Bài này đi qua `ASGITransport` thật (mặc định
    `client=("127.0.0.1", 123)`, cố định cho MỌI request qua transport này — xem
    `httpx.ASGITransport.__init__`), gửi hơn `_CAPACITY` request liên tiếp cùng 1 "IP" và kỳ vọng
    request thừa bị 429, không phải 401/500 (không cần email/mật khẩu đúng — chặn ở TẦNG rate-limit,
    trước khi chạm DB/bcrypt)."""
    body = {"email": "rate-limit-probe@acme.com", "password": "whatever-password"}

    for _ in range(int(rate_limit._CAPACITY)):
        res = await client.post("/api/auth/login", json=body)
        assert res.status_code == 401, f"trong hạn mức phải 401 (email không tồn tại) — thấy {res.status_code}"

    limited_res = await client.post("/api/auth/login", json=body)
    assert limited_res.status_code == 429, f"vượt hạn mức phải 429 — thấy {limited_res.status_code}"


async def test_login_rate_limit_is_per_ip_not_global(monkeypatch: pytest.MonkeyPatch, admin_pool: Pool) -> None:
    """Đối trọng bài trên: 1 IP bị 429 KHÔNG được làm IP khác bị vạ lây — rate-limit đúng nghĩa
    "theo IP", không phải 1 bộ đếm toàn cục chặn nhầm người dùng thật khác đang đăng nhập bình
    thường cùng lúc kẻ tấn công dồn dập từ IP khác.

    Cần `admin_pool` dù bài này không seed dữ liệu gì — review `app#17` đợt 9: thiếu fixture này,
    bài chỉ xanh NHỜ 1 bài KHÁC chạy trước đó đã tự `ensure_all_schemas` (qua `admin_pool` của
    NÓ) trong CÙNG lần chạy suite; đứng riêng/chạy đầu tiên, `SELECT ... FROM core.users` trong
    `login()` sẽ vỡ vì bảng chưa tồn tại (relation không tồn tại) -> 500, không phải 401 kỳ vọng."""
    del admin_pool
    monkeypatch.setattr(jwt_auth, "get_settings", _settings)
    app = create_app()
    body = {"email": "rate-limit-probe-2@acme.com", "password": "whatever-password"}

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("10.0.0.1", 1)), base_url="http://test"
    ) as attacker:
        for _ in range(int(rate_limit._CAPACITY)):
            await attacker.post("/api/auth/login", json=body)
        blocked_res = await attacker.post("/api/auth/login", json=body)
        assert blocked_res.status_code == 429

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("10.0.0.2", 1)), base_url="http://test"
    ) as bystander:
        ok_res = await bystander.post("/api/auth/login", json=body)
        assert ok_res.status_code == 401, "IP khác không liên quan không được ăn 429 lây"
