"""Celery 任务：心跳、每日 SRM 复算（对运行中的实验重算分流均衡）。"""
from __future__ import annotations

import time

from sqlalchemy import select

from src.config.celery_app import TASK_DAILY, TASK_HEARTBEAT, celery_app
from src.config.settings import settings
from src.utils.logger import get_logger

logger = get_logger("tasks")


def _redis():
    import redis

    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


@celery_app.task(name=TASK_HEARTBEAT)
def heartbeat() -> str:
    try:
        _redis().set(f"{settings.app_name}:worker:heartbeat", time.time())
        return "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("heartbeat failed: %s", exc)
        return "error"


@celery_app.task(name=TASK_DAILY)
def daily_srm_refresh() -> dict:
    """每天 07:40 重算分流健康度与每日回传对账。

    有回传批次的实验：先按当前 active 批次重算对账；只有全部统计日齐全才生成/刷新
    分析批次（compute 内部按统计日指纹比对，未变的日期不会重算，整体未变直接复用）。
    未齐全的实验保持阻断、不产出结论。手工录入样本的存量实验沿用旧 SRM 快照刷新。
    """
    from src.config.database import session_scope
    from src.models.experiment import Experiment
    from src.services import analysis_service, audit_service, canonical_service, reconciliation_service, srm_service
    from src.utils.errors import AppError

    done = 0
    reused = 0
    skipped = 0
    with session_scope() as db:
        experiments = db.scalars(select(Experiment).where(Experiment.status == "running")).all()
        for experiment in experiments:
            if canonical_service.has_returns(db, experiment.id):
                reconciliation_service.reconcile_all(db, experiment)
                try:
                    result = analysis_service.compute(
                        db,
                        experiment,
                        operator_email="scheduler@abcheck.io",
                        reason="定时重算受影响统计日",
                    )
                except AppError as exc:
                    logger.warning("daily analysis skip %s: %s", experiment.code, exc.code)
                    skipped += 1
                    continue
                if result.get("reused"):
                    reused += 1
                    continue
                done += 1
                audit_service.record(
                    db,
                    actor_email="scheduler@abcheck.io",
                    actor_role="admin",
                    action="analysis.run",
                    entity="analysis_run",
                    entity_id=result["id"],
                    detail={
                        "trigger": "schedule",
                        "run_no": result["run_no"],
                        "recomputed_dates": result["recomputed_dates"],
                    },
                    layer="worker",
                )
                continue
            try:
                result = srm_service.check(db, experiment)
            except AppError as exc:
                logger.warning("daily srm skip %s: %s", experiment.code, exc.code)
                skipped += 1
                continue
            done += 1
            audit_service.record(
                db,
                actor_email="scheduler@" + settings.app_name + ".io",
                actor_role="admin",
                action="srm.check",
                entity="experiment",
                entity_id=experiment.id,
                detail={"trigger": "schedule", "chi2": result["chi2"], "alarm": result["alarm"]},
                layer="worker",
            )
    logger.info("daily refresh finished: %s recomputed / %s reused / %s skipped", done, reused, skipped)
    return {"experiments": done, "reused": reused, "skipped": skipped}
