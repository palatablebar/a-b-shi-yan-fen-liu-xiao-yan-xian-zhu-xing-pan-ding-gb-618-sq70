"""分流样本服务：按 (实验, 变体, 渠道, 指标, 日期) 登记与聚合。"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import CHANNEL_LABELS, CHANNEL_ORDER, METRIC_LABELS, RETURN_SOURCE_SHORT
from src.models.assignment import Assignment
from src.models.experiment import Experiment
from src.models.variant import Variant
from src.utils.errors import AppError
from src.utils.timeutil import iso_date, iso_utc


def serialize(row: Assignment, variant: Variant | None = None) -> dict:
    return {
        "id": row.id,
        "experiment_id": row.experiment_id,
        "variant_id": row.variant_id,
        "variant_code": variant.code if variant else "",
        "variant_name": variant.name if variant else "",
        "channel": row.channel,
        "channel_label": CHANNEL_LABELS.get(row.channel, row.channel),
        "metric_key": row.metric_key,
        "metric_label": METRIC_LABELS.get(row.metric_key, row.metric_key),
        "stat_date": iso_date(row.stat_date),
        "samples": row.samples,
        "conversions": row.conversions,
        "rate": round(row.conversions / row.samples, 6) if row.samples else 0.0,
        "operator_email": row.operator_email,
        "updated_at": iso_utc(row.updated_at),
    }


def list_assignments(
    db: Session,
    experiment_id: int,
    *,
    variant_code: str | None = None,
    channel: str | None = None,
    metric_key: str | None = None,
    limit: int = 60,
    offset: int = 0,
) -> dict:
    experiment = db.get(Experiment, experiment_id)
    if experiment is not None:
        from src.services import canonical_service

        if canonical_service.has_returns(db, experiment_id):
            rows = canonical_service.rows(
                db,
                experiment,
                metric_key=metric_key,
                variant_code=variant_code,
                channel=channel,
            )
            rows.sort(key=lambda row: (row["stat_date"], row["channel"], row["metric_key"]), reverse=True)
            total = len(rows)
            variants = {
                item.code: item
                for item in db.scalars(select(Variant).where(Variant.experiment_id == experiment_id)).all()
            }
            page = rows[offset : offset + limit]
            return {
                "items": [
                    {
                        "id": row["id"],
                        "experiment_id": row["experiment_id"],
                        "variant_id": row["variant_id"],
                        "variant_code": row["variant_code"],
                        "variant_name": row["variant_name"],
                        "channel": row["channel"],
                        "channel_label": CHANNEL_LABELS.get(row["channel"], row["channel"]),
                        "metric_key": row["metric_key"],
                        "metric_label": METRIC_LABELS.get(row["metric_key"], row["metric_key"]),
                        "stat_date": iso_date(row["stat_date"]),
                        "samples": row["samples"],
                        "conversions": row["conversions"],
                        "rate": round(row["conversions"] / row["samples"], 6) if row["samples"] else 0.0,
                        "operator_email": RETURN_SOURCE_SHORT.get(row.get("source") or "", "对账采用"),
                        "source": row.get("source"),
                        "origin": "reconciliation",
                        "updated_at": None,
                    }
                    for row in page
                ],
                "total": total,
                "limit": limit,
                "offset": offset,
                "origin": "reconciliation",
            }
    stmt = select(Assignment).where(Assignment.experiment_id == experiment_id)
    count_stmt = select(func.count()).select_from(Assignment).where(Assignment.experiment_id == experiment_id)
    if variant_code:
        variant = db.scalar(select(Variant).where(Variant.experiment_id == experiment_id, Variant.code == variant_code))
        variant_id = variant.id if variant else -1
        stmt = stmt.where(Assignment.variant_id == variant_id)
        count_stmt = count_stmt.where(Assignment.variant_id == variant_id)
    if channel:
        stmt = stmt.where(Assignment.channel == channel)
        count_stmt = count_stmt.where(Assignment.channel == channel)
    if metric_key:
        stmt = stmt.where(Assignment.metric_key == metric_key)
        count_stmt = count_stmt.where(Assignment.metric_key == metric_key)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(
        stmt.order_by(Assignment.stat_date.desc(), Assignment.channel, Assignment.metric_key)
        .limit(limit)
        .offset(offset)
    ).all()
    variants = {item.id: item for item in db.scalars(select(Variant).where(Variant.experiment_id == experiment_id)).all()}
    return {
        "items": [serialize(row, variants.get(row.variant_id)) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "origin": "assignment",
    }


def variant_totals(db: Session, experiment: Experiment, metric_key: str | None = None) -> list[dict]:
    """按变体聚合分流样本（SRM 的输入）。

    有每日回传的实验只认对账后 canonical 的值；没有回传时回退手工录入。
    """
    from src.services import canonical_service

    metric = metric_key or experiment.primary_metric
    variants = db.scalars(
        select(Variant).where(Variant.experiment_id == experiment.id).order_by(Variant.sort_order, Variant.id)
    ).all()
    totals = canonical_service.variant_metric_totals(db, experiment, metric)
    grand_total = sum(bucket["samples"] for bucket in totals.values())
    return [
        {
            "variant_id": item.id,
            "code": item.code,
            "name": item.name,
            "ratio": item.ratio,
            "is_control": item.is_control,
            "samples": totals.get(item.code, {}).get("samples", 0),
            "conversions": totals.get(item.code, {}).get("conversions", 0),
            "rate": round(totals[item.code]["conversions"] / totals[item.code]["samples"], 6)
            if totals.get(item.code, {}).get("samples")
            else 0.0,
            "share_pct": round(totals.get(item.code, {}).get("samples", 0) / grand_total * 100, 4)
            if grand_total
            else 0.0,
        }
        for item in variants
    ]


def metric_keys(db: Session, experiment_id: int) -> list[str]:
    experiment = db.get(Experiment, experiment_id)
    if experiment is not None:
        from src.services import canonical_service

        keys = sorted({row["metric_key"] for row in canonical_service.rows(db, experiment)})
        if keys:
            return keys
    rows = db.execute(
        select(Assignment.metric_key).where(Assignment.experiment_id == experiment_id).group_by(Assignment.metric_key)
    ).all()
    return [row[0] for row in rows]


def channel_matrix(db: Session, experiment: Experiment, metric_key: str | None = None) -> dict:
    """渠道 × 变体样本矩阵（页面「各变体样本对照」表），取对账后可用值。"""
    from src.services import canonical_service

    metric = metric_key or experiment.primary_metric
    variants = db.scalars(
        select(Variant).where(Variant.experiment_id == experiment.id).order_by(Variant.sort_order, Variant.id)
    ).all()
    matrix = {channel: {variant.code: {"samples": 0, "conversions": 0} for variant in variants} for channel in CHANNEL_ORDER}
    for row in canonical_service.rows(db, experiment, metric_key=metric):
        bucket = matrix.setdefault(row["channel"], {}).setdefault(row["variant_code"], {"samples": 0, "conversions": 0})
        bucket["samples"] += int(row["samples"])
        bucket["conversions"] += int(row["conversions"])
    return {
        "metric_key": metric,
        "metric_label": METRIC_LABELS.get(metric, metric),
        "variants": [{"code": item.code, "name": item.name, "is_control": item.is_control} for item in variants],
        "channels": [
            {"key": channel, "label": CHANNEL_LABELS.get(channel, channel), "cells": matrix.get(channel, {})}
            for channel in CHANNEL_ORDER
        ],
    }


def daily_series(db: Session, experiment: Experiment, metric_key: str | None = None) -> list[dict]:
    from src.services import canonical_service

    metric = metric_key or experiment.primary_metric
    buckets: dict[date, dict[str, int]] = {}
    for row in canonical_service.rows(db, experiment, metric_key=metric):
        bucket = buckets.setdefault(row["stat_date"], {"samples": 0, "conversions": 0})
        bucket["samples"] += int(row["samples"])
        bucket["conversions"] += int(row["conversions"])
    return [
        {"date": iso_date(day), "samples": values["samples"], "conversions": values["conversions"]}
        for day, values in sorted(buckets.items())
    ]


def create_assignment(db: Session, experiment: Experiment, payload: dict, *, operator_email: str) -> Assignment:
    variant = db.scalar(
        select(Variant).where(Variant.experiment_id == experiment.id, Variant.code == payload["variant_code"])
    )
    if variant is None:
        raise AppError("NOT_FOUND", 404, {"entity": "variant", "code": payload["variant_code"]})
    existing = db.scalar(
        select(Assignment).where(
            Assignment.experiment_id == experiment.id,
            Assignment.variant_id == variant.id,
            Assignment.channel == payload["channel"],
            Assignment.metric_key == payload["metric_key"],
            Assignment.stat_date == payload["stat_date"],
        )
    )
    if existing is not None:
        raise AppError(
            "CONFLICT",
            409,
            {
                "reason": "同一 (实验, 变体, 渠道, 指标, 日期) 的分流样本已存在",
                "existing_id": existing.id,
            },
        )
    row = Assignment(
        experiment_id=experiment.id,
        variant_id=variant.id,
        channel=payload["channel"],
        metric_key=payload["metric_key"],
        stat_date=payload["stat_date"],
        samples=int(payload["samples"]),
        conversions=int(payload["conversions"]),
        operator_email=operator_email,
    )
    db.add(row)
    db.flush()
    return row


def upsert_assignment(
    db: Session,
    experiment: Experiment,
    *,
    variant_id: int,
    channel: str,
    metric_key: str,
    stat_date: date,
    samples: int,
    conversions: int,
    operator_email: str,
) -> Assignment:
    """seed 专用：自然键 upsert，不覆盖人工改过的复核/决策状态。"""
    existing = db.scalar(
        select(Assignment).where(
            Assignment.experiment_id == experiment.id,
            Assignment.variant_id == variant_id,
            Assignment.channel == channel,
            Assignment.metric_key == metric_key,
            Assignment.stat_date == stat_date,
        )
    )
    if existing is None:
        existing = Assignment(
            experiment_id=experiment.id,
            variant_id=variant_id,
            channel=channel,
            metric_key=metric_key,
            stat_date=stat_date,
            samples=samples,
            conversions=conversions,
            operator_email=operator_email,
        )
        db.add(existing)
        db.flush()
        return existing
    existing.samples = samples
    existing.conversions = conversions
    db.flush()
    return existing
