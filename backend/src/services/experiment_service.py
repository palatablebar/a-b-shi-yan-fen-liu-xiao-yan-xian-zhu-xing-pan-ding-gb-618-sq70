"""实验登记服务：实验 + 变体的读写与序列化。"""
from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    CORRECTION_LABELS,
    EXPERIMENT_STATUS_LABELS,
    METRIC_LABELS,
    SRM_STATUS_LABELS,
    VERDICT_LABELS,
)
from src.models.assignment import Assignment
from src.models.decision import Decision
from src.models.experiment import Experiment
from src.models.metric_result import MetricResult
from src.models.variant import Variant
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_date, iso_utc


def serialize_variant(variant: Variant) -> dict:
    return {
        "id": variant.id,
        "code": variant.code,
        "name": variant.name,
        "ratio": variant.ratio,
        "ratio_pct": round(variant.ratio * 100, 2),
        "is_control": variant.is_control,
        "note": variant.note,
    }


def serialize(experiment: Experiment, *, variants: list[Variant] | None = None) -> dict:
    return {
        "id": experiment.id,
        "code": experiment.code,
        "name": experiment.name,
        "hypothesis": experiment.hypothesis,
        "owner": experiment.owner,
        "primary_metric": experiment.primary_metric,
        "primary_metric_label": METRIC_LABELS.get(experiment.primary_metric, experiment.primary_metric),
        "status": experiment.status,
        "status_label": EXPERIMENT_STATUS_LABELS.get(experiment.status, experiment.status),
        "baseline_rate": experiment.baseline_rate,
        "planned_mde": experiment.planned_mde,
        "alpha": experiment.alpha,
        "power_target": experiment.power_target,
        "traffic_note": experiment.traffic_note,
        "started_on": iso_date(experiment.started_on),
        "ended_on": iso_date(experiment.ended_on),
        "srm": {
            "status": experiment.srm_status,
            "status_label": SRM_STATUS_LABELS.get(experiment.srm_status, experiment.srm_status),
            "chi2": experiment.srm_chi2,
            "df": experiment.srm_df,
            "p_value": experiment.srm_p_value,
            "checked_at": iso_utc(experiment.srm_checked_at),
            "alarm": experiment.has_srm_alarm,
            "required_per_variant": experiment.required_per_variant,
            "power_at_current": experiment.power_at_current,
            "mde_at_current": experiment.mde_at_current,
            "payload": experiment.srm_payload or {},
        },
        "variants": [serialize_variant(item) for item in (variants or [])],
        "created_at": iso_utc(experiment.created_at),
        "updated_at": iso_utc(experiment.updated_at),
    }


def _latest_analysis_map(db: Session, ids: list[int]) -> dict[int, dict]:
    from src.models.analysis_run import AnalysisRun

    if not ids:
        return {}
    rows = db.scalars(
        select(AnalysisRun).where(AnalysisRun.experiment_id.in_(ids)).order_by(AnalysisRun.run_no)
    ).all()
    latest: dict[int, dict] = {}
    for row in rows:
        current = latest.get(row.experiment_id)
        if current is None or row.run_no >= current["latest_run_no"]:
            latest[row.experiment_id] = {
                "latest_run_no": row.run_no,
                "status": row.status,
                "computed_at": iso_utc(row.computed_at),
                "date_from": row.date_from,
                "date_to": row.date_to,
            }
    return latest


def list_experiments(
    db: Session,
    *,
    q: str | None = None,
    status: str | None = None,
    primary_metric: str | None = None,
    srm_alarm: bool | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    stmt = select(Experiment)
    count_stmt = select(func.count()).select_from(Experiment)
    if q:
        like = f"%{q.strip()}%"
        clause = or_(Experiment.name.ilike(like), Experiment.code.ilike(like), Experiment.hypothesis.ilike(like))
        stmt = stmt.where(clause)
        count_stmt = count_stmt.where(clause)
    if status:
        stmt = stmt.where(Experiment.status == status)
        count_stmt = count_stmt.where(Experiment.status == status)
    if primary_metric:
        stmt = stmt.where(Experiment.primary_metric == primary_metric)
        count_stmt = count_stmt.where(Experiment.primary_metric == primary_metric)
    if srm_alarm is not None:
        stmt = stmt.where(Experiment.has_srm_alarm.is_(srm_alarm))
        count_stmt = count_stmt.where(Experiment.has_srm_alarm.is_(srm_alarm))
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(Experiment.id).limit(limit).offset(offset)).all()
    variant_map = _variants_by_experiment(db, [row.id for row in rows])
    analysis_map = _latest_analysis_map(db, [row.id for row in rows])
    items = []
    for row in rows:
        item = serialize(row, variants=variant_map.get(row.id, []))
        item["analysis"] = analysis_map.get(
            row.id,
            {"latest_run_no": None, "status": "none", "computed_at": None, "date_from": None, "date_to": None},
        )
        items.append(item)
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _variants_by_experiment(db: Session, ids: list[int]) -> dict[int, list[Variant]]:
    if not ids:
        return {}
    rows = db.scalars(select(Variant).where(Variant.experiment_id.in_(ids)).order_by(Variant.sort_order, Variant.id)).all()
    grouped: dict[int, list[Variant]] = {}
    for row in rows:
        grouped.setdefault(row.experiment_id, []).append(row)
    return grouped


def get_experiment(db: Session, experiment_id: int) -> Experiment:
    experiment = db.get(Experiment, experiment_id)
    if experiment is None:
        raise not_found("experiment", experiment_id)
    return experiment


def get_variants(db: Session, experiment_id: int) -> list[Variant]:
    return list(
        db.scalars(
            select(Variant).where(Variant.experiment_id == experiment_id).order_by(Variant.sort_order, Variant.id)
        ).all()
    )


def detail(db: Session, experiment_id: int) -> dict:
    experiment = get_experiment(db, experiment_id)
    variants = get_variants(db, experiment_id)
    decision = db.scalar(select(Decision).where(Decision.experiment_id == experiment_id))
    metrics = db.scalars(
        select(MetricResult).where(MetricResult.experiment_id == experiment_id).order_by(MetricResult.metric_key)
    ).all()
    payload = serialize(experiment, variants=variants)
    # 样本合计只统计主指标口径，同时给出 canonical 来源说明
    from src.services import analysis_service, canonical_service

    if canonical_service.has_returns(db, experiment.id):
        totals = canonical_service.variant_metric_totals(db, experiment, experiment.primary_metric)
        payload["sample_total"] = sum(bucket["samples"] for bucket in totals.values())
        payload["assignment_rows"] = sum(1 for row in canonical_service.canonical_rows(db, experiment))
        payload["sample_origin"] = "reconciliation"
    else:
        sample_rows = db.execute(
            select(func.sum(Assignment.samples), func.sum(Assignment.conversions), func.count())
            .where(Assignment.experiment_id == experiment_id, Assignment.metric_key == experiment.primary_metric)
        ).one()
        payload["sample_total"] = int(sample_rows[0] or 0)
        payload["assignment_rows"] = int(sample_rows[2] or 0)
        payload["sample_origin"] = "assignment"
    latest = analysis_service.latest_run(db, experiment.id)
    payload["analysis"] = {
        "latest_run_id": latest.id if latest else None,
        "latest_run_no": latest.run_no if latest else None,
        "status": latest.status if latest else "none",
        "computed_at": latest.computed_at.isoformat(timespec="seconds") + "Z" if latest and latest.computed_at else None,
        "date_from": latest.date_from if latest else None,
        "date_to": latest.date_to if latest else None,
        "dates_used": latest.dates_used if latest else [],
        "decision_basis": latest.decision_basis if latest else {},
    }
    payload["metrics"] = [
        {
            "metric_key": row.metric_key,
            "metric_name": row.metric_name,
            "variant_code": row.variant_code,
            "p_value": row.p_value,
            "q_value": row.q_value,
            "lift": row.lift,
            "significant": row.significant,
            "verdict": row.verdict,
            "verdict_label": VERDICT_LABELS.get(row.verdict, row.verdict),
            "method": row.method,
            "method_label": CORRECTION_LABELS.get(row.method, row.method),
            "is_primary": row.is_primary,
        }
        for row in metrics
    ]
    payload["decision"] = (
        {
            "id": decision.id,
            "verdict": decision.verdict,
            "verdict_label": VERDICT_LABELS.get(decision.verdict, decision.verdict),
            "rationale": decision.rationale,
            "decided_by": decision.decided_by,
            "decided_at": iso_utc(decision.decided_at),
            "analysis_run_no": decision.analysis_run_no,
            "revision": decision.revision,
            "status": decision.status,
            "stale_reason": decision.stale_reason,
        }
        if decision
        else None
    )
    return payload


def create_experiment(db: Session, payload: dict, *, owner_default: str) -> Experiment:
    exists = db.scalar(select(Experiment).where(Experiment.code == payload["code"]))
    if exists is not None:
        raise AppError("CONFLICT", 409, {"code": payload["code"], "entity": "experiment"})
    experiment = Experiment(
        code=payload["code"],
        name=payload["name"],
        hypothesis=payload.get("hypothesis") or "",
        owner=payload.get("owner") or owner_default,
        primary_metric=payload.get("primary_metric") or "order_rate",
        status=payload.get("status") or "running",
        baseline_rate=float(payload.get("baseline_rate") or 0.1),
        planned_mde=float(payload.get("planned_mde") or 0.01),
        alpha=float(payload.get("alpha") or 0.05),
        power_target=float(payload.get("power_target") or 0.8),
        traffic_note=payload.get("traffic_note"),
        started_on=payload.get("started_on"),
        ended_on=payload.get("ended_on"),
    )
    db.add(experiment)
    db.flush()
    for index, item in enumerate(payload.get("variants") or []):
        db.add(
            Variant(
                experiment_id=experiment.id,
                code=item["code"],
                name=item["name"],
                ratio=float(item["ratio"]),
                is_control=bool(item.get("is_control")),
                note=item.get("note"),
                sort_order=index,
            )
        )
    db.flush()
    return experiment


def update_experiment(db: Session, experiment: Experiment, payload: dict) -> Experiment:
    for field in ("name", "hypothesis", "owner", "status", "baseline_rate", "planned_mde", "alpha", "power_target", "traffic_note"):
        if field in payload and payload[field] is not None:
            setattr(experiment, field, payload[field])
    db.flush()
    return experiment
