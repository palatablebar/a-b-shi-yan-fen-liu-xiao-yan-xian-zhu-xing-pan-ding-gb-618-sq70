"""分析批次服务：只有在同一批统计日完成对账后，才生成成套结论。

「成套」= 一次分析批次里同时落：
  - 分流校验（算法 A，基于 canonical 的分流数）
  - 指标结果（算法 B，基于 canonical 的分流数 + 转化数）
  - 决策依据（主指标 q / lift / 功效 / SRM 状态合起来的可复述理由）
并记录本批次用了哪些回传批次（双方批次数、指纹、日期）。
分流校验与指标结果必须引用同一份数据快照，避免页面「用一边的分流、另一边的显著性」。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import ANALYSIS_STATUS_LABELS, CORRECTION_LABELS, METRIC_LABELS, AnalysisStatus, ReturnSource
from src.models.analysis_run import AnalysisRun
from src.models.experiment import Experiment
from src.models.metric_result import MetricResult
from src.models.variant import Variant
from src.services import reconciliation_service, return_service, variant_service
from src.services.algorithms import metric_test as metric_algorithm
from src.services.algorithms import srm as srm_algorithm
from src.services.algorithms import stats
from src.utils.errors import AppError
from src.utils.timeutil import iso_date, iso_utc, utcnow


def serialize_run(row: AnalysisRun) -> dict:
    return {
        "id": row.id,
        "experiment_id": row.experiment_id,
        "run_no": row.run_no,
        "status": row.status,
        "status_label": ANALYSIS_STATUS_LABELS.get(row.status, row.status),
        "snapshot_fingerprint": row.snapshot_fingerprint,
        "date_fingerprints": row.date_fingerprints or {},
        "date_from": row.date_from,
        "date_to": row.date_to,
        "dates_used": row.dates_used or [],
        "method": row.method,
        "method_label": CORRECTION_LABELS.get(row.method, row.method),
        "alpha": row.alpha,
        "srm": row.srm_payload or {},
        "metrics": row.metric_payload or {},
        "decision_basis": row.decision_basis or {},
        "sources": row.sources or {},
        "invalidated_reason": row.invalidated_reason,
        "invalidated_at": iso_utc(row.invalidated_at),
        "superseded_by": row.superseded_by,
        "previous_run_id": row.previous_run_id,
        "recomputed_dates": row.recomputed_dates or [],
        "reused_dates": row.reused_dates or [],
        "srm_reused": bool((row.sources or {}).get("srm_reused")),
        "computed_by": row.computed_by,
        "computed_at": iso_utc(row.computed_at),
    }


def latest_run(db: Session, experiment_id: int, *, status: str | None = None) -> AnalysisRun | None:
    stmt = select(AnalysisRun).where(AnalysisRun.experiment_id == experiment_id)
    if status:
        stmt = stmt.where(AnalysisRun.status == status)
    return db.scalar(stmt.order_by(AnalysisRun.run_no.desc(), AnalysisRun.id.desc()))


def list_runs(db: Session, experiment_id: int | None = None, *, status: str | None = None, limit: int = 30, offset: int = 0) -> dict:
    stmt = select(AnalysisRun)
    count_stmt = select(AnalysisRun)
    if experiment_id:
        stmt = stmt.where(AnalysisRun.experiment_id == experiment_id)
        count_stmt = count_stmt.where(AnalysisRun.experiment_id == experiment_id)
    if status:
        stmt = stmt.where(AnalysisRun.status == status)
        count_stmt = count_stmt.where(AnalysisRun.status == status)
    total = len(db.scalars(count_stmt).all())
    rows = db.scalars(stmt.order_by(AnalysisRun.id.desc()).limit(limit).offset(offset)).all()
    return {"items": [serialize_run(row) for row in rows], "total": total, "limit": limit, "offset": offset}


def invalidate(
    db: Session,
    experiment_id: int,
    *,
    reason: str,
    only_days: list[date] | None = None,
) -> int:
    """让包含受影响日期的旧分析批次作废。

    only_days=None 表示全量作废（例如变体/基准参数变化）。
    """
    affected_days = {iso_date(day) for day in only_days} if only_days is not None else None
    count = 0
    for row in db.scalars(
        select(AnalysisRun).where(
            AnalysisRun.experiment_id == experiment_id,
            AnalysisRun.status == AnalysisStatus.READY.value,
        )
    ).all():
        if affected_days is not None and not affected_days.intersection(set(row.dates_used or [])):
            continue
        row.status = AnalysisStatus.INVALIDATED.value
        row.invalidated_reason = reason
        row.invalidated_at = utcnow()
        # 引用该批次的决策同步转 stale，避免继续被当成有效结论
        from src.services import decision_service

        decision_service.mark_stale(db, experiment_id, row.id, reason)
        count += 1
    if count:
        db.flush()
    return count


def _build_srm_input(snapshot: dict, variants: list[Variant], metric_key: str) -> list[dict]:
    observed = {variant.code: 0 for variant in variants}
    for day_variants in snapshot.values():
        for variant_code, metrics in day_variants.items():
            cell = metrics.get(metric_key)
            if cell and variant_code in observed:
                observed[variant_code] += int(cell.get("n") or 0)
    result = []
    for variant in variants:
        result.append(
            {
                "code": variant.code,
                "name": variant.name,
                "ratio": variant.ratio,
                "observed": observed[variant.code],
                "is_control": variant.is_control,
            }
        )
    return result


def _build_metrics_input(snapshot: dict, variants: list[Variant], experiment: Experiment, treatment: Variant) -> list[dict]:
    control = next((item for item in variants if item.is_control), None)
    if control is None:
        raise AppError("VALIDATION", 422, {"reason": "实验缺少对照组变体"})

    metric_keys = sorted(
        {
            metric_key
            for day_variants in snapshot.values()
            for metrics in day_variants.values()
            for metric_key in metrics.keys()
        }
    )
    metrics = []
    for metric_key in metric_keys:
        control_n = control_c = variant_n = variant_c = 0
        for day_variants in snapshot.values():
            control_cell = (day_variants.get(control.code) or {}).get(metric_key)
            variant_cell = (day_variants.get(treatment.code) or {}).get(metric_key)
            if control_cell:
                control_n += int(control_cell.get("n") or 0)
                control_c += int(control_cell.get("c") or 0)
            if variant_cell:
                variant_n += int(variant_cell.get("n") or 0)
                variant_c += int(variant_cell.get("c") or 0)
        if control_n <= 0 or variant_n <= 0:
            continue
        metrics.append(
            {
                "metric_key": metric_key,
                "metric_name": METRIC_LABELS.get(metric_key, metric_key),
                "is_primary": metric_key == experiment.primary_metric,
                "control": {"n": control_n, "conversions": control_c},
                "variant": {"code": treatment.code, "name": treatment.name, "n": variant_n, "conversions": variant_c},
            }
        )
    return metrics


def _decision_basis(experiment: Experiment, srm: dict, metric_outcome: dict) -> dict:
    primary = metric_outcome.get("primary") if metric_outcome else None
    if not primary:
        return {
            "available": False,
            "suggested_verdict": "continue",
            "rationale": "没有可取用的主指标检验结果，只能继续观察。",
            "blockers": ["primary_metric_missing"],
        }
    blockers = []
    if srm.get("alarm"):
        blockers.append("srm_alarm")
    if not primary.get("significant"):
        blockers.append("primary_not_significant")
    power = primary.get("power_at_current") or 0.0
    if power < experiment.power_target:
        blockers.append("underpowered")
    suggested = primary.get("verdict") or "continue"
    rationale = (
        f"主指标 {primary['metric_name']}：对照 {primary['control_rate']:.2%} → 实验 {primary['variant_rate']:.2%}，"
        f"lift {primary['lift']:+.2%}；{CORRECTION_LABELS.get(metric_outcome.get('method', 'bh'), metric_outcome.get('method', ''))} "
        f"校正后 q={primary['q_value']:.4f}；SRM 状态 {'告警' if srm.get('alarm') else '均衡'}"
        f"（p={srm.get('p_value') if srm.get('p_value') is not None else '—'}）；"
        f"当前功效 {power:.1%}（目标 {experiment.power_target:.0%}）。"
    )
    if "srm_alarm" in blockers:
        rationale += " 分流不均，指标差异不可信，先把分流修好。"
        suggested = "continue"
    elif "primary_not_significant" in blockers:
        rationale += " 校正后不显著，不能据此上线或停止。"
        suggested = "continue"
    elif suggested == "ship" and "underpowered" in blockers:
        rationale += " 效应显著但功效偏低，建议继续加样复核后再上线。"
    return {
        "available": True,
        "suggested_verdict": suggested,
        "primary_metric": primary["metric_key"],
        "primary_metric_label": primary["metric_name"],
        "lift": primary["lift"],
        "lift_ci": [primary["lift_ci_low"], primary["lift_ci_high"]],
        "q_value": primary["q_value"],
        "p_value": primary["p_value"],
        "significant": primary["significant"],
        "power_at_current": power,
        "power_target": experiment.power_target,
        "srm_status": srm.get("status"),
        "srm_p_value": srm.get("p_value"),
        "rationale": rationale,
        "blockers": blockers,
    }


def _sources_snapshot(db: Session, experiment_id: int, days: list[date]) -> dict:
    """记录本批次引用的双方原始批次：批次数、最新 revision、指纹。"""
    sources: dict[str, dict] = {}
    wanted = {day for day in days}
    for source in (ReturnSource.ASSIGNMENT.value, ReturnSource.METRIC.value):
        batches = [
            row
            for row in return_service.active_batches(db, experiment_id, source=source)
            if row.stat_date in wanted
        ]
        sources[source] = {
            "batches": len(batches),
            "dates": len({row.stat_date for row in batches}),
            "rows": sum(row.rows_count for row in batches),
            "revisions": {iso_date(row.stat_date): row.revision for row in batches},
            "batch_nos": {iso_date(row.stat_date): row.batch_no for row in batches},
            "fingerprints": {iso_date(row.stat_date): row.fingerprint for row in batches},
            "latest_received_at": iso_utc(max((row.received_at for row in batches), default=None)),
        }
    total_batches = sum(source["batches"] for source in sources.values())
    sources["total_batches"] = total_batches
    return sources


def compute(
    db: Session,
    experiment: Experiment,
    *,
    method: str = "bh",
    alpha: float | None = None,
    power: float | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    operator_email: str = "system",
    reason: str = "手动生成分析批次",
) -> dict:
    if method not in ("bh", "bonferroni"):
        raise AppError("VALIDATION", 422, {"reason": "method 只支持 bh / bonferroni"})
    # 生成结论前先用当前 active 批次重算一次对账，防止刚接入的新批次绕过门禁
    reconciliation_service.reconcile_all(db, experiment, date_from=date_from, date_to=date_to)
    reconciliation = reconciliation_service.summarize(
        db, experiment, date_from=date_from, date_to=date_to
    )
    if not reconciliation["dates_total"]:
        raise AppError(
            "NO_SAMPLES",
            422,
            {"reason": "该实验还没有任何回传批次，先接入实验平台与指标库的每日回传"},
        )
    if not reconciliation["all_ready"]:
        raise AppError(
            "NOT_RECONCILED",
            422,
            {
                "reason": "同一批统计日尚未完成对账（存在延迟或未解释的冲突），不能生成分析结论",
                "ready_dates": reconciliation["ready_dates"],
                "dates_total": reconciliation["dates_total"],
                "blocking": reconciliation["blocking"][:20],
                "status_counts": reconciliation["status_counts"],
            },
        )

    dataset = reconciliation_service.dataset(db, experiment, date_from=date_from, date_to=date_to)
    if not dataset["dates"]:
        raise AppError("NO_SAMPLES", 422, {"reason": "对账完成后没有可用的分流格"})

    previous = latest_run(db, experiment.id)
    if previous is not None and previous.status == AnalysisStatus.READY.value:
        if (
            previous.snapshot_fingerprint == dataset["fingerprint"]
            and previous.method == method
            and abs(float(previous.alpha) - float(alpha if alpha is not None else experiment.alpha)) < 1e-12
        ):
            payload = serialize_run(previous)
            payload["reused"] = True
            payload["srm_reused"] = True
            payload["recomputed_dates"] = []
            payload["reused_dates"] = payload["dates_used"]
            return payload

    # 按统计日指纹比对：只把真正变过的日期算作重算，其余沿用
    current_fps = dataset["date_fingerprints"]
    previous_fps = (previous.date_fingerprints or {}) if previous is not None else {}
    recomputed_dates = sorted(day for day, fp in current_fps.items() if previous_fps.get(day) != fp)
    reused_dates = sorted(day for day in current_fps if previous_fps.get(day) == current_fps[day])

    snapshot = dataset["snapshot"]
    variants = variant_service.list_variants(db, experiment.id)
    srm_input = _build_srm_input(snapshot, variants, experiment.primary_metric)
    if sum(item["observed"] for item in srm_input) <= 0:
        raise AppError("NO_SAMPLES", 422, {"reason": "主指标没有可用的分流样本"})

    # SRM 只依赖分流数，指标检验依赖分流数 + 转化数。
    # 若只是「转化数」被改且分流数未变，SRM 部分可以沿用旧批次结果，只重算指标与决策依据。
    srm_reused = False
    previous_srm = (previous.srm_payload if previous is not None else None) or {}
    if (
        previous is not None
        and previous_srm
        and previous_srm.get("total") == sum(item["observed"] for item in srm_input)
        and [cell.get("observed") for cell in previous_srm.get("cells", [])] == [item["observed"] for item in srm_input]
        and all(
            previous_srm.get(key) == value
            for key, value in (
                ("baseline_rate", stats.round_half_up(experiment.baseline_rate, 6)),
                ("planned_mde", stats.round_half_up(experiment.planned_mde, 6)),
                ("alpha", alpha if alpha is not None else experiment.alpha),
                ("power_target", power if power is not None else experiment.power_target),
            )
        )
    ):
        srm_result = previous_srm
        srm_reused = True
    else:
        try:
            srm_result = srm_algorithm.run_srm(
                srm_input,
                baseline_rate=experiment.baseline_rate,
                planned_mde=experiment.planned_mde,
                alpha=alpha if alpha is not None else experiment.alpha,
                power=power if power is not None else experiment.power_target,
            )
        except ValueError as exc:
            raise AppError("VALIDATION", 422, {"reason": str(exc)}) from exc

    treatments = [item for item in variants if not item.is_control]
    if not treatments:
        raise AppError("VALIDATION", 422, {"reason": "实验缺少实验组变体"})
    metric_results = {}
    for treatment in treatments:
        metrics_input = _build_metrics_input(snapshot, variants, experiment, treatment)
        if not metrics_input:
            raise AppError("NO_SAMPLES", 422, {"reason": f"变体 {treatment.code} 没有可用的完整指标格"})
        try:
            outcome = metric_algorithm.run_metric_tests(
                metrics_input,
                method=method,
                alpha=alpha if alpha is not None else experiment.alpha,
            )
        except ValueError as exc:
            raise AppError("VALIDATION", 422, {"reason": str(exc)}) from exc
        metric_results[treatment.code] = outcome

    # 主变体的主指标作为决策依据；多实验组时取主指标 q 最小者
    primary_outcome = None
    primary_variant = None
    for code, outcome in metric_results.items():
        primary = outcome.get("primary")
        if primary is None:
            continue
        if primary_outcome is None or primary["q_value"] < primary_outcome["primary"]["q_value"]:
            primary_outcome = outcome
            primary_variant = code
    decision_basis = _decision_basis(experiment, srm_result, primary_outcome)
    if primary_variant:
        decision_basis["variant_code"] = primary_variant

    days = [date.fromisoformat(day) for day in dataset["dates"]]
    run_no = (db.scalar(select(AnalysisRun.run_no).where(AnalysisRun.experiment_id == experiment.id).order_by(AnalysisRun.run_no.desc())) or 0) + 1
    if previous is not None and previous.status == AnalysisStatus.READY.value:
        previous.status = AnalysisStatus.INVALIDATED.value
        previous.invalidated_reason = reason
        previous.invalidated_at = utcnow()
        previous.superseded_by = run_no
        from src.services import decision_service

        decision_service.mark_stale(db, experiment.id, previous.id, reason)

    run = AnalysisRun(
        experiment_id=experiment.id,
        run_no=run_no,
        status=AnalysisStatus.READY.value,
        snapshot_fingerprint=dataset["fingerprint"],
        date_fingerprints=dataset["date_fingerprints"],
        date_from=iso_date(days[0]),
        date_to=iso_date(days[-1]),
        dates_used=[iso_date(day) for day in days],
        method=method,
        alpha=alpha if alpha is not None else experiment.alpha,
        previous_run_id=previous.id if previous is not None else None,
        recomputed_dates=recomputed_dates,
        reused_dates=reused_dates,
        srm_payload=srm_result,
        metric_payload={
            "method": method,
            "alpha": alpha if alpha is not None else experiment.alpha,
            "variants": {
                code: {
                    "variant_code": code,
                    "variant_name": next(item.name for item in variants if item.code == code),
                    "metrics_count": outcome["metrics_count"],
                    "significant_count": outcome["significant_count"],
                    "primary": outcome["primary"],
                    "items": outcome["items"],
                    "note": outcome["note"],
                }
                for code, outcome in metric_results.items()
            },
        },
        decision_basis=decision_basis,
        sources={**_sources_snapshot(db, experiment.id, days), "srm_reused": srm_reused},
        computed_by=operator_email,
    )
    db.add(run)

    # 同步实验的 SRM 快照，占位符与旧逻辑一致；同时让旧 MetricResult 退役
    experiment.srm_chi2 = srm_result["chi2"]
    experiment.srm_df = srm_result["df"]
    experiment.srm_p_value = srm_result["p_value"]
    experiment.srm_status = srm_result["status"]
    experiment.has_srm_alarm = srm_result["alarm"]
    experiment.srm_checked_at = utcnow()
    experiment.required_per_variant = srm_result["required_per_variant"]
    experiment.power_at_current = srm_result["power_at_current"]
    experiment.mde_at_current = srm_result["mde_at_current"]
    experiment.srm_payload = srm_result
    db.flush()

    for code, outcome in metric_results.items():
        treatment = next(item for item in treatments if item.code == code)
        for item in outcome["items"]:
            row = db.scalar(
                select(MetricResult).where(
                    MetricResult.experiment_id == experiment.id,
                    MetricResult.variant_code == code,
                    MetricResult.metric_key == item["metric_key"],
                )
            )
            if row is None:
                row = MetricResult(
                    experiment_id=experiment.id,
                    variant_id=treatment.id,
                    variant_code=code,
                    variant_name=treatment.name,
                    metric_key=item["metric_key"],
                    metric_name=item["metric_name"],
                )
                db.add(row)
            row.variant_id = treatment.id
            row.variant_name = treatment.name
            row.metric_name = item["metric_name"]
            row.is_primary = item["is_primary"]
            row.method = method
            row.control_n = item["control_n"]
            row.control_conversions = item["control_conversions"]
            row.variant_n = item["variant_n"]
            row.variant_conversions = item["variant_conversions"]
            row.control_rate = item["control_rate"]
            row.variant_rate = item["variant_rate"]
            row.diff = item["diff"]
            row.z = item["z"]
            row.p_value = item["p_value"]
            row.q_value = item["q_value"]
            row.q_bh = item["q_bh"]
            row.q_bonferroni = item["q_bonferroni"]
            row.ci_low = item["ci_low"]
            row.ci_high = item["ci_high"]
            row.lift = item["lift"]
            row.lift_ci_low = item["lift_ci_low"]
            row.lift_ci_high = item["lift_ci_high"]
            row.significant = item["significant"]
            row.verdict = item["verdict"]
            row.required_n = item["required_n"]
            row.power_at_current = item["power_at_current"]
            row.alpha = item["alpha"]
            row.payload = {**item, "analysis_run_id": run.id, "analysis_run_no": run_no}
            row.computed_at = utcnow()
            row.computed_by = operator_email
    db.flush()
    return serialize_run(run)
