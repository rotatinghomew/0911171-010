"""本地明细数据仓：事件不可变，按数据截止点决定可见范围。

每行事件：
- id          稳定明细编号，用于从简报数字查回明细范围
- event_time  带时区的业务发生时间（ISO 字符串或 datetime）
- received_at 数据进入本地仓的时间（UTC），用于模拟补数
- 其余字段     维度（region/channel）与度量字段（user_id/amount/orders 等）

cutoff 语义：只把 received_at <= cutoff 的事件纳入计算。补到的事件
received_at 更晚，在更早的截止点下不可见，因此同一份原稿的口径可复现；
用新的截止点重算时，窗口是否仍在补数会标记 window_complete。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .metrics import iso_z, parse_event_time


@dataclass(frozen=True)
class Event:
    id: str
    event_time: object
    received_at: object
    fields: dict

    def row(self) -> dict:
        row = dict(self.fields)
        row["id"] = self.id
        row["event_time"] = self.event_time
        row["received_at"] = self.received_at
        return row


class Warehouse:
    def __init__(self, events: list[dict] | None = None) -> None:
        self._events: list[Event] = []
        for raw in events or []:
            self.add(raw)

    def add(self, raw: dict) -> Event:
        missing = {"id", "event_time", "received_at"} - raw.keys()
        if missing:
            raise ValueError(f"事件缺少必需字段: {sorted(missing)}")
        if any(event.id == raw["id"] for event in self._events):
            raise ValueError(f"事件编号重复: {raw['id']}")
        event = Event(
            id=str(raw["id"]),
            event_time=parse_event_time(raw["event_time"]),
            received_at=parse_event_time(raw["received_at"]),
            fields={key: value for key, value in raw.items()
                    if key not in {"id", "event_time", "received_at"}},
        )
        self._events.append(event)
        return event

    def visible(self, cutoff) -> list[dict]:
        cutoff_dt = parse_event_time(cutoff)
        rows = [event.row() for event in self._events
                if event.received_at <= cutoff_dt]
        return sorted(rows, key=lambda row: row["id"])

    def window_complete(self, window_end, cutoff) -> bool:
        """截止点已越过窗口结束时刻，才认为窗口不会再收到补数。"""
        return parse_event_time(cutoff) >= parse_event_time(window_end)

    def by_ids(self, row_ids: list[str]) -> list[dict]:
        wanted = set(row_ids)
        return [event.row() for event in self._events if event.id in wanted]

    def save(self, path: str | Path) -> None:
        payload = [self._jsonable(event.row()) for event in self._events]
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2,
                      default=self._json_default),
            encoding="utf-8")

    @staticmethod
    def _json_default(value):
        if isinstance(value, datetime):
            return iso_z(value)
        raise TypeError(f"不可序列化的类型: {type(value)!r}")

    def _jsonable(self, row: dict) -> dict:
        return {key: (iso_z(value) if isinstance(value, datetime) else value)
                for key, value in row.items()}

    @classmethod
    def load(cls, path: str | Path) -> "Warehouse":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(payload)
