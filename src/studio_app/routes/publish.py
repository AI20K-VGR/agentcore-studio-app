"""`POST /api/agents/{agent_id}/publish` (Kế hoạch 2, A4) — chạy đủ `golden_set_ref` qua
`EvalHarness.run()` (evalhub, đã implement xong) ra `Scorecard`, rồi gọi `publish()`
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

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError
from studio_contracts import Edge, Node, Recipe, Scorecard
from studio_engine.agent_loop import AgentLoopExhausted
from studio_evalhub.golden_case import GoldenSet
from studio_evalhub.golden_store import GoldenSetNotFound, GoldenSetScopeError, read_golden_set
from studio_evalhub.harness import EvalHarness
from studio_evalhub.judge import LLMJudge
from studio_kb.doc_factory import TENANT_IDS
from studio_kb.postgres import PgKbSearch
from studio_workbench import create_recipe
from studio_workbench.publish import publish, recipe_hash
from studio_workbench.tenant_wall import ResolvedContext
from studio_workbench.validator import graph_lint

from studio_app.authz import fetch_fresh_identity, require_admin
from studio_app.core._db import get_pool
from studio_app.eval_adapter import EngineAgentRunner
from studio_app.middleware import get_request_connection, get_request_session
from studio_app.obs.trace_writer import PgTraceWriter
from studio_app.providers.factory import build_embedding, build_llm
from studio_app.settings import get_settings

router = APIRouter(prefix="/api/agents", tags=["publish"])


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
    instructions: str
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


async def _evaluate(agent_id: str, body: PublishRequest, session: ResolvedContext) -> tuple[Recipe, Scorecard]:
    """Dựng recipe từ canvas + chạy NGUYÊN golden_set_ref qua `EvalHarness.run()` thật — dùng chung
    cho cả `/evaluate` (chỉ xem điểm, KHÔNG ghi DB) lẫn `/publish` (chấm rồi ghi DB nếu đạt). Mỗi
    lần gọi route nào cũng chấm LẠI TỪ ĐẦU — route `/publish` không tin bất kỳ Scorecard nào UI đã
    thấy trước đó qua `/evaluate` (tag vs fence: UI chỉ gợi ý nút sáng/tắt, server luôn tự verify
    lại, không bao giờ nhận thẳng verdict client tự khai)."""
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
            instructions=body.instructions,
            tool_whitelist=body.tool_whitelist,
            nodes=nodes,
            edges=edges,
            temperature=body.temperature,
            golden_set_ref=body.golden_set_ref,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        graph_lint(recipe)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"recipe không qua graph_lint(): {exc}") from exc

    # `session.tenant_id`, KHÔNG phải `recipe.tenant_id` — dù hai giá trị này bằng nhau ở đây
    # (`create_recipe(tenant_id=session.tenant_id)` ngay trên). Đọc từ recipe sẽ là lần đầu tiên
    # trong route này một quyết định phạm vi lấy từ đối tượng do client dựng nên; hôm nay vô hại,
    # nhưng nó dựng sẵn đúng khuôn mà INV-1 tồn tại để cấm. Phiên khai tenant, recipe thì không.
    golden = await _load_golden_set(recipe.golden_set_ref, session.tenant_id)

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
        # `recipe=` ở đây, `certified_recipe()` (eval_adapter.py) tự dựng `create_recipe_d4(...)` —
        # một recipe CỐ ĐỊNH, không liên quan gì tới canvas — làm recipe THẬT SỰ chạy qua từng case
        # golden-set, trong khi `recipe_hash(recipe)` ở dưới băm recipe CANVAS. Hai đối tượng khác
        # nhau về `agent_config`/`dag`/`kb_binding` (đo được: recipe được chấm còn có 1 node
        # `tool-call` admin chưa từng vẽ) — cổng đối chiếu hash ở `publish()` khi đó so hash CANVAS
        # với chính nó, không so được với thứ THẬT SỰ đã chạy, nên luôn khớp một cách vô nghĩa.
        # `graph_lint(recipe)` đã chạy Ở TRÊN, ngay sau khi `create_recipe(...)` dựng xong
        # recipe (neo bằng TÊN lệnh gọi, không phải số dòng — số dòng đã trôi thật ngay trong PR
        # này, review app#26 🟡) — đúng tiền điều kiện `GRAPH-LINT-CONTRACT`
        # mà `run_case` (eval_adapter.py) đòi hỏi cho nhánh `recipe=` được tiêm.
        recipe=recipe,
    )

    tenant_ids: dict[str, UUID] = dict(TENANT_IDS)
    try:
        scorecard = await EvalHarness().run(
            recipe.agent_id,
            recipe.golden_set_ref,
            golden_set=golden,
            runner=runner,
            tenant_ids=tenant_ids,
            threshold_success=recipe.scorecard_threshold.success,
            threshold_citation_accuracy=recipe.scorecard_threshold.citation_accuracy,
            # DEC-03 hoàn thiện tại `studio_workbench.publish.recipe_hash()` — đây là mắt xích
            # DUY NHẤT còn thiếu của đường ống (evalhub đã mở `recipe_hash=` từ D20/`DEC-D20-02`,
            # chỉ chưa có caller nào tính+truyền giá trị thật). Băm ĐÚNG `recipe` vừa graph_lint
            # sạch ở trên — Scorecard trả về giờ chứng nhận đúng recipe SẼ publish, KHÔNG phải một
            # recipe khác được dựng lại sau đó, CHỈ ĐÚNG vì `runner` ở trên được tiêm `recipe=recipe`
            # (review app#26 ⛔ — thiếu dòng đó thì `certified_recipe()` tự dựng recipe khác để
            # chạy, và hash ở đây sẽ chứng nhận nhầm đối tượng dù bản thân phép so ở `publish()`
            # vẫn "khớp" một cách vô nghĩa).
            recipe_hash=recipe_hash(recipe),
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
    """Chấm điểm NGUYÊN golden_set_ref qua `EvalHarness` thật, trả `Scorecard` — KHÔNG gọi
    `publish()`, không ghi `wb.recipes`. Dùng cho UI hiện điểm TRƯỚC khi quyết bấm Publish, để nút
    Publish có căn cứ bật/tắt mà không phải "bấm thử xem có được không"."""
    session = get_request_session()

    # Cùng gate role-gap đã đóng ở `routes/runs.py::create_run` — route này trước bản vá cũng gọi
    # được bởi bất kỳ ai đã đăng nhập, không riêng admin.
    identity = await fetch_fresh_identity(get_request_connection(), session.user)
    require_admin(identity.roles)

    recipe, scorecard = await _evaluate(agent_id, body, session)
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
    require_admin(identity.roles)

    recipe, scorecard = await _evaluate(agent_id, body, session)

    try:
        await publish(recipe, scorecard, conn=get_request_connection())
    except ValueError as exc:
        # `publish()` raise ValueError cho CẢ 4 nhánh chặn: graph_lint nội bộ của nó (đã kiểm ở
        # trên nên khó trúng lại), `recipe_hash is None`, `recipe_hash` LỆCH với recipe đang publish
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
