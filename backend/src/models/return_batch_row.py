"""回传明细行：一个批次里的格子（变体 × 渠道 × 指标 × 日期）。

批次被 superseded 后其行保留（审计/导出可追到双方原始批次数），
但只有 active 批次的行参与对账。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import Date, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base


class ReturnBatchRow(Base):
    __tablename__ = "return_batch_rows"
    __table_args__ = (
        UniqueConstraint("batch_id", "variant_code", "channel", "metric_key", name="uq_return_batch_row"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("return_batches.id", ondelete="CASCADE"), index=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(16), index=True)
    stat_date: Mapped[date] = mapped_column(Date, index=True)
    variant_code: Mapped[str] = mapped_column(String(40), index=True)
    channel: Mapped[str] = mapped_column(String(20))
    metric_key: Mapped[str] = mapped_column(String(40), index=True)
    samples: Mapped[int] = mapped_column(Integer, default=0)
    conversions: Mapped[int] = mapped_column(Integer, default=0)
