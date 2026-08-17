---
id: apps-studio.adr.real-auth-system
type: adr
status: accepted
owner: SWE — @Dozyboy
date: 2026-08-14
related: app#17, app#13, app#11
signature: Approve trên app#17 (ADR-D11-01 §"Cơ chế chữ ký") — không tự-điền tên ở đây
---

# ADR — Hệ thống đăng nhập thật thay `_DEMO_ACCOUNTS` (app#17)

## Bối cảnh

`_DEMO_ACCOUNTS` (dict cứng, không mật khẩu, `routes/auth.py`) là toàn bộ auth ban đầu. Hai issue
(`app#13` — role thiếu thực tế; `app#11` — không bài test nào ghim bất biến phân quyền) mở ra câu
hỏi lớn hơn: thay hẳn bằng hệ thống thật (superadmin bootstrap → company-admin → nhân viên), không
phải vá tiếp registry cứng.

Bản kế hoạch ban đầu (đã trao đổi với chủ repo trước khi mở PR) định giữ `/api/auth/demo-login` và
`/api/auth/login` chạy **song song** — "demo tiện cho dev-stack, login là đường thật cho
production". Quyết định này bị **đảo ngược** giữa chừng (2026-08-14): review `app#17` (Chặn 1) chỉ
ra 2 đường không độc lập — cửa yếu nhất (`demo-login`, không mật khẩu) mở được `POST
/api/admin/users`/`companies`, vì JWT nó phát hành không phân biệt được nguồn với JWT của
`/api/auth/login`. Route `demo-login` cùng `_DEMO_ACCOUNTS` bị xoá hẳn (không deprecate song song)
— commit `6de63b8`.

## Quyết định

1. **Chỉ một đường đăng nhập**: `POST /api/auth/login`, tra `core.users` bằng email + verify mật
   khẩu bcrypt. Mọi tài khoản (kể cả dùng để dev/test) phải có dòng thật trong `core.users`.

2. **`core.users` KHÔNG bật RLS theo tenant** — khác `wb.recipes`/`kb.chunks`. Bước login (tra
   email) xảy ra TRƯỚC khi biết tenant nào (gà-trứng: chưa đăng nhập thì chưa có `app.tenant_id`
   để RLS lọc theo). Nếu bật `FORCE ROW LEVEL SECURITY`, ngay cả `studio_owner` cũng bị chặn tra
   email lúc chưa có tenant context — sập luôn bước login. Ranh giới "admin công ty chỉ quản được
   user tenant mình" enforce ở tầng APPLICATION (route tự so `session.tenant_id`), giống
   `core.tenants` hiện tại — không phải sơ suất, là lựa chọn có chủ đích cho đúng loại bảng
   identity/registry này.

3. **Tenant hệ thống `__system__`**: superadmin không thuộc công ty nào, nhưng
   `core.users.tenant_id` CỐ Ý không nullable (giữ nguyên `ResolvedContext`/`tenant_wall.py`/RLS ở
   mọi bảng khác — không phải sửa dây chuyền cho 1 trường hợp đặc biệt). `scripts/seed_superadmin.py`
   tự tạo tenant `__system__` nếu chưa có. `POST /api/admin/users` chặn (400) nếu người gọi có
   `superadmin` nhưng không có `admin` — tránh user mới rơi âm thầm vào `__system__` (review
   `app#17`, "nên sửa" #3).

4. **Bootstrap superadmin NGOÀI luồng API** (`scripts/seed_superadmin.py`, chạy tay 1 lần) — không
   có endpoint HTTP nào tạo được superadmin: nếu có, đó tự nó là lỗ hổng leo quyền (ai xác minh
   được người gọi API đó có quyền phong superadmin cho chính họ?).

## Hệ quả đã chấp nhận

- `app#13`/`app#11` đóng theo hướng "registry không còn tồn tại" (không phải "đã vá đúng registry
  đó") — `_DEMO_ACCOUNTS`/`_EXPECTED_TENANT`/`test_auth_registry_invariants.py` xoá hẳn.
- README kit-root đồng bộ cùng đợt (bảng tài khoản demo không còn ý nghĩa) — PR riêng ở kit-root,
  bump con trỏ `apps/studio` sau khi cả hai merge.
- Không còn đường đăng nhập nào "khỏi cần mật khẩu" cho dev-stack — mọi lần seed dev-stack đều cần
  chạy `seed_superadmin.py` trước khi đăng nhập được.
- Timing/status-code oracle cho email tồn tại/không ở `POST /api/auth/login`
  (`DUMMY_PASSWORD_HASH`, `jwt_auth.verify_password`'s 72-byte guard) và validation-error leak
  (`app.py`'s `_redact_sensitive_validation_errors`) là những mặt còn lại của CÙNG quyết định này —
  1 đường đăng nhập thật nghĩa là đường đó phải chịu đúng mức soi mà một hệ thống identity thật
  cần, khác hẳn 1 dict demo không ai kỳ vọng chịu tải đó.

- **Cross-tenant email-exists oracle ở `POST /api/admin/users`/`POST /api/admin/companies` — CHẤP
  NHẬN, KHÁC mặt oracle ở trên** (review `app#17` đợt 5, Chặn A — sửa lại đoạn này vì bản trước nói
  ngược: coi 2 mặt oracle là "cùng 1 quyết định đã xử lý", trong khi mặt này CHƯA xử lý). `email`
  UNIQUE toàn hệ thống (không theo tenant) — company-admin gọi `create_user`/`create_company` với
  1 email đã tồn tại (kể cả ở tenant KHÁC, kể cả chính email superadmin) nhận `409`, lộ ra "email
  này tồn tại ở đâu đó trong hệ thống" bằng đúng 1 request, không cần mẹo timing nào. Đây KHÁC oracle
  ở `login()`: oracle đó cần dò TỪNG email một để biết nó có tồn tại; oracle này thêm khả năng dò
  ĐƯỢC những email admin/superadmin có giá trị cao (mục tiêu brute-force tiếp theo) từ một tài
  khoản company-admin cấp thấp.
  - **Lý do chấp nhận thay vì sửa ngay**: sửa thật đòi đổi `core.users.email` UNIQUE sang
    `(tenant_id, email)`, kéo theo `login()` không còn tra được chỉ bằng email (cần thêm định danh
    công ty ở form đăng nhập) — đụng `apps/web#5` (LoginForm đang mở song song, ngoài phạm vi
    `app#17`). Đổi thiết kế đó cần bàn với `apps/web` trước, không làm đơn phương ở PR này.
  - **Ai chịu rủi ro**: người vận hành hệ thống (biết superadmin email dễ bị dò hơn qua route quản
    trị nội bộ, không phải endpoint công khai — company-admin đã là tài khoản có xác thực, không
    phải người lạ ẩn danh). Giảm nhẹ hiện có: route `/api/admin/*` đòi Bearer JWT hợp lệ của 1
    company-admin/superadmin thật (không phải endpoint mở), nên kẻ tấn công phải có ít nhất 1 tài
    khoản admin hợp lệ trước khi dò được oracle này.
  - **Theo dõi**: nếu `apps/web` chuyển sang yêu cầu định danh công ty lúc đăng nhập (hoặc có nhu
    cầu bảo mật khác đẩy ưu tiên lên), đổi UNIQUE theo tenant là hướng sửa thật — ghi 1 issue riêng
    khi quyết định đó chín, không phải trong `app#17`.

- **Stale JWT roles/tenant — CHỈ đóng cho `POST /api/admin/companies`/`POST /api/admin/users`,
  CHƯA đóng cho phần còn lại của hệ thống** (review `app#17` đợt 8→9 — đợt 8 sửa 2 route admin, đợt
  9 review lại chỉ ra khung "zero remaining risk" ngụ ý ở đợt 8 nói quá tay). `session.roles`/
  `session.tenant_id` là claim JWT — ảnh chụp lúc đăng nhập, sống tới `jwt_expire_minutes` (mặc
  định 480 phút). 2 route admin giờ tra `roles`/`tenant_id` TƯƠI từ `core.users` mỗi request (đợt
  8) — nhưng đây là 1 THỂ HIỆN của vấn đề, không phải TOÀN BỘ vấn đề:
  - `tenant_context_middleware` (`middleware.py:109`) vẫn `SET LOCAL app.tenant_id` từ
    `session.tenant_id` (JWT) cho MỌI request, làm nguồn duy nhất cho RLS ở `wb.*`/`kb.*`.
  - `routes/runs.py`, `routes/chat.py`, `routes/publish.py`, content-section role fence ở
    `packages/engine` đều còn phân quyền bằng `session.roles`/`session.tenant_id` trực tiếp.
  - **Hệ quả thật**: 1 nhân viên bị thu hồi role `hr` (sửa `core.users.roles` trong DB) vẫn đọc
    được tài liệu HR qua fence tới khi JWT hết hạn tự nhiên (tối đa 480 phút) — bán kính LỚN HƠN 2
    route admin vừa vá (đó chỉ chặn được leo quyền TẠO tài khoản/công ty, không chặn được đọc dữ
    liệu tenant-scoped bằng role đã bị thu hồi).
  - **Lý do chưa vá trọn ở `app#17`**: vá đúng nghĩa đòi middleware tự tra lại `roles`/`tenant_id`
    MỖI request (round-trip DB thêm cho MỌI route, không riêng 2 route admin) — đổi hiệu năng của
    toàn hệ thống (kể cả SSE chat, đường lưu lượng cao), cần đo/thiết kế riêng (cache ngắn hạn? tra
    mỗi N phút thay vì mỗi request? tenant/roles đổi hiếm tới mức nào?) — không phải sửa rẻ như 2
    route admin (đã có sẵn round-trip, chỉ cần lấy thêm cột).
  - **Theo dõi**: cần 1 issue/PR riêng đo đúng phạm vi + phương án (round-trip mỗi request vs cache
    TTL ngắn vs JWT revocation list) trước khi vá — ghi lại ở đây để không ai đọc code 2 route admin
    rồi kết luận nhầm "hệ thống đã hết stale-JWT risk".

## Chữ ký

Theo ADR-D11-01 §"Cơ chế chữ ký": chữ ký thật = Approve trên `app#17`, không phải bảng tự-điền ở
đây. File này chỉ ghi quyết định + lý do; lấy dấu vết chữ ký thật tại thời điểm đọc bằng:

```bash
gh pr view 17 --repo AI20K-VGR/agentcore-studio-app --json reviews,headRefOid \
  --jq '.headRefOid[0:8] as $h | .reviews[] | "\(.author.login) \(.state) \(.commit.oid[0:8]) \(if .commit.oid[0:8]==$h then "còn hiệu lực" else "STALE" end)"'
```
