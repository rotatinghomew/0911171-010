"""指标定义与统一计算口径。

指标定义是可 JSON 序列化的 dict（消息里带的就是定义本身，便于核对）：

{
  "id": "conversion_rate_v1",
  "name": "转化率",
  "numerator":   {"distinct": "user_id", "filter": {"orders": 1}},
  "denominator": {"distinct": "user_id"},
  "comparison":  {"offset_days": 7}
}

度量（numerator / denominator）支持三种形态：
- {"field": "amount"}                对字段求和（None 不参与）
- {"distinct": "user_id"}            对去重对象计数
- {"count": true}                    明细行数
均可带 "filter"：对行做等值过滤，例如 {"channel": "web"}。
denominator 为 null 表示纯量指标（只有分子）。

计算结果用 status 显式表达口径状态，绝不把缺失或零分母伪装成 0：
- "ok"               正常出值
- "no_data"          窗口内无明细 / 度量字段全部缺失，value 为 null
- "zero_denominator" 分母为 0，value 为 null
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

VALUE_SCALE = 6
PPT_SCALE = 4


def parse_event_time(value: str | datetime) -> datetime:
    """把 ISO 8601 时间统一成带时区的 UTC datetime。"""
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(f"事件时间缺少时区信息: {value!r}")
    return moment.astimezone(timezone.utc)


def iso_z(moment: datetime) -> str:
    """UTC 时间的稳定文本表示。"""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _matches_filter(row: dict, filters: dict | None) -> bool:
    if not filters:
        return True
    return all(row.get(key) == expected for key, expected in filters.items())


def _measure(rows: list[dict], spec: dict | None) -> tuple[float | int | None, bool]:
    """返回 (度量值, 是否实际观测到数据)。

    去重/求和时跳过空值（None）的行；只要窗口内存在任一非空观测即为已观测，
    个别行缺字段不能让整个指标伪装成无数据。
    """
    if spec is None:
        return None, True
    selected = [row for row in rows if _matches_filter(row, spec.get("filter"))]
    if "field" in spec:
        field = spec["field"]
        values = [float(row[field]) for row in selected if row.get(field) is not None]
        return (sum(values), bool(values))
    if "distinct" in spec:
        field = spec["distinct"]
        distinct = {row[field] for row in selected if row.get(field) is not None}
        # selected 非空但去重键全空 -> 观测到 0 个对象（zero_denominator）；
        # selected 为空 -> 该口径下根本没有行（no_data）
        return (len(distinct), bool(selected))
    if spec.get("count"):
        return (len(selected), bool(selected))
    raise ValueError(f"无法识别的度量定义: {spec!r}")


def _empty_result(definition: dict, window_start: datetime, window_end: datetime,
                  detail_row_ids: list[str], status: str) -> dict:
    return {
        "metric_id": definition["id"],
        "metric_name": definition.get("name", definition["id"]),
        "status": status,
        "value": None,
        "numerator": None,
        "denominator": None,
        "window_start": iso_z(window_start),
        "window_end": iso_z(window_end),
        "detail_row_ids": detail_row_ids,
    }


def evaluate(rows: Iterable[dict], definition: dict,
             window_start: datetime, window_end: datetime) -> dict:
    """按同一口径在给定窗口上计算单个指标。"""
    in_window = [row for row in rows
                 if window_start <= row["event_time"] < window_end]
    detail_row_ids = sorted(row["id"] for row in in_window)
    if not in_window:
        return _empty_result(definition, window_start, window_end,
                             detail_row_ids, "no_data")

    numerator, num_seen = _measure(in_window, definition.get("numerator"))
    denominator, den_seen = _measure(in_window, definition.get("denominator"))
    # 无观测的判定：分母口径下没有任何匹配行；纯量指标则看分子。
    # 有行但去重键全空（denominator==0 且 den_seen）属于零分母，不是缺数据。
    effectively_seen = den_seen if definition.get("denominator") is not None else num_seen
    if not effectively_seen:
        return _empty_result(definition, window_start, window_end,
                             detail_row_ids, "no_data")
    if definition.get("denominator") is not None and denominator == 0:
        result = _empty_result(definition, window_start, window_end,
                               detail_row_ids, "zero_denominator")
        result["numerator"] = numerator
        result["denominator"] = denominator
        return result

    if definition.get("denominator") is None:
        value = numerator
    else:
        value = round(numerator / denominator, VALUE_SCALE)
    return {
        "metric_id": definition["id"],
        "metric_name": definition.get("name", definition["id"]),
        "status": "ok",
        "value": value,
        "numerator": numerator,
        "denominator": denominator,
        "window_start": iso_z(window_start),
        "window_end": iso_z(window_end),
        "detail_row_ids": detail_row_ids,
    }


def _comparison_window(definition: dict, window_start: datetime,
                       window_end: datetime,
                       explicit_compare: tuple[datetime, datetime] | None = None
                       ) -> tuple[datetime, datetime] | None:
    if explicit_compare is not None:
        return explicit_compare
    comparison = definition.get("comparison")
    if not comparison:
        return None
    offset = timedelta(days=comparison["offset_days"])
    return window_start - offset, window_end - offset


def _evaluate_comparison(rows, definition, explicit_compare,
                         window_start, window_end):
    compare = _comparison_window(definition, window_start, window_end,
                                 explicit_compare)
    if compare is None:
        return None
    return evaluate(rows, definition, compare[0], compare[1])


def _delta(current: dict, previous: dict | None) -> dict | None:
    if previous is None or current["status"] != "ok" or previous["status"] != "ok":
        return None
    absolute = round(current["value"] - previous["value"], VALUE_SCALE)
    return {"absolute": absolute,
            "percentage_points": round(absolute * 100, PPT_SCALE)}


def evaluate_with_comparison(rows: Iterable[dict], definition: dict,
                             window_start: datetime, window_end: datetime,
                             comparison_window: tuple[datetime, datetime] | None = None) -> dict:
    """计算指标并附比较期间结果；任一期不可比时 delta 为 null，不编造数字。

    comparison_window 由调度层按区域本地业务日给出时优先使用，
    否则退回按定义偏移的固定时长平移。
    """
    result = evaluate(rows, definition, window_start, window_end)
    previous = _evaluate_comparison(rows, definition, comparison_window,
                                    window_start, window_end)
    result["comparison"] = previous
    result["delta"] = _delta(result, previous)
    return result


def evaluate_pivot(rows: Iterable[dict], definition: dict,
                   window_start: datetime, window_end: datetime,
                   dimension: str,
                   comparison_window: tuple[datetime, datetime] | None = None) -> dict:
    """透视到维度后，合计值仍用同一口径在合并明细上重算。

    各分组的比率不能直接平均：合计始终是「合并分子 / 合并分母」，
    去重对象也在合并后的明细集合上重新去重。比较期在全量行上独立评估，
    不能只拿当前窗口的子集去查比较窗口。
    """
    rows = list(rows)
    in_window = [row for row in rows
                 if window_start <= row["event_time"] < window_end]
    grouped: dict[Any, list[dict]] = defaultdict(list)
    for row in in_window:
        grouped[row.get(dimension)].append(row)

    groups = []
    for value in sorted(grouped, key=lambda item: ("" if item is None else str(item))):
        current = evaluate(grouped[value], definition, window_start, window_end)
        previous = _evaluate_comparison(rows, definition, comparison_window,
                                        window_start, window_end)
        current["comparison"] = previous
        current["delta"] = _delta(current, previous)
        groups.append({
            "dimension": dimension,
            "dimension_value": value,
            "metric": current,
        })
    total_current = evaluate(in_window, definition, window_start, window_end)
    total_previous = _evaluate_comparison(rows, definition, comparison_window,
                                          window_start, window_end)
    total_current["comparison"] = total_previous
    total_current["delta"] = _delta(total_current, total_previous)
    return {
        "metric_id": definition["id"],
        "dimension": dimension,
        "groups": groups,
        "total": total_current,
    }
