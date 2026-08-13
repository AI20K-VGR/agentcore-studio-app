"""`POST /api/agents/{agent_id}/publish` (Kế hoạch 2, A4) — chạy đủ `golden_set_ref` qua
`EvalHarness.run()` (evalhub, đã implement xong) ra `Scorecard`, rồi gọi `publish()`
(`studio_workbench.publish`, đã implement xong PR#22/D18) để gate + ghi `wb.recipes`.

`golden_set_path`: theo đúng `DEC-D16-01` (evalhub/docs/decisions/scorecard.md) —
"`src/studio_evalhub/` tuyệt đối không mang hằng số `packages/kb/...`... composition root
(CLI/`apps/studio`/fixture) là chỗ DUY NHẤT biết golden-30 nằm ở đâu" — nên hằng số đường dẫn
NẰM Ở ĐÂY, đúng chỗ được chỉ định, không phải một vi phạm layering.

**Sẽ LUÔN trả 409** cho tới khi AIE-2 xong `recipe_hash` producer (DEC-03): `compute_scorecard()`
(evalhub) hiện không có tham số nào để set `recipe_hash`, luôn `None` — `publish()` fail-closed
trên đúng field đó TRƯỚC CẢ khi đọc `gate.verdict`. Đây là hành vi ĐÚNG, không phải bug của route
này — xem `docs/reports`/Kế hoạch 2 Phần 4 (rủi ro team-wide) để biết ai đang chặn việc này.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import studio_kb
from fastapi import APIRouter, HTTPException
from pydantic import ValidationError
from studio_contracts import Edge, Node
from studio_evalhub.harness import EvalHarness
from studio_kb.doc_factory import TENANT_IDS
from studio_kb.postgres import PgKbSearch
from studio_workbench.builder import create_dynamic_recipe
from studio_workbench.publish import publish
from studio_workbench.validator import graph_lint

from studio_app.core._db import get_pool
from studio_app.eval_adapter import EngineAgentRunner
from studio_app.middleware import get_request_connection, get_request_session
from studio_app.obs.trace_writer import PgTraceWriter
from studio_app.providers.factory import CallistoEmbedding, build_llm
from studio_app.routes.runs import RunRequest

router = APIRouter(prefix="/api/agents", tags=["publish"])

# DEC-D16-01: composition root là nơi DUY NHẤT được phép biết golden-set nằm ở đâu trên đĩa.
_GOLDEN_SET_DIR = Path(studio_kb.__file__).resolve().parent.parent.parent / "golden"


@router.post("/{agent_id}/publish")
async def publish_agent(agent_id: str, body: RunRequest) -> dict[str, object]:
    session = get_request_session()

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
        recipe = create_dynamic_recipe(
            agent_id=body.agent_id,
            tenant_id=session.tenant_id,
            instructions=body.instructions,
            model=body.model,
            tool_whitelist=body.tool_whitelist,
            nodes=nodes,
            edges=edges,
            kb_id=body.kb_id,
            scope=body.scope,
            golden_set_ref=body.golden_set_ref,
            success_threshold=body.success_threshold,
            citation_accuracy_threshold=body.citation_accuracy_threshold,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        graph_lint(recipe)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"recipe không qua graph_lint(): {exc}") from exc

    golden_set_path = _GOLDEN_SET_DIR / f"{recipe.golden_set_ref}.yaml"
    if not golden_set_path.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"golden_set_ref {recipe.golden_set_ref!r} không có file tương ứng ở {golden_set_path}",
        )

    pool = await get_pool()
    embedding = CallistoEmbedding()
    runner = EngineAgentRunner(
        kb_search=PgKbSearch(pool, embedding),
        llm=build_llm(),
        embedding=embedding,
        trace_writer=PgTraceWriter(pool),
    )

    tenant_ids: dict[str, UUID] = dict(TENANT_IDS)
    scorecard = await EvalHarness().run(
        recipe.agent_id,
        recipe.golden_set_ref,
        golden_set_path=golden_set_path,
        runner=runner,
        tenant_ids=tenant_ids,
        threshold_success=recipe.scorecard_threshold.success,
        threshold_citation_accuracy=recipe.scorecard_threshold.citation_accuracy,
    )

    try:
        await publish(recipe, scorecard, conn=get_request_connection())
    except ValueError as exc:
        # `publish()` raise ValueError cho CẢ 2 nhánh chặn (graph_lint nội bộ của nó — đã kiểm ở
        # trên nên khó trúng lại — và gate.verdict='FAIL'/recipe_hash=None). 409, không 400: đây
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
