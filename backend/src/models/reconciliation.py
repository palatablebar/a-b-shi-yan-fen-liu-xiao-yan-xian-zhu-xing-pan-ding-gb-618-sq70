"""对账结论：每个 (实验, 统计日, 变体, 渠道, 指标) 一行，记录状态与人工解释。

状态 complete / delayed / conflict / reconciled。conflict 只有填了解释并指定
采用侧后才转 reconciled；延迟格不允许单边凑合。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class ReconciliationCell(Base):
    __tablename__ = "reconciliation_cells"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id", "stat_date", "variant_code", "channel", "metric_key",
            name="uq_reconciliation_cell",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), index=True)
    stat_date: Mapped[date] = mapped_column(Date, index=True)
    variant_code: Mapped[str] = mapped_column(String(40), index=True)
    channel: Mapped[str] = mapped_column(String(20))
    metric_key: Mapped[str] = mapped_column(String(40), index=True)

    assignment_batch_id: Mapped[int | None] = mapped_column(ForeignKey("return_batches.id", ondelete="SET NULL"), nullable=True)
    metric_batch_id: Mapped[int | None] = mapped_column(ForeignKey("return_batches.id", ondelete="SET NULL"), nullable=True)
    assignment_samples: Mapped[int | None] = mapped_column(Integer, nullable=True)
    assignment_conversions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metric_samples: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metric_conversions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sample_delta: Mapped[int | None] = mapped_column(Integer, nullable=True)
    conversion_delta: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[str] = mapped_column(String(12), index=True)
    reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)  # assignment | metric
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    explained_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    explained_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    canonical_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    canonical_samples: Mapped[int | None] = mapped_column(Integer, nullable=True)
    canonical_conversions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usable: Mapped[bool] = mapped_column(default=False, index=True)
    auto_explainable: Mapped[bool] = mapped_column(default=False)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
