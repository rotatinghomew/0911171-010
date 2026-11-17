"""可重复的合成样例：三个区域、两渠道、去重转化与补数事件。

所有时间均显式带时区；received_at 模拟数据进入本地仓的时刻，
late 行的 received_at 晚于首次发送的截止点，用于演示补数更正。
"""
from __future__ import annotations

from .scheduler import RegionSchedule

SCHEDULES = {
    "us-east": RegionSchedule("us-east", "America/New_York", 9, 0, 1),
    "china": RegionSchedule("china", "Asia/Shanghai", 8, 30, 1),
    "eu": RegionSchedule("eu", "Europe/Berlin", 7, 0, 1),
}

ROSTER = {
    "us-east": ["us-lead@example.invalid", "us-finance@example.invalid"],
    "china": ["cn-lead@example.invalid"],
    "eu": ["eu-lead@example.invalid"],
}

# 指标定义（随消息一起送达，负责人可直接核对口径）
CONVERSION_RATE = {
    "id": "conversion_rate_v1",
    "name": "用户转化率",
    "numerator": {"distinct": "user_id", "filter": {"ordered": True}},
    "denominator": {"distinct": "user_id"},
    "comparison": {"offset_days": 7},
}

ORDER_VALUE = {
    "id": "order_value_v1",
    "name": "成交金额",
    "numerator": {"field": "amount", "filter": {"ordered": True}},
    "denominator": None,
    "comparison": {"offset_days": 7},
}

DEFINITIONS = [CONVERSION_RATE, ORDER_VALUE]
DIMENSION = "channel"


def _event(event_id: str, event_time: str, received_at: str,
           region: str, channel: str, user_id: str,
           ordered: bool, amount: float | None = None) -> dict:
    return {
        "id": event_id,
        "event_time": event_time,
        "received_at": received_at,
        "region": region,
        "channel": channel,
        "user_id": user_id,
        "ordered": ordered,
        "amount": amount,
    }


def sample_events() -> list[dict]:
    events = []
    # ---- 美东业务日 2026-11-01（UTC 窗口 2026-11-01T04:00Z 至 2026-11-02T04:00Z）
    events += [
        # web 渠道：u1/u2 有订单，u3 无订单 → 2/3
        _event("e-us-001", "2026-11-01T12:00:00Z", "2026-11-02T05:30:00Z",
               "us-east", "web", "u1", True, 120.0),
        _event("e-us-002", "2026-11-01T13:00:00Z", "2026-11-02T05:31:00Z",
               "us-east", "web", "u2", True, 80.0),
        _event("e-us-003", "2026-11-01T14:00:00Z", "2026-11-02T05:32:00Z",
               "us-east", "web", "u3", False),
        # app 渠道：u4 有订单，u5 无 → 1/2
        _event("e-us-004", "2026-11-01T15:00:00Z", "2026-11-02T05:33:00Z",
               "us-east", "app", "u4", True, 200.0),
        _event("e-us-005", "2026-11-01T16:00:00Z", "2026-11-02T05:34:00Z",
               "us-east", "app", "u5", False),
        # 同一用户 web/app 重复触达：去重后合计分子 3，分母 5，不能按渠道比率平均
        _event("e-us-006", "2026-11-01T17:00:00Z", "2026-11-02T05:35:00Z",
               "us-east", "app", "u1", False),
        # 补数：首次截止点 2026-11-02T06:00Z 之后才入库，带来新转化用户 u6
        _event("e-us-007-late", "2026-11-01T18:00:00Z", "2026-11-02T07:15:00Z",
               "us-east", "app", "u6", True, 50.0),
    ]
    # ---- 美东 7 天前业务日 2026-10-25，作为比较期间
    events += [
        _event("e-us-101", "2026-10-25T12:00:00Z", "2026-10-26T05:30:00Z",
               "us-east", "web", "u10", True, 100.0),
        _event("e-us-102", "2026-10-25T13:00:00Z", "2026-10-26T05:31:00Z",
               "us-east", "web", "u11", False),
        _event("e-us-103", "2026-10-25T14:00:00Z", "2026-10-26T05:32:00Z",
               "us-east", "app", "u12", True, 60.0),
        _event("e-us-104", "2026-10-25T15:00:00Z", "2026-10-26T05:33:00Z",
               "us-east", "app", "u13", False),
    ]
    # ---- 中国区业务日 2026-11-01（UTC+8 窗口）
    events += [
        _event("e-cn-001", "2026-11-01T02:00:00Z", "2026-11-01T10:00:00Z",
               "china", "web", "c1", True, 300.0),
        _event("e-cn-002", "2026-11-01T03:00:00Z", "2026-11-01T10:05:00Z",
               "china", "web", "c2", False),
        _event("e-cn-003", "2026-11-01T05:00:00Z", "2026-11-01T10:10:00Z",
               "china", "app", "c3", True, 150.0),
    ]
    # ---- 欧洲区业务日 2026-11-01：只有曝光无成交，转化率为零分母之外的真实 0
    events += [
        _event("e-eu-001", "2026-11-01T08:00:00Z", "2026-11-01T12:00:00Z",
               "eu", "web", "e1", False),
        _event("e-eu-002", "2026-11-01T09:00:00Z", "2026-11-01T12:05:00Z",
               "eu", "app", "e2", False),
    ]
    return events
