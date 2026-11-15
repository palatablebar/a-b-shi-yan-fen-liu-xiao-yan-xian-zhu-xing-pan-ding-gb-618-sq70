"""幂等 seed：3 账号 / 6 实验 / 13 变体 / 1,092 行分流样本 / 指标检验 / 4 条决策。

自然键 upsert：
  users.email
  experiments.code
  variants(experiment_id, code)
  assignments(experiment_id, variant_id, channel, metric_key, stat_date)
  return_batches(experiment_id, source, stat_date, batch_no, revision)
  metric_results(experiment_id, variant_code, metric_key)
  decisions(experiment_id)
重复执行不新增、不覆盖人工改过的决策/状态；SEED_SUMMARY 两次逐字一致。

每日回传演示（两条源）：
  全部实验都按 7 天 × 4 渠道回传「实验平台（分流）」与「指标库（转化）」；
  EXP-2613-FEEDSORT 第 1 天制造一次同批次号改数（revision，旧批次 superseded），
  最后一天制造一个未解释的冲突格，seed 会补解释并采用指标库 → 已和解；
  EXP-2614-SUGGEST 最后一天故意缺指标库回传 → 延迟，分析批次被门禁挡住。

口径映射（模板 SEED_SUMMARY 键名固定，语义按本项目落点）：
  sources  = 实验数
  runs     = 分流样本行数（assignments）
  findings = 指标检验结果数（metric_results）
  pending  = 还没有决策的实验数
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from src.config.database import session_scope
from src.constants.enums import ReturnSource
from src.models.assignment import Assignment
from src.models.audit import AuditLog
from src.models.decision import Decision
from src.models.experiment import Experiment
from src.models.metric_result import MetricResult
from src.models.reconciliation import ReconciliationCell
from src.models.return_batch import ReturnBatch
from src.models.user import User
from src.models.variant import Variant
from src.services import (
    analysis_service,
    assignment_service,
    decision_service,
    experiment_service,
    reconciliation_service,
    return_service,
)
from src.utils.errors import AppError
from src.utils.security import hash_password

from scripts.demo_config import (
    DECISIONS,
    DEMO_PASSWORD,
    DEMO_USERS,
    EXPERIMENTS,
    ORG,
    assignment_cells,
)

SEED_OPERATOR = "seed@abcheck.io"
REVISION_DEMO_EXPERIMENT = "EXP-2613-FEEDSORT"
DELAY_DEMO_EXPERIMENT = "EXP-2614-SUGGEST"


def ensure_users(db) -> int:
    for email, display_name, role in DEMO_USERS:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            db.add(
                User(
                    email=email,
                    password_hash=hash_password(DEMO_PASSWORD),
                    display_name=display_name,
                    role=role,
                    org=ORG,
                )
            )
    db.flush()
    return db.scalar(select(func.count()).select_from(User)) or 0


def ensure_experiments(db) -> int:
    for spec in EXPERIMENTS:
        experiment = db.scalar(select(Experiment).where(Experiment.code == spec["code"]))
        if experiment is None:
            experiment = experiment_service.create_experiment(
                db,
                {
                    "code": spec["code"],
                    "name": spec["name"],
                    "hypothesis": spec["hypothesis"],
                    "owner": ORG,
                    "primary_metric": spec["primary_metric"],
                    "status": spec["status"],
                    "baseline_rate": spec["baseline_rate"],
                    "planned_mde": spec["planned_mde"],
                    "alpha": 0.05,
                    "power_target": 0.8,
                    "traffic_note": spec["traffic_note"],
                    "started_on": spec["started_on"],
                    "ended_on": None,
                    "variants": spec["variants"],
                },
                owner_default=ORG,
            )
            experiment.seed_key = f"seed-exp-{spec['code']}"
        else:
            # 只更新元信息，不动状态与 SRM 快照（人工改过就保留）
            experiment.hypothesis = spec["hypothesis"]
            experiment.baseline_rate = spec["baseline_rate"]
            experiment.planned_mde = spec["planned_mde"]
            experiment.traffic_note = spec["traffic_note"]
        db.flush()
    return db.scalar(select(func.count()).select_from(Experiment)) or 0


def ensure_variants(db) -> dict[str, dict[str, Variant]]:
    mapping: dict[str, dict[str, Variant]] = {}
    for spec in EXPERIMENTS:
        experiment = db.scalar(select(Experiment).where(Experiment.code == spec["code"]))
        if experiment is None:
            continue
        existing = {item.code: item for item in experiment_service.get_variants(db, experiment.id)}
        for index, variant_spec in enumerate(spec["variants"]):
            if variant_spec["code"] not in existing:
                db.add(
                    Variant(
                        experiment_id=experiment.id,
                        code=variant_spec["code"],
                        name=variant_spec["name"],
                        ratio=variant_spec["ratio"],
                        is_control=variant_spec["is_control"],
                        note=variant_spec.get("note"),
                        sort_order=index,
                    )
                )
        db.flush()
        mapping[spec["code"]] = {item.code: item for item in experiment_service.get_variants(db, experiment.id)}
    return mapping


def ensure_assignments(db, variants: dict[str, dict[str, Variant]]) -> int:
    experiments = {item.code: item for item in db.scalars(select(Experiment)).all()}
    for spec in EXPERIMENTS:
        experiment = experiments.get(spec["code"])
        if experiment is None:
            continue
        for cell in assignment_cells(spec):
            variant = variants[spec["code"]][cell["variant_code"]]
            assignment_service.upsert_assignment(
                db,
                experiment,
                variant_id=variant.id,
                channel=cell["channel"],
                metric_key=cell["metric_key"],
                stat_date=cell["stat_date"],
                samples=cell["samples"],
                conversions=cell["conversions"],
                operator_email=SEED_OPERATOR,
            )
    return db.scalar(select(func.count()).select_from(Assignment)) or 0


def _split_return_rows(cells: list[dict], stat_date: date) -> dict[str, list[dict]]:
    """把一天的 assignments 拆成两条源：实验平台带分流数，指标库带转化数。"""
    day_cells = [cell for cell in cells if cell["stat_date"] == stat_date]
    assignment_rows = []
    metric_rows = []
    for cell in day_cells:
        base = {
            "variant_code": cell["variant_code"],
            "channel": cell["channel"],
            "metric_key": cell["metric_key"],
        }
        assignment_rows.append({**base, "samples": cell["samples"], "conversions": cell["conversions"]})
        metric_rows.append({**base, "samples": cell["samples"], "conversions": cell["conversions"]})
    return {"assignment": assignment_rows, "metric": metric_rows}


def _ensure_batch(
    db,
    experiment,
    source: str,
    stat_date: date,
    batch_no: str,
    rows: list[dict],
    *,
    note: str | None = None,
) -> dict:
    existing = db.scalar(
        select(ReturnBatch)
        .where(
            ReturnBatch.experiment_id == experiment.id,
            ReturnBatch.source == source,
            ReturnBatch.stat_date == stat_date,
            ReturnBatch.batch_no == batch_no,
            ReturnBatch.status == "active",
        )
        .order_by(ReturnBatch.revision.desc(), ReturnBatch.id)
    )
    from src.services.algorithms import reconcile as reconcile_algorithm

    if existing is not None and existing.fingerprint == reconcile_algorithm.fingerprint_rows(rows):
        return {"result": "duplicate", "changed": False, "batch": existing}
    return return_service.ingest(
        db,
        experiment,
        {"source": source, "stat_date": stat_date, "batch_no": batch_no, "rows": rows, "note": note},
        received_by=SEED_OPERATOR,
        invalidate=lambda day, reason: None,
    )


CONFLICT_DEMO_EXPERIMENT = "EXP-2612-WELCOME"


def _conflict_metric_rows(split: dict, day_index: int) -> list[dict]:
    """冲突演示：指标库把某一格的转化数重算多了 3 个。"""
    rows = [dict(row) for row in split["metric"]]
    if rows:
        rows[0]["conversions"] = min(rows[0]["samples"], rows[0]["conversions"] + 3)
    return rows


def ensure_returns(db, variants: dict[str, dict[str, Variant]]) -> dict:
    """为全部实验回传两条源的每日批次，并保留三处刻意演示（幂等）。"""
    experiments = {item.code: item for item in db.scalars(select(Experiment)).all()}
    created = 0
    duplicates = 0
    dates_by_experiment: dict[str, list[date]] = {}
    for spec in EXPERIMENTS:
        experiment = experiments.get(spec["code"])
        if experiment is None:
            continue
        cells = assignment_cells(spec)
        days = sorted({cell["stat_date"] for cell in cells})
        dates_by_experiment[spec["code"]] = days
        conflict_day = days[2] if spec["code"] == CONFLICT_DEMO_EXPERIMENT and len(days) >= 3 else None
        for stat_date in days:
            split = _split_return_rows(cells, stat_date)
            for source in (ReturnSource.ASSIGNMENT.value, ReturnSource.METRIC.value):
                # 延迟演示：EXP-2614 最后一天指标库没回传
                if (
                    spec["code"] == DELAY_DEMO_EXPERIMENT
                    and source == ReturnSource.METRIC.value
                    and stat_date == days[-1]
                ):
                    continue
                note = None
                rows = split[source]
                if source == ReturnSource.METRIC.value and stat_date == conflict_day:
                    rows = _conflict_metric_rows(split, 2)
                    note = "指标库口径重算：转化数 +3"
                elif spec["code"] == DELAY_DEMO_EXPERIMENT and stat_date == days[-1]:
                    note = "指标库当日回传未到，次日补"
                outcome = _ensure_batch(
                    db, experiment, source, stat_date,
                    f"{spec['code']}-{source}-{stat_date.strftime('%Y%m%d')}",
                    rows, note=note,
                )
                created += 1 if outcome["result"] == "created" else 0
                duplicates += 1 if outcome["result"] == "duplicate" else 0

    # 修订演示：EXP-2613 第一天指标库先报中间版再报终版 → revision 2/3，旧批次 superseded
    revision_experiment = experiments.get(REVISION_DEMO_EXPERIMENT)
    if revision_experiment is not None:
        days = dates_by_experiment.get(REVISION_DEMO_EXPERIMENT) or []
        if days:
            day = days[0]
            spec = next(item for item in EXPERIMENTS if item["code"] == REVISION_DEMO_EXPERIMENT)
            split = _split_return_rows(assignment_cells(spec), day)
            batch_no = f"{REVISION_DEMO_EXPERIMENT}-metric-{day.strftime('%Y%m%d')}"
            current = db.scalar(
                select(ReturnBatch)
                .where(
                    ReturnBatch.experiment_id == revision_experiment.id,
                    ReturnBatch.source == ReturnSource.METRIC.value,
                    ReturnBatch.stat_date == day,
                    ReturnBatch.status == "active",
                )
                .order_by(ReturnBatch.revision.desc(), ReturnBatch.id)
            )
            if current is not None and current.revision == 1 and current.batch_no == batch_no:
                adjusted = [dict(row) for row in split["metric"]]
                if adjusted and adjusted[0]["samples"] >= 2:
                    adjusted[0] = {**adjusted[0], "samples": adjusted[0]["samples"] - 2}
                _ensure_batch(
                    db, revision_experiment, ReturnSource.METRIC.value, day, batch_no, adjusted,
                    note="口径重算中间版",
                )
                _ensure_batch(
                    db, revision_experiment, ReturnSource.METRIC.value, day, batch_no, split["metric"],
                    note="重算终版（同批次号 revision +1）",
                )

    total = db.scalar(select(func.count()).select_from(ReturnBatch)) or 0
    return {"created": created, "duplicates": duplicates, "total_batches": total}


def ensure_reconciliation(db) -> dict:
    """对账全部实验；延时格不解释（门禁演示），冲突格由 seed 补解释并采用指标库。"""
    experiments = db.scalars(select(Experiment).order_by(Experiment.id)).all()
    for experiment in experiments:
        reconciliation_service.reconcile_all(db, experiment)
    prepared = 0
    for experiment_id, cell_id in _conflict_cells_needing_explanation(db):
        cell = db.get(ReconciliationCell, cell_id)
        if cell is None:
            continue
        experiment = db.scalar(select(Experiment).where(Experiment.id == experiment_id))
        reconciliation_service.explain(
            db,
            experiment,
            cell.id,
            {
                "explanation": "指标库晚到批次的转化口径重算（去重了一次重复上报的订单），以指标库为准。",
                "resolution": ReturnSource.METRIC.value,
            },
            explained_by="analyst@abcheck.io",
            invalidate=lambda day, reason: None,
        )
        prepared += 1
    ready = sum(
        1 for experiment in experiments if reconciliation_service.summarize(db, experiment)["all_ready"]
    )
    return {"prepared_conflicts": prepared, "ready_experiments": ready}


def _conflict_cells_needing_explanation(db) -> list[tuple[int, int]]:
    rows = db.scalars(
        select(ReconciliationCell).where(
            ReconciliationCell.status == "conflict",
            ReconciliationCell.explanation.is_(None),
        )
    ).all()
    return [(row.experiment_id, row.id) for row in rows]


def run_checks(db) -> int:
    """为已对账齐全的实验生成分析批次；没对账的留空（旧单源校验不再参与）。"""
    experiments = db.scalars(select(Experiment).order_by(Experiment.id)).all()
    for experiment in experiments:
        try:
            analysis_service.compute(
                db,
                experiment,
                method="bh",
                operator_email=SEED_OPERATOR,
                reason="seed 初始化",
            )
        except AppError:
            continue
    db.flush()
    return db.scalar(select(func.count()).select_from(MetricResult)) or 0


def ensure_decisions(db) -> int:
    experiments = {item.code: item for item in db.scalars(select(Experiment)).all()}
    for code, (verdict, template) in DECISIONS.items():
        experiment = experiments.get(code)
        if experiment is None:
            continue
        existing = decision_service.get_by_experiment(db, experiment.id)
        if existing is not None:
            continue
        primary = db.scalar(
            select(MetricResult).where(
                MetricResult.experiment_id == experiment.id,
                MetricResult.is_primary.is_(True),
            )
        )
        context = {
            "chi2": experiment.srm_chi2 or 0.0,
            "p": experiment.srm_p_value or 1.0,
            "c": primary.control_rate if primary else 0.0,
            "v": primary.variant_rate if primary else 0.0,
            "lift": primary.lift if primary else 0.0,
            "q": primary.q_value if primary else 1.0,
        }
        decision_service.create(
            db,
            experiment,
            {"verdict": verdict, "rationale": template.format(**context)},
            decided_by="admin@abcheck.io",
        )
        experiment.status = "finished"
        decision = decision_service.get_by_experiment(db, experiment.id)
        if decision is not None:
            decision.seed_key = f"seed-decision-{code}"
    db.flush()
    return db.scalar(select(func.count()).select_from(Decision)) or 0


def ensure_audit(db) -> None:
    seeds = [
        ("seed-audit-1", "admin@abcheck.io", "admin", "experiment.create", "experiment", {"code": "EXP-2603-CARTBTN"}),
        ("seed-audit-2", "seed@abcheck.io", "admin", "srm.check", "experiment", {"code": "EXP-2603-CARTBTN", "alarm": True}),
        ("seed-audit-3", "seed@abcheck.io", "admin", "metric.check", "experiment", {"method": "bh"}),
    ]
    for seed_key, email, role, action, entity, detail in seeds:
        exists = db.scalar(select(AuditLog).where(AuditLog.seed_key == seed_key))
        if exists is None:
            db.add(
                AuditLog(
                    actor_email=email,
                    actor_role=role,
                    action=action,
                    entity=entity,
                    detail=detail,
                    layer="seed",
                    seed_key=seed_key,
                )
            )
    db.flush()


def seed_all() -> dict:
    with session_scope() as db:
        users = ensure_users(db)
        experiments = ensure_experiments(db)
        variants = ensure_variants(db)
        assignments = ensure_assignments(db, variants)
        returns = ensure_returns(db, variants)
        ensure_reconciliation(db)
        findings = run_checks(db)
        ensure_decisions(db)
        ensure_audit(db)
        pending = db.scalar(
            select(func.count())
            .select_from(Experiment)
            .where(~Experiment.id.in_(select(Decision.experiment_id)))
        ) or 0
        summary = (
            f"SEED_SUMMARY brand=abcheck users={users} sources={experiments} "
            f"runs={assignments} findings={findings} pending={pending} "
            f"batches={returns['total_batches']}"
        )
        print(summary, flush=True)
        return {
            "users": users,
            "sources": experiments,
            "runs": assignments,
            "findings": findings,
            "pending": pending,
            "batches": returns["total_batches"],
        }


if __name__ == "__main__":
    seed_all()
