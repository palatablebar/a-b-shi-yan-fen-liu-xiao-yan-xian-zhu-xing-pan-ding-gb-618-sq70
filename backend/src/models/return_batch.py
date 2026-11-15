"""回传批次：实验平台（assignment）与指标库（metric）每天各回传一次原始数据。

幂等口径：同一 (实验, 来源, 统计日, 批次号) 重复提交且内容指纹一致 → 视为重试，
不新增、不重复累计；内容指纹变化 → 生成新 revision，旧 revision 置 superseded，
受影响统计日标脏并让旧分析结论作废。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class ReturnBatch(Base):
    __tablename__ = "return_batches"
    __table_args__ = (
        UniqueConstraint(
            "experiment_id", "source", "stat_date", "batch_no", "revision",
            name="uq_return_batch_revision",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(16), index=True)  # assignment | metric
    stat_date: Mapped[date] = mapped_column(Date, index=True)
    batch_no: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    rows_count: Mapped[int] = mapped_column(Integer, default=0)
    samples_total: Mapped[int] = mapped_column(Integer, default=0)
    conversions_total: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(12), default="active", index=True)  # active | superseded
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_by: Mapped[str] = mapped_column(String(160), default="system")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
