"""`grant_scorer_privileges()` — `studio_scorer` chỉ được SELECT, và chỉ trên `obs.trace_events`.

`studio_scorer` (`docker/postgres-init/00-roles.sql`, repo kit) có `BYPASSRLS` vì bộ chấm của evalhub
phải đọc xuyên tenant để `tenant_scope_ok` bắt được run trộn tenant (`evalhub#37`). Hệ quả: với role
này **không policy nào chặn**, nên quyền BẢNG là lưới **duy nhất** còn lại. Một quyền thừa ở đây
không phải là bất tiện — nó là cửa hậu xuyên tenant.

Bài khoá quyền đặt ở ĐÂY chứ không ở repo kit, vì hàm cấp quyền sống ở đây: GRANT phải chạy SAU
`ensure_all_schemas()` (bảng chưa tồn tại lúc initdb). CI của kit reconstruct workspace theo con trỏ
submodule đang ghim, nên một bài ở kit khẳng định về hàm này sẽ đỏ cho tới tận lần bump con trỏ.
"""

from __future__ import annotations

from typing import Any

import pytest
from studio_app.core.schema import grant_scorer_privileges

_QUYEN = (
    "SELECT table_schema || '.' || table_name || ':' || privilege_type "
    "FROM information_schema.table_privileges WHERE grantee = 'studio_scorer' ORDER BY 1"
)


async def _co_role_scorer(pool: Any) -> bool:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'studio_scorer'")
        return await cur.fetchone() is not None


async def test_scorer_chi_co_SELECT_va_chi_tren_obs_trace_events(admin_pool: Any) -> None:
    """KHÓA: sau khi cấp quyền, tập quyền của studio_scorer phải ĐÚNG BẰNG một phần tử.

    Assert bằng `==` chứ không `in`: `in` sẽ xanh kể cả khi ai đó thêm INSERT hoặc thêm bảng thứ
    hai — mà đó chính xác là mutation cần bắt.
    """
    if not await _co_role_scorer(admin_pool):
        pytest.skip(
            "DB test chưa có role studio_scorer — volume có trước evalhub#37 thì initdb KHÔNG chạy "
            "lại. Áp tay: docker exec -i <pg> psql -U postgres -d studio_test "
            "< docker/postgres-init/00-roles.sql"
        )
    await grant_scorer_privileges(admin_pool)
    async with admin_pool.connection() as conn:
        cur = await conn.execute(_QUYEN)
        quyen = {r[0] for r in await cur.fetchall()}
    assert quyen == {"obs.trace_events:SELECT"}, (
        f"studio_scorer phải có ĐÚNG SELECT trên obs.trace_events, đang có: {sorted(quyen)}. "
        "Role này có BYPASSRLS ⇒ mọi quyền thừa là quyền xuyên tenant mà không policy nào chặn."
    )


async def test_scorer_khong_duoc_ALTER_DEFAULT_PRIVILEGES(admin_pool: Any) -> None:
    """KHÓA: KHÔNG có default-privilege nào cho studio_scorer.

    `grant_app_privileges` cố ý dùng `ALTER DEFAULT PRIVILEGES` để bảng tương lai tự có quyền —
    đúng cho studio_app (bị RLS chặn). Sao chép nếp đó sang studio_scorer sẽ lặng lẽ cấp quyền
    xuyên tenant trên MỌI bảng ai đó tạo sau này. Bài này chặn đúng cú copy-paste ấy.
    """
    if not await _co_role_scorer(admin_pool):
        pytest.skip("DB test chưa có role studio_scorer — xem bài trên")
    await grant_scorer_privileges(admin_pool)
    async with admin_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM pg_default_acl d WHERE array_to_string(d.defaclacl, ',') LIKE '%studio_scorer%'"
        )
        row = await cur.fetchone()
    assert row is not None and row[0] == 0, "studio_scorer không được có ALTER DEFAULT PRIVILEGES nào"


async def test_grant_khong_no_khi_thieu_role_hoac_thieu_bang(admin_pool: Any) -> None:
    """KHÓA: hàm phải no-op im lặng, không raise — boot của app không được chết chỉ vì một volume
    cũ chưa có role. evalhub đã fail-closed ở phía nó (`UnscopedReadUnavailable`), nên chỗ này im
    lặng là đúng phân vai, không phải nuốt lỗi."""
    await grant_scorer_privileges(admin_pool)
    await grant_scorer_privileges(admin_pool)  # idempotent
