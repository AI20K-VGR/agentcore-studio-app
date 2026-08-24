"""`core/golden_seed.py` — đường nạp file → `eval.golden_sets`.

Đường nạp là nửa còn lại của cutover: đổi đường ĐỌC sang DB mà không có đường nạp thì mọi tenant
nhận 400 ngay request đầu. Hai nửa hỏng theo hai kiểu khác nhau nên có hai file test riêng —
`test_publish_reads_golden_from_db.py` giữ nửa đọc.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from psycopg import sql
from studio_app.core.golden_seed import GOLDEN_SET_DIR, iter_bundled_golden_sets, seed_golden_sets
from studio_evalhub.golden_store import GoldenSetNotFound, read_golden_set
from studio_kb.doc_factory import TENANT_IDS

ANKOR_ID = TENANT_IDS["ankor"]
BOREA_ID = TENANT_IDS["borea"]


async def _bind(conn: Any, tenant_id: UUID) -> None:
    await conn.execute(sql.SQL("SET LOCAL app.tenant_id = {}").format(sql.Literal(str(tenant_id))))


def test_duyet_dung_moi_file_yaml_co_ref(tmp_path: Path) -> None:
    """3 file: một bộ hợp lệ, một YAML hỏng, một mapping không khai `golden_set_ref`.

    Fixture bất đối xứng có chủ đích — nếu cả 3 file đều hợp lệ, bài sẽ xanh kể cả khi hàm bỏ hết
    guard và cứ thế `load_golden_set` mọi file (rồi vỡ ở file đầu tiên không phải bộ case)."""
    hop_le = {
        "golden_set_ref": "bo-hop-le-v1",
        "cases": [
            {
                "case_id": "c1",
                "query": "q?",
                "tenant": "ankor",
                "section_roles": ["public"],
                "expected_tenant": "ankor",
                "expected_section_role": "public",
                "expected": "a",
            }
        ],
    }
    (tmp_path / "a-hop-le.yaml").write_text(yaml.safe_dump(hop_le), encoding="utf-8")
    (tmp_path / "b-yaml-hong.yaml").write_text("{[không phải yaml hợp lệ", encoding="utf-8")
    (tmp_path / "c-khong-co-ref.yaml").write_text(yaml.safe_dump({"cases": []}), encoding="utf-8")

    ket_qua = list(iter_bundled_golden_sets(tmp_path))

    assert [g.golden_set_ref for g in ket_qua] == ["bo-hop-le-v1"], (
        "chỉ file khai `golden_set_ref` dạng chuỗi mới được yield; 2 file kia phải bị bỏ qua "
        "IM LẶNG chứ không làm cả lượt nạp đổ"
    )


def test_thu_muc_dong_goi_san_duyet_ra_bo_that() -> None:
    """Chống rỗng-nghĩa: thư mục thật phải yield ra bộ, không phải danh sách rỗng.

    Không có bài này, `iter_bundled_golden_sets` trả rỗng vì bất kỳ lý do gì (glob sai, thư mục sai)
    vẫn làm `seed_golden_sets` "chạy xong" và in ra 0 bộ — script báo thành công, DB vẫn trống, và
    lỗi chỉ lộ ra ở request `/publish` đầu tiên."""
    refs = [g.golden_set_ref for g in iter_bundled_golden_sets()]

    assert "callisto-2.0-golden-30-v1" in refs, (
        f"bộ 2.0 (mặc định của `PublishRequest.golden_set_ref`) phải nạp được từ {GOLDEN_SET_DIR}; thấy {refs}"
    )
    assert len(refs) == len(set(refs)), f"hai file khai trùng `golden_set_ref` — bộ sau sẽ đè bộ trước: {refs}"


@pytest.mark.asyncio
async def test_nap_xong_doc_lai_duoc_dung_bo(admin_pool: Any) -> None:
    """Nạp cho một tenant rồi đọc lại qua chính `read_golden_set` mà route dùng."""
    async with admin_pool.connection() as conn, conn.transaction():
        await _bind(conn, ANKOR_ID)
        refs = await seed_golden_sets(conn, ANKOR_ID)
        doc_lai = await read_golden_set(conn, "callisto-2.0-golden-30-v1", ANKOR_ID)

    assert "callisto-2.0-golden-30-v1" in refs
    assert len(doc_lai.cases) == 30, "bộ đọc lại từ DB phải đủ 30 case như file nguồn"


@pytest.mark.asyncio
async def test_nap_hai_lan_khong_loi_va_khong_nhan_doi(admin_pool: Any) -> None:
    """`write_golden_set` upsert — chạy lại script phải là no-op về nội dung.

    Script này được mô tả là "chạy lại được nhiều lần"; nếu lần hai vỡ (`UNIQUE` violation) thì mô
    tả đó sai và người vận hành sẽ phát hiện lúc production."""
    async with admin_pool.connection() as conn, conn.transaction():
        await _bind(conn, ANKOR_ID)
        lan_1 = await seed_golden_sets(conn, ANKOR_ID)
        lan_2 = await seed_golden_sets(conn, ANKOR_ID)
        sau_hai_lan = await read_golden_set(conn, "callisto-2.0-golden-30-v1", ANKOR_ID)

    assert lan_1 == lan_2
    assert len(sau_hai_lan.cases) == 30, "upsert phải THAY nội dung, không nối thêm"


@pytest.mark.asyncio
async def test_nap_cho_tenant_nay_khong_lo_sang_tenant_kia(admin_pool: Any) -> None:
    """Nạp cho **borea** xong, ankor vẫn phải `GoldenSetNotFound` trên cùng ref.

    Ref ngẫu nhiên + thư mục tạm: bộ đóng gói sẵn có thể đã được bài khác nạp cho tenant kia trong
    cùng DB test, và bài sẽ xanh/đỏ theo thứ tự chạy chứ không theo hành vi.

    **Vì sao borea chứ không phải ankor.** Bản đầu của bài này nạp cho ankor rồi kiểm borea không
    thấy — và một mutant thay `write_golden_set(conn, golden, tenant_id)` bằng `…, TENANT_IDS
    ["ankor"]` (bỏ qua tham số, ghi cứng một tenant) **sống sót**: cả 12 bài vẫn xanh, vì tenant
    ghi cứng trùng đúng tenant bài đang thử. Ankor là mặc định của mọi fixture quanh đây, nên nó là
    giá trị mà một lỗi ghi-cứng có nhiều khả năng trùng nhất — tức là giá trị tệ nhất để kiểm bằng.
    Nạp cho borea làm thế lệch trở lại thật: mutant sẽ ghi sang ankor trong khi connection bind
    borea, và `_assert_scope` bắt ngay."""
    ref = f"chi-cua-mot-tenant-{uuid4()}"
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        thu_muc = Path(td)
        (thu_muc / "bo.yaml").write_text(
            yaml.safe_dump(
                {
                    "golden_set_ref": ref,
                    "cases": [
                        {
                            "case_id": "c1",
                            "query": "q?",
                            "tenant": "ankor",
                            "section_roles": ["public"],
                            "expected_tenant": "ankor",
                            "expected_section_role": "public",
                            "expected": "a",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        async with admin_pool.connection() as conn, conn.transaction():
            await _bind(conn, BOREA_ID)
            await seed_golden_sets(conn, BOREA_ID, golden_dir=thu_muc)

        async with admin_pool.connection() as conn, conn.transaction():
            await _bind(conn, BOREA_ID)
            assert (await read_golden_set(conn, ref, BOREA_ID)).golden_set_ref == ref

        async with admin_pool.connection() as conn, conn.transaction():
            await _bind(conn, ANKOR_ID)
            with pytest.raises(GoldenSetNotFound):
                await read_golden_set(conn, ref, ANKOR_ID)
