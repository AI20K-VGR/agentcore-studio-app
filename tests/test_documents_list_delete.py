"""`routes/documents.py` — phần đọc/xoá của tab Tài liệu (`GET /api/admin/documents`,
`POST /api/admin/documents/delete`).

Bài ở đây cố ý CHỈ đo hai thứ thuần, chạy được không cần Postgres: cách suy tên hiển thị, và chốt
chặn embedding trên đường chỉ-đọc. Phần route đi qua DB theo đúng khuôn `test_documents_routes.py`
(skip khi thiếu `STUDIO_DATABASE_URL_ADMIN`) — không dựng khuôn thứ hai cho cùng một loại test.
"""

from __future__ import annotations

import pytest
from studio_app.routes.documents import _display_name, _ReadOnlyEmbedding


def test_display_name_bo_dung_tien_to_phong_ban() -> None:
    assert _display_name("hr-chinh-sach-nghi-phep", "hr") == "chinh-sach-nghi-phep"


def test_display_name_khong_cat_nham_khi_slug_phong_ban_co_gach() -> None:
    """Vế đắt của hàm này. `doc_id = f"{slug(section_role)}-{slug(stem)}"`, mà slug của tên phòng
    ban thường CÓ gạch (`"Nhan su"` → `"nhan-su"`). Cắt bằng `split("-", 1)` sẽ ra `"su-chinh-sach"`
    — tên tài liệu dính một mảnh tên phòng ban, và nhìn vào không ai đọc ra là sai."""
    assert _display_name("nhan-su-chinh-sach", "Nhan su") == "chinh-sach"


def test_display_name_giu_nguyen_khi_khong_khop_tien_to() -> None:
    """Dòng cũ (ghi trước khi khoá `doc_id` mang `section_role`, app#58) không có tiền tố nào để
    cắt. Trả nguyên còn hơn trả rỗng: một dòng trống trên bảng KB là thứ người dùng không hành
    động được."""
    assert _display_name("chinh-sach", "hr") == "chinh-sach"


async def test_read_only_embedding_nem_neu_bi_goi() -> None:
    """Chốt chặn, không phải bài hình thức: `KbPipeline` đòi một embedding ở constructor nhưng
    đường đọc/xoá không embed gì. Nếu sau này ai thêm một lời gọi có embed vào hai route đó, bài
    này (và chính lần chạy thật) phải đỏ ngay, thay vì âm thầm ghi vector rác vào `kb.chunks`."""
    with pytest.raises(AssertionError, match="không được embed"):
        await _ReadOnlyEmbedding().embed(["a", "b"])
