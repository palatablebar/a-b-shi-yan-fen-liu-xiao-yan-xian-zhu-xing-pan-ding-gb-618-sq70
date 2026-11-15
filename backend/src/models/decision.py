"""决策：一次实验一个结论（上线 / 继续观察 / 停止），出决策后不可重复创建（409）。

决策必须引用一个有效的分析批次；该批次作废后决策转 stale，需基于新批次改判。
改判历史落在 decision_revisions。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class Decision(Base):
    __tablename__ = "decisions"
    __table_args__ = (UniqueConstraint("experiment_id", name="uq_decision_experiment"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), index=True)
    verdict: Mapped[str] = mapped_column(String(16), index=True)
    rationale: Mapped[str] = mapped_column(Text, default="")
    decided_by: Mapped[str] = mapped_column(String(160), default="")
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    analysis_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    analysis_run_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active | stale
    stale_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

