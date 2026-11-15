"""算法控制器：分流校验（算法 A）与指标检验（算法 B）。

两条口径：
- 有每日回传的实验：只能通过「生成分析批次」把分流校验、指标结果、决策依据
  在一份 canonical 快照上成套算出来；单独跑 SRM / 指标检验会 409，防止
  「分流用一边、显著性用另一边」的错配。
- 没有回传批次（手工录入样本的演示/存量实验）：保留原有单独校验入口。
"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import (
    audit_service,
    canonical_service,
    experiment_service,
    metric_service,
    srm_service,
)
from src.utils.errors import AppError


def list_metrics(db: Session, **filters) -> dict:
    return metric_service.list_metrics(db, **filters)


def srm_snapshot(db: Session, experiment_id: int) -> dict:
    experiment = experiment_service.get_experiment(db, experiment_id)
    return srm_service.snapshot(experiment)


def _require_manual_mode(db: Session, experiment) -> None:
    if canonical_service.has_returns(db, experiment.id):
        raise AppError(
            "CONFLICT",
            409,
            {
                "reason": "该实验已有每日回传批次，分流校验与指标检验必须通过分析批次成套生成，不能单独跑",
                "action": "POST /api/experiments/{id}/analysis-runs",
            },
        )


def run_srm(db: Session, experiment_id: int, payload: dict, principal: dict, request: Request) -> dict:
    experiment = experiment_service.get_experiment(db, experiment_id)
    _require_manual_mode(db, experiment)
    result = srm_service.check(
        db,
        experiment,
        alpha=payload.get("alpha"),
        power=payload.get("power"),
        alarm_p=payload.get("alarm_p"),
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="srm.check",
        entity="experiment",
        entity_id=experiment.id,
        detail={
            "chi2": result["chi2"],
            "p_value": result["p_value"],
            "alarm": result["alarm"],
            "required_per_variant": result["required_per_variant"],
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return result


def run_metric_tests(db: Session, experiment_id: int, payload: dict, principal: dict, request: Request) -> dict:
    experiment = experiment_service.get_experiment(db, experiment_id)
    _require_manual_mode(db, experiment)
    result = metric_service.run_checks(
        db,
        experiment,
        method=payload.get("method") or "bh",
        alpha=payload.get("alpha"),
        variant_code=payload.get("variant_code"),
        operator_email=principal["email"],
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="metric.check",
        entity="experiment",
        entity_id=experiment.id,
        detail={
            "method": result["method"],
            "total": result["total"],
            "significant": sum(item["significant_count"] for item in result["results"]),
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return result
