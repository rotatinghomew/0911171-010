"""按区域业务日的本地调度，含夏令时跳变的明确安排。

区域配置 RegionSchedule：
- timezone        区域业务时区（IANA 名称，如 America/New_York）
- send_hour/minute 该区域期望的本地发送时刻
- window_offset_days 数据业务日相对发送日的偏移（1 表示发送前一业务日的数据）

DST 安排：
- 春季跳空（本地时刻不存在，如美国 02:00→03:00）：按 wall time 构造后由
  zoneinfo 映射，fold=0 取跳变后的时刻，即实际发送顺延一个跳变量；
- 秋季重叠（同一本地时刻出现两次）：fold=0 明确取第一次（DST 仍生效的一次）；
- 跳变日的业务日窗口仍以本地 00:00 到次日 00:00 为准，长度由真实时区决定
  （23 或 25 小时），不人为补齐，保证与区域实际业务日一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .metrics import iso_z


@dataclass(frozen=True)
class RegionSchedule:
    region: str
    timezone: str
    send_hour: int
    send_minute: int = 0
    window_offset_days: int = 1

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def business_window(schedule: RegionSchedule, business_day: date) -> tuple[datetime, datetime]:
    """业务日 [本地 00:00, 次日 00:00) 的 UTC 半开区间。"""
    zone = schedule.zone()
    start_local = datetime(business_day.year, business_day.month, business_day.day, tzinfo=zone)
    end_local = datetime.combine(business_day + timedelta(days=1),
                                 datetime.min.time(), tzinfo=zone)
    return start_local.astimezone(ZoneInfo("UTC")), end_local.astimezone(ZoneInfo("UTC"))


def comparison_window(schedule: RegionSchedule, business_day: date,
                      offset_days: int) -> tuple[datetime, datetime]:
    """比较期间按本地业务日平移，避免 DST 日按固定 24 小时错位。"""
    return business_window(schedule, business_day - timedelta(days=offset_days))


def business_day_for_send(schedule: RegionSchedule, send_date: date) -> date:
    return send_date - timedelta(days=schedule.window_offset_days)


def local_send_moment(schedule: RegionSchedule, send_date: date) -> datetime:
    """发送日的本地发送时刻，DST 两种异常都有明确安排。

    - 秋季重叠（同一 wall time 出现两次）：fold=0 明确取第一次（DST 仍生效）；
    - 春季跳空（wall time 不存在）：zoneinfo 不会自动顺延，这里显式把时刻
      后移一个跳变量，落在跳变后的同一相对位置，并在返回值上保留该安排。
    """
    zone = schedule.zone()
    moment = datetime(send_date.year, send_date.month, send_date.day,
                      schedule.send_hour, schedule.send_minute,
                      tzinfo=zone, fold=0)
    # PEP 495：先转 UTC 再转回本地，wall time 变了说明该时刻落在跳空区间；
    # 重叠时刻两种 fold 都真实存在，fold=0 即第一次，无需移动。
    roundtrip = moment.astimezone(ZoneInfo("UTC")).astimezone(zone)
    if roundtrip.replace(tzinfo=None) != moment.replace(tzinfo=None):
        gap = (moment.replace(fold=1).utcoffset()
               - moment.replace(fold=0).utcoffset())
        moment = moment + gap
    return moment


def next_run(schedule: RegionSchedule, after: datetime,
             max_days_ahead: int = 400) -> tuple[datetime, date]:
    """返回 after 之后的下一次 (UTC 发送时刻, 对应业务日)。

    逐本地日推进，跳过历史时刻；DST 跳空日由 local_send_moment 显式顺延。
    """
    if after.tzinfo is None:
        raise ValueError("after 必须带时区")
    after_utc = after.astimezone(ZoneInfo("UTC"))
    probe_date = after.astimezone(schedule.zone()).date()
    for _ in range(max_days_ahead + 1):
        moment_utc = local_send_moment(schedule, probe_date).astimezone(ZoneInfo("UTC"))
        if moment_utc > after_utc:
            return moment_utc, business_day_for_send(schedule, probe_date)
        probe_date += timedelta(days=1)
    raise RuntimeError("未在预期范围内找到下一次调度")


def upcoming_runs(schedule: RegionSchedule, after: datetime,
                  count: int) -> list[dict]:
    """列出接下来 count 次调度，用于跨日与 DST 的可重复核对。"""
    runs = []
    cursor = after
    for _ in range(count):
        moment, day = next_run(schedule, cursor)
        window_start, window_end = business_window(schedule, day)
        runs.append({
            "region": schedule.region,
            "timezone": schedule.timezone,
            "send_at": iso_z(moment),
            "business_day": day.isoformat(),
            "window_start": iso_z(window_start),
            "window_end": iso_z(window_end),
        })
        cursor = moment
    return runs
