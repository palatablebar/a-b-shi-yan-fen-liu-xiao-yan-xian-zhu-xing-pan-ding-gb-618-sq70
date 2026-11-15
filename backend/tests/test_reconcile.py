"""对账算法（纯函数）：齐全 / 延迟 / 冲突 / 已和解 分开处理。"""
from __future__ import annotations

from src.services.algorithms import reconcile


def _row(variant, channel, metric, samples, conversions):
    return {
        "variant_code": variant,
        "channel": channel,
        "metric_key": metric,
        "samples": samples,
        "conversions": conversions,
    }


def test_complete_when_both_sides_match():
    rows = [_row("control", "feed", "order_rate", 1200, 121)]
    result = reconcile.reconcile_cells(rows, [dict(rows[0])])
    assert result["status"] == "complete"
    assert result["ready"] is True
    assert result["cells"][0]["status"] == "complete"
    assert result["cells"][0]["canonical_samples"] == 1200
    assert result["cells"][0]["canonical_source"] == "assignment"
    assert result["cells"][0]["usable"] is True


def test_delayed_when_one_side_missing_and_not_usable():
    left = [_row("control", "feed", "order_rate", 1200, 121)]
    result = reconcile.reconcile_cells(left, [])
    assert result["status"] == "delayed"
    assert result["ready"] is False
    cell = result["cells"][0]
    assert cell["status"] == "delayed"
    assert cell["reason"] == reconcile.REASON_MISSING_METRIC
    assert cell["usable"] is False
    assert cell["canonical_samples"] is None
    # 延迟格不能拿单侧数据凑合
    assert result["blocking"][0]["status"] == "delayed"


def test_conflict_requires_explanation_and_resolution():
    left = [_row("control", "feed", "order_rate", 1200, 121)]
    right = [_row("control", "feed", "order_rate", 1200, 118)]
    result = reconcile.reconcile_cells(left, right)
    assert result["status"] == "conflict"
    assert result["ready"] is False
    cell = result["cells"][0]
    assert cell["status"] == "conflict"
    assert cell["reason"] == reconcile.REASON_CONVERSIONS
    assert cell["conversion_delta"] == -3
    assert cell["usable"] is False


def test_conflict_becomes_reconciled_with_resolution():
    left = [_row("control", "feed", "order_rate", 1200, 121)]
    right = [_row("control", "feed", "order_rate", 1200, 118)]
    result = reconcile.reconcile_cells(
        left,
        right,
        explanations=[
            {
                "variant_code": "control",
                "channel": "feed",
                "metric_key": "order_rate",
                "explanation": "指标库去重了 3 笔重复上报",
                "resolution": "metric",
            }
        ],
    )
    assert result["status"] == "reconciled"
    assert result["ready"] is True
    cell = result["cells"][0]
    assert cell["status"] == "reconciled"
    assert cell["canonical_source"] == "metric"
    assert cell["canonical_conversions"] == 118
    assert cell["usable"] is True


def test_explanation_without_valid_resolution_stays_conflict():
    left = [_row("control", "feed", "order_rate", 1200, 121)]
    right = [_row("control", "feed", "order_rate", 1200, 118)]
    result = reconcile.reconcile_cells(
        left,
        right,
        explanations=[
            {
                "variant_code": "control",
                "channel": "feed",
                "metric_key": "order_rate",
                "explanation": "说不清",
                "resolution": "whatever",
            }
        ],
    )
    assert result["status"] == "conflict"


def test_day_with_any_delayed_cell_is_blocked_even_if_rest_are_clean():
    left = [
        _row("control", "feed", "order_rate", 100, 10),
        _row("control", "push", "order_rate", 100, 11),
    ]
    right = [_row("control", "feed", "order_rate", 100, 10)]
    result = reconcile.reconcile_cells(left, right)
    assert result["status"] == "delayed"
    assert result["complete"] == 1
    assert result["delayed"] == 1
    assert result["ready"] is False
    assert len(result["blocking"]) == 1


def test_mixed_complete_and_reconciled_is_ready():
    left = [
        _row("control", "feed", "order_rate", 100, 10),
        _row("control", "push", "order_rate", 200, 20),
    ]
    right = [
        _row("control", "feed", "order_rate", 100, 10),
        _row("control", "push", "order_rate", 200, 19),
    ]
    result = reconcile.reconcile_cells(
        left,
        right,
        explanations=[
            {"variant_code": "control", "channel": "push", "metric_key": "order_rate", "explanation": "晚到重算", "resolution": "metric"},
        ],
    )
    assert result["status"] == "reconciled"
    assert result["ready"] is True
    assert result["reconciled"] == 1
    assert result["complete"] == 1


def test_invalid_counts_are_conflicts_even_if_both_sides_match():
    bad = [_row("control", "feed", "order_rate", 100, 120)]
    result = reconcile.reconcile_cells(bad, [dict(bad[0])])
    assert result["status"] == "conflict"
    assert result["cells"][0]["reason"] == reconcile.REASON_INVALID


def test_fingerprint_is_order_independent_and_change_sensitive():
    rows_a = [
        _row("control", "feed", "order_rate", 100, 10),
        _row("treat", "push", "order_rate", 200, 20),
    ]
    rows_b = list(reversed(rows_a))
    assert reconcile.fingerprint_rows(rows_a) == reconcile.fingerprint_rows(rows_b)
    rows_c = [_row("control", "feed", "order_rate", 101, 10), rows_a[1]]
    assert reconcile.fingerprint_rows(rows_a) != reconcile.fingerprint_rows(rows_c)


def test_snapshot_aggregation_and_fingerprint():
    rows = [
        {"stat_date": "2026-06-01", "variant_code": "control", "metric_key": "order_rate", "samples": 100, "conversions": 10},
        {"stat_date": "2026-06-01", "variant_code": "control", "metric_key": "order_rate", "samples": 50, "conversions": 5},
        {"stat_date": "2026-06-02", "variant_code": "control", "metric_key": "order_rate", "samples": 30, "conversions": 3},
    ]
    snapshot = reconcile.build_snapshot(rows)
    assert snapshot["2026-06-01"]["control"]["order_rate"] == {"n": 150, "c": 15}
    assert snapshot["2026-06-02"]["control"]["order_rate"] == {"n": 30, "c": 3}
    fp = reconcile.snapshot_fingerprint(snapshot)
    assert fp == reconcile.snapshot_fingerprint(reconcile.build_snapshot(list(reversed(rows))))
    changed = reconcile.build_snapshot(rows[:-1])
    assert reconcile.snapshot_fingerprint(changed) != fp


def test_reconcile_is_deterministic():
    left = [_row("control", "feed", "order_rate", 1200, 121)]
    right = [_row("control", "feed", "order_rate", 1200, 118)]
    first = reconcile.reconcile_cells(left, right)
    second = reconcile.reconcile_cells(left, right)
    assert first == second
