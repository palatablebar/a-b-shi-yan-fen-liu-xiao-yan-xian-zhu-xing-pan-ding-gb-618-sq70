"""每日回传接入：实验平台与指标库各一条数据源。

幂等与修订规则（对账的地基）：
1. 自然键 = (实验, 来源, 统计日, 批次号)。同批次号 + 同内容指纹重试 → duplicate，
   不新增批次、不重复累计、不触发重算。
2. 同批次号但内容变了 → revision + 1，生成新批次，旧批次 superseded。
3. 同来源同一天来了新批次号 → 旧的活动批次 superseded，新批次生效。
4. 只有「内容真的变了」才把该统计日标脏并让包含这一天的旧分析批次作废。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import RETURN_SOURCE_LABELS, ReturnBatchStatus, ReturnSource
from src.models.experiment import Experiment
from src.models.return_batch import ReturnBatch
from src.models.return_batch_row import ReturnBatchRow
from src.services.algorithms import reconcile as reconcile_algorithm
from src.utils.errors import AppError
from src.utils.timeutil import iso_date, iso_utc, utcnow


def serialize_batch(row: ReturnBatch, *, include_rows: bool = False) -> dict:
    payload = {
        "id": row.id,
        "experiment_id": row.experiment_id,
        "source": row.source,
        "source_label": RETURN_SOURCE_LABELS.get(row.source, row.source),
        "stat_date": iso_date(row.stat_date),
        "batch_no": row.batch_no,
        "revision": row.revision,
        "fingerprint": row.fingerprint,
        "rows_count": row.rows_count,
        "samples_total": row.samples_total,
        "conversions_total": row.conversions_total,
        "rate": round(row.conversions_total / row.samples_total, 6) if row.samples_total else 0.0,
        "status": row.status,
        "note": row.note,
        "received_by": row.received_by,
        "received_at": iso_utc(row.received_at),
        "superseded_at": iso_utc(row.superseded_at),
    }
    return payload


def _normalize_rows(payload_rows: list[dict]) -> list[dict]:
    rows = []
    for item in payload_rows:
        rows.append(
            {
                "variant_code": str(item["variant_code"]).strip(),
                "channel": str(item["channel"]).strip(),
                "metric_key": str(item["metric_key"]).strip(),
                "samples": int(item["samples"]),
                "conversions": int(item["conversions"]),
            }
        )
    return rows


def active_batches(
    db: Session,
    experiment_id: int,
    *,
    source: str | None = None,
    stat_date=None,
) -> list[ReturnBatch]:
    stmt = select(ReturnBatch).where(
        ReturnBatch.experiment_id == experiment_id,
        ReturnBatch.status == ReturnBatchStatus.ACTIVE.value,
    )
    if source:
        stmt = stmt.where(ReturnBatch.source == source)
    if stat_date is not None:
        stmt = stmt.where(ReturnBatch.stat_date == stat_date)
    return list(db.scalars(stmt.order_by(ReturnBatch.stat_date, ReturnBatch.source)).all())


def has_returns(db: Session, experiment_id: int) -> bool:
    return bool(
        db.scalar(
            select(func.count())
            .select_from(ReturnBatch)
            .where(ReturnBatch.experiment_id == experiment_id)
        )
    )


def source_dates(db: Session, experiment_id: int) -> dict[str, set]:
    """每个来源当前 active 批次覆盖的统计日。"""
    mapping: dict[str, set] = {ReturnSource.ASSIGNMENT.value: set(), ReturnSource.METRIC.value: set()}
    for row in active_batches(db, experiment_id):
        mapping.setdefault(row.source, set()).add(row.stat_date)
    return mapping


def batches_for_date(db: Session, experiment_id: int, stat_date) -> dict[str, ReturnBatch | None]:
    result: dict[str, ReturnBatch | None] = {ReturnSource.ASSIGNMENT.value: None, ReturnSource.METRIC.value: None}
    for row in active_batches(db, experiment_id, stat_date=stat_date):
        result[row.source] = row
    return result


def rows_of_batch(db: Session, batch_id: int) -> list[dict]:
    rows = db.scalars(
        select(ReturnBatchRow)
        .where(ReturnBatchRow.batch_id == batch_id)
        .order_by(ReturnBatchRow.variant_code, ReturnBatchRow.channel, ReturnBatchRow.metric_key)
    ).all()
    return [
        {
            "variant_code": row.variant_code,
            "channel": row.channel,
            "metric_key": row.metric_key,
            "samples": row.samples,
            "conversions": row.conversions,
        }
        for row in rows
    ]


def list_batches(db: Session, experiment_id: int, *, source: str | None = None) -> dict:
    stmt = select(ReturnBatch).where(ReturnBatch.experiment_id == experiment_id)
    if source:
        stmt = stmt.where(ReturnBatch.source == source)
    rows = db.scalars(
        stmt.order_by(ReturnBatch.stat_date.desc(), ReturnBatch.source, ReturnBatch.batch_no, ReturnBatch.revision.desc())
    ).all()
    active = [row for row in rows if row.status == ReturnBatchStatus.ACTIVE.value]
    superseded = [row for row in rows if row.status == ReturnBatchStatus.SUPERSEDED.value]
    per_source = {}
    for key in (ReturnSource.ASSIGNMENT.value, ReturnSource.METRIC.value):
        per_source[key] = {
            "source": key,
            "source_label": RETURN_SOURCE_LABELS.get(key, key),
            "active_batches": sum(1 for row in active if row.source == key),
            "superseded_batches": sum(1 for row in superseded if row.source == key),
            "active_dates": len({row.stat_date for row in active if row.source == key}),
            "rows": sum(row.rows_count for row in active if row.source == key),
        }
    dates = sorted({row.stat_date for row in active})
    return {
        "experiment_id": experiment_id,
        "items": [serialize_batch(row) for row in rows],
        "total": len(rows),
        "active_total": len(active),
        "superseded_total": len(superseded),
        "per_source": per_source,
        "dates": [{"date": iso_date(day)} for day in dates],
        "generated_at": iso_utc(utcnow()),
    }


def ingest(
    db: Session,
    experiment: Experiment,
    payload: dict,
    *,
    received_by: str,
    invalidate=None,
) -> dict:
    """写入一条回传批次，返回 {result, batch, affected_date, changed}。

    invalidate(date, reason) 由调用方注入，避免 service 之间循环依赖。
    """
    source = payload["source"]
    if source not in (ReturnSource.ASSIGNMENT.value, ReturnSource.METRIC.value):
        raise AppError("VALIDATION", 422, {"reason": "source 只支持 assignment / metric"})
    stat_date = payload["stat_date"]
    batch_no = str(payload["batch_no"]).strip()
    rows = _normalize_rows(payload["rows"])
    fingerprint = reconcile_algorithm.fingerprint_rows(rows)
    samples_total = sum(row["samples"] for row in rows)
    conversions_total = sum(row["conversions"] for row in rows)

    same_batch = db.scalar(
        select(ReturnBatch)
        .where(
            ReturnBatch.experiment_id == experiment.id,
            ReturnBatch.source == source,
            ReturnBatch.stat_date == stat_date,
            ReturnBatch.batch_no == batch_no,
        )
        .order_by(ReturnBatch.revision.desc())
    )
    if same_batch is not None and same_batch.fingerprint == fingerprint:
        return {
            "result": "duplicate",
            "changed": False,
            "affected_date": iso_date(stat_date),
            "batch": serialize_batch(same_batch),
            "superseded": [],
        }

    replaced: list[ReturnBatch] = []
    revision = 1
    if same_batch is not None:
        revision = int(same_batch.revision) + 1
        for row in db.scalars(
            select(ReturnBatch).where(
                ReturnBatch.experiment_id == experiment.id,
                ReturnBatch.source == source,
                ReturnBatch.stat_date == stat_date,
                ReturnBatch.batch_no == batch_no,
                ReturnBatch.status == ReturnBatchStatus.ACTIVE.value,
            )
        ).all():
            row.status = ReturnBatchStatus.SUPERSEDED.value
            row.superseded_at = utcnow()
            replaced.append(row)
    else:
        for row in db.scalars(
            select(ReturnBatch).where(
                ReturnBatch.experiment_id == experiment.id,
                ReturnBatch.source == source,
                ReturnBatch.stat_date == stat_date,
                ReturnBatch.status == ReturnBatchStatus.ACTIVE.value,
            )
        ).all():
            row.status = ReturnBatchStatus.SUPERSEDED.value
            row.superseded_at = utcnow()
            replaced.append(row)

    batch = ReturnBatch(
        experiment_id=experiment.id,
        source=source,
        stat_date=stat_date,
        batch_no=batch_no,
        revision=revision,
        fingerprint=fingerprint,
        rows_count=len(rows),
        samples_total=samples_total,
        conversions_total=conversions_total,
        status=ReturnBatchStatus.ACTIVE.value,
        note=payload.get("note"),
        received_by=received_by,
    )
    db.add(batch)
    db.flush()
    for row in rows:
        db.add(
            ReturnBatchRow(
                batch_id=batch.id,
                experiment_id=experiment.id,
                source=source,
                stat_date=stat_date,
                variant_code=row["variant_code"],
                channel=row["channel"],
                metric_key=row["metric_key"],
                samples=row["samples"],
                conversions=row["conversions"],
            )
        )
    db.flush()

    content_changed = not replaced or any(row.fingerprint != fingerprint for row in replaced)
    if content_changed and invalidate is not None:
        invalidate(stat_date, f"{RETURN_SOURCE_LABELS.get(source, source)} {stat_date} 批次 {batch_no} 内容更新")
    return {
        "result": "revision" if same_batch is not None else "created",
        "changed": content_changed,
        "affected_date": iso_date(stat_date),
        "batch": serialize_batch(batch),
        "superseded": [serialize_batch(row) for row in replaced],
    }
