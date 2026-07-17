# agentcore-studio-app

> Composition root: `ensure_all_schemas`, `grant_app_privileges`, FastAPI wiring, RLS middleware (`SET LOCAL app.tenant_id`).

**Owner:** mentor · **Loại:** uv workspace member (Python 3.14) · **Repo cha:** [agentcore-studio-kit](https://github.com/hieubui2409/agentcore-studio-kit)

## Repo này là gì
Submodule `apps/studio` của workspace `agentcore-studio-kit`. Owner: **mentor**. Đây là **composition root** — lắp ráp toàn bộ domain, dựng schema, cấp quyền, wiring FastAPI + middleware RLS hai-role.

## ⚠️ Không build/test độc lập được
`agentcore-studio-app` lắp ráp cả workspace nên phụ thuộc toàn bộ domain + uv.lock + `docker/postgres-init` của repo cha, và cần **Postgres** cho test (queue / trace / RLS). Vì vậy:
- **Làm việc qua repo cha:** `git clone --recursive git@github.com:hieubui2409/agentcore-studio-kit.git`, rồi `cd apps/studio` để sửa / commit / push chính repo này.
- **Test đầy đủ:** đẩy PR → CI tự **dựng lại full workspace** rồi chạy `pytest apps/studio/tests` (Phương án B).

## CI
`.github/workflows/ci.yml` chỉ là **stub** gọi reusable workflow chung ở repo cha:
`hieubui2409/agentcore-studio-kit/.github/workflows/reusable-domain-ci.yml@main`.
Muốn đổi quy trình CI thì sửa ở repo cha (1 chỗ).

## Quy tắc
- Là composition root nên chạm nhiều domain — ưu tiên wiring, không viết logic domain ở đây.
- Đổi contract → sang repo `agentcore-studio-contracts` (2-approval).
- Không commit tài liệu mentor/rubric/answer-key (pre-commit `nda-denylist` chặn).

📖 Phân quyền + luồng thao tác đầy đủ: [GITFLOWS.md](https://github.com/hieubui2409/agentcore-studio-kit/blob/main/GITFLOWS.md)
