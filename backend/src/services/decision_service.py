"""决策服务：一次实验一个当前结论；改判留修订史，且必须引用有效分析批次。

规则：
- 同一实验已有 active 决策 → 重复创建仍 409（保持原验收口径）。
- 决策引用的分析批次被作废 → 决策转 stale；基于新批次改判时，旧版本写入
  decision_revisions，Decision 行原地更新（revision + 1），仍只有一条当前决策。
"""
from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from src.constants.enums import VERDICT_LABELS, AnalysisStatus
from src.models.analysis_run import AnalysisRun
from src.models.decision import Decision
from src.models.decision_revision import DecisionRevision
from src.models.experiment import Experiment
from src.utils.errors import AppError
from src.utils.timeutil import iso_utc, utcnow


def serialize(row: Decision, experiment: Experiment | None = None) -> dict:
    return {
        "id": row.id,
        "experiment_id": row.experiment_id,
        "experiment_code": experiment.code if experiment else "",
        "experiment_name": experiment.name if experiment else "",
        "verdict": row.verdict,
        "verdict_label": VERDICT_LABELS.get(row.verdict, row.verdict),
        "rationale": row.rationale,
        "decided_by": row.decided_by,
        "decided_at": iso_utc(row.decided_at),
        "analysis_run_id": row.analysis_run_id,
        "analysis_run_no": row.analysis_run_no,
        "revision": row.revision,
        "status": row.status,
        "status_label": "有效" if row.status == "active" else "已过期",
        "stale_reason": row.stale_reason,
    }


def get_by_experiment(db: Session, experiment_id: int) -> Decision | None:
    return db.scalar(select(Decision).where(Decision.experiment_id == experiment_id))


def revisions(db: Session, experiment_id: int) -> list[dict]:
    rows = db.scalars(
        select(DecisionRevision)
        .where(DecisionRevision.experiment_id == experiment_id)
        .order_by(DecisionRevision.revision)
    ).all()
    return [
        {
            "id": row.id,
            "revision": row.revision,
            "verdict": row.verdict,
            "verdict_label": VERDICT_LABELS.get(row.verdict, row.verdict),
            "rationale": row.rationale,
            "decided_by": row.decided_by,
            "decided_at": iso_utc(row.decided_at),
            "analysis_run_id": row.analysis_run_id,
            "status": row.status,
            "invalidated_reason": row.invalidated_reason,
        }
        for row in rows
    ]


def mark_stale(db: Session, experiment_id: int, analysis_run_id: int, reason: str) -> int:
    """分析批次作废后，引用它的决策转 stale。返回影响的决策数。"""
    row = db.scalar(
        select(Decision).where(
            Decision.experiment_id == experiment_id,
            Decision.analysis_run_id == analysis_run_id,
            Decision.status == "active",
        )
    )
    if row is None:
        return 0
    row.status = "stale"
    row.stale_reason = reason
    for revision in db.scalars(
        select(DecisionRevision).where(
            DecisionRevision.decision_id == row.id,
            DecisionRevision.status == "active",
        )
    ).all():
        revision.status = "invalidated"
        revision.invalidated_reason = reason
    db.flush()
    return 1


def create(
    db: Session,
    experiment: Experiment,
    payload: dict,
    *,
    decided_by: str,
    analysis_run: AnalysisRun | None = None,
) -> Decision:
    existing = get_by_experiment(db, experiment.id)
    if existing is not None and existing.status == "active":
        raise AppError(
            "CONFLICT",
            409,
            {"reason": "该实验已有决策，自然键 experiment_id 唯一", "existing_id": existing.id},
        )
    if analysis_run is None:
        analysis_run = db.scalar(
            select(AnalysisRun)
            .where(
                AnalysisRun.experiment_id == experiment.id,
                AnalysisRun.status == AnalysisStatus.READY.value,
            )
            .order_by(AnalysisRun.run_no.desc())
        )
    if analysis_run is None:
        raise AppError(
            "VALIDATION",
            422,
            {"reason": "没有有效的分析批次，先完成对账并生成分析结论"},
        )
    if existing is not None:
        # 改判：旧版本留档（已有该 revision 的行就复用，避免重复），当前决策原地更新
        prior = db.scalar(
            select(DecisionRevision)
            .where(
                DecisionRevision.decision_id == existing.id,
                DecisionRevision.revision == existing.revision,
            )
            .order_by(DecisionRevision.id.desc())
        )
        if prior is None:
            db.add(
                DecisionRevision(
                    decision_id=existing.id,
                    experiment_id=experiment.id,
                    revision=existing.revision,
                    verdict=existing.verdict,
                    rationale=existing.rationale,
                    decided_by=existing.decided_by,
                    decided_at=existing.decided_at,
                    analysis_run_id=existing.analysis_run_id,
                    status="superseded",
                )
            )
        elif prior.status == "active":
            prior.status = "superseded"
        existing.revision = int(existing.revision or 1) + 1
        existing.status = "active"
        existing.stale_reason = None
        row = existing
    else:
        row = Decision(experiment_id=experiment.id, revision=1)
        db.add(row)
    row.verdict = payload["verdict"]
    row.rationale = payload["rationale"]
    row.decided_by = decided_by
    row.decided_at = utcnow()
    row.analysis_run_id = analysis_run.id
    row.analysis_run_no = analysis_run.run_no
    db.flush()
    db.add(
        DecisionRevision(
            decision_id=row.id,
            experiment_id=experiment.id,
            revision=row.revision,
            verdict=row.verdict,
            rationale=row.rationale,
            decided_by=row.decided_by,
            decided_at=row.decided_at,
            analysis_run_id=row.analysis_run_id,
            status="active",
        )
    )
    db.flush()
    return row


def list_decisions(
    db: Session,
    *,
    verdict: str | None = None,
    q: str | None = None,
    limit: int = 40,
    offset: int = 0,
) -> dict:
    stmt = select(Decision)
    count_stmt = select(func.count()).select_from(Decision)
    if verdict:
        stmt = stmt.where(Decision.verdict == verdict)
        count_stmt = count_stmt.where(Decision.verdict == verdict)
    if q:
        like = f"%{q.strip()}%"
        clause = or_(Decision.rationale.ilike(like), Decision.decided_by.ilike(like))
        stmt = stmt.where(clause)
        count_stmt = count_stmt.where(clause)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(Decision.decided_at.desc(), Decision.id.desc()).limit(limit).offset(offset)).all()
    experiments = {
        item.id: item
        for item in db.scalars(select(Experiment).where(Experiment.id.in_([row.experiment_id for row in rows]))).all()
    } if rows else {}
    return {
        "items": [serialize(row, experiments.get(row.experiment_id)) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
