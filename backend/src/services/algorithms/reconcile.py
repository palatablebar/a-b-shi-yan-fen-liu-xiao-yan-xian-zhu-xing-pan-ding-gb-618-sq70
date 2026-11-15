"""两份回传对账（纯函数）：实验平台分流数 vs 指标库转化数。

输入是同一统计日、同一实验下两侧的「格」：
  assignment 侧（实验平台）: {"variant_code","channel","metric_key","samples","conversions"}
  metric 侧（指标库）     : 同结构
对账键 = (变体, 渠道, 指标)，外层还有 统计日（由调用方传入）。

三种结论严格分开：
  complete   齐全：两侧都有，且 samples / conversions 完全一致
  delayed    延迟：只有一侧有这一格（另一侧还没回传 / 漏跑），不能拿去算
  conflict   冲突：两侧都有但数值不同
  reconciled 已和解：冲突已由人工填写解释并指定采用的侧（assignment / metric）

设计约束：
- 只做纯计算，不碰数据库；同样输入两次逐位一致。
- 延迟格不允许「先拿一侧凑合」，必须等另一侧回传。
- 冲突格只有「有解释 + 指定采用侧」才算和解。
"""
from __future__ import annotations

import hashlib
import json

SOURCE_ASSIGNMENT = "assignment"
SOURCE_METRIC = "metric"

STATUS_COMPLETE = "complete"
STATUS_DELAYED = "delayed"
STATUS_CONFLICT = "conflict"
STATUS_RECONCILED = "reconciled"

DAY_COMPLETE = "complete"
DAY_RECONCILED = "reconciled"
DAY_CONFLICT = "conflict"
DAY_DELAYED = "delayed"

REASON_MISSING_ASSIGNMENT = "missing_assignment_side"
REASON_MISSING_METRIC = "missing_metric_side"
REASON_SAMPLES = "samples_differ"
REASON_CONVERSIONS = "conversions_differ"
REASON_BOTH = "samples_and_conversions_differ"
REASON_INVALID = "invalid_counts"

REASON_LABELS = {
    REASON_MISSING_ASSIGNMENT: "实验平台该格未回传",
    REASON_MISSING_METRIC: "指标库该格未回传",
    REASON_SAMPLES: "分流数不一致",
    REASON_CONVERSIONS: "转化数不一致",
    REASON_BOTH: "分流数与转化数都不一致",
    REASON_INVALID: "转化数超过分流数",
}

RESOLUTIONS = (SOURCE_ASSIGNMENT, SOURCE_METRIC)


def cell_key(row: dict) -> tuple[str, str, str]:
    return (
        str(row.get("variant_code") or ""),
        str(row.get("channel") or ""),
        str(row.get("metric_key") or ""),
    )


def rows_map(rows: list[dict]) -> dict[tuple[str, str, str], dict]:
    """把行列表变成以对账键为索引的 dict；重复键保留最后一个（调用方应先校验）。"""
    return {cell_key(row): row for row in rows if isinstance(row, dict)}


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def fingerprint_rows(rows: list[dict]) -> str:
    """批次指纹：规范化排序后取 SHA-256，用于识别「同批次号重试但内容变了」。"""
    canonical = sorted(
        (
            {
                "variant_code": cell_key(row)[0],
                "channel": cell_key(row)[1],
                "metric_key": cell_key(row)[2],
                "samples": _int(row.get("samples")),
                "conversions": _int(row.get("conversions")),
            }
            for row in rows
            if isinstance(row, dict)
        ),
        key=lambda item: (item["variant_code"], item["channel"], item["metric_key"]),
    )
    text = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _explanation_map(explanations: list[dict] | dict | None) -> dict[tuple[str, str, str], dict]:
    if not explanations:
        return {}
    if isinstance(explanations, dict):
        return {key: value for key, value in explanations.items() if isinstance(value, dict)}
    result: dict[tuple[str, str, str], dict] = {}
    for item in explanations:
        if isinstance(item, dict):
            result[cell_key(item)] = item
    return result


def reconcile_cells(
    assignment_rows: list[dict],
    metric_rows: list[dict],
    *,
    explanations: list[dict] | dict | None = None,
) -> dict:
    """对账一天的格子，返回逐格结论 + 当天汇总。

    explanations 里每项：{"variant_code","channel","metric_key","explanation","resolution"}
    resolution ∈ assignment / metric；没有解释或采用侧非法的冲突仍算未和解。
    """
    left = rows_map(assignment_rows)
    right = rows_map(metric_rows)
    explained = _explanation_map(explanations)

    keys = sorted(set(left) | set(right), key=lambda item: (item[0], item[1], item[2]))
    cells: list[dict] = []
    for key in keys:
        variant_code, channel, metric_key = key
        a_row = left.get(key)
        m_row = right.get(key)
        cell = {
            "variant_code": variant_code,
            "channel": channel,
            "metric_key": metric_key,
            "assignment_present": a_row is not None,
            "metric_present": m_row is not None,
            "assignment_samples": _int(a_row.get("samples")) if a_row else None,
            "assignment_conversions": _int(a_row.get("conversions")) if a_row else None,
            "metric_samples": _int(m_row.get("samples")) if m_row else None,
            "metric_conversions": _int(m_row.get("conversions")) if m_row else None,
            "sample_delta": None,
            "conversion_delta": None,
            "reason": None,
            "status": None,
            "resolution": None,
            "explanation": None,
            "explained_by": None,
            "explained_at": None,
            "canonical_source": None,
            "canonical_samples": None,
            "canonical_conversions": None,
            "usable": False,
            "auto_explainable": False,
        }

        note = explained.get(key) or {}
        if note.get("explanation"):
            cell["explanation"] = str(note["explanation"])
            cell["explained_by"] = note.get("explained_by")
            cell["explained_at"] = note.get("explained_at")

        if a_row is not None and m_row is not None:
            a_s, a_c = _int(a_row.get("samples")), _int(a_row.get("conversions"))
            m_s, m_c = _int(m_row.get("samples")), _int(m_row.get("conversions"))
            cell["sample_delta"] = m_s - a_s
            cell["conversion_delta"] = m_c - a_c
            if a_c > a_s or m_c > m_s or a_s < 0 or m_s < 0:
                cell["status"] = STATUS_CONFLICT
                cell["reason"] = REASON_INVALID
            elif a_s == m_s and a_c == m_c:
                cell["status"] = STATUS_COMPLETE
                cell["canonical_source"] = SOURCE_ASSIGNMENT
                cell["canonical_samples"] = a_s
                cell["canonical_conversions"] = a_c
                cell["usable"] = True
                cell["auto_explainable"] = True
            else:
                cell["status"] = STATUS_CONFLICT
                if a_s != m_s and a_c != m_c:
                    cell["reason"] = REASON_BOTH
                elif a_s != m_s:
                    cell["reason"] = REASON_SAMPLES
                else:
                    cell["reason"] = REASON_CONVERSIONS
                resolution = note.get("resolution") if isinstance(note.get("resolution"), str) else None
                if cell["explanation"] and resolution in RESOLUTIONS:
                    cell["status"] = STATUS_RECONCILED
                    cell["resolution"] = resolution
                    cell["canonical_source"] = resolution
                    if resolution == SOURCE_METRIC:
                        cell["canonical_samples"] = m_s
                        cell["canonical_conversions"] = m_c
                    else:
                        cell["canonical_samples"] = a_s
                        cell["canonical_conversions"] = a_c
                    cell["usable"] = True
        elif a_row is not None:
            cell["status"] = STATUS_DELAYED
            cell["reason"] = REASON_MISSING_METRIC
            cell["sample_delta"] = None
            cell["conversion_delta"] = None
        else:
            cell["status"] = STATUS_DELAYED
            cell["reason"] = REASON_MISSING_ASSIGNMENT
        cells.append(cell)

    complete = sum(1 for cell in cells if cell["status"] == STATUS_COMPLETE)
    reconciled = sum(1 for cell in cells if cell["status"] == STATUS_RECONCILED)
    delayed = sum(1 for cell in cells if cell["status"] == STATUS_DELAYED)
    conflict = sum(1 for cell in cells if cell["status"] == STATUS_CONFLICT)
    if not cells:
        day_status = DAY_DELAYED
    elif delayed:
        day_status = DAY_DELAYED
    elif conflict:
        day_status = DAY_CONFLICT
    elif reconciled:
        day_status = DAY_RECONCILED
    else:
        day_status = DAY_COMPLETE
    ready = day_status in (DAY_COMPLETE, DAY_RECONCILED)
    return {
        "status": day_status,
        "ready": ready,
        "cells_total": len(cells),
        "complete": complete,
        "reconciled": reconciled,
        "delayed": delayed,
        "conflict": conflict,
        "usable": complete + reconciled,
        "cells": cells,
        "blocking": _blocking(cells),
    }


def _blocking(cells: list[dict]) -> list[dict]:
    """blockers：为什么这一天还不能进分析。逐格列出，便于页面/导出解释。"""
    blockers: list[dict] = []
    for cell in cells:
        if cell["status"] in (STATUS_COMPLETE, STATUS_RECONCILED):
            continue
        blockers.append(
            {
                "variant_code": cell["variant_code"],
                "channel": cell["channel"],
                "metric_key": cell["metric_key"],
                "status": cell["status"],
                "reason": cell["reason"],
                "reason_label": REASON_LABELS.get(cell["reason"], cell["reason"]),
            }
        )
    return blockers


def aggregate_snapshot(date_snapshot: dict) -> dict:
    """把按日快照合并成算法输入。

    date_snapshot: {"YYYY-MM-DD": {variant: {metric: {"n": int, "c": int}}}}
    返回: {variant: {metric: {"n": int, "c": int}}}，按变体/指标排序后逐位稳定。
    """
    merged: dict[str, dict[str, dict[str, int]]] = {}
    for day in sorted(date_snapshot or {}):
        variants = date_snapshot[day] or {}
        for variant_code in sorted(variants):
            metrics = variants[variant_code] or {}
            for metric_key in sorted(metrics):
                cell = metrics[metric_key] or {}
                target = merged.setdefault(variant_code, {}).setdefault(metric_key, {"n": 0, "c": 0})
                target["n"] += _int(cell.get("n"))
                target["c"] += _int(cell.get("c"))
    return merged


def snapshot_fingerprint(snapshot: dict) -> str:
    """分析快照指纹：用于识别「源数据没变」与「只重算受影响日期」。"""
    canonical = {
        str(day): {
            str(variant): {
                str(metric): {"n": _int(cell.get("n")), "c": _int(cell.get("c"))}
                for metric, cell in sorted((metrics or {}).items())
            }
            for variant, metrics in sorted((variants or {}).items())
        }
        for day, variants in sorted((snapshot or {}).items())
    }
    text = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_snapshot(rows: list[dict]) -> dict:
    """把按日期的行聚合成 date_snapshot：{day: {variant: {metric: {n, c}}}}。"""
    snapshot: dict[str, dict[str, dict[str, dict[str, int]]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = str(row.get("stat_date") or "")
        variant_code = str(row.get("variant_code") or "")
        metric_key = str(row.get("metric_key") or "")
        if not day or not variant_code or not metric_key:
            continue
        cell = snapshot.setdefault(day, {}).setdefault(variant_code, {}).setdefault(metric_key, {"n": 0, "c": 0})
        cell["n"] += _int(row.get("samples"))
        cell["c"] += _int(row.get("conversions"))
    return {
        day: {
            variant: {metric: {"n": _int(cell["n"]), "c": _int(cell["c"])} for metric, cell in sorted(metrics.items())}
            for variant, metrics in sorted(variants.items())
        }
        for day, variants in sorted(snapshot.items())
    }
