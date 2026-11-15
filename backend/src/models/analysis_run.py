"""分析批次：对账齐全后生成的一整套结论（分流校验 + 指标结果 + 决策依据）。

状态机：
  ready       —— 对账齐全/和解，结论可引用
  invalidated —— 源数据变化（新 revision / 新解释），旧结论作废，保留供追溯
  recomputing —— 正在按受影响统计日重算
批次按「参与统计日集合 + 源快照指纹」标识；指纹一致时重跑同一实验直接复用，
不做无意义的重复计算。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id", ondelete="CASCADE"), index=True)
    run_no: Mapped[int] = mapped_column(Integer, default=1, index=True)
    status: Mapped[str] = mapped_column(String(14), default="ready", index=True)
    snapshot_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    date_fingerprints: Mapped[dict] = mapped_column(JSON, default=dict)
    date_from: Mapped[str | None] = mapped_column(String(10), nullable=True)
    date_to: Mapped[str | None] = mapped_column(String(10), nullable=True)
    dates_used: Mapped[list] = mapped_column(JSON, default=list)
    method: Mapped[str] = mapped_column(String(16), default="bh")
    alpha: Mapped[float] = mapped_column(default=0.05)

    srm_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    metric_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    decision_basis: Mapped[dict] = mapped_column(JSON, default=dict)
    sources: Mapped[dict] = mapped_column(JSON, default=dict)

    invalidated_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    superseded_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    previous_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recomputed_dates: Mapped[list] = mapped_column(JSON, default=list)
    reused_dates: Mapped[list] = mapped_column(JSON, default=list)
    computed_by: Mapped[str] = mapped_column(String(160), default="system")
    computed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
