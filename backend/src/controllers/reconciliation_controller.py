"""回传、对账与分析批次控制器：写操作记领域审计。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import (
    analysis_service,
    audit_service,
    experiment_service,
    reconciliation_service,
    return_service,
)


def _invalidate(db: Session, experiment_id: int, days: list, reason: str) -> int:
    return analysis_service.invalidate(db, experiment_id, reason=reason, only_days=days)


def list_batches(db: Session, experiment_id: int, source: str | None = None) -> dict:
    experiment_service.get_experiment(db, experiment_id)
    return return_service.list_batches(db, experiment_id, source=source)


def ingest_batch(db: Session, experiment_id: int, payload: dict, principal: dict, request: Request) -> dict:
    experiment = experiment_service.get_experiment(db, experiment_id)
    outcome = return_service.ingest(
        db,
        experiment,
        payload,
        received_by=principal["email"],
        invalidate=lambda day, reason: _invalidate(db, experiment.id, [day], reason),
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="return.ingest",
        entity="return_batch",
        entity_id=outcome["batch"]["id"],
        detail={
            "experiment": experiment.code,
            "source": payload["source"],
            "stat_date": outcome["affected_date"],
            "batch_no": payload["batch_no"],
            "result": outcome["result"],
            "rows": outcome["batch"]["rows_count"],
            "changed": outcome["changed"],
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return outcome


def reconcile(db: Session, experiment_id: int, payload: dict, principal: dict, request: Request) -> dict:
    experiment = experiment_service.get_experiment(db, experiment_id)
    result = reconciliation_service.reconcile_all(
        db,
        experiment,
        date_from=payload.get("date_from"),
        date_to=payload.get("date_to"),
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="reconcile.run",
        entity="experiment",
        entity_id=experiment.id,
        detail={
            "experiment": experiment.code,
            "dates_total": result["dates_total"],
            "ready": result["ready"],
            "conflict_dates": result["conflict_dates"],
            "delayed_dates": result["delayed_dates"],
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return result


def summary(db: Session, experiment_id: int, date_from=None, date_to=None) -> dict:
    experiment = experiment_service.get_experiment(db, experiment_id)
    # 读取也重算一次，保证页面看到的逐格状态与当前 active 批次一致
    reconciliation_service.reconcile_all(db, experiment, date_from=date_from, date_to=date_to)
    db.commit()
    return reconciliation_service.summarize(db, experiment, date_from=date_from, date_to=date_to)


def cells(db: Session, experiment_id: int, **filters) -> dict:
    experiment = experiment_service.get_experiment(db, experiment_id)
    return reconciliation_service.list_cells(db, experiment, **filters)


def explain(db: Session, experiment_id: int, cell_id: int, payload: dict, principal: dict, request: Request) -> dict:
    experiment = experiment_service.get_experiment(db, experiment_id)
    result = reconciliation_service.explain(
        db,
        experiment,
        cell_id,
        payload,
        explained_by=principal["email"],
        invalidate=lambda day, reason: _invalidate(db, experiment.id, [day], reason),
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="reconcile.explain",
        entity="reconciliation_cell",
        entity_id=cell_id,
        detail={
            "experiment": experiment.code,
            "stat_date": result["cell"]["stat_date"],
            "resolution": payload["resolution"],
            "explanation": payload["explanation"],
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return result


def run_analysis(db: Session, experiment_id: int, payload: dict, principal: dict, request: Request) -> dict:
    experiment = experiment_service.get_experiment(db, experiment_id)
    result = analysis_service.compute(
        db,
        experiment,
        method=payload.get("method") or "bh",
        alpha=payload.get("alpha"),
        power=payload.get("power"),
        date_from=payload.get("date_from"),
        date_to=payload.get("date_to"),
        operator_email=principal["email"],
        reason="手动生成分析批次",
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="analysis.run",
        entity="analysis_run",
        entity_id=result["id"],
        detail={
            "experiment": experiment.code,
            "run_no": result["run_no"],
            "dates_used": len(result["dates_used"]),
            "recomputed_dates": result["recomputed_dates"],
            "reused": result.get("reused", False),
            "batch_total": result["sources"].get("total_batches"),
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return result


def list_runs(db: Session, experiment_id: int | None, status: str | None, limit: int, offset: int) -> dict:
    return analysis_service.list_runs(db, experiment_id, status=status, limit=limit, offset=offset)


def latest_run(db: Session, experiment_id: int) -> dict:
    experiment_service.get_experiment(db, experiment_id)
    row = analysis_service.latest_run(db, experiment_id)
    return {"item": analysis_service.serialize_run(row) if row else None}
