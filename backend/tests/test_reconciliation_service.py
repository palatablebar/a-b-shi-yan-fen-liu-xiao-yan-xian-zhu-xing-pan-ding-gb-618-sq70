"""回传 / 对账 / 分析批次的服务层行为（内存 SQLite，不依赖 Docker）。

覆盖验收要点：
- 同一批次重试不重复累计；
- 源数据变化只重算受影响统计日、旧结论作废；
- 延迟与冲突分开处理，未齐全不允许生成结论；
- 决策引用有效批次，批次作废后决策过期、可改判。
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from src.models.analysis_run import AnalysisRun
from src.models.decision import Decision
from src.models.experiment import Experiment
from src.models.return_batch import ReturnBatch
from src.models.return_batch_row import ReturnBatchRow
from src.services import (
    analysis_service,
    canonical_service,
    export_service,
    reconciliation_service,
    return_service,
)


def _experiment(db, code: str) -> Experiment:
    return db.scalar(select(Experiment).where(Experiment.code == code))


def _active_rows(db, experiment, source, day):
    batch = db.scalar(
        select(ReturnBatch).where(
            ReturnBatch.experiment_id == experiment.id,
            ReturnBatch.source == source,
            ReturnBatch.stat_date == day,
            ReturnBatch.status == "active",
        )
    )
    assert batch is not None
    return [
        {"variant_code": row.variant_code, "channel": row.channel, "metric_key": row.metric_key,
         "samples": row.samples, "conversions": row.conversions}
        for row in db.scalars(select(ReturnBatchRow).where(ReturnBatchRow.batch_id == batch.id)).all()
    ], batch


def test_seed_is_idempotent_and_has_both_sources(seeded):
    db = seeded
    from scripts import seed_demo

    summary = seed_demo.seed_all()
    assert summary["batches"] == db.scalar(select(func.count()).select_from(ReturnBatch))
    assert summary["runs"] == 1092
    experiment = _experiment(db, "EXP-2610-CASH8")
    sources = return_service.source_dates(db, experiment.id)
    assert len(sources["assignment"]) == 7
    assert len(sources["metric"]) == 7


def test_duplicate_retry_does_not_accumulate(seeded):
    db = seeded
    experiment = _experiment(db, "EXP-2610-CASH8")
    days = sorted(return_service.source_dates(db, experiment.id)["assignment"])
    day = days[-1]
    rows, batch = _active_rows(db, experiment, "assignment", day)
    before_batches = db.scalar(select(func.count()).select_from(ReturnBatch))
    before_run_status = db.scalar(
        select(AnalysisRun.status).where(AnalysisRun.experiment_id == experiment.id, AnalysisRun.status == "ready")
    )
    outcome = return_service.ingest(
        db,
        experiment,
        {"source": "assignment", "stat_date": day, "batch_no": batch.batch_no, "rows": rows},
        received_by="tester@abcheck.io",
        invalidate=lambda d, r: analysis_service.invalidate(db, experiment.id, reason=r, only_days=[d]),
    )
    assert outcome["result"] == "duplicate"
    assert outcome["changed"] is False
    assert db.scalar(select(func.count()).select_from(ReturnBatch)) == before_batches
    assert (
        db.scalar(
            select(AnalysisRun.status).where(
                AnalysisRun.experiment_id == experiment.id, AnalysisRun.status == "ready"
            )
        )
        == before_run_status
    )


def test_content_change_creates_revision_and_invalidates_run(seeded):
    db = seeded
    experiment = _experiment(db, "EXP-2610-CASH8")
    days = sorted(return_service.source_dates(db, experiment.id)["assignment"])
    day = days[-1]
    rows, batch = _active_rows(db, experiment, "assignment", day)
    ready = db.scalar(
        select(AnalysisRun).where(AnalysisRun.experiment_id == experiment.id, AnalysisRun.status == "ready")
    )
    changed = [dict(item) for item in rows]
    changed[0]["conversions"] += 2
    outcome = return_service.ingest(
        db,
        experiment,
        {"source": "assignment", "stat_date": day, "batch_no": batch.batch_no, "rows": changed},
        received_by="tester@abcheck.io",
        invalidate=lambda d, r: analysis_service.invalidate(db, experiment.id, reason=r, only_days=[d]),
    )
    assert outcome["result"] == "revision"
    assert outcome["batch"]["revision"] == 2
    assert len(outcome["superseded"]) == 1
    db.refresh(ready)
    assert ready.status == "invalidated"

    decision = db.scalar(select(Decision).where(Decision.experiment_id == experiment.id))
    assert decision.status == "stale"

    day_result = reconciliation_service.reconcile_date(db, experiment, day)
    assert day_result["status"] == "conflict"


def test_analysis_gate_blocks_when_reconciliation_incomplete(seeded):
    db = seeded
    experiment = _experiment(db, "EXP-2614-SUGGEST")
    from src.utils.errors import AppError

    try:
        analysis_service.compute(db, experiment, operator_email="tester@abcheck.io")
    except AppError as exc:
        assert exc.code == "NOT_RECONCILED"
        assert exc.status_code == 422
        assert exc.detail["blocking"]
    else:  # pragma: no cover
        raise AssertionError("延迟未处理时不应生成分析批次")


def test_partial_recompute_only_recomputes_changed_days(seeded):
    db = seeded
    experiment = _experiment(db, "EXP-2610-CASH8")
    days = sorted(return_service.source_dates(db, experiment.id)["assignment"])
    day = days[-1]
    rows, batch = _active_rows(db, experiment, "assignment", day)
    changed = [dict(item) for item in rows]
    changed[0]["conversions"] += 2
    return_service.ingest(
        db,
        experiment,
        {"source": "assignment", "stat_date": day, "batch_no": batch.batch_no, "rows": changed},
        received_by="tester@abcheck.io",
        invalidate=lambda d, r: analysis_service.invalidate(db, experiment.id, reason=r, only_days=[d]),
    )
    day_result = reconciliation_service.reconcile_date(db, experiment, day)
    assert day_result["conflict"] == 1
    cell = reconciliation_service.list_cells(db, experiment, status="conflict")["items"][0]
    reconciliation_service.explain(
        db,
        experiment,
        cell["id"],
        {"explanation": "实验平台补报一笔订单，采用实验平台", "resolution": "assignment"},
        explained_by="analyst@abcheck.io",
        invalidate=lambda d, r: analysis_service.invalidate(db, experiment.id, reason=r, only_days=[d]),
    )
    run = analysis_service.compute(db, experiment, operator_email="tester@abcheck.io", reason="retest")
    assert run["run_no"] == 2
    assert run["recomputed_dates"] == [day.isoformat()]
    assert len(run["reused_dates"]) == len(days) - 1
    again = analysis_service.compute(db, experiment, operator_email="tester@abcheck.io", reason="retest")
    assert again.get("reused") is True
    assert again["run_no"] == 2


def test_single_source_checks_are_blocked_for_returned_experiments(seeded):
    db = seeded
    experiment = _experiment(db, "EXP-2610-CASH8")
    from src.controllers import metric_controller
    from src.utils.errors import AppError

    try:
        metric_controller._require_manual_mode(db, experiment)
    except AppError as exc:
        assert exc.status_code == 409
    else:  # pragma: no cover
        raise AssertionError("已有回传批次时不允许单独跑 SRM / 指标检验")


def test_canonical_values_match_both_sources_after_reconciliation(seeded):
    db = seeded
    experiment = _experiment(db, "EXP-2610-CASH8")
    assert canonical_service.has_returns(db, experiment.id) is True
    totals = canonical_service.variant_metric_totals(db, experiment, experiment.primary_metric)
    assert totals["control"]["samples"] == 10200
    assert totals["treat_flat"]["samples"] == 10208


def test_exports_carry_source_batch_counts_and_reasons(seeded):
    db = seeded
    experiment = _experiment(db, "EXP-2610-CASH8")
    runs_csv = export_service.analysis_runs_csv(db, experiment.id).decode("utf-8-sig")
    header = runs_csv.splitlines()[0]
    assert "实验平台批次" in header and "指标库批次" in header
    rows = runs_csv.splitlines()[1:]
    assert "14" in rows[0]  # 7 + 7 批
    recon_csv = export_service.reconciliation_csv(db, experiment.id).decode("utf-8-sig")
    recon_header = recon_csv.splitlines()[0]
    assert "差异原因" in recon_header and "采用侧" in recon_header
    metrics_csv = export_service.metrics_csv(db, experiment.id).decode("utf-8-sig")
    assert "分析批次号" in metrics_csv.splitlines()[0]


def test_conversion_only_change_reuses_srm_block(seeded):
    db = seeded
    experiment = _experiment(db, "EXP-2610-CASH8")
    days = sorted(return_service.source_dates(db, experiment.id)["assignment"])
    day = days[-1]
    a_rows, a_batch = _active_rows(db, experiment, "assignment", day)
    m_rows, m_batch = _active_rows(db, experiment, "metric", day)
    a_changed = [dict(item) for item in a_rows]
    m_changed = [dict(item) for item in m_rows]
    a_changed[0]["conversions"] += 1
    m_changed[0]["conversions"] += 1
    invalidate = lambda d, r: analysis_service.invalidate(db, experiment.id, reason=r, only_days=[d])
    return_service.ingest(db, experiment, {"source": "assignment", "stat_date": day, "batch_no": a_batch.batch_no, "rows": a_changed}, received_by="t@x.io", invalidate=invalidate)
    return_service.ingest(db, experiment, {"source": "metric", "stat_date": day, "batch_no": m_batch.batch_no, "rows": m_changed}, received_by="t@x.io", invalidate=invalidate)
    assert reconciliation_service.reconcile_date(db, experiment, day)["status"] == "complete"
    run = analysis_service.compute(db, experiment, operator_email="t@x.io", reason="conversion only")
    assert run["srm_reused"] is True
    assert run["recomputed_dates"] == [day.isoformat()]


def test_delayed_side_arrival_unblocks_analysis(seeded):
    db = seeded
    experiment = _experiment(db, "EXP-2614-SUGGEST")
    from src.utils.errors import AppError

    days = sorted(return_service.source_dates(db, experiment.id)["assignment"])
    day = days[-1]
    a_rows, a_batch = _active_rows(db, experiment, "assignment", day)
    with pytest.raises(AppError):
        analysis_service.compute(db, experiment, operator_email="t@x.io")
    return_service.ingest(
        db,
        experiment,
        {"source": "metric", "stat_date": day, "batch_no": f"EXP-2614-SUGGEST-metric-{day.strftime('%Y%m%d')}", "rows": a_rows},
        received_by="t@x.io",
        invalidate=lambda d, r: analysis_service.invalidate(db, experiment.id, reason=r, only_days=[d]),
    )
    reconciliation_service.reconcile_all(db, experiment)
    summary = reconciliation_service.summarize(db, experiment)
    assert summary["all_ready"] is True
    run = analysis_service.compute(db, experiment, operator_email="t@x.io", reason="late arrival")
    assert run["status"] == "ready"
    assert run["dates_used"][-1] == day.isoformat()
