"""`routes/publish.py::_evaluate` — map exception mới từ `run_agent_loop()` (app#44) thành HTTP status
sạch, không để rơi thành 500 trần không kiểm soát.

Trước app#44, `_evaluate()` chỉ bọc `EvalHarness().run()` bằng `except ValueError` (map 400 — đúng
1 nguyên nhân duy nhất khi đó: `golden_set_ref` sai khớp file `.yaml`). `eval_adapter.py::run_case()`
giờ chạy qua `run_agent_loop()`, và hàm đó có thể ném thêm 2 loại exception KHÔNG tồn tại ở
`interpreter.run()`: `AgentLoopExhausted` (hết `max_turns` chưa có câu trả lời cuối) và
`PermissionError` (tenant_id hỏng sau fence — phòng thủ, không nên trúng thật). Cả 3 giờ đều map
**500** — quyết định đã chốt với user (AskUserQuestion, cùng phiên với app#44): cả 3 là lỗi hệ
thống/recipe hỏng, không phải lỗi INPUT của client gọi `/evaluate`/`/publish`.

Không dựng DB thật: gọi `_evaluate()` trực tiếp, monkeypatch `EvalHarness` thành double ném đúng
exception cần đo (cùng khuôn `test_publish_wires_judge.py`'s `_StubEvalHarness`/`_SpyRunner`,
`publish_module.EngineAgentRunner`/`publish_module.EvalHarness`) — CỘNG `publish_module.get_pool`
thành 1 sentinel giả (`_evaluate()` chạm `get_pool()` để dựng `PgKbSearch`/`PgTraceWriter` TRƯỚC khi
gọi `EvalHarness` bị stub; cả 2 constructor chỉ lưu `pool` — không mở connection thật lúc dựng, xem
`packages/kb/src/studio_kb/postgres.py::PgKbSearch.__init__`/`obs/trace_writer.py::PgTraceWriter.__init__`
— và `_SpyRunner` không bao giờ gọi `.search()`/`.write()` nên sentinel không cần giả lập gì thêm).
Khác `test_publish_wires_judge.py` (dùng fixture `pool` thật, skip nếu thiếu DSN): bài này không cần
Postgres sống, chạy được ở mọi môi trường kể cả không có Docker/DB — đúng tinh thần TDD của app#44,
không để việc verify treo vào hạ tầng ngoài phạm vi issue."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import studio_app.routes.publish as publish_module
from fastapi import HTTPException
from studio_app.routes.publish import PublishRequest, _evaluate
from studio_app.settings import LlmProvider, Settings
from studio_contracts import Scorecard
from studio_engine.agent_loop import AgentLoopExhausted
from studio_engine.interpreter import RunResult
from studio_evalhub.golden_case import GoldenCase, GoldenSet
from studio_workbench.tenant_wall import ResolvedContext

ANKOR_ID = UUID("a0000000-0000-0000-0000-000000000001")


class _SentinelPool:
    """Không mở connection thật — chỉ cần thoả `Pool` đủ để `PgKbSearch.__init__`/
    `PgTraceWriter.__init__` lưu tham chiếu; `_SpyRunner` không bao giờ gọi `.search()`/`.write()`
    nên object này không cần làm gì hơn tồn tại."""


class _SpyRunner:
    def __init__(self, **kwargs: Any) -> None:
        del kwargs


class _RaisingEvalHarness:
    """Double thay `EvalHarness` — `run()` ném thẳng `_exc` đã chuẩn bị, không chạm golden-set/DB
    thật. Instance-level (không classmethod) vì `_evaluate()` gọi `EvalHarness()` rồi `.run(...)` —
    cần constructor 0-arg."""

    _exc: BaseException = ValueError("placeholder — set qua _set_exc trước khi dùng")

    async def run(self, agent_id: str, golden_set_ref: str, **kwargs: Any) -> Scorecard:
        del agent_id, golden_set_ref, kwargs
        raise self._exc


async def _fake_tenant_name(tenant_id: object) -> str:  # noqa: ARG001
    """Tên tenant giả cho bài không dựng connection request-scope."""
    return "ankor"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql://unused/unused",
        database_url_admin="postgresql://unused/unused",
        jwt_secret="test-secret-at-least-32-bytes-long",
        llm_provider=LlmProvider.GEMINI,
        judge_cache_path=tmp_path / "judge-cache.json",
        judge_cap_path=tmp_path / "judge-cap.json",
    )


def _body() -> PublishRequest:
    # app#78 (workbench#48): star topology mới cấm "end" và đòi đúng 1 node llm-step — 1 node
    # llm-step đơn (0 edge) là hình tối thiểu pass cả `enforce_agent_shape`/`enforce_agent_topology`.
    return PublishRequest(
        agent_id="agent-exc-mapping",
        system_prompt="x",
        tool_whitelist=[],
        nodes=[{"id": "n1", "type": "llm-step", "params": {}}],
        edges=[],
    )


async def _fake_get_pool() -> _SentinelPool:
    return _SentinelPool()


async def _fake_load_golden_set(ref: str, tenant_id: UUID) -> GoldenSet:
    """Thay `_load_golden_set` (đọc `eval.golden_sets`) — cutover file→DB.

    Bài trong file này CỐ Ý không đụng DB (xem `_SentinelPool`): chúng đo việc nối dây quanh
    `EvalHarness.run()`, không đo nguồn bộ case. Sau cutover `_evaluate()` cần một request-scoped
    connection đã bind `app.tenant_id` — dựng thật ở đây sẽ kéo cả DB + middleware vào một bài
    không nói gì về hai thứ đó, và mỗi tầng thêm vào là một chỗ bài đỏ vì lý do khác.

    Nguồn bộ case có bài riêng, đúng chỗ: `test_publish_reads_golden_from_db.py`.
    """
    del tenant_id
    return GoldenSet(
        golden_set_ref=ref,
        cases=[
            GoldenCase(
                case_id="c1",
                query="q?",
                tenant="ankor",
                section_roles=["public"],
                expected_tenant="ankor",
                expected_section_role="public",
                expected="a",
            )
        ],
    )


async def _run_evaluate_with(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, exc: BaseException) -> HTTPException:
    monkeypatch.setattr(publish_module, "get_pool", _fake_get_pool)
    monkeypatch.setattr(publish_module, "_load_golden_set", _fake_load_golden_set)
    # `_evaluate` giờ đọc `core.tenants.name` để dựng bảng tra tenant cho `EvalHarness` (trước là
    # fixture `TENANT_IDS` chỉ có 2 tenant demo, nên mọi tenant thật ném `KeyError`). Bài này stub
    # tầng DB, nên stub luôn chỗ đọc mới — cùng lý do `_load_golden_set` đã được stub ngay trên.
    monkeypatch.setattr(publish_module, "_tenant_name", _fake_tenant_name)
    monkeypatch.setattr(publish_module, "EngineAgentRunner", _SpyRunner)
    _RaisingEvalHarness._exc = exc
    monkeypatch.setattr(publish_module, "EvalHarness", _RaisingEvalHarness)
    monkeypatch.setattr(publish_module, "get_settings", lambda: _settings(tmp_path))
    # `_SpyRunner` discards every kwarg it receives, nhưng `_evaluate()` vẫn gọi `build_llm()`/
    # `build_embedding()` thật trước khi tới đó — 2 hàm đó tự gọi `get_settings()` từ
    # `studio_app.settings` (không đi qua `publish_module.get_settings` đã patch ở trên, tham
    # chiếu module khác), nên môi trường không có biến `STUDIO_*` thật sẽ vỡ ở đây trước khi
    # chạm được exception cần đo. Không cần giá trị thật — `_SpyRunner` không bao giờ dùng chúng.
    monkeypatch.setattr(publish_module, "build_llm", lambda: None)
    monkeypatch.setattr(publish_module, "build_embedding", lambda: None)

    session = ResolvedContext(tenant_id=ANKOR_ID, user="admin@acme.com", system_roles=["admin"])
    with pytest.raises(HTTPException) as exc_info:
        await _evaluate("agent-exc-mapping", _body(), session)
    return exc_info.value


async def test_agent_loop_exhausted_maps_to_500(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    exhausted = AgentLoopExhausted(
        "agent loop exhausted after 6 turn(s) without a final answer",
        partial=RunResult(run_id="r1", events=[], final_state={}),
        turns=6,
    )
    http_exc = await _run_evaluate_with(monkeypatch, tmp_path, exhausted)
    assert http_exc.status_code == 500
    assert "AgentLoopExhausted" in str(http_exc.detail) or "hết" in str(http_exc.detail).lower()


async def test_permission_error_maps_to_500(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    http_exc = await _run_evaluate_with(monkeypatch, tmp_path, PermissionError("tenant_id không phải UUID hợp lệ"))
    assert http_exc.status_code == 500


async def test_value_error_still_maps_to_400_with_generic_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Hồi quy — hành vi CŨ (400) không đổi. Message giờ KHÔNG được giả định cứng "golden_set_ref sai"
    (đó chỉ còn là 1 trong ≥2 nguyên nhân thật có thể tạo `ValueError` sau app#44 — nguyên nhân kia:
    LLM tự phát `TOOL_CALL:` một tool ngoài `tool_whitelist`, từ `WhitelistGuardedDispatch`)."""
    http_exc = await _run_evaluate_with(monkeypatch, tmp_path, ValueError("golden_set_ref không khớp"))
    assert http_exc.status_code == 400
