"""回传 / 对账 / 分析批次路由。

角色口径沿用现有：写操作（回传、对账、解释冲突、生成分析）限 admin；
读（批次、逐格、分析批次）三角色可读。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import CHECK_ROLES, READ_ROLES
from src.controllers import reconciliation_controller
from src.middlewares.rbac import require_roles
from src.schemas.return_batch import AnalysisRunIn, ReconcileExplainIn, ReconcileQueryIn, ReturnBatchIn
from src.utils.pagination import page_params

router = APIRouter(prefix="/experiments/{experiment_id}", tags=["reconciliation"])


@router.get("/return-batches")
def list_batches(
    experiment_id: int,
    source: str | None = Query(default=None),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return reconciliation_controller.list_batches(db, experiment_id, source)


@router.post("/return-batches", status_code=201)
def ingest_batch(
    experiment_id: int,
    payload: ReturnBatchIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*CHECK_ROLES)),
) -> dict:
    return reconciliation_controller.ingest_batch(db, experiment_id, payload.model_dump(), principal, request)


@router.get("/reconciliation")
def reconciliation_summary(
    experiment_id: int,
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    from src.utils.timeutil import parse_date

    return reconciliation_controller.summary(db, experiment_id, parse_date(date_from), parse_date(date_to))


@router.post("/reconciliation")
def run_reconciliation(
    experiment_id: int,
    payload: ReconcileQueryIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*CHECK_ROLES)),
) -> dict:
    return reconciliation_controller.reconcile(db, experiment_id, payload.model_dump(), principal, request)


@router.get("/reconciliation/cells")
def reconciliation_cells(
    experiment_id: int,
    status: str | None = None,
    stat_date: str | None = None,
    variant_code: str | None = None,
    channel: str | None = None,
    limit: int = Query(100),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    from src.utils.timeutil import parse_date

    page = page_params(limit, offset)
    return reconciliation_controller.cells(
        db,
        experiment_id,
        status=status,
        stat_date=parse_date(stat_date),
        variant_code=variant_code,
        channel=channel,
        limit=page.limit,
        offset=page.offset,
    )


@router.post("/reconciliation/cells/{cell_id}/explain")
def explain_cell(
    experiment_id: int,
    cell_id: int,
    payload: ReconcileExplainIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*CHECK_ROLES)),
) -> dict:
    return reconciliation_controller.explain(db, experiment_id, cell_id, payload.model_dump(), principal, request)


@router.get("/analysis-runs")
def list_runs(
    experiment_id: int,
    status: str | None = None,
    limit: int = Query(30),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return reconciliation_controller.list_runs(db, experiment_id, status, page.limit, page.offset)


@router.post("/analysis-runs", status_code=201)
def run_analysis(
    experiment_id: int,
    payload: AnalysisRunIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*CHECK_ROLES)),
) -> dict:
    return reconciliation_controller.run_analysis(db, experiment_id, payload.model_dump(), principal, request)


@router.get("/analysis-runs/latest")
def latest_run(
    experiment_id: int,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return reconciliation_controller.latest_run(db, experiment_id)
