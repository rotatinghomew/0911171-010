import unittest
from datetime import datetime, timezone

from digest.metrics import evaluate, evaluate_pivot, evaluate_with_comparison, parse_event_time


def row(rid, event_time, **fields):
    data = {"id": rid, "event_time": parse_event_time(event_time)}
    data.update(fields)
    return data


START = datetime(2026, 11, 1, 0, tzinfo=timezone.utc)
END = datetime(2026, 11, 2, 0, tzinfo=timezone.utc)

RATE = {
    "id": "cvr",
    "name": "转化率",
    "numerator": {"distinct": "user_id", "filter": {"ordered": True}},
    "denominator": {"distinct": "user_id"},
    "comparison": {"offset_days": 7},
}
AMOUNT = {
    "id": "amt",
    "name": "成交金额",
    "numerator": {"field": "amount", "filter": {"ordered": True}},
    "denominator": None,
}


class MetricCalculationTests(unittest.TestCase):
    def test_rate_uses_distinct_numerator_and_denominator(self):
        rows = [
            row("1", "2026-11-01T01:00Z", user_id="u1", ordered=True, amount=10.0),
            row("2", "2026-11-01T02:00Z", user_id="u1", ordered=True, amount=20.0),
            row("3", "2026-11-01T03:00Z", user_id="u2", ordered=False),
        ]
        result = evaluate(rows, RATE, START, END)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["numerator"], 1)  # u1 去重
        self.assertEqual(result["denominator"], 2)  # u1/u2 去重
        self.assertEqual(result["value"], 0.5)
        self.assertEqual(result["detail_row_ids"], ["1", "2", "3"])

    def test_missing_data_is_not_zero(self):
        result = evaluate([], RATE, START, END)
        self.assertEqual(result["status"], "no_data")
        self.assertIsNone(result["value"])

    def test_missing_measure_field_is_no_data(self):
        rows = [row("1", "2026-11-01T01:00Z", user_id=None, ordered=True)]
        result = evaluate(rows, RATE, START, END)
        # 行存在但去重键全空：分母观测到 0，属于零分母而非窗口无数据
        self.assertEqual(result["status"], "zero_denominator")
        self.assertIsNone(result["value"])

    def test_filter_matches_no_rows_is_no_data(self):
        rows = [row("1", "2026-11-01T01:00Z", user_id="u1", ordered=False)]
        definition = dict(RATE, numerator={"distinct": "user_id",
                                           "filter": {"ordered": True}},
                          denominator={"distinct": "user_id",
                                       "filter": {"channel": "web"}})
        result = evaluate(rows, definition, START, END)
        # 分母过滤条件匹配不到任何行：该口径下无观测，区别于零分母
        self.assertEqual(result["status"], "no_data")
        self.assertIsNone(result["value"])

    def test_zero_denominator_is_distinct_from_zero_value(self):
        rows = [row("1", "2026-11-01T01:00Z", user_id="u1", ordered=True,
                    visitor_id=None)]
        definition = dict(RATE, denominator={"distinct": "visitor_id"})
        result = evaluate(rows, definition, START, END)
        self.assertEqual(result["status"], "zero_denominator")
        self.assertIsNone(result["value"])
        self.assertEqual(result["numerator"], 1)
        self.assertEqual(result["denominator"], 0)

    def test_real_zero_is_reported_as_zero(self):
        rows = [
            row("1", "2026-11-01T01:00Z", user_id="u1", ordered=False),
            row("2", "2026-11-01T02:00Z", user_id="u2", ordered=False),
        ]
        result = evaluate(rows, RATE, START, END)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["value"], 0.0)
        self.assertEqual(result["numerator"], 0)

    def test_pure_amount_indicator_has_no_denominator(self):
        rows = [
            row("1", "2026-11-01T01:00Z", ordered=True, amount=10.0),
            row("2", "2026-11-01T02:00Z", ordered=False, amount=99.0),
            row("3", "2026-11-01T03:00Z", ordered=True, amount=None),
        ]
        result = evaluate(rows, AMOUNT, START, END)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["value"], 10.0)
        self.assertIsNone(result["denominator"])

    def test_window_is_half_open_and_tz_aware(self):
        rows = [
            row("at-start", "2026-11-01T00:00:00Z", user_id="u1", ordered=True),
            row("at-end", "2026-11-02T00:00:00Z", user_id="u2", ordered=True),
        ]
        result = evaluate(rows, RATE, START, END)
        self.assertEqual(result["detail_row_ids"], ["at-start"])

    def test_comparison_period_and_delta(self):
        rows = [
            row("cur", "2026-11-01T01:00Z", user_id="u1", ordered=True),
            row("cur2", "2026-11-01T02:00Z", user_id="u2", ordered=False),
            # 比较期为 7 天前：2026-10-25 00:00Z 至 2026-10-26 00:00Z
            row("old", "2026-10-25T01:00Z", user_id="u9", ordered=False),
            row("old2", "2026-10-25T02:00Z", user_id="u10", ordered=True),
        ]
        result = evaluate_with_comparison(rows, RATE, START, END)
        self.assertEqual(result["comparison"]["window_start"],
                         "2026-10-25T00:00:00Z")
        self.assertEqual(result["comparison"]["status"], "ok")
        self.assertEqual(result["comparison"]["value"], 0.5)
        self.assertEqual(result["delta"]["absolute"], 0.0)
        self.assertEqual(result["delta"]["percentage_points"], 0.0)

    def test_delta_is_null_when_comparison_has_no_data(self):
        result = evaluate_with_comparison(
            [row("cur", "2026-11-01T01:00Z", user_id="u1", ordered=True)],
            RATE, START, END)
        self.assertEqual(result["comparison"]["status"], "no_data")
        self.assertIsNone(result["delta"])

    def test_explicit_comparison_window_used_by_pivot_total_and_groups(self):
        # 比较期事件落在区域本地窗口（04:00Z 起），UTC 平移窗口（00:00Z 起）会漏掉
        rows = [
            row("cur", "2026-11-01T12:00Z", channel="web", user_id="u1", ordered=True),
            row("old", "2026-10-25T12:00Z", channel="web", user_id="u9", ordered=False),
        ]
        compare = (datetime(2026, 10, 25, 4, tzinfo=timezone.utc),
                   datetime(2026, 10, 26, 4, tzinfo=timezone.utc))
        pivot = evaluate_pivot(rows, RATE, START, END, "channel", compare)
        self.assertEqual(pivot["total"]["comparison"]["status"], "ok")
        self.assertEqual(pivot["total"]["comparison"]["value"], 0.0)
        self.assertEqual(pivot["groups"][0]["metric"]["comparison"]["status"], "ok")
        # 不传显式窗口时退回 UTC 平移（00:00Z 起），同样能看到 12:00Z 的事件
        fallback = evaluate_pivot(rows, RATE, START, END, "channel")
        self.assertEqual(fallback["total"]["comparison"]["status"], "ok")


class PivotTests(unittest.TestCase):
    def test_pivot_total_recomputes_on_pooled_rows_not_average_of_rates(self):
        rows = [
            # web: 1/2 = 0.5
            row("w1", "2026-11-01T01:00Z", channel="web", user_id="u1", ordered=True),
            row("w2", "2026-11-01T02:00Z", channel="web", user_id="u2", ordered=False),
            # app: u3/u4 成交，u1 重复触达未成交 → 去重后 2/3
            row("a1", "2026-11-01T03:00Z", channel="app", user_id="u3", ordered=True),
            row("a2", "2026-11-01T04:00Z", channel="app", user_id="u4", ordered=True),
            # u1 在 app 也有一条：跨渠道去重后合计分母为 4（u1/u2/u3/u4）
            row("a3", "2026-11-01T05:00Z", channel="app", user_id="u1", ordered=False),
        ]
        pivot = evaluate_pivot(rows, RATE, START, END, "channel")
        by_group = {g["dimension_value"]: g["metric"]["value"]
                    for g in pivot["groups"]}
        self.assertEqual(by_group["app"], round(2 / 3, 6))
        self.assertEqual(by_group["web"], 0.5)
        # 错误口径 (0.5 + 2/3) / 2 ≈ 0.5833 必须不出现
        total = pivot["total"]
        self.assertEqual(total["numerator"], 3)
        self.assertEqual(total["denominator"], 4)
        self.assertEqual(total["value"], 0.75)

    def test_pivot_group_with_no_events_is_absent_not_zero(self):
        rows = [row("w1", "2026-11-01T01:00Z", channel="web",
                    user_id="u1", ordered=True)]
        pivot = evaluate_pivot(rows, RATE, START, END, "channel")
        self.assertEqual([g["dimension_value"] for g in pivot["groups"]], ["web"])
        self.assertEqual(pivot["total"]["status"], "ok")

    def test_group_rate_zero_and_total_follow_same_definition(self):
        rows = [
            row("a1", "2026-11-01T03:00Z", channel="app", user_id="u3", ordered=False),
            row("a2", "2026-11-01T04:00Z", channel="app", user_id="u4", ordered=False),
        ]
        pivot = evaluate_pivot(rows, RATE, START, END, "channel")
        self.assertEqual(pivot["groups"][0]["metric"]["status"], "ok")
        self.assertEqual(pivot["groups"][0]["metric"]["value"], 0.0)
        self.assertEqual(pivot["total"]["value"], 0.0)


if __name__ == "__main__":
    unittest.main()
