"""回传、对账、分析批次的请求体。"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ReturnRowIn(BaseModel):
    variant_code: str = Field(min_length=1, max_length=40)
    channel: Literal["feed", "push", "search", "mp"]
    metric_key: Literal["cart_rate", "order_rate", "retention_d1", "click_rate", "search_success"]
    samples: int = Field(ge=0, le=50_000_000)
    conversions: int = Field(ge=0, le=50_000_000)

    @model_validator(mode="after")
    def _conversions_not_more_than_samples(self) -> "ReturnRowIn":
        if self.conversions > self.samples:
            raise ValueError("转化数不能大于分流数")
        return self


class ReturnBatchIn(BaseModel):
    source: Literal["assignment", "metric"]
    stat_date: date
    batch_no: str = Field(min_length=1, max_length=64)
    rows: list[ReturnRowIn] = Field(min_length=1, max_length=500)
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _no_duplicate_cells(self) -> "ReturnBatchIn":
        keys = [(row.variant_code, row.channel, row.metric_key) for row in self.rows]
        if len(set(keys)) != len(keys):
            raise ValueError("同一批次内 (变体, 渠道, 指标) 不能重复")
        return self


class ReconcileExplainIn(BaseModel):
    explanation: str = Field(min_length=4, max_length=1000)
    resolution: Literal["assignment", "metric"]


class AnalysisRunIn(BaseModel):
    method: Literal["bh", "bonferroni"] = "bh"
    alpha: float | None = Field(default=None, gt=0, lt=0.5)
    power: float | None = Field(default=None, gt=0, lt=1)
    date_from: date | None = None
    date_to: date | None = None

    @model_validator(mode="after")
    def _range_ok(self) -> "AnalysisRunIn":
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from 不能晚于 date_to")
        return self


class ReconcileQueryIn(BaseModel):
    date_from: date | None = None
    date_to: date | None = None
