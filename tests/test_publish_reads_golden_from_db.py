"""`routes/publish.py::_evaluate` đọc bộ golden từ `eval.golden_sets`, KHÔNG còn từ đĩa (cutover).

## Vì sao fixture ở đây cố ý BẤT ĐỐI XỨNG

Bộ đóng gói sẵn trên đĩa (`callisto-2.0-golden-30-v1.yaml`) có **30 case**. Bài chính dưới đây nạp
vào DB một bộ **2 case** dưới ĐÚNG ref đó, rồi assert bộ mà harness nhận có 2 case.

Bất đối xứng đó là toàn bộ giá trị của bài. Nếu nạp vào DB đúng 30 case y hệt file, bài sẽ xanh cả
khi route vẫn đọc đĩa — nó không phân biệt được hai đường, tức không kiểm được thứ nó mang tên.
Cùng lý lẽ `test_golden_store.py` đã dùng: một fixture đối xứng qua hai đường chỉ chứng minh hai
đường tồn tại, không chứng minh đường nào được đi.

Số 2 (không phải 0) cũng có chủ đích: một bộ RỖNG sẽ khiến `GoldenSetNotFound` hoặc một mẫu số 0
xen vào, và bài sẽ xanh vì lý do khác hẳn lý do nó cần xanh.

## Vì sao gọi thẳng `_evaluate` chứ không qua HTTP

Bài này đo **nguồn dữ liệu**, không đo tầng HTTP. Đi qua `TestClient` sẽ kéo theo JWT/login/role
gate — tất cả đã có bài riêng — và mỗi tầng đó là một chỗ bài có thể đỏ vì lý do không liên quan.
`_evaluate` là chỗ hẹp nhất còn chứa được hành vi cần đo.

Đổi lại, request-scope phải tự dựng: `_load_golden_set` gọi `get_request_connection()`, thứ mà
`tenant_context_middleware` mới set trong đời thật. Test set thẳng ContextVar, cùng cách
`test_agents_routes.py` set `_request_session`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from psycopg import sql
from studio_app import middleware
from studio_app.core.golden_seed import GOLDEN_SET_DIR
from studio_app.routes import publish as publish_module
from studio_app.routes.publish import PublishRequest, _evaluate
from studio_app.settings import LlmProvider, Settings
from studio_contracts import Aggregate, CaseResult, Gate, GateThreshold, Scorecard
from studio_evalhub.golden_case import GoldenCase, GoldenSet
from studio_evalhub.golden_store import write_golden_set
from studio_kb.doc_factory import TENANT_IDS
from studio_workbench.tenant_wall import ResolvedContext

pytestmark = pytest.mark.asyncio

ANKOR_ID = TENANT_IDS["ankor"]
BOREA_ID = TENANT_IDS["borea"]
_REF = "callisto-2.0-golden-30-v1"


def _case(case_id: str) -> GoldenCase:
    return GoldenCase(
        case_id=case_id,
        query="Nhân viên xin nghỉ phép cần báo trước bao lâu?",
        tenant="ankor",
        section_roles=["public"],
        expected_tenant="ankor",
        expected_section_role="public",
        expected="Báo trước 3 ngày.",
    )


class _SentinelPool:
    """`_evaluate()` chạm `get_pool()` để dựng `PgKbSearch`/`PgTraceWriter` TRƯỚC khi tới đoạn cần
    đo. Cả hai chỉ giữ tham chiếu, không mở connection lúc dựng — cùng khuôn
    `test_publish_exception_mapping.py`."""


class _SpyRunner:
    def __init__(self, **kwargs: Any) -> None:
        del kwargs


class _CapturingEvalHarness:
    """Bắt kwargs `_evaluate()` truyền, không chạm hạ tầng eval thật."""

    last_kwargs: dict[str, Any] = {}

    async def run(self, agent_id: str, golden_set_ref: str, **kwargs: Any) -> Scorecard:
        _CapturingEvalHarness.last_kwargs = kwargs
        return Scorecard(
            agent_id=agent_id,
            golden_set_ref=golden_set_ref,
            results=[CaseResult(case_id="c1", expected="x", actual="x", success=True, citation_accuracy=1.0)],
            aggregate=Aggregate(success_rate=1.0, citation_accuracy=1.0, n_scored_citation=1),
            gate=Gate(threshold=GateThreshold(success=0.9, citation_accuracy=0.95), verdict="PASS"),
            recipe_hash="stub-not-checked-here",
        )


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
    return PublishRequest(
        agent_id="agent-golden-from-db",
        instructions="x",
        tool_whitelist=[],
        nodes=[{"id": "n1", "type": "kb-retrieve", "params": {}}, {"id": "n2", "type": "end", "params": {}}],
        edges=[{"from": "n1", "to": "n2"}],
    )


def _patch_around_the_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Vô hiệu mọi thứ QUANH đoạn đọc golden, giữ nguyên chính đoạn đọc."""

    async def _fake_get_pool() -> _SentinelPool:
        return _SentinelPool()

    monkeypatch.setattr(publish_module, "get_pool", _fake_get_pool)
    monkeypatch.setattr(publish_module, "EngineAgentRunner", _SpyRunner)
    monkeypatch.setattr(publish_module, "EvalHarness", _CapturingEvalHarness)
    monkeypatch.setattr(publish_module, "get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr(publish_module, "build_llm", lambda: None)
    monkeypatch.setattr(publish_module, "build_embedding", lambda: None)


async def _bind(conn: Any, tenant_id: UUID) -> None:
    await conn.execute(sql.SQL("SET LOCAL app.tenant_id = {}").format(sql.Literal(str(tenant_id))))


async def test_bo_case_di_vao_harness_den_tu_db_chu_khong_phai_file(
    admin_pool: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nạp 2 case vào DB dưới ref mà FILE trên đĩa có 30 — harness phải nhận 2.

    Đây là bài duy nhất trong file phân biệt được hai đường. Ba bài còn lại kiểm nhánh lỗi, và
    nhánh lỗi thì đường-đọc-file cũ cũng có, nên chúng KHÔNG thay được bài này."""
    _patch_around_the_read(monkeypatch, tmp_path)
    hai_case = GoldenSet(golden_set_ref=_REF, cases=[_case("db-01"), _case("db-02")])

    async with admin_pool.connection() as conn, conn.transaction():
        await _bind(conn, ANKOR_ID)
        await write_golden_set(conn, hai_case, ANKOR_ID)

        token = middleware._request_conn.set(conn)
        try:
            session = ResolvedContext(tenant_id=ANKOR_ID, user="admin@ankor.vn", roles=["admin"])
            await _evaluate("agent-golden-from-db", _body(), session)
        finally:
            middleware._request_conn.reset(token)

    nhan_duoc = _CapturingEvalHarness.last_kwargs["golden_set"]
    assert isinstance(nhan_duoc, GoldenSet)
    assert [c.case_id for c in nhan_duoc.cases] == ["db-01", "db-02"], (
        "harness phải nhận đúng bộ đã nạp vào DB. Nếu thấy 30 case, route vẫn đang đọc file trên đĩa."
    )
    assert "golden_set_path" not in _CapturingEvalHarness.last_kwargs, (
        "route không được truyền cả hai nguồn — `EvalHarness.run` từ chối khi nhận đủ 2 (evalhub#48)"
    )


async def test_file_tren_dia_van_co_30_case(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chống rỗng-nghĩa cho bài trên: khẳng định 30-vs-2 là một thế lệch THẬT.

    Không có bài này, ngày ai đó rút file 2.0 xuống còn 2 case, bài chính vẫn xanh nhưng đã thôi
    phân biệt được hai đường — nó sẽ xanh kể cả khi route quay về đọc đĩa."""
    del monkeypatch
    import yaml

    raw = yaml.safe_load((GOLDEN_SET_DIR / f"{_REF}.yaml").read_text(encoding="utf-8"))
    assert len(raw["cases"]) == 30, (
        f"file {_REF}.yaml phải có 30 case để thế lệch 30-vs-2 của bài chính còn nghĩa, thấy {len(raw['cases'])}"
    )


async def test_chua_nap_bo_nao_thi_400_va_chi_duong_nap(
    admin_pool: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ref chưa có trong DB ⇒ 400, và thông điệp phải chỉ ra đường nạp.

    Dùng ref ngẫu nhiên: một ref cố định có thể đã được bài khác/lần chạy khác nạp vào cùng DB test,
    và bài sẽ đỏ vì trạng thái để lại chứ không vì hành vi."""
    _patch_around_the_read(monkeypatch, tmp_path)
    body = _body()
    body_chua_nap = body.model_copy(update={"golden_set_ref": f"chua-nap-{uuid4()}"})

    async with admin_pool.connection() as conn, conn.transaction():
        await _bind(conn, ANKOR_ID)
        token = middleware._request_conn.set(conn)
        try:
            session = ResolvedContext(tenant_id=ANKOR_ID, user="admin@ankor.vn", roles=["admin"])
            with pytest.raises(HTTPException) as bat:
                await _evaluate("agent-golden-from-db", body_chua_nap, session)
        finally:
            middleware._request_conn.reset(token)

    assert bat.value.status_code == 400
    assert "seed_golden_sets.py" in str(bat.value.detail), (
        "sau cutover, nguyên nhân phổ biến nhất của 400 này là DB chưa seed — thông điệp phải nói ra"
    )


async def test_connection_bind_tenant_khac_thi_500_chu_khong_phai_400(
    admin_pool: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Connection bind tenant KHÁC phiên ⇒ 500, không được lẫn vào 400.

    Đây là bất biến đắt nhất của cutover. Dưới RLS `FORCE` cả hai ca đều cho `SELECT` trả 0 dòng —
    *chưa nạp bộ nào* và *connection sai scope* trông y hệt nhau ở tầng SQL. Gộp chúng làm một sẽ
    biến một lỗi lập trình (route cầm nhầm connection) thành một thông điệp bảo người dùng đi chạy
    script seed, và họ sẽ chạy, và nó sẽ không sửa được gì — im lặng mãi mãi. Bài này ép hai ca ra
    hai mã khác nhau."""
    _patch_around_the_read(monkeypatch, tmp_path)

    async with admin_pool.connection() as conn, conn.transaction():
        await _bind(conn, BOREA_ID)  # connection: borea
        token = middleware._request_conn.set(conn)
        try:
            session = ResolvedContext(tenant_id=ANKOR_ID, user="admin@ankor.vn", roles=["admin"])  # phiên: ankor
            with pytest.raises(HTTPException) as bat:
                await _evaluate("agent-golden-from-db", _body(), session)
        finally:
            middleware._request_conn.reset(token)

    assert bat.value.status_code == 500, (
        "connection sai scope là lỗi hệ thống, không phải lỗi input của client — gộp vào 400 sẽ "
        "chỉ người dùng đi chạy script seed cho một lỗi mà script không sửa được"
    )
    assert "GoldenSetScopeError" in str(bat.value.detail)


async def test_moi_tenant_doc_bo_cua_rieng_minh(
    admin_pool: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cùng một ref, hai tenant, hai bộ case khác nhau — mỗi phiên phải nhận bộ của tenant mình.

    Đây là thứ đường-đọc-file cũ **cấu trúc không làm được**: một file trên đĩa thì mọi tenant chấm
    bằng đúng nó. `UNIQUE (tenant_id, golden_set_ref)` (evalhub#46) là thứ làm bài này khả thi."""
    _patch_around_the_read(monkeypatch, tmp_path)
    ref = f"hai-tenant-{uuid4()}"
    body_ref = _body().model_copy(update={"golden_set_ref": ref})

    async def _chay(tenant_id: UUID, case_id: str) -> list[str]:
        async with admin_pool.connection() as conn, conn.transaction():
            await _bind(conn, tenant_id)
            await write_golden_set(conn, GoldenSet(golden_set_ref=ref, cases=[_case(case_id)]), tenant_id)
            token = middleware._request_conn.set(conn)
            try:
                session = ResolvedContext(tenant_id=tenant_id, user="u@x.vn", roles=["admin"])
                await _evaluate("agent-golden-from-db", body_ref, session)
            finally:
                middleware._request_conn.reset(token)
        return [c.case_id for c in _CapturingEvalHarness.last_kwargs["golden_set"].cases]

    assert await _chay(ANKOR_ID, "chi-cua-ankor") == ["chi-cua-ankor"]
    assert await _chay(BOREA_ID, "chi-cua-borea") == ["chi-cua-borea"]
