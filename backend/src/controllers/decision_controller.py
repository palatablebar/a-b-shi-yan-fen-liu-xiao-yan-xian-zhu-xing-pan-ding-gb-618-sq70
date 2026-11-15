"""决策控制器：决策必须引用有效分析批次，改判留修订史。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import analysis_service, audit_service, decision_service, experiment_service


def list_decisions(db: Session, **filters) -> dict:
    return decision_service.list_decisions(db, **filters)


def decision_revisions(db: Session, experiment_id: int) -> dict:
    experiment_service.get_experiment(db, experiment_id)
    return {"items": decision_service.revisions(db, experiment_id)}


def create_decision(db: Session, experiment_id: int, payload: dict, principal: dict, request: Request) -> dict:
    experiment = experiment_service.get_experiment(db, experiment_id)
    run = analysis_service.latest_run(db, experiment.id, status="ready")
    decision = decision_service.create(
        db,
        experiment,
        payload,
        decided_by=principal["email"],
        analysis_run=run,
    )
    experiment.status = "finished"
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="decision.create",
        entity="decision",
        entity_id=decision.id,
        detail={
            "experiment": experiment.code,
            "verdict": decision.verdict,
            "revision": decision.revision,
            "analysis_run_no": decision.analysis_run_no,
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return decision_service.serialize(decision, experiment)
