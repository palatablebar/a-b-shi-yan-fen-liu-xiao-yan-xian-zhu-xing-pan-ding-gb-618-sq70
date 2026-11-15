"""导出服务：实验台账 / 分流样本 / 指标检验 / 决策清单 / 对账 / 分析批次 CSV。

所有导出都带溯源列：双方原始批次数、各自统计日、差异原因与采用侧，
导出的分流样本与指标检验按 canonical 值给出，不再取「最近一次上传」。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    CHANNEL_LABELS,
    CORRECTION_LABELS,
    EXPERIMENT_STATUS_LABELS,
    METRIC_LABELS,
    RECONCILE_RESOLUTION_LABELS,
    RECONCILE_STATUS_LABELS,
    SRM_STATUS_LABELS,
    VERDICT_LABELS,
)
from src.models.analysis_run import AnalysisRun
from src.models.assignment import Assignment
from src.models.decision import Decision
from src.models.experiment import Experiment
from src.models.metric_result import MetricResult
from src.models.reconciliation import ReconciliationCell
from src.models.return_batch import ReturnBatch
from src.models.variant import Variant
from src.services import canonical_service
from src.services.algorithms import reconcile as reconcile_algorithm
from src.utils.csv_export import to_csv


def _fmt(value: float | None, digits: int = 4) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def _source_counts(db: Session, experiment_id: int) -> dict[str, dict[str, object]]:
    rows = db.execute(
        select(ReturnBatch.source, ReturnBatch.status, func.count())
        .where(ReturnBatch.experiment_id == experiment_id)
        .group_by(ReturnBatch.source, ReturnBatch.status)
    ).all()
    counts: dict[str, dict[str, object]] = {}
    for source, status, count in rows:
        bucket = counts.setdefault(source, {"active": 0, "superseded": 0, "dates": set()})
        bucket[status] = int(count)
        if status == "active":
            active_dates = db.execute(
                select(ReturnBatch.stat_date)
                .where(
                    ReturnBatch.experiment_id == experiment_id,
                    ReturnBatch.source == source,
                    ReturnBatch.status == "active",
                )
                .distinct()
            ).all()
            bucket["dates"] = {row[0] for row in active_dates}
    return counts


def _reconcile_trace(db: Session, experiment_id: int) -> dict[str, object]:
    rows = db.scalars(
        select(ReconciliationCell).where(ReconciliationCell.experiment_id == experiment_id)
    ).all()
    conflicts = [row for row in rows if row.status == "conflict"]
    reconciled = [row for row in rows if row.status == "reconciled"]
    delayed = [row for row in rows if row.status == "delayed"]
    reasons = sorted({row.reason for row in rows if row.reason})
    return {
        "cells": len(rows),
        "delayed": len(delayed),
        "conflict": len(conflicts),
        "reconciled": len(reconciled),
        "reasons": reasons,
        "reasons_label": "; ".join(reconcile_algorithm.REASON_LABELS.get(reason, reason) for reason in reasons),
        "resolution_counts": {
            source: sum(1 for row in reconciled if row.resolution == source)
            for source in ("assignment", "metric")
        },
        "dates": len({row.stat_date for row in rows}),
        "ready": bool(rows) and not conflicts and not delayed,
    }


def experiments_csv(db: Session, experiment_id: int | None = None) -> bytes:
    stmt = select(Experiment)
    if experiment_id:
        stmt = stmt.where(Experiment.id == experiment_id)
    experiments = db.scalars(stmt.order_by(Experiment.id)).all()
    variants = {
        row.id: row for row in db.scalars(select(Variant)).all()
    }
    decisions = {row.experiment_id: row for row in db.scalars(select(Decision)).all()}
    latest_runs = {}
    for row in db.scalars(
        select(AnalysisRun).where(AnalysisRun.status == "ready").order_by(AnalysisRun.run_no)
    ).all():
        latest_runs[row.experiment_id] = row
    headers = [
        "实验编号", "实验名称", "主指标", "状态", "负责人", "基准转化率", "目标最小效应",
        "SRM χ²", "df", "SRM p 值", "SRM 状态", "每变体所需样本", "当前功效", "当前可检测效应",
        "变体分流计划", "决策", "决策状态", "分析批次号", "实验平台有效批次", "指标库有效批次",
        "对账延迟格", "对账冲突格", "对账差异原因", "开始日期", "结束日期",
    ]
    rows = []
    for item in experiments:
        plan = " / ".join(
            f"{variant.code}:{variant.ratio:.2f}"
            for variant in sorted(variants.values(), key=lambda v: (v.experiment_id, v.sort_order))
            if variant.experiment_id == item.id
        )
        decision = decisions.get(item.id)
        run = latest_runs.get(item.id)
        source_counts = _source_counts(db, item.id)
        trace = _reconcile_trace(db, item.id)
        rows.append(
            [
                item.code,
                item.name,
                METRIC_LABELS.get(item.primary_metric, item.primary_metric),
                EXPERIMENT_STATUS_LABELS.get(item.status, item.status),
                item.owner,
                _fmt(item.baseline_rate),
                _fmt(item.planned_mde),
                _fmt(item.srm_chi2, 2),
                item.srm_df or "",
                _fmt(item.srm_p_value, 6),
                SRM_STATUS_LABELS.get(item.srm_status, item.srm_status),
                item.required_per_variant or "",
                _fmt(item.power_at_current),
                _fmt(item.mde_at_current),
                plan,
                VERDICT_LABELS.get(decision.verdict, "") if decision else "",
                decision.status if decision else "",
                run.run_no if run else "",
                source_counts.get("assignment", {}).get("active", 0),
                source_counts.get("metric", {}).get("active", 0),
                trace["delayed"],
                trace["conflict"],
                trace["reasons_label"],
                item.started_on.isoformat() if item.started_on else "",
                item.ended_on.isoformat() if item.ended_on else "",
            ]
        )
    return to_csv(headers, rows)


def assignments_csv(db: Session, experiment_id: int | None = None) -> bytes:
    """分流样本：有回传的实验按 canonical 值导出，并带对账溯源列。"""
    stmt = select(Assignment)
    if experiment_id:
        stmt = stmt.where(Assignment.experiment_id == experiment_id)
    rows_in = db.scalars(stmt.order_by(Assignment.experiment_id, Assignment.stat_date, Assignment.channel)).all()
    variants = {row.id: row for row in db.scalars(select(Variant)).all()}
    experiments = {row.id: row for row in db.scalars(select(Experiment)).all()}
    headers = [
        "实验编号", "变体", "渠道", "指标", "日期", "样本数", "转化数", "转化率", "记录人",
        "数据来源", "对账状态", "差异原因", "采用侧", "实验平台批次", "指标库批次",
    ]
    rows = []
    for experiment in experiments.values():
        if experiment_id and experiment.id != experiment_id:
            continue
        if canonical_service.has_returns(db, experiment.id):
            for row in canonical_service.canonical_rows(db, experiment):
                rows.append(_assignment_row(db, experiment, row, variants, canonical=True))
        else:
            for row in rows_in:
                if row.experiment_id != experiment.id:
                    continue
                variant = variants.get(row.variant_id)
                rows.append(
                    [
                        experiment.code,
                        variant.code if variant else "",
                        CHANNEL_LABELS.get(row.channel, row.channel),
                        METRIC_LABELS.get(row.metric_key, row.metric_key),
                        row.stat_date.isoformat() if row.stat_date else "",
                        row.samples,
                        row.conversions,
                        _fmt(row.conversions / row.samples if row.samples else 0.0),
                        row.operator_email,
                        "手工录入",
                        "",
                        "",
                        "",
                        "",
                        "",
                    ]
                )
    return to_csv(headers, rows)


def _assignment_row(db: Session, experiment: Experiment, row: dict, variants: dict, *, canonical: bool) -> list:
    return [
        experiment.code,
        row["variant_code"],
        CHANNEL_LABELS.get(row["channel"], row["channel"]),
        METRIC_LABELS.get(row["metric_key"], row["metric_key"]),
        row["stat_date"].isoformat() if hasattr(row["stat_date"], "isoformat") else str(row["stat_date"]),
        row["samples"],
        row["conversions"],
        _fmt(row["conversions"] / row["samples"] if row["samples"] else 0.0),
        row.get("operator_email", ""),
        "对账后 canonical" if canonical else "手工录入",
        RECONCILE_STATUS_LABELS.get(row.get("reconcile_status") or "", ""),
        "",
        "",
        "",
        "",
    ]


def metrics_csv(db: Session, experiment_id: int | None = None) -> bytes:
    stmt = select(MetricResult)
    if experiment_id:
        stmt = stmt.where(MetricResult.experiment_id == experiment_id)
    rows_in = db.scalars(
        stmt.order_by(MetricResult.experiment_id, MetricResult.variant_code, MetricResult.q_value)
    ).all()
    experiments = {row.id: row for row in db.scalars(select(Experiment)).all()}
    latest_runs: dict[int, AnalysisRun] = {}
    for row in db.scalars(select(AnalysisRun).where(AnalysisRun.status == "ready").order_by(AnalysisRun.run_no)).all():
        latest_runs[row.experiment_id] = row
    headers = [
        "实验编号", "变体", "指标", "是否主指标", "校正方法", "对照样本", "对照转化", "对照转化率",
        "实验样本", "实验转化", "实验转化率", "绝对差", "z 值", "p 值", "q 值(BH)", "q 值(Bonferroni)",
        "CI 下限", "CI 上限", "相对提升", "lift CI 下限", "lift CI 上限", "显著", "结论", "所需样本", "当前功效",
        "分析批次号", "实验平台批次", "指标库批次", "使用统计日",
    ]
    rows = []
    for item in rows_in:
        experiment = experiments.get(item.experiment_id)
        run = latest_runs.get(item.experiment_id)
        source_counts = _source_counts(db, item.experiment_id) if experiment else {}
        rows.append(
            [
                experiment.code if experiment else "",
                item.variant_code,
                item.metric_name or METRIC_LABELS.get(item.metric_key, item.metric_key),
                "是" if item.is_primary else "否",
                CORRECTION_LABELS.get(item.method, item.method),
                item.control_n,
                item.control_conversions,
                _fmt(item.control_rate),
                item.variant_n,
                item.variant_conversions,
                _fmt(item.variant_rate),
                _fmt(item.diff),
                _fmt(item.z, 3),
                _fmt(item.p_value, 6),
                _fmt(item.q_bh, 6),
                _fmt(item.q_bonferroni, 6),
                _fmt(item.ci_low),
                _fmt(item.ci_high),
                _fmt(item.lift),
                _fmt(item.lift_ci_low),
                _fmt(item.lift_ci_high),
                "是" if item.significant else "否",
                VERDICT_LABELS.get(item.verdict, item.verdict),
                item.required_n,
                _fmt(item.power_at_current),
                run.run_no if run else "",
                source_counts.get("assignment", {}).get("active", 0),
                source_counts.get("metric", {}).get("active", 0),
                (run.date_from + "~" + run.date_to) if run and run.date_from else "",
            ]
        )
    return to_csv(headers, rows)


def decisions_csv(db: Session) -> bytes:
    decisions = db.scalars(select(Decision).order_by(Decision.id)).all()
    experiments = {row.id: row for row in db.scalars(select(Experiment)).all()}
    headers = [
        "实验编号", "实验名称", "结论", "决策状态", "改判次数", "理由", "决策人", "决策时间",
        "分析批次号", "SRM p 值", "实验平台批次", "指标库批次", "对账差异原因",
    ]
    rows = []
    for item in decisions:
        experiment = experiments.get(item.experiment_id)
        source_counts = _source_counts(db, item.experiment_id) if experiment else {}
        trace = _reconcile_trace(db, item.experiment_id) if experiment else {"reasons_label": ""}
        rows.append(
            [
                experiment.code if experiment else "",
                experiment.name if experiment else "",
                VERDICT_LABELS.get(item.verdict, item.verdict),
                item.status,
                item.revision,
                item.rationale,
                item.decided_by,
                item.decided_at.strftime("%Y-%m-%d %H:%M") if item.decided_at else "",
                item.analysis_run_no or "",
                _fmt(experiment.srm_p_value, 6) if experiment else "",
                source_counts.get("assignment", {}).get("active", 0),
                source_counts.get("metric", {}).get("active", 0),
                trace.get("reasons_label", ""),
            ]
        )
    return to_csv(headers, rows)


def reconciliation_csv(db: Session, experiment_id: int | None = None) -> bytes:
    """逐格对账导出：双方原始值、差值、状态、原因、采用侧、解释人。"""
    stmt = select(ReconciliationCell)
    if experiment_id:
        stmt = stmt.where(ReconciliationCell.experiment_id == experiment_id)
    rows_in = db.scalars(
        stmt.order_by(
            ReconciliationCell.experiment_id,
            ReconciliationCell.stat_date,
            ReconciliationCell.variant_code,
            ReconciliationCell.channel,
            ReconciliationCell.metric_key,
        )
    ).all()
    experiments = {row.id: row for row in db.scalars(select(Experiment)).all()}
    batches = {row.id: row for row in db.scalars(select(ReturnBatch)).all()}
    headers = [
        "实验编号", "统计日", "变体", "渠道", "指标",
        "实验平台批次号", "实验平台 revision", "实验平台分流数", "实验平台转化数",
        "指标库批次号", "指标库 revision", "指标库分流数", "指标库转化数",
        "分流差", "转化差", "对账状态", "差异原因", "采用侧", "采用的样本数", "采用的转化数",
        "解释", "解释人", "解释时间", "实验平台批次状态", "指标库批次状态",
    ]
    rows = []
    for row in rows_in:
        experiment = experiments.get(row.experiment_id)
        left = batches.get(row.assignment_batch_id) if row.assignment_batch_id else None
        right = batches.get(row.metric_batch_id) if row.metric_batch_id else None
        rows.append(
            [
                experiment.code if experiment else "",
                row.stat_date.isoformat() if row.stat_date else "",
                row.variant_code,
                CHANNEL_LABELS.get(row.channel, row.channel),
                METRIC_LABELS.get(row.metric_key, row.metric_key),
                left.batch_no if left else "",
                left.revision if left else "",
                "" if row.assignment_samples is None else row.assignment_samples,
                "" if row.assignment_conversions is None else row.assignment_conversions,
                right.batch_no if right else "",
                right.revision if right else "",
                "" if row.metric_samples is None else row.metric_samples,
                "" if row.metric_conversions is None else row.metric_conversions,
                "" if row.sample_delta is None else row.sample_delta,
                "" if row.conversion_delta is None else row.conversion_delta,
                RECONCILE_STATUS_LABELS.get(row.status, row.status),
                reconcile_algorithm.REASON_LABELS.get(row.reason or "", "") if row.status != "complete" else "",
                RECONCILE_RESOLUTION_LABELS.get(row.resolution or "", ""),
                "" if row.canonical_samples is None else row.canonical_samples,
                "" if row.canonical_conversions is None else row.canonical_conversions,
                row.explanation or "",
                row.explained_by or "",
                row.explained_at.strftime("%Y-%m-%d %H:%M") if row.explained_at else "",
                left.status if left else "",
                right.status if right else "",
            ]
        )
    return to_csv(headers, rows)


def analysis_runs_csv(db: Session, experiment_id: int | None = None) -> bytes:
    """分析批次台账：每次结论用了哪些日期、双方批次，重算了哪些、沿用了哪些。"""
    stmt = select(AnalysisRun)
    if experiment_id:
        stmt = stmt.where(AnalysisRun.experiment_id == experiment_id)
    rows_in = db.scalars(stmt.order_by(AnalysisRun.experiment_id, AnalysisRun.run_no)).all()
    experiments = {row.id: row for row in db.scalars(select(Experiment)).all()}
    headers = [
        "实验编号", "分析批次号", "状态", "统计日范围", "使用统计日数",
        "重算统计日", "沿用统计日", "校正方法", "SRM 状态", "SRM p 值",
        "主指标", "主指标 lift", "主指标 q", "建议结论", "决策依据",
        "实验平台批次", "指标库批次", "双方批次数合计", "快照指纹", "作废原因", "计算人", "计算时间",
    ]
    rows = []
    for row in rows_in:
        experiment = experiments.get(row.experiment_id)
        srm = row.srm_payload or {}
        basis = row.decision_basis or {}
        sources = row.sources or {}
        rows.append(
            [
                experiment.code if experiment else "",
                row.run_no,
                row.status,
                f"{row.date_from}~{row.date_to}",
                len(row.dates_used or []),
                " ".join(row.recomputed_dates or []),
                " ".join(row.reused_dates or []),
                CORRECTION_LABELS.get(row.method, row.method),
                SRM_STATUS_LABELS.get(srm.get("status", ""), srm.get("status", "")),
                _fmt(srm.get("p_value"), 6),
                basis.get("primary_metric_label", ""),
                _fmt(basis.get("lift")),
                _fmt(basis.get("q_value"), 6),
                VERDICT_LABELS.get(basis.get("suggested_verdict", ""), basis.get("suggested_verdict", "")),
                basis.get("rationale", ""),
                (sources.get("assignment") or {}).get("batches", 0),
                (sources.get("metric") or {}).get("batches", 0),
                sources.get("total_batches", 0),
                row.snapshot_fingerprint[:12],
                row.invalidated_reason or "",
                row.computed_by,
                row.computed_at.strftime("%Y-%m-%d %H:%M") if row.computed_at else "",
            ]
        )
    return to_csv(headers, rows)
