import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from digest.scheduler import (
    RegionSchedule,
    business_day_for_send,
    business_window,
    local_send_moment,
    next_run,
    upcoming_runs,
)

NY = RegionSchedule("us-east", "America/New_York", 9, 0, 1)
SH = RegionSchedule("china", "Asia/Shanghai", 8, 30, 1)
BERLIN = RegionSchedule("eu", "Europe/Berlin", 2, 30, 1)


class BusinessWindowTests(unittest.TestCase):
    def test_business_window_uses_local_midnight_in_utc(self):
        start, end = business_window(NY, date(2026, 11, 2))
        # 11 月 2 日美东为 UTC-5（回退发生在 11 月 1 日凌晨）
        self.assertEqual(start, datetime(2026, 11, 2, 5, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 11, 3, 5, tzinfo=timezone.utc))

    def test_business_window_on_dst_fall_back_day_is_25_hours(self):
        # 2026-11-01 是美国回退日（02:00 -> 01:00）
        start, end = business_window(NY, date(2026, 11, 1))
        self.assertEqual((end - start).total_seconds(), 25 * 3600)

    def test_business_window_on_dst_spring_forward_day_is_23_hours(self):
        # 2026-03-08 是美国跳空日（02:00 -> 03:00）
        start, end = business_window(NY, date(2026, 3, 8))
        self.assertEqual((end - start).total_seconds(), 23 * 3600)

    def test_window_offset_maps_send_date_to_previous_business_day(self):
        self.assertEqual(business_day_for_send(NY, date(2026, 11, 2)),
                         date(2026, 11, 1))

    def test_no_dst_region_keeps_fixed_offset(self):
        start, end = business_window(SH, date(2026, 11, 1))
        self.assertEqual(start, datetime(2026, 10, 31, 16, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 11, 1, 16, tzinfo=timezone.utc))


class NextRunTests(unittest.TestCase):
    def test_next_run_is_after_given_moment_in_utc(self):
        after = datetime(2026, 11, 2, 13, tzinfo=timezone.utc)  # 美东 08:00
        moment, day = next_run(NY, after)
        self.assertEqual(moment, datetime(2026, 11, 2, 14, tzinfo=timezone.utc))
        self.assertEqual(day, date(2026, 11, 1))

    def test_exactly_at_send_time_moves_to_next_day(self):
        at_send = datetime(2026, 11, 2, 14, tzinfo=timezone.utc)
        moment, day = next_run(NY, at_send)
        self.assertEqual(moment, datetime(2026, 11, 3, 14, tzinfo=timezone.utc))
        self.assertEqual(day, date(2026, 11, 2))

    def test_spring_forward_gap_send_moment_shifts_by_one_hour(self):
        # 本地 09:00 不受跳空影响；改用 Berlin 02:30 落在跳空小时内验证顺延
        # Berlin 2026-03-29 02:00->03:00，02:30 不存在，显式顺延到 03:30 CEST
        moment = local_send_moment(BERLIN, date(2026, 3, 29))
        self.assertEqual(moment.replace(tzinfo=None),
                         datetime(2026, 3, 29, 3, 30))
        self.assertEqual(moment.astimezone(ZoneInfo("UTC")),
                         datetime(2026, 3, 29, 1, 30, tzinfo=timezone.utc))
        self.assertEqual(moment.tzname(), "CEST")

    def test_fall_back_overlap_uses_first_occurrence(self):
        # New York 2026-11-01 本地 01:30 出现两次；fold=0 取 EDT（第一次），
        # fold=1 取 EST（第二次），两者相差 1 小时
        zone = ZoneInfo("America/New_York")
        first = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
        second = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
        self.assertEqual(first.tzname(), "EDT")
        self.assertEqual(second.tzname(), "EST")
        self.assertEqual(first.utcoffset() - second.utcoffset(),
                         datetime(2026, 11, 1, 1, 30) - datetime(2026, 11, 1, 0, 30))

    def test_upcoming_runs_cross_dst_with_correct_utc_offsets(self):
        runs = upcoming_runs(NY, datetime(2026, 3, 6, 0, tzinfo=timezone.utc), 4)
        send_ats = [run["send_at"] for run in runs]
        # 3/6、3/7 美东 EST 09:00 -> 14:00Z；3/8 起 EDT 09:00 -> 13:00Z
        self.assertEqual(send_ats, [
            "2026-03-06T14:00:00Z",
            "2026-03-07T14:00:00Z",
            "2026-03-08T13:00:00Z",
            "2026-03-09T13:00:00Z",
        ])
        self.assertEqual([run["business_day"] for run in runs],
                         ["2026-03-05", "2026-03-06", "2026-03-07", "2026-03-08"])

    def test_naive_moment_is_rejected(self):
        with self.assertRaises(ValueError):
            next_run(NY, datetime(2026, 11, 2, 13))


if __name__ == "__main__":
    unittest.main()
