"""`POST /api/agents/{agent_id}/publish` (Kế hoạch 2, A4) — chạy `golden_set_ref` qua
`EvalHarness.run(core_only=True)` (evalhub, đã implement xong) ra `Scorecard`, rồi gọi `publish()`
(`studio_workbench.publish`, đã implement xong PR#22/D18) để gate + ghi `wb.recipes`.

**Bộ golden đọc từ `eval.golden_sets`, không còn từ đĩa** (cutover, `evalhub#47`/`#48`). Trước bản
vá này route glob `studio_kb/golden/*.yaml` rồi khớp `golden_set_ref` khai bên trong — một đường
**không có khái niệm tenant**: mọi tenant bị chấm bằng đúng một file, và bộ case mà một tenant tự
nạp qua `POST /api/admin/golden-sets` (app#56) không bao giờ được dùng để chấm. Giờ route đọc theo
`(tenant_id phiên, recipe.golden_set_ref)` qua `read_golden_set`, và RLS `FORCE` trên
`eval.golden_sets` là hàng rào — không phải một mệnh đề `WHERE` mà route tự nhớ viết.

`DEC-D16-01` (evalhub/docs/decisions/scorecard.md) vẫn được giữ, chỉ dời chỗ: "composition root là
chỗ DUY NHẤT biết golden-30 nằm ở đâu" giờ ứng với **đường nạp** (`core/golden_seed.py`, cùng
`apps/studio`) thay vì đường đọc. Route này sau cutover không còn biết gì về đĩa.

**KHÔNG còn LUÔN trả 409** (sửa lại đợt review app#26 — trước bản vá này, `EvalHarness.run()` gọi
ở `_evaluate()` bên dưới không truyền `recipe_hash=`, nên `compute_scorecard()` luôn nhận `None`
và `publish()` fail-closed trên đúng field đó TRƯỚC CẢ khi đọc `gate.verdict`, bất kể agent tốt
xấu ra sao). `studio_workbench.publish.recipe_hash()` (DEC-03, hoàn thiện tại đây) giờ tính hash
thật trước khi gọi harness — 409 giờ chỉ còn xảy ra khi `gate.verdict == "FAIL"` thật hoặc
`recipe_hash` lệch, đúng nghĩa "recipe chưa đủ điều kiện xuất bản" mà status code đó mô tả.

**Vòng lặp review app#26 ⛔ (đã đóng ở cùng bản vá này):** bản vá gốc nối `recipe_hash()` mà
KHÔNG truyền `recipe=` vào `EngineAgentRunner` bên dưới — hậu quả là `certified_recipe()`
(`eval_adapter.py`) tự dựng lại 1 recipe KHÁC (`create_recipe_d4(...)`) để thật sự chạy qua
harness, trong khi hash lại băm recipe CANVAS. Cổng đối chiếu hash ở `publish()` khi đó so hash
canvas với chính nó — luôn khớp một cách vô nghĩa, biến "fail-closed" cũ thành "fail-open kèm
chứng nhận sai đối tượng". Truyền `recipe=recipe` (xem `_evaluate` bên dưới) đóng đúng lỗ đó —
recipe được băm giờ CHÍNH LÀ recipe được harness chạy qua từng case.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError
from studio_contracts import Edge, Node, NodeType, Recipe, Scorecard
from studio_engine.agent_loop import AgentLoopExhausted
from studio_evalhub.eval_job_store import (
    create_eval_job,
    fail_eval_job,
    finish_eval_job,
    read_eval_job,
    record_job_progress,
    sweep_stale_jobs,
)
from studio_evalhub.golden_case import GoldenSet
from studio_evalhub.golden_store import GoldenSetNotFound, GoldenSetScopeError, read_golden_set
from studio_evalhub.harness import EvalHarness
from studio_evalhub.judge import LLMJudge
from studio_evalhub.no_kb_golden import NO_KB_TENANT_LABEL, no_kb_golden_set
from studio_evalhub.scorecard_store import read_pending_scorecard, write_pending_scorecard
from studio_kb.doc_factory import TENANT_IDS
from studio_kb.postgres import PgKbSearch
from studio_workbench import create_recipe
from studio_workbench.publish import publish, recipe_hash
from studio_workbench.tenant_wall import ResolvedContext
from studio_workbench.validator import enforce_agent_shape, enforce_agent_topology

from studio_app import middleware
from studio_app.authz import fetch_fresh_identity, require_admin
from studio_app.core._db import get_pool
from studio_app.core.golden_autogen import auto_golden_set_ref
from studio_app.eval_adapter import EngineAgentRunner
from studio_app.middleware import get_request_connection, get_request_session
from studio_app.obs.trace_writer import PgTraceWriter
from studio_app.providers.factory import build_embedding, build_llm
from studio_app.settings import get_settings

router = APIRouter(prefix="/api/agents", tags=["publish"])

jobs_router = APIRouter(prefix="/api/eval-jobs", tags=["publish"])
"""Router RIÊNG: mã job không thuộc namespace của một agent nào — nó là mã của một LƯỢT CHẤM,
và tra nó không cần biết agent nào trước."""


class PublishRequest(BaseModel):
    """app#44 — TÁCH RIÊNG khỏi `routes/runs.py::RunRequest` (trước đó `/evaluate`/`/publish` dùng
    CHUNG 1 model với `POST /api/runs`). `routes/runs.py` đổi hẳn shape theo mục D
    (`PROJECT-SCOPE-DEMO-DAY30.md`) thành connectivity-check tĩnh — không còn `nodes`/`edges`/
    `kb_id`/`scope`/`golden_set_ref`/threshold nào cả. `/evaluate`/`/publish` vẫn cần `nodes`/
    `edges` để `create_recipe(...)` dựng `recipe.dag` (Recipe.dag vẫn là field bắt buộc trên
    contract dù `run_agent_loop()` không đọc nó — xem `docs/system-architecture.md`).

    kit#212/workbench#39: `model`/`kb_id`/`scope`/`success_threshold`/`citation_accuracy_threshold`
    không còn là input client — `create_recipe()` hardcode cố định các giá trị này làm quyết định
    nền tảng. `temperature` thay vào đó là input cấu hình động thật của người dùng."""

    agent_id: str
    system_prompt: str
    tool_whitelist: list[str] = Field(default_factory=list)
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    # Xem giải thích đầy đủ (bộ golden 2.0, app#30, mốc land) ở docstring gốc `routes/runs.py::RunRequest`
    # trước app#44 — không lặp lại ở đây, lý do chọn mặc định này không đổi.
    golden_set_ref: str = "callisto-2.0-golden-30-v1"
    # kit#212/workbench#39 — model/kb_id/scope/success_threshold/citation_accuracy_threshold không
    # còn là input client: create_recipe() hardcode cố định các giá trị này làm quyết định nền
    # tảng. temperature thay vào đó là input cấu hình động thật của người dùng.
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    # `tenant_id` CỐ Ý KHÔNG có field ở đây — cùng lý do T1 IDOR đã khoá ở `RunRequest` gốc
    # (`test_routes_runs.py`, trước app#44): tenant luôn tới từ `get_request_session()`.


async def _tenant_name(tenant_id: UUID) -> str:
    """Tên tenant từ `core.tenants` — thứ `GoldenCase.tenant` mang.

    Trước bản vá này route dựng bảng tra bằng `dict(TENANT_IDS)` (`studio_kb.doc_factory`), một
    **fixture Sprint 1 chỉ có 2 tenant demo** (`ankor`/`borea`) mà chính docstring của nó dán nhãn
    *"KHÔNG phải cách phân giải thật"*. `EvalHarness` tra bằng `tenant_ids[case.tenant]`, còn
    `GoldenCase.tenant` được `golden_autogen.regenerate_for_section` điền bằng `core.tenants.name`
    THẬT — nên mọi tenant không tên `ankor`/`borea` làm cổng ném `KeyError` thô, không phải 400 có
    thông điệp. Đo trên DB demo: `core.tenants.name` là `['Acme Demo', 'Ankor', '__system__']`,
    không tên nào tra được. Đường upload đã đọc `core.tenants` đúng (`documents.py::_tenant_slug`),
    chỉ đường chấm điểm còn dùng fixture."""
    cur = await get_request_connection().execute("SELECT name FROM core.tenants WHERE id = %s", (tenant_id,))
    row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"tenant {tenant_id} không có trong core.tenants")
    return str(row[0])


async def _resolve_golden_set(recipe: Recipe, tenant_id: UUID) -> GoldenSet:
    """Bộ golden dùng để chấm agent CÓ gắn KB — ba nấc, dừng ở nấc đầu tiên cho ra kết quả.

    **Nấc 1 — canvas khai.** `params["section_roles"]` của node `kb-retrieve` là chỗ người dựng
    agent NÓI nó gắn kho nào; suy ref bằng `auto_golden_set_ref` (đúng tên
    `golden_autogen.regenerate_for_section` đã sinh lúc upload). Đây là đường đúng và là đường duy
    nhất không mơ hồ khi tenant có nhiều kho.

    **Nấc 2 — `recipe.golden_set_ref`.** Hành vi cũ, giữ nguyên cho mọi caller đang khai ref tường
    minh (test, bộ seed `callisto-*`, bộ người viết nạp qua `POST /api/admin/golden-sets`).

    **Nấc 3 — cầu tạm, và CHỈ khi câu trả lời là duy nhất.** Node `kb-retrieve` hôm nay có
    `fields: []` (`apps/web/src/recipe/contract.ts` — *"chỉ còn là 1 biểu tượng đánh dấu"*) nên nấc
    1 luôn rỗng, và canvas gửi `golden_set_ref` mặc định cứng `"callisto-2.0-golden-30-v1"` — một
    bộ demo tenant thật không có, nên nấc 2 ném `GoldenSetNotFound` và **mọi lần bấm Chấm điểm đều
    400**. Khi đó lấy các phòng ban thật sự có chunk: đúng một ⇒ dùng nó; nhiều hơn ⇒ 400 nêu đích
    danh lựa chọn, KHÔNG đoán. Đoán ở đây là chấm agent bằng bộ của phòng ban khác rồi trả về một
    Scorecard trông hợp lệ — đúng lớp "chứng nhận sai đối tượng".

    Nấc 3 biến mất khi canvas có ô chọn phòng ban cho node KB; lúc đó nấc 1 luôn trả lời được.
    """
    declared: list[str] = []
    for node in recipe.dag.nodes:
        if node.type is not NodeType.KB_RETRIEVE:
            continue
        # `Node.params` là `dict[str, object]` tự do (contract), nên phải tự thu hẹp kiểu — một
        # `section_roles: "hr"` (chuỗi, không phải list) sẽ tách thành 2 ký tự nếu lặp thẳng.
        raw = node.params.get("section_roles")
        if isinstance(raw, list):
            declared.extend(str(role) for role in raw)
    if declared:
        return await _load_golden_set(auto_golden_set_ref(sorted(set(declared))[0]), tenant_id)

    try:
        return await _load_golden_set(recipe.golden_set_ref, tenant_id)
    except HTTPException as exc:
        if exc.status_code != 400:
            raise
        cur = await get_request_connection().execute(
            "SELECT DISTINCT section_role FROM kb.chunks ORDER BY section_role"
        )
        available = [str(row[0]) for row in await cur.fetchall()]
        if len(available) != 1:
            raise
        return await _load_golden_set(auto_golden_set_ref(available[0]), tenant_id)


async def _load_golden_set(ref: str, tenant_id: UUID) -> GoldenSet:
    """Bộ case của `(tenant_id, ref)` từ `eval.golden_sets`. Không có ⇒ 400, sai scope ⇒ 500.

    Dùng `get_request_connection()` chứ không mở connection mới từ `get_pool()`: `eval.golden_sets`
    bật RLS `FORCE`, và chỉ connection do `tenant_context_middleware` giữ mới đã `SET LOCAL
    app.tenant_id`. Một connection mới lấy từ pool **chưa** bind — dưới `FORCE` nó không lỗi, nó
    trả 0 dòng, và route sẽ báo "chưa nạp bộ case" cho một tenant đã nạp đầy đủ. Đây đúng là ca mà
    `GoldenSetScopeError` tồn tại để tách ra, nên nó được map 500 (lỗi hệ thống) chứ không gộp vào
    400 cùng `GoldenSetNotFound` (trạng thái hợp lệ của dữ liệu).

    400 cho `GoldenSetNotFound`, cùng mã lỗi mà đường-đọc-file cũ trả khi không khớp file nào — với
    client, "ref này không chấm được" không đổi bản chất. Thông điệp nêu tên script nạp: sau cutover,
    nguyên nhân phổ biến nhất không còn là gõ sai ref mà là **DB chưa được seed**, và một thông điệp
    chỉ nói "không tìm thấy" sẽ để người đọc đi tìm nhầm chỗ.
    """
    try:
        return await read_golden_set(get_request_connection(), ref, tenant_id)
    except GoldenSetNotFound as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                f"golden_set_ref {ref!r} chưa có trong eval.golden_sets cho tenant này. "
                f"Nạp bộ đóng gói sẵn: `uv run python apps/studio/scripts/seed_golden_sets.py`, "
                f"hoặc tự nạp bộ của bạn qua `POST /api/admin/golden-sets`."
            ),
        ) from exc
    except GoldenSetScopeError as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


async def _build_recipe(agent_id: str, body: PublishRequest, session: ResolvedContext) -> Recipe:
    """Dựng + validate `Recipe` từ canvas. Tách khỏi `_evaluate` để `/publish` tính được
    `recipe_hash` mà KHÔNG phải chạy cả bộ golden — đó là chỗ tra điểm tạm (`scorecard_store`).

    Không chạm DB, không gọi LLM: chỉ `create_recipe` + 2 lint. Nên gọi nó hai lần (một ở đây, một
    trong `_evaluate` khi phải chấm thật) rẻ và không có tác dụng phụ nào."""
    if body.agent_id != agent_id:
        raise HTTPException(
            status_code=400,
            detail=f"agent_id trên path ({agent_id!r}) khác agent_id trong body ({body.agent_id!r})",
        )

    try:
        nodes = [Node.model_validate(n) for n in body.nodes]
        edges = [Edge.model_validate(e) for e in body.edges]
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"node/edge không hợp lệ: {exc.errors()!r}") from exc

    try:
        recipe = create_recipe(
            agent_id=body.agent_id,
            tenant_id=session.tenant_id,
            system_prompt=body.system_prompt,
            tool_whitelist=body.tool_whitelist,
            nodes=nodes,
            edges=edges,
            temperature=body.temperature,
            golden_set_ref=body.golden_set_ref,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        # app#78 (workbench#48 breaking rename): `graph_lint()` bị xoá, thay bằng 2 hàm tách rời —
        # `enforce_agent_shape` (agent_config/kb_binding/golden_set_ref) + `enforce_agent_topology`
        # (recipe.dag, hình sao). Gọi tuần tự cả 2, cùng 1 try/except — đúng pattern nội bộ
        # `packages/workbench`'s own `publish()` đã dùng (commit 9b23520), ValueError nổi lên đây bắt.
        enforce_agent_shape(recipe)
        enforce_agent_topology(recipe)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"recipe không qua validator: {exc}") from exc

    return recipe


async def _evaluate(
    agent_id: str,
    body: PublishRequest,
    session: ResolvedContext,
    *,
    on_progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> tuple[Recipe, Scorecard]:
    """Dựng recipe từ canvas + chạy `golden_set_ref` qua `EvalHarness.run(core_only=True)` thật —
    tập **Core**, không phải cả bộ (xem chú thích tại lời gọi bên dưới). Dùng chung cho cả
    `/evaluate` (chỉ xem điểm) lẫn `/publish` (khi chưa có điểm tạm nào khớp `recipe_hash`).

    Server vẫn KHÔNG BAO GIỜ nhận verdict client tự khai — điểm tái dùng được là điểm chính server
    đã ghi ở lượt `/evaluate` trước, tra bằng `recipe_hash` server tự tính (`scorecard_store`)."""
    recipe = await _build_recipe(agent_id, body, session)

    # `session.tenant_id`, KHÔNG phải `recipe.tenant_id` — dù hai giá trị này bằng nhau ở đây
    # (`create_recipe(tenant_id=session.tenant_id)` ngay trên). Đọc từ recipe sẽ là lần đầu tiên
    # trong route này một quyết định phạm vi lấy từ đối tượng do client dựng nên; hôm nay vô hại,
    # nhưng nó dựng sẵn đúng khuôn mà INV-1 tồn tại để cấm. Phiên khai tenant, recipe thì không.
    # Recipe KHÔNG có node `kb-retrieve` thì không trích dẫn được gì, nên bộ thường luôn cho
    # `citation_accuracy = 0.0` và loại agent đó không bao giờ publish được. Chấm nó bằng bộ dựng
    # sẵn `builtin-no-kb-v1` — mọi case nhánh trả-lời, `expected` là câu nói-không-biết, nên cả hai
    # trục đo được mà không nới chốt nào (xem docstring `studio_evalhub.no_kb_golden`). Bộ đó KHÔNG
    # nằm trong `eval.golden_sets`; nó là hằng số trong mã nên không đi qua `_load_golden_set`.
    has_kb_node = any(node.type is NodeType.KB_RETRIEVE for node in recipe.dag.nodes)
    if has_kb_node:
        # `golden_set_ref` SUY từ phòng ban đã gắn, KHÔNG đọc `recipe.golden_set_ref`: field đó mang
        # mặc định cứng `"callisto-2.0-golden-30-v1"` (`PublishRequest`) — một bộ demo mà tenant
        # thật không có, nên mọi lần bấm Chấm điểm đều 400 `GoldenSetNotFound`. Bộ đúng là bộ
        # `golden_autogen.regenerate_for_section` đã sinh sẵn lúc upload, tên theo đúng quy ước
        # `auto_golden_set_ref` — nối hai đầu đó lại là toàn bộ nội dung của đoạn này.
        golden = await _resolve_golden_set(recipe, session.tenant_id)
    else:
        golden = no_kb_golden_set()

    # `Pool` (không phải `get_request_connection()`) — rủi ro pool self-deadlock CHƯA được giải
    # quyết ở route này, xem giải thích đầy đủ + lý do ở `routes/runs.py` (review `app#17` đợt 4,
    # sửa lại 1 lập luận SAI ở đợt 3: `get_pool()` là connection THỨ HAI cộng thêm vào connection
    # middleware đã giữ suốt request, không phải "tiết kiệm" hơn). Route này VẪN dùng
    # `get_request_connection()` riêng cho ghi publish cuối cùng (`publish(recipe, scorecard,
    # conn=get_request_connection())` dưới) — đúng, đó KHÔNG cộng thêm connection nào (tái dùng
    # connection middleware đã giữ), khác hẳn `get_pool()` ở đây.
    pool = await get_pool()
    settings = get_settings()
    embedding = build_embedding()
    runner = EngineAgentRunner(
        kb_search=PgKbSearch(pool, embedding),
        llm=build_llm(),
        embedding=embedding,
        trace_writer=PgTraceWriter(pool),
        # kit#127 (review app#26 ⛔) — recipe được CHẤM phải là recipe được PUBLISH. Không truyền
        # `recipe=` ở đây, `certified_recipe()` (eval_adapter.py) tự dựng `create_recipe_d4(...)`
        # (workbench#41 — nay là `create_recipe(...)`, cùng ý nghĩa) —
        # một recipe CỐ ĐỊNH, không liên quan gì tới canvas — làm recipe THẬT SỰ chạy qua từng case
        # golden-set, trong khi `recipe_hash(recipe)` ở dưới băm recipe CANVAS. Hai đối tượng khác
        # nhau về `agent_config`/`dag`/`kb_binding` (đo được: recipe được chấm còn có 1 node
        # `tool-call` admin chưa từng vẽ) — cổng đối chiếu hash ở `publish()` khi đó so hash CANVAS
        # với chính nó, không so được với thứ THẬT SỰ đã chạy, nên luôn khớp một cách vô nghĩa.
        # `enforce_agent_shape(recipe)` + `enforce_agent_topology(recipe)` đã chạy Ở TRÊN, ngay sau
        # khi `create_recipe(...)` dựng xong recipe (neo bằng TÊN lệnh gọi, không phải số dòng — số
        # dòng đã trôi thật ngay trong PR này, review app#26 🟡; app#78 đổi từ `graph_lint(recipe)`
        # cũ sang 2 hàm này, workbench#48) — đúng tiền điều kiện `GRAPH-LINT-CONTRACT`
        # mà `run_case` (eval_adapter.py) đòi hỏi cho nhánh `recipe=` được tiêm.
        recipe=recipe,
    )

    # `TENANT_IDS` giữ lại cho bộ seed sẵn (`callisto-*`) vốn khai `tenant="ankor"`; tên tenant
    # THẬT thêm vào sau nên nó thắng khi trùng. Xem `_tenant_name` cho lỗi `KeyError` mà vế thứ hai
    # vá. Bộ dựng sẵn mang nhãn hằng số `__no_kb_agent__` — ánh xạ nó về tenant của PHIÊN để case
    # chạy dưới đúng công ty đang publish, không phải một tenant demo nào.
    tenant_ids: dict[str, UUID] = dict(TENANT_IDS)
    tenant_ids[await _tenant_name(session.tenant_id)] = session.tenant_id
    if not has_kb_node:
        tenant_ids[NO_KB_TENANT_LABEL] = session.tenant_id
    try:
        scorecard = await EvalHarness().run(
            recipe.agent_id,
            # `golden.golden_set_ref`, KHÔNG phải `recipe.golden_set_ref`: hai giá trị bằng nhau ở
            # nhánh có KB, nhưng nhánh không-KB chấm bằng bộ dựng sẵn — khai ref của recipe ở đây
            # sẽ cho ra một Scorecard nói nó được chấm bằng một bộ nó chưa từng chạy.
            golden.golden_set_ref,
            golden_set=golden,
            runner=runner,
            tenant_ids=tenant_ids,
            threshold_success=recipe.scorecard_threshold.success,
            threshold_citation_accuracy=recipe.scorecard_threshold.citation_accuracy,
            # Chấm trên tập **Core** (`evalhub.select_core`), không phải cả bộ. Cổng này chạy ĐỒNG
            # BỘ trong một request HTTP: cả bộ golden nhân với trần `DEFAULT_MAX_TURNS` của
            # agent-loop (vừa lên 6→20, engine#45) cho một request dài tới mức hỏng dưới dạng
            # **timeout** thay vì dưới dạng scorecard trượt — hai kiểu hỏng người vận hành đọc rất
            # khác nhau, và cái sau mới là thứ cổng này sinh ra để nói.
            #
            core_only=True,
            # `core_min_answer=1`, KHÔNG phải mặc định 10 của evalhub — và đây là chỗ cần đọc kỹ.
            #
            # Bật `core_only` để giải bài **timeout**: đó là toàn bộ mục đích. Nhưng mặc định
            # `min_answer=10` của `select_core` kèm theo một thứ khác hẳn — một **tiền điều kiện
            # publish mới**: bộ nào dưới 10 case trả-lời thì `CoreSelectionError` → 400, tức không
            # publish được, nơi trước bản vá này publish được bình thường (đường cũ chạy cả bộ,
            # không có ngưỡng tối thiểu nào).
            #
            # Bán kính đã ĐO, không phỏng đoán: mọi fixture golden của các bài publish hiện có
            # (`test_publish_reads_golden_from_db.py`, `test_publish_wires_judge.py`,
            # `test_evaluate_certifies_the_recipe_it_scores.py`) chỉ có 1–2 case. Và từ app#61,
            # golden set sinh tự động theo từng `section_role` lúc upload nên tenant vừa nạp một
            # tài liệu cũng dưới ngưỡng — bộ nhỏ là ca THƯỜNG.
            #
            # Nâng ngưỡng đó là một quyết định SẢN PHẨM (đánh đổi: `success_rate` trên bộ nhỏ ít ý
            # nghĩa, đổi lấy việc tenant nhỏ publish được), tách hẳn khỏi bài timeout mà PR này
            # giải. Không gộp hai thứ vào một lần đổi: `1` giữ nguyên khả năng publish như hôm nay,
            # còn chọn `10` hay số khác thì để một lần chốt riêng, tường minh.
            core_min_answer=1,
            # DEC-03 hoàn thiện tại `studio_workbench.publish.recipe_hash()` — đây là mắt xích
            # DUY NHẤT còn thiếu của đường ống (evalhub đã mở `recipe_hash=` từ D20/`DEC-D20-02`,
            # chỉ chưa có caller nào tính+truyền giá trị thật). Băm ĐÚNG `recipe` vừa qua
            # `enforce_agent_shape`/`enforce_agent_topology` sạch ở trên — Scorecard trả về giờ
            # chứng nhận đúng recipe SẼ publish, KHÔNG phải một
            # recipe khác được dựng lại sau đó, CHỈ ĐÚNG vì `runner` ở trên được tiêm `recipe=recipe`
            # (review app#26 ⛔ — thiếu dòng đó thì `certified_recipe()` tự dựng recipe khác để
            # chạy, và hash ở đây sẽ chứng nhận nhầm đối tượng dù bản thân phép so ở `publish()`
            # vẫn "khớp" một cách vô nghĩa).
            recipe_hash=recipe_hash(recipe),
            # Chỉ đường chạy nền truyền móc này; đường đồng bộ để `None` và không đổi gì.
            on_progress=on_progress,
            # `apps/studio#20` — nhánh judge từng bị bỏ IM LẶNG: `EvalHarness.run` nhận `judge=`
            # từ D18 (`kit#118`) nhưng đường production chưa bao giờ truyền, nên
            # `if judge is not None and …` (`harness.py`) không bao giờ đúng. Không lỗi, không
            # cảnh báo — chỉ là một tầng chấm chưa từng chạy.
            #
            # Judge dựng ở ĐÂY chứ không trong `studio_evalhub`: `DEC-D18-02` cấm `judge.py` đọc
            # env hay dựng provider, và composition root là chỗ DUY NHẤT biết provider thật nào
            # được dựng — cùng hình seam với `AgentRunner`.
            #
            # An toàn nhờ `evalhub#30` đã vào con trỏ TRƯỚC bản vá này: `_duoc_hoi_judge` chặn
            # judge lật cổng `DEC-05` (`no-trace-no-proof`). Nối `judge=` khi cổng đó chưa có là
            # bật một fail-open — case `events == []` mà `answer` chứa đúng cụm `expected` sẽ được
            # judge cho PASS, tất định. Thứ tự đó là điều kiện, không phải sở thích.
            judge=LLMJudge(
                build_llm(),
                cache_path=settings.judge_cache_path,
                cap_path=settings.judge_cap_path,
            ),
        )
    except ValueError as exc:
        # Cutover: nguồn `ValueError` đầu tiên của khối này đã BIẾN MẤT khỏi đây. Trước, `EvalHarness.
        # run()` tự gọi `load_golden_set(path, expect_ref=…)` và ném `ValueError` khi ref trong file
        # lệch ref khai trong recipe. Giờ bộ case được đọc + kiểm TRƯỚC lời gọi này
        # (`_load_golden_set` ở trên), nên ca đó không còn đi qua đường này nữa — và `GoldenSetNotFound`
        # tuy LÀ một `ValueError` cũng không tới được đây vì đã bị bắt tại chỗ đọc, sớm hơn hẳn.
        # Giữ nguyên khối: nguồn thứ hai bên dưới vẫn sống.
        #
        # app#44: từ khi `EngineAgentRunner.run_case()` (eval_adapter.py) chạy qua `run_agent_loop()`
        # thay `interpreter.run()`, `ValueError` KHÔNG còn chỉ có 1 nguồn — `WhitelistGuardedDispatch`
        # (engine) cũng ném đúng kiểu này khi LLM tự phát `TOOL_CALL:` một tool NGOÀI
        # `agent_config.tool_whitelist` (model tự ý gọi, không phải lỗi client khai `golden_set_ref`
        # sai). Message KHÔNG còn giả định cứng nguyên nhân — trả nguyên `str(exc)` (đã đủ cụ thể ở
        # cả 2 nguồn) thay vì diễn giải sai 1 trong 2 trường hợp.
        #
        # NGUỒN THỨ BA từ khi bật `core_only=True` ở trên: `evalhub.CoreSelectionError` — nó LÀ một
        # `ValueError` nên rơi vào đúng khối này, và 400 là đúng ý ("bộ golden của tenant chưa đủ
        # để chấm", không phải bug hệ thống). Thông điệp của nó tự nói rõ thiếu bao nhiêu case
        # trả-lời và thiếu ở bộ golden chứ không ở luật chọn, nên `str(exc)` vẫn là thứ đúng để
        # trả — giữ nguyên quy tắc "không diễn giải cứng nguyên nhân" của khối này.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (AgentLoopExhausted, PermissionError) as exc:
        # app#44 — 2 loại exception MỚI `run_agent_loop()` có thể ném mà `interpreter.run()` không
        # có (xem docstring `agent_loop.py`, "Handoff to app#44"): `AgentLoopExhausted` (hết
        # `max_turns` chưa có câu trả lời cuối trên 1 case golden-set) và `PermissionError`
        # (`session_context.tenant_id` không phải UUID hợp lệ sau fence — phòng thủ, không nên
        # trúng thật qua đường `resolve_session` thật). Quyết định đã chốt với user (AskUserQuestion,
        # cùng phiên app#44): CẢ HAI map 500 — đây là lỗi hệ thống/recipe hỏng khi chấm golden-set,
        # không phải lỗi INPUT của client gọi `/evaluate`/`/publish`, cùng mức nghiêm trọng
        # `graph_lint()` đỏ trên recipe đã published từng dùng ở `routes/chat.py`. Không bắt bằng
        # `except Exception` chung: giữ MỌI exception KHÁC (bug thật, lỗi lập trình) lộ nguyên dạng
        # thay vì bị nuốt thành 500 vô danh.
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc
    return recipe, scorecard


@router.post("/{agent_id}/evaluate")
async def evaluate_agent(agent_id: str, body: PublishRequest) -> dict[str, object]:
    """Chấm điểm `golden_set_ref` qua `EvalHarness` thật, trả `Scorecard` — KHÔNG gọi `publish()`,
    không ghi `wb.recipes`. Dùng cho UI hiện điểm TRƯỚC khi quyết bấm Publish, để nút Publish có
    căn cứ bật/tắt mà không phải "bấm thử xem có được không".

    Chấm trên tập **Core**, không phải cả bộ — route này đi qua chính `_evaluate()` mà `/publish`
    dùng, nên nó thừa hưởng `core_only=True` ở đó (xem chú thích tại lời gọi `EvalHarness.run`).
    Điều đó là **bắt buộc**, không phải chi tiết cài đặt: điểm hiện ở đây là thứ người dùng lấy để
    quyết bấm Publish, nên nó phải được đo trên **đúng tập** mà cổng Publish sẽ chấm. Hai route
    chấm hai tập khác nhau thì con số xem trước không còn nói gì về kết quả thật."""
    session = get_request_session()

    # Cùng gate role-gap đã đóng ở `routes/runs.py::create_run` — route này trước bản vá cũng gọi
    # được bởi bất kỳ ai đã đăng nhập, không riêng admin.
    identity = await fetch_fresh_identity(get_request_connection(), session.user)
    require_admin(identity.system_roles)

    recipe, scorecard = await _evaluate(agent_id, body, session)

    # Ghi lại điểm để `/publish` khỏi chấm lại từ đầu (`scorecard_store`). Client KHÔNG cầm gì —
    # nó chỉ gửi lại recipe, server tự băm và tự tra, nên `verdict` không bao giờ đi qua tay client.
    # `recipe_version` để `NULL`: đây là điểm tạm, không phải chứng nhận của một version đã publish.
    await write_pending_scorecard(get_request_connection(), scorecard, session.tenant_id)

    return {
        "agent_id": recipe.agent_id,
        "tenant_id": str(recipe.tenant_id),
        "scorecard": scorecard.model_dump(mode="json"),
    }


@router.post("/{agent_id}/publish")
async def publish_agent(agent_id: str, body: PublishRequest) -> dict[str, object]:
    """409 giờ chỉ còn nghĩa "recipe chưa qua được gate" (`gate.verdict == 'FAIL'`) — KHÔNG còn
    LUÔN 409 như trước khi `recipe_hash` (DEC-03) có producer thật (xem docstring module + `_evaluate`
    ở trên: `EvalHarness().run()` giờ được truyền `recipe_hash=recipe_hash(recipe)` thật)."""
    session = get_request_session()

    # Cùng gate role-gap đã đóng ở `routes/runs.py::create_run`.
    identity = await fetch_fresh_identity(get_request_connection(), session.user)
    require_admin(identity.system_roles)

    # Tái dùng điểm của lượt Chấm điểm nếu recipe KHÔNG đổi — khoá bằng `recipe_hash`, thứ server
    # tự tính từ recipe client vừa gửi. Sửa một ký tự trên canvas là hash đổi, tra không ra, chấm
    # lại. Không tìm thấy thì chạy nguyên đường cũ, nên caller gọi thẳng `/publish` (test, script)
    # không đổi hành vi một dòng nào.
    #
    # Vì sao an toàn dù `/evaluate` là đường client gọi được: client không gửi Scorecard, không gửi
    # verdict, không gửi cả hash — nó chỉ gửi recipe. Mọi thứ quyết định cổng đều do server tính
    # hoặc đọc từ DB dưới RLS của chính tenant đó.
    recipe = await _build_recipe(agent_id, body, session)
    cached = await read_pending_scorecard(get_request_connection(), recipe.agent_id, recipe_hash(recipe))
    if cached is None:
        recipe, scorecard = await _evaluate(agent_id, body, session)
    else:
        scorecard = cached

    try:
        await publish(recipe, scorecard, conn=get_request_connection())
    except ValueError as exc:
        # `publish()` raise ValueError cho CẢ 4 nhánh chặn: `enforce_agent_shape`/
        # `enforce_agent_topology` nội bộ của nó (app#78/workbench#48 — đã kiểm ở trên nên khó
        # trúng lại), `recipe_hash is None`, `recipe_hash` LỆCH với recipe đang publish
        # (`workbench#27`, review app#26 ⛔), và `gate.verdict == 'FAIL'`. 409, không 400: đây
        # không phải lỗi INPUT của client, mà là "recipe hợp lệ nhưng CHƯA ĐỦ ĐIỀU KIỆN xuất bản".
        raise HTTPException(
            status_code=409,
            detail={"message": str(exc), "scorecard": scorecard.model_dump(mode="json")},
        ) from exc

    return {
        "agent_id": recipe.agent_id,
        "tenant_id": str(recipe.tenant_id),
        "status": "published",
        "scorecard": scorecard.model_dump(mode="json"),
    }


# ---------------------------------------------------------------------------------------------
# Chấm điểm chạy nền (`kit` — việc 5). Xem `studio_evalhub.eval_job_store` cho lý do và hình dạng.
# ---------------------------------------------------------------------------------------------

_STALE_EVAL_JOB_SECONDS = 120
"""Job `running` im lặng quá lâu ⇒ coi như tiến trình đã chết giữa chừng.

Ngưỡng đo *"lâu không cập nhật"*, không phải *"chạy đã lâu"* (`sweep_stale_jobs`), nên một lượt chấm
100 case chạy 8 phút vẫn an toàn miễn nó còn báo tiến độ sau mỗi case."""


_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()
"""Giữ tham chiếu tới task đang chạy.

`asyncio.create_task` chỉ giữ **weak reference**: không giữ ở đâu cả thì GC thu task giữa chừng và
lượt chấm biến mất không dấu vết. Đây là cái bẫy `asyncio` tự ghi trong tài liệu của chính nó."""


@asynccontextmanager
async def _tenant_scoped_connection(tenant_id: UUID) -> AsyncIterator[Any]:
    """Kết nối riêng cho task nền, đã bind `app.tenant_id`.

    Task nền chạy SAU khi request kết thúc, nên `get_request_connection()` không dùng được —
    connection đó đã đóng. Nó phải tự lấy từ pool và **tự cầm hàng rào tenant**, thay vì được
    `tenant_context_middleware` cầm hộ. Đây là chỗ dễ rò tenant nhất của cả việc này.

    `SET LOCAL` trong một transaction ngắn chứ không `set_config(..., false)` ở mức session: giá
    trị mức session **sống sót** khi connection trả về pool, nên một task chết giữa chừng để lại
    một connection còn bind tenant cho request kế tiếp nhặt được. `SET LOCAL` tự hết hiệu lực khi
    transaction đóng — không phụ thuộc vào việc ta có nhớ dọn hay không.

    Hệ quả: mỗi bước DB của task là một transaction ngắn RIÊNG, không phải một transaction dài ôm
    cả lượt chấm. Đó là chủ đích — giữ một transaction mở suốt 5-10 phút là giữ khoá và làm phình
    bảng, và lượt chấm không cần tính nguyên tử giữa các bước.
    """
    pool = await get_pool()
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
        token = middleware._request_conn.set(conn)
        try:
            yield conn
        finally:
            middleware._request_conn.reset(token)


async def _run_eval_job(job_id: UUID, agent_id: str, body: PublishRequest, session: ResolvedContext) -> None:
    """Chạy trọn một lượt chấm ngoài vòng đời request, rồi ghi kết quả vào đúng chỗ `/publish` tra.

    Không ném ra ngoài: task nền không có ai bắt, nên mọi lỗi phải thành `status='failed'` kèm
    thông điệp — một job treo `running` mãi là thứ người dùng ngồi đợi một cách vô vọng."""

    async def _on_progress(done: int, total: int) -> None:
        async with _tenant_scoped_connection(session.tenant_id) as conn:
            await record_job_progress(conn, job_id, done, total)

    try:
        async with _tenant_scoped_connection(session.tenant_id) as conn:
            _, scorecard = await _evaluate(agent_id, body, session, on_progress=_on_progress)
            await write_pending_scorecard(conn, scorecard, session.tenant_id)
            await finish_eval_job(conn, job_id)
    except Exception as exc:  # noqa: BLE001 — xem docstring: không ai bắt hộ task nền
        detail = exc.detail if isinstance(exc, HTTPException) else f"{type(exc).__name__}: {exc}"
        with contextlib.suppress(Exception):
            async with _tenant_scoped_connection(session.tenant_id) as conn:
                await fail_eval_job(conn, job_id, str(detail))


@router.post("/{agent_id}/evaluate-async", status_code=202)
async def evaluate_agent_async(agent_id: str, body: PublishRequest) -> dict[str, object]:
    """Khởi động một lượt Chấm điểm chạy nền, trả **ngay** mã job.

    Khác `/evaluate` (đồng bộ, giữ request mở suốt lượt chấm): route này nhả kết nối ngay, nên bộ
    100 case chạy 5-10 phút không còn là một request treo. `/evaluate` giữ nguyên cho bộ nhỏ và cho
    caller đang có.

    Recipe được dựng + lint **đồng bộ** ở đây, trước khi tạo job: recipe hỏng phải ra 400 ngay lúc
    bấm, không phải một job `failed` mà người dùng đợi rồi mới biết. Chỉ phần TỐN THỜI GIAN mới đi
    xuống nền.
    """
    session = get_request_session()
    identity = await fetch_fresh_identity(get_request_connection(), session.user)
    require_admin(identity.system_roles)

    recipe = await _build_recipe(agent_id, body, session)
    job_id = await create_eval_job(get_request_connection(), session.tenant_id, recipe.agent_id, recipe_hash(recipe))

    task = asyncio.create_task(_run_eval_job(job_id, agent_id, body, session))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)

    return {"job_id": str(job_id), "status": "running", "agent_id": recipe.agent_id}


@jobs_router.get("/{job_id}")
async def read_eval_job_status(job_id: UUID) -> dict[str, object]:
    """Trạng thái + tiến độ của một lượt chấm nền; kèm Scorecard khi đã xong.

    Scorecard **không** lưu trên job — nó nằm ở `eval.scorecards` (`scorecard_store`), đúng chỗ
    `/publish` tra. Route này ghép hai thứ đó lại bằng `(agent_id, recipe_hash)` có sẵn trên job,
    nên không có nguồn sự thật thứ hai cho cùng một verdict.
    """
    session = get_request_session()
    identity = await fetch_fresh_identity(get_request_connection(), session.user)
    require_admin(identity.system_roles)

    conn = get_request_connection()

    # Dọn job treo NGAY TRONG đường đọc, không phải bằng một lượt quét lúc khởi động.
    #
    # Tiến trình chết giữa lượt chấm để lại job `running` mà không ai chuyển sang `failed` — người
    # dùng mở lại trang và ngồi đợi một thứ đã không còn chạy. Lượt quét lúc boot giải được ca đó
    # nhưng phải đi XUYÊN tenant, mà `eval.eval_jobs` bật `FORCE ROW LEVEL SECURITY`: chủ bảng cũng
    # bị lọc, và `SET LOCAL row_security = off` KHÔNG vượt được (Postgres trả `InsufficientPrivilege`
    # kèm gợi ý `ALTER TABLE NO FORCE` — đo được, không phải suy luận). Muốn quét xuyên tenant phải
    # nới hàng rào hoặc lặp qua từng tenant, cả hai đều đắt hơn thứ nó mua.
    #
    # Ở đây thì miễn phí: route này đã chạy dưới RLS của đúng tenant sở hữu job, nên `sweep` tự giới
    # hạn đúng phạm vi. Và nó chạy ĐÚNG LÚC có người nhìn — job treo mà không ai tra thì không phiền
    # ai.
    await sweep_stale_jobs(conn, stale_after_seconds=_STALE_EVAL_JOB_SECONDS)

    job = await read_eval_job(conn, job_id)
    if job is None:
        # RLS đã lọc job của tenant khác thành `None`, nên 404 ở đây là MỘT nhánh cho cả "không có"
        # lẫn "của công ty khác" — không rò tin tồn-tại-hay-không qua status code khác nhau.
        raise HTTPException(status_code=404, detail=f"không có lượt chấm nào mang mã {job_id}")

    payload: dict[str, object] = {
        "job_id": str(job.job_id),
        "agent_id": job.agent_id,
        "status": job.status,
        "done": job.done,
        "total": job.total,
        "detail": job.detail,
    }
    if job.status == "done":
        scorecard = await read_pending_scorecard(conn, job.agent_id, job.recipe_hash)
        payload["scorecard"] = None if scorecard is None else scorecard.model_dump(mode="json")
    return payload
