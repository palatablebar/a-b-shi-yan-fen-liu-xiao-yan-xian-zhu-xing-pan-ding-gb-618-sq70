"""对账服务：按实验、变体、渠道、统计日核对两份回传。

职责：
- reconcile_date：对某一天的 active 批次做逐格对账，落 reconciliation_cells。
- summarize：把某区间的对账结果按日/按来源/按状态汇总，供页面与导出。
- explain：给冲突格填解释并指定采用侧，转 reconciled；触发受影响日期的重算。
- dataset：只取「齐全 + 已和解」的 canonical 值喂给分析，绝不拿单边数据凑合。
"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    CHANNEL_LABELS,
    METRIC_LABELS,
    RECONCILE_RESOLUTION_LABELS,
    RECONCILE_STATUS_LABELS,
    RETURN_SOURCE_LABELS,
    ReconcileStatus,
    ReturnSource,
)
from src.models.experiment import Experiment
from src.models.reconciliation import ReconciliationCell
from src.models.return_batch import ReturnBatch
from src.models.variant import Variant
from src.services import return_service
from src.services.algorithms import reconcile as reconcile_algorithm
from src.utils.errors import AppError
from src.utils.timeutil import iso_date, iso_utc, utcnow


def _serialize_cell(row: ReconciliationCell, variant: Variant | None = None) -> dict:
    return {
        "id": row.id,
        "experiment_id": row.experiment_id,
        "stat_date": iso_date(row.stat_date),
        "variant_code": row.variant_code,
        "variant_name": variant.name if variant else "",
        "channel": row.channel,
        "channel_label": CHANNEL_LABELS.get(row.channel, row.channel),
        "metric_key": row.metric_key,
        "metric_label": METRIC_LABELS.get(row.metric_key, row.metric_key),
        "assignment_batch_id": row.assignment_batch_id,
        "metric_batch_id": row.metric_batch_id,
        "assignment_samples": row.assignment_samples,
        "assignment_conversions": row.assignment_conversions,
        "metric_samples": row.metric_samples,
        "metric_conversions": row.metric_conversions,
        "sample_delta": row.sample_delta,
        "conversion_delta": row.conversion_delta,
        "status": row.status,
        "status_label": RECONCILE_STATUS_LABELS.get(row.status, row.status),
        "reason": row.reason,
        "reason_label": reconcile_algorithm.REASON_LABELS.get(row.reason, row.reason or ""),
        "resolution": row.resolution,
        "resolution_label": RECONCILE_RESOLUTION_LABELS.get(row.resolution, ""),
        "explanation": row.explanation,
        "explained_by": row.explained_by,
        "explained_at": iso_utc(row.explained_at),
        "canonical_source": row.canonical_source,
        "canonical_samples": row.canonical_samples,
        "canonical_conversions": row.canonical_conversions,
        "usable": row.usable,
        "auto_explainable": row.auto_explainable,
        "updated_at": iso_utc(row.updated_at),
    }


def _date_batch_map(db: Session, experiment_id: int) -> dict[date, dict[str, ReturnBatch | None]]:
    mapping: dict[date, dict[str, ReturnBatch | None]] = {}
    for row in return_service.active_batches(db, experiment_id):
        mapping.setdefault(row.stat_date, {})[row.source] = row
    return mapping


def _rows(db: Session, batch: ReturnBatch | None) -> list[dict]:
    return return_service.rows_of_batch(db, batch.id) if batch is not None else []


def _sorted_days(db: Session, experiment_id: int, date_from: date | None, date_to: date | None) -> list[date]:
    days = sorted(_date_batch_map(db, experiment_id))
    if date_from:
        days = [day for day in days if day >= date_from]
    if date_to:
        days = [day for day in days if day <= date_to]
    return days


def reconcile_date(db: Session, experiment: Experiment, stat_date: date) -> dict:
    """对一天做对账并 upsert 到 reconciliation_cells；返回当天汇总。"""
    batches = return_service.batches_for_date(db, experiment.id, stat_date)
    assignment_batch = batches.get(ReturnSource.ASSIGNMENT.value)
    metric_batch = batches.get(ReturnSource.METRIC.value)
    existing = {
        (row.variant_code, row.channel, row.metric_key): row
        for row in db.scalars(
            select(ReconciliationCell).where(
                ReconciliationCell.experiment_id == experiment.id,
                ReconciliationCell.stat_date == stat_date,
            )
        ).all()
    }
    explanations = []
    for key, row in existing.items():
        if row.explanation:
            explanations.append(
                {
                    "variant_code": key[0],
                    "channel": key[1],
                    "metric_key": key[2],
                    "explanation": row.explanation,
                    "resolution": row.resolution,
                    "explained_by": row.explained_by,
                    "explained_at": row.explained_at,
                }
            )
    outcome = reconcile_algorithm.reconcile_cells(
        _rows(db, assignment_batch),
        _rows(db, metric_batch),
        explanations=explanations,
    )
    seen = set()
    for cell in outcome["cells"]:
        key = (cell["variant_code"], cell["channel"], cell["metric_key"])
        seen.add(key)
        row = existing.get(key)
        if row is None:
            row = ReconciliationCell(
                experiment_id=experiment.id,
                stat_date=stat_date,
                variant_code=key[0],
                channel=key[1],
                metric_key=key[2],
            )
            db.add(row)
        row.assignment_batch_id = assignment_batch.id if assignment_batch else None
        row.metric_batch_id = metric_batch.id if metric_batch else None
        row.assignment_samples = cell["assignment_samples"]
        row.assignment_conversions = cell["assignment_conversions"]
        row.metric_samples = cell["metric_samples"]
        row.metric_conversions = cell["metric_conversions"]
        row.sample_delta = cell["sample_delta"]
        row.conversion_delta = cell["conversion_delta"]
        row.status = cell["status"]
        row.reason = cell["reason"]
        row.resolution = cell["resolution"]
        row.canonical_source = cell["canonical_source"]
        row.canonical_samples = cell["canonical_samples"]
        row.canonical_conversions = cell["canonical_conversions"]
        row.usable = bool(cell["usable"])
        row.auto_explainable = bool(cell["auto_explainable"])
        # 解释字段在 reconcile_date 里保留（explain 写入，后续重算沿用）
        row.explained_by = cell["explained_by"]
        row.explained_at = cell["explained_at"]
        if cell["explanation"]:
            row.explanation = cell["explanation"]
    # 已不再出现在任一侧的旧格：删除，避免幽灵行
    for key, row in existing.items():
        if key not in seen:
            db.delete(row)
    db.flush()
    return {
        "stat_date": iso_date(stat_date),
        "assignment_batch": return_service.serialize_batch(assignment_batch) if assignment_batch else None,
        "metric_batch": return_service.serialize_batch(metric_batch) if metric_batch else None,
        **{key: outcome[key] for key in ("status", "ready", "cells_total", "complete", "reconciled", "delayed", "conflict", "usable", "blocking")},
    }


def reconcile_all(
    db: Session,
    experiment: Experiment,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict:
    days = _sorted_days(db, experiment.id, date_from, date_to)
    results = [reconcile_date(db, experiment, day) for day in days]
    ready_days = [item for item in results if item["ready"]]
    return {
        "experiment_id": experiment.id,
        "experiment_code": experiment.code,
        "dates_total": len(results),
        "ready_dates": len(ready_days),
        "delayed_dates": sum(1 for item in results if item["status"] == "delayed"),
        "conflict_dates": sum(1 for item in results if item["status"] == "conflict"),
        "reconciled_dates": sum(1 for item in results if item["status"] == "reconciled"),
        "complete_dates": sum(1 for item in results if item["status"] == "complete"),
        "ready": bool(results) and len(ready_days) == len(results),
        "dates": results,
    }


def summarize(
    db: Session,
    experiment: Experiment,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict:
    """区间对账总览：按日状态、按来源批次数、按状态计数、待处理冲突/延迟清单。"""
    batch_map = _date_batch_map(db, experiment.id)
    days = sorted(batch_map)
    if date_from:
        days = [day for day in days if day >= date_from]
    if date_to:
        days = [day for day in days if day <= date_to]
    cell_rows = db.scalars(
        select(ReconciliationCell)
        .where(
            ReconciliationCell.experiment_id == experiment.id,
            ReconciliationCell.stat_date.in_(days),
        )
        .order_by(ReconciliationCell.stat_date, ReconciliationCell.variant_code, ReconciliationCell.channel, ReconciliationCell.metric_key)
    ).all() if days else []
    variants = {row.code: row for row in db.scalars(select(Variant).where(Variant.experiment_id == experiment.id)).all()}

    by_date: dict[date, list[ReconciliationCell]] = {}
    for row in cell_rows:
        by_date.setdefault(row.stat_date, []).append(row)
    day_items = []
    for day in days:
        rows = by_date.get(day, [])
        batches = batch_map.get(day, {})
        counts = {status: 0 for status in ("complete", "delayed", "conflict", "reconciled")}
        for row in rows:
            if row.status in counts:
                counts[row.status] += 1
        if not rows:
            day_status = "delayed"
        elif counts["delayed"]:
            day_status = "delayed"
        elif counts["conflict"]:
            day_status = "conflict"
        elif counts["reconciled"]:
            day_status = "reconciled"
        else:
            day_status = "complete"
        day_items.append(
            {
                "stat_date": iso_date(day),
                "status": day_status,
                "status_label": RECONCILE_STATUS_LABELS.get(day_status, day_status),
                "assignment_batch": return_service.serialize_batch(batches[ReturnSource.ASSIGNMENT.value])
                if batches.get(ReturnSource.ASSIGNMENT.value)
                else None,
                "metric_batch": return_service.serialize_batch(batches[ReturnSource.METRIC.value])
                if batches.get(ReturnSource.METRIC.value)
                else None,
                "cells_total": len(rows),
                **counts,
                "ready": day_status in ("complete", "reconciled") and bool(rows),
            }
        )

    status_counts = {
        ReconcileStatus.COMPLETE.value: sum(1 for row in cell_rows if row.status == "complete"),
        ReconcileStatus.DELAYED.value: sum(1 for row in cell_rows if row.status == "delayed"),
        ReconcileStatus.CONFLICT.value: sum(1 for row in cell_rows if row.status == "conflict"),
        ReconcileStatus.RECONCILED.value: sum(1 for row in cell_rows if row.status == "reconciled"),
    }
    blocking = [
        _serialize_cell(row, variants.get(row.variant_code))
        for row in cell_rows
        if row.status in ("delayed", "conflict")
    ]
    per_source = {}
    for key in (ReturnSource.ASSIGNMENT.value, ReturnSource.METRIC.value):
        active = [batch_map[day][key] for day in days if batch_map.get(day, {}).get(key)]
        per_source[key] = {
            "source": key,
            "source_label": RETURN_SOURCE_LABELS.get(key, key),
            "active_batches": len(active),
            "active_dates": len({row.stat_date for row in active}),
            "rows": sum(row.rows_count for row in active),
            "samples_total": sum(row.samples_total for row in active),
            "conversions_total": sum(row.conversions_total for row in active),
            "latest_received_at": iso_utc(max((row.received_at for row in active), default=None)),
        }
    missing_sources = [
        iso_date(day) for day in days if not batch_map.get(day, {}).get(ReturnSource.ASSIGNMENT.value)
    ], [
        iso_date(day) for day in days if not batch_map.get(day, {}).get(ReturnSource.METRIC.value)
    ]
    return {
        "experiment_id": experiment.id,
        "experiment_code": experiment.code,
        "date_from": iso_date(days[0]) if days else None,
        "date_to": iso_date(days[-1]) if days else None,
        "dates_total": len(days),
        "ready_dates": sum(1 for item in day_items if item["ready"]),
        "all_ready": bool(day_items) and all(item["ready"] for item in day_items),
        "status_counts": status_counts,
        "blocking_total": len(blocking),
        "blocking": blocking[:100],
        "per_source": per_source,
        "missing_assignment_dates": missing_sources[0],
        "missing_metric_dates": missing_sources[1],
        "days": day_items,
        "generated_at": iso_utc(utcnow()),
    }


def list_cells(
    db: Session,
    experiment: Experiment,
    *,
    status: str | None = None,
    stat_date: date | None = None,
    variant_code: str | None = None,
    channel: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    stmt = select(ReconciliationCell).where(ReconciliationCell.experiment_id == experiment.id)
    count_stmt = select(func.count()).select_from(ReconciliationCell).where(ReconciliationCell.experiment_id == experiment.id)
    if status:
        stmt = stmt.where(ReconciliationCell.status == status)
        count_stmt = count_stmt.where(ReconciliationCell.status == status)
    if stat_date:
        stmt = stmt.where(ReconciliationCell.stat_date == stat_date)
        count_stmt = count_stmt.where(ReconciliationCell.stat_date == stat_date)
    if variant_code:
        stmt = stmt.where(ReconciliationCell.variant_code == variant_code)
        count_stmt = count_stmt.where(ReconciliationCell.variant_code == variant_code)
    if channel:
        stmt = stmt.where(ReconciliationCell.channel == channel)
        count_stmt = count_stmt.where(ReconciliationCell.channel == channel)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(
        stmt.order_by(ReconciliationCell.stat_date, ReconciliationCell.variant_code, ReconciliationCell.channel, ReconciliationCell.metric_key)
        .limit(limit)
        .offset(offset)
    ).all()
    variants = {row.code: row for row in db.scalars(select(Variant).where(Variant.experiment_id == experiment.id)).all()}
    return {"items": [_serialize_cell(row, variants.get(row.variant_code)) for row in rows], "total": total, "limit": limit, "offset": offset}


def explain(
    db: Session,
    experiment: Experiment,
    cell_id: int,
    payload: dict,
    *,
    explained_by: str,
    invalidate=None,
) -> dict:
    row = db.get(ReconciliationCell, cell_id)
    if row is None or row.experiment_id != experiment.id:
        raise AppError("NOT_FOUND", 404, {"entity": "reconciliation_cell", "id": cell_id})
    if row.status not in (ReconcileStatus.CONFLICT.value, ReconcileStatus.RECONCILED.value):
        raise AppError(
            "VALIDATION",
            422,
            {"reason": "只有冲突格需要解释；延迟格要等另一侧回传", "status": row.status},
        )
    resolution = payload["resolution"]
    if resolution not in (ReturnSource.ASSIGNMENT.value, ReturnSource.METRIC.value):
        raise AppError("VALIDATION", 422, {"reason": "resolution 只支持 assignment / metric"})
    if resolution == ReturnSource.ASSIGNMENT.value and row.assignment_samples is None:
        raise AppError("VALIDATION", 422, {"reason": "实验平台这一侧没有数据，不能指定采用它"})
    if resolution == ReturnSource.METRIC.value and row.metric_samples is None:
        raise AppError("VALIDATION", 422, {"reason": "指标库这一侧没有数据，不能指定采用它"})
    row.explanation = payload["explanation"].strip()
    row.resolution = resolution
    row.explained_by = explained_by
    row.explained_at = utcnow()
    db.flush()
    # 重新对账该日，把状态推进到 reconciled
    summary = reconcile_date(db, experiment, row.stat_date)
    if invalidate is not None:
        invalidate(row.stat_date, f"冲突格 {row.variant_code}/{row.channel}/{row.metric_key} 填了解释并采用 {resolution}")
    refreshed = db.get(ReconciliationCell, cell_id)
    variants = {item.code: item for item in db.scalars(select(Variant).where(Variant.experiment_id == experiment.id)).all()}
    return {"cell": _serialize_cell(refreshed, variants.get(refreshed.variant_code)), "day": summary}


def dataset(
    db: Session,
    experiment: Experiment,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
) -> dict:
    """只取可用格的 canonical 值，按统计日聚合成分析输入。

    返回 {"snapshot": {day: {variant: {metric: {n,c}}}}, "dates": [...], "blocking": [...]}。
    """
    stmt = select(ReconciliationCell).where(ReconciliationCell.experiment_id == experiment.id)
    if date_from:
        stmt = stmt.where(ReconciliationCell.stat_date >= date_from)
    if date_to:
        stmt = stmt.where(ReconciliationCell.stat_date <= date_to)
    rows = db.scalars(stmt.order_by(ReconciliationCell.stat_date, ReconciliationCell.variant_code)).all()
    usable_rows = []
    blocking = []
    for row in rows:
        if row.usable and row.canonical_samples is not None:
            usable_rows.append(
                {
                    "stat_date": iso_date(row.stat_date),
                    "variant_code": row.variant_code,
                    "metric_key": row.metric_key,
                    "samples": row.canonical_samples,
                    "conversions": row.canonical_conversions or 0,
                    "channel": row.channel,
                }
            )
        else:
            blocking.append(
                {
                    "stat_date": iso_date(row.stat_date),
                    "variant_code": row.variant_code,
                    "channel": row.channel,
                    "metric_key": row.metric_key,
                    "status": row.status,
                    "reason": row.reason,
                }
            )
    snapshot = reconcile_algorithm.build_snapshot(usable_rows)
    date_fingerprints = {
        day: reconcile_algorithm.snapshot_fingerprint({day: variants})
        for day, variants in sorted(snapshot.items())
    }
    return {
        "snapshot": snapshot,
        "dates": sorted(snapshot),
        "date_fingerprints": date_fingerprints,
        "fingerprint": reconcile_algorithm.snapshot_fingerprint(snapshot),
        "cells_used": len(usable_rows),
        "blocking": blocking,
    }


def merge_date_snapshots(previous: dict, updated: dict, days: list[date]) -> dict:
    """只替换指定统计日的快照，其余沿用旧值 —— 支持「只重算受影响部分」。"""
    target = {key: value for key, value in (previous or {}).items()}
    for day in days:
        key = iso_date(day)
        if key in updated:
            target[key] = updated[key]
        else:
            target.pop(key, None)
    return target


def rebuild_range(days: list[date]) -> tuple[date, date] | None:
    if not days:
        return None
    return min(days), max(days)


def expand_days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
