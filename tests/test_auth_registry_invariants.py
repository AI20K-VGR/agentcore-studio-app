"""`_DEMO_ACCOUNTS` là một bảng khai quyền, không phải dữ liệu trang trí (`apps#11`, review
dholmes0207, D20). Trước bài này, 2 test duy nhất chạm registry đều "mù cấu trúc": một bài so
sánh với CHÍNH registry (đổi registry thì kỳ vọng tự đổi theo — tautology,
`test_routes_auth.py:65`), một bài chỉ check `"admin" in roles` (xanh với MỌI tập role,
`test_routes_auth.py:82`). 4 mutant mô phỏng leo tenant/leo role (đổi tenant admin sang borea,
thêm role ngoài vocab, leo thang finance→admin, guest→admin) đều sống — không bài nào bắt được.

Bài này ghim TAY (không đọc lại từ `_DEMO_ACCOUNTS`, cố tình trùng lặp — với 1 bảng khai quyền,
trùng lặp là tính năng: ai đổi quyền phải đổi ở 2 chỗ và giải thích vì sao) 2 bất biến:
1. tenant của mỗi tài khoản không đổi so với danh sách viết tay.
2. roles ⊆ SECTION_VOCAB ∪ {"admin"} — không có tài khoản nào tự phong role ngoài vocab.

Mẫu copy từ `packages/kb/tests/test_doc_factory.py` (Duy trích dẫn trong `apps#11`):
`assert {c.section_role for c in load_callisto()} <= SECTION_VOCAB`.
"""

from __future__ import annotations

from studio_app.routes.auth import _DEMO_ACCOUNTS
from studio_kb.doc_factory import SECTION_VOCAB

# Viết tay, KHÔNG đọc lại từ _DEMO_ACCOUNTS — đối trọng trực tiếp lỗi tautology app#11 đã chỉ ra.
_EXPECTED_TENANT: dict[str, str] = {
    "admin@ankor.vn": "ankor",
    "hr@ankor.vn": "ankor",
    "finance@ankor.vn": "ankor",
    "intern@ankor.vn": "ankor",
    "admin@borea.vn": "borea",
    "nhanvien@borea.vn": "borea",
}

_ROLE_VOCAB: frozenset[str] = SECTION_VOCAB | {"admin"}


def test_demo_accounts_has_exactly_the_expected_emails() -> None:
    """Sàn: không âm thầm thêm/xoá tài khoản mà bài dưới không biết — nếu ai thêm 1 email mới vào
    _DEMO_ACCOUNTS mà quên thêm vào bảng ghim tay này, bài này đỏ TRƯỚC, chỉ đúng chỗ thiếu."""
    assert set(_DEMO_ACCOUNTS) == set(_EXPECTED_TENANT)


def test_demo_accounts_tenant_matches_hand_pinned_table() -> None:
    """Mutant #1 (nặng nhất, app#11 mục 1): đổi tenant admin@ankor.vn sang borea — leo tenant qua
    đường demo-login. Bảng kỳ vọng viết tay, không suy ra từ chính registry."""
    for email, expected_tenant in _EXPECTED_TENANT.items():
        actual_tenant, _roles = _DEMO_ACCOUNTS[email]
        assert actual_tenant == expected_tenant, (email, actual_tenant, expected_tenant)


def test_demo_accounts_roles_subset_of_closed_vocab() -> None:
    """Mutant #2 (app#11 mục 2): thêm role `"superuser"` ngoài SECTION_VOCAB cho bất kỳ tài
    khoản nào — phải đỏ, không phải mọi chuỗi đều là role hợp lệ."""
    for email, (_tenant, roles) in _DEMO_ACCOUNTS.items():
        assert set(roles) <= _ROLE_VOCAB, (email, roles, sorted(_ROLE_VOCAB))


def test_non_admin_accounts_cannot_hold_admin_role() -> None:
    """Mutant #3 (app#11 mục 3): finance@ankor.vn -> ["admin", "finance"] — leo thang quyền một
    tài khoản KHÔNG phải admin lên ngang admin. Chỉ 2 tài khoản admin@* được giữ role "admin"."""
    admin_accounts = {"admin@ankor.vn", "admin@borea.vn"}
    for email, (_tenant, roles) in _DEMO_ACCOUNTS.items():
        if email not in admin_accounts:
            assert "admin" not in roles, f"{email} không phải tài khoản admin nhưng có role admin"


def test_every_account_can_read_public_content() -> None:
    """`app#13` (DE): mọi nhân viên phải đọc được quy định chung công ty (`public`). Trước bản vá:
    hr@/finance@ thiếu public (0 chunk khi hỏi tài liệu chung); guest@ có 0 role hoàn toàn."""
    for email, (_tenant, roles) in _DEMO_ACCOUNTS.items():
        assert "public" in roles, f"{email} không đọc được tài liệu chung công ty (thiếu 'public')"
