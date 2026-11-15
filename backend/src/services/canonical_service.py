"""canonical 数据视图：页面与算法统一从「对账后可用的值」取数。

有回传批次的实验：只认 reconciliation_cells 里 usable=True 的 canonical 值；
没有回传批次的实验：回退到手工录入的 assignments（保持既有演示与单测口径）。
这样页面展示、SRM、指标检验、分析批次四条路径看到的是同一份数，不会
「分流用实验平台、显著性用指标库旧结果」。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models.assignment import Assignment
from src.models.experiment import Experiment
from src.models.reconciliation import ReconciliationCell
from src.models.return_batch import ReturnBatch
from src.models.variant import Variant
from src.services.algorithms import reconcile as reconcile_algorithm


def has_returns(db: Session, experiment_id: int) -> bool:
    return bool(
        db.scalar(
            select(ReturnBatch.id).where(ReturnBatch.experiment_id == experiment_id).limit(1)
        )
    )


def canonical_rows(
    db: Session,
    experiment: Experiment,
    *,
    metric_key: str | None = None,
    variant_code: str | None = None,
    channel: str | None = None,
    stat_date: date | None = None,
) -> list[dict]:
    """返回与 assignments 同构的行，值来自 canonical（齐全/已和解）。"""
    stmt = select(ReconciliationCell).where(
        ReconciliationCell.experiment_id == experiment.id,
        ReconciliationCell.usable.is_(True),
    )
    if metric_key:
        stmt = stmt.where(ReconciliationCell.metric_key == metric_key)
    if variant_code:
        stmt = stmt.where(ReconciliationCell.variant_code == variant_code)
    if channel:
        stmt = stmt.where(ReconciliationCell.channel == channel)
    if stat_date:
        stmt = stmt.where(ReconciliationCell.stat_date == stat_date)
    rows = db.scalars(
        stmt.order_by(ReconciliationCell.stat_date, ReconciliationCell.variant_code, ReconciliationCell.channel, ReconciliationCell.metric_key)
    ).all()
    variants = {
        item.code: item
        for item in db.scalars(select(Variant).where(Variant.experiment_id == experiment.id)).all()
    }
    result = []
    for row in rows:
        variant = variants.get(row.variant_code)
        result.append(
            {
                "id": row.id,
                "experiment_id": row.experiment_id,
                "variant_id": variant.id if variant else None,
                "variant_code": row.variant_code,
                "variant_name": variant.name if variant else "",
                "channel": row.channel,
                "metric_key": row.metric_key,
                "stat_date": row.stat_date,
                "samples": int(row.canonical_samples or 0),
                "conversions": int(row.canonical_conversions or 0),
                "operator_email": row.canonical_source or "",
                "source": row.canonical_source,
                "reconcile_status": row.status,
                "origin": "reconciliation",
            }
        )
    return result


def assignment_rows(
    db: Session,
    experiment: Experiment,
    *,
    metric_key: str | None = None,
    variant_code: str | None = None,
    channel: str | None = None,
) -> list[dict]:
    stmt = select(Assignment).where(Assignment.experiment_id == experiment.id)
    if metric_key:
        stmt = stmt.where(Assignment.metric_key == metric_key)
    if channel:
        stmt = stmt.where(Assignment.channel == channel)
    variants = {
        item.id: item
        for item in db.scalars(select(Variant).where(Variant.experiment_id == experiment.id)).all()
    }
    if variant_code:
        target = next((item for item in variants.values() if item.code == variant_code), None)
        stmt = stmt.where(Assignment.variant_id == (target.id if target else -1))
    rows = db.scalars(
        stmt.order_by(Assignment.stat_date, Assignment.channel, Assignment.metric_key, Assignment.variant_id)
    ).all()
    return [
        {
            "id": row.id,
            "experiment_id": row.experiment_id,
            "variant_id": row.variant_id,
            "variant_code": variants[row.variant_id].code if row.variant_id in variants else "",
            "variant_name": variants[row.variant_id].name if row.variant_id in variants else "",
            "channel": row.channel,
            "metric_key": row.metric_key,
            "stat_date": row.stat_date,
            "samples": row.samples,
            "conversions": row.conversions,
            "operator_email": row.operator_email,
            "source": None,
            "reconcile_status": None,
            "origin": "assignment",
        }
        for row in rows
    ]


def rows(db: Session, experiment: Experiment, **filters) -> list[dict]:
    if has_returns(db, experiment.id):
        return canonical_rows(db, experiment, **filters)
    return assignment_rows(db, experiment, **filters)


def variant_metric_totals(db: Session, experiment: Experiment, metric_key: str) -> dict[str, dict[str, int]]:
    """{variant_code: {"samples","conversions"}}，只含 canonical / 手工样本的可用值。"""
    totals: dict[str, dict[str, int]] = {}
    for row in rows(db, experiment, metric_key=metric_key):
        bucket = totals.setdefault(row["variant_code"], {"samples": 0, "conversions": 0})
        bucket["samples"] += int(row["samples"])
        bucket["conversions"] += int(row["conversions"])
    return totals


def snapshot(db: Session, experiment: Experiment) -> dict:
    """当前 canonical 的日期快照，用于分析批次与增量重算。"""
    usable = rows(db, experiment)
    compact = [
        {
            "stat_date": row["stat_date"].isoformat() if hasattr(row["stat_date"], "isoformat") else str(row["stat_date"]),
            "variant_code": row["variant_code"],
            "metric_key": row["metric_key"],
            "channel": row["channel"],
            "samples": row["samples"],
            "conversions": row["conversions"],
        }
        for row in usable
    ]
    built = reconcile_algorithm.build_snapshot(compact)
    return built


def date_fingerprints(snapshot: dict) -> dict[str, str]:
    return {
        day: reconcile_algorithm.snapshot_fingerprint({day: variants})
        for day, variants in sorted((snapshot or {}).items())
    }
