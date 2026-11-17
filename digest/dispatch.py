"""可核对的持久化分发器。

设计要点
--------
1. 原稿与更正都是不可变的 issue 文件（state/issues/<id>.json），更正通过
   original_issue_id / supersedes 关联原稿，绝不覆盖已送达内容。
2. 内容指纹覆盖窗口、数据截止点、指标定义与全部计算结果；截止点推进后
   重算，内容不变就不产生新稿，内容变化才生成关联原稿的更正。
3. 每个 (issue, 收件人) 一条独立投递台账（pending/delivered）。进程中断后
   重开 Dispatcher，deliver_pending 从未送达处继续；邮箱投递本身幂等。
4. 收件人列表在发稿时快照进 issue。之后名册变动只影响新稿，旧稿既不换
   内容也不按新名册重发；旧稿的未投台账仍按快照补完。
5. 指标结果带 detail_row_ids，配合 trace_metric 可从已送达数字查回明细
   范围、口径定义、截止点与更正原因。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .mailbox import LocalMailbox, canonical_json, content_hash
from .metrics import evaluate_pivot, iso_z
from .scheduler import RegionSchedule, business_window, comparison_window
from .warehouse import Warehouse

ISSUE_SCHEMA = "metric-digest/2"


def _atomic_dump(path: Path, payload: dict) -> None:
    """仅用于不可变文件的原子创建：目标已存在时保持原样。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                    dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_name, path)
        except FileExistsError:
            pass
    finally:
        os.unlink(tmp_name)


def _atomic_replace(path: Path, payload: dict) -> None:
    """用于状态文件的原子覆盖（os.replace 在同文件系统上原子）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                    dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        os.unlink(tmp_name)
        raise


def _digest(parts: list) -> str:
    return hashlib.sha256(canonical_json(parts).encode("utf-8")).hexdigest()[:16]


class Dispatcher:
    def __init__(self, state_dir: str | Path,
                 schedules: dict[str, RegionSchedule],
                 roster: dict[str, list[str]] | None = None,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.state_dir = Path(state_dir)
        self.issues_dir = self.state_dir / "issues"
        self.ledger_dir = self.state_dir / "deliveries"
        self.issues_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        self.schedules = dict(schedules)
        self.roster = {region: list(people) for region, people in (roster or {}).items()}
        self.clock = clock or (lambda: datetime.now(tz=timezone.utc))

    # ---------------------------------------------------------------- 发稿
    def publish_issue(self, region: str, business_day, warehouse: Warehouse,
                      definitions: list[dict], cutoff, dimension: str,
                      recipients: list[str] | None = None) -> dict:
        schedule = self.schedules[region]
        business_day = _as_date(business_day)
        window_start, window_end = business_window(schedule, business_day)
        rows = warehouse.visible(cutoff)
        # 区域口径：只纳入本区域事件，避免跨区域明细混入同一窗口
        rows = [row for row in rows if row.get("region") == region]
        reports = []
        for definition in definitions:
            compare = None
            if definition.get("comparison"):
                compare = comparison_window(
                    schedule, business_day,
                    definition["comparison"]["offset_days"])
            reports.append(evaluate_pivot(rows, definition, window_start,
                                          window_end, dimension, compare))
        window_complete = warehouse.window_complete(window_end, cutoff)
        fingerprint = _digest([
            region, business_day.isoformat(), schedule.timezone,
            iso_z(window_start), iso_z(window_end), iso_z(_utc(cutoff)),
            window_complete, dimension, definitions, reports,
        ])

        original = self._load_original(region, business_day)
        if original is not None and original["content_fingerprint"] == fingerprint:
            return self._load_latest(region, business_day)

        if recipients is None:
            recipients = list(self.roster.get(region, []))
        recipients = sorted(set(recipients))

        now = iso_z(self.clock())
        if original is None:
            issue_id = _digest(["issue", region, business_day.isoformat(), "v0"])
            issue = self._issue_record(
                issue_id, region, schedule, business_day, window_start, window_end,
                cutoff, window_complete, dimension, definitions, reports,
                fingerprint, recipients, now, sequence=0,
                original_issue_id=None, supersedes=None, correction_reason=None)
        else:
            latest = self._load_latest(region, business_day)
            issue_id = _digest(["issue", original["issue_id"],
                                latest["sequence"] + 1, fingerprint])
            reason = self._correction_reason(original, latest, cutoff,
                                             window_complete, warehouse, rows)
            issue = self._issue_record(
                issue_id, region, schedule, business_day, window_start, window_end,
                cutoff, window_complete, dimension, definitions, reports,
                fingerprint, recipients, now, sequence=latest["sequence"] + 1,
                original_issue_id=original["issue_id"],
                supersedes=latest["issue_id"], correction_reason=reason)
        _atomic_dump(self.issues_dir / f"{issue_id}.json", issue)
        self._create_ledger(issue, recipients)
        return issue

    @staticmethod
    def _issue_record(issue_id, region, schedule, business_day, window_start,
                      window_end, cutoff, window_complete, dimension, definitions,
                      reports, fingerprint, recipients, now, sequence,
                      original_issue_id, supersedes, correction_reason) -> dict:
        return {
            "schema": ISSUE_SCHEMA,
            "issue_id": issue_id,
            "region": region,
            "timezone": schedule.timezone,
            "business_day": business_day.isoformat(),
            "window_start": iso_z(window_start),
            "window_end": iso_z(window_end),
            "data_cutoff": iso_z(_utc(cutoff)),
            "window_complete": window_complete,
            "dimension": dimension,
            "metric_definitions": definitions,
            "reports": reports,
            "content_fingerprint": fingerprint,
            "sequence": sequence,
            "original_issue_id": original_issue_id,
            "supersedes": supersedes,
            "correction_reason": correction_reason,
            "recipients": recipients,
            "created_at": now,
        }

    def _correction_reason(self, original, latest, new_cutoff, window_complete,
                           warehouse, current_rows) -> str:
        old_cutoff = latest["data_cutoff"]
        old_rows = {row["id"] for row in warehouse.visible(old_cutoff)
                    if row.get("region") == latest["region"]}
        new_rows = {row["id"] for row in current_rows}
        added = sorted(new_rows - old_rows)
        parts = [
            f"补数更正：数据截止点由 {old_cutoff} 推进至 {iso_z(_utc(new_cutoff))}",
            f"本区域新增明细 {len(added)} 行",
        ]
        if added:
            parts.append(f"明细编号 {added}")
        if window_complete != latest["window_complete"]:
            parts.append(f"窗口完整性标记变更为 {window_complete}")
        parts.append(f"原稿 {original['issue_id']} 保持不变，本稿为其第 "
                     f"{latest['sequence'] + 1} 次更正")
        return "；".join(parts)

    # ---------------------------------------------------------------- 投递
    def _create_ledger(self, issue: dict, recipients: list[str]) -> None:
        for recipient in recipients:
            record = {
                "issue_id": issue["issue_id"],
                "delivery_id": issue["issue_id"],
                "recipient": recipient,
                "state": "pending",
                "content_hash": content_hash(issue),
                "attempts": 0,
                "receipt": None,
            }
            path = self._ledger_path(issue["issue_id"], recipient)
            if not path.exists():
                _atomic_dump(path, record)

    def deliver_pending(self, mailbox: LocalMailbox,
                        issue_id: str | None = None) -> list[dict]:
        """把所有 pending 台账投递出去；可按 issue 限定。中断后可安全重跑。"""
        results = []
        for path in sorted(self.ledger_dir.glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["state"] == "delivered":
                continue
            if issue_id is not None and record["issue_id"] != issue_id:
                continue
            issue = self.load_issue(record["issue_id"])
            record["attempts"] += 1
            receipt = mailbox.deliver(record["delivery_id"],
                                      record["recipient"], issue)
            record["state"] = "delivered"
            record["receipt"] = receipt
            _atomic_replace(path, record)
            results.append(record)
        return results

    def delivery_status(self, issue_id: str) -> list[dict]:
        records = []
        for path in sorted(self.ledger_dir.glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["issue_id"] == issue_id:
                records.append(record)
        return records

    # ---------------------------------------------------------------- 核对
    def load_issue(self, issue_id: str) -> dict:
        return json.loads((self.issues_dir / f"{issue_id}.json").read_text(encoding="utf-8"))

    def list_issues(self, region: str, business_day) -> list[dict]:
        business_day = _as_date(business_day).isoformat()
        issues = [json.loads(path.read_text(encoding="utf-8"))
                  for path in self.issues_dir.glob("*.json")]
        matched = [issue for issue in issues
                   if issue["region"] == region and issue["business_day"] == business_day]
        return sorted(matched, key=lambda item: item["sequence"])

    def correction_chain(self, region: str, business_day) -> list[dict]:
        return self.list_issues(region, business_day)

    def trace_metric(self, issue_id: str, metric_id: str,
                     dimension_value=None, warehouse: Warehouse | None = None) -> dict:
        """从已送达数字查回口径、明细范围与更正链路。"""
        issue = self.load_issue(issue_id)
        report = next(item for item in issue["reports"]
                      if item["metric_id"] == metric_id)
        definition = next(item for item in issue["metric_definitions"]
                          if item["id"] == metric_id)
        if dimension_value is None:
            node = report["total"]
            scope = "total"
        else:
            node = next(group["metric"] for group in report["groups"]
                        if group["dimension_value"] == dimension_value)
            scope = f"{report['dimension']}={dimension_value}"
        detail_ids = node["detail_row_ids"]
        trace = {
            "issue_id": issue_id,
            "region": issue["region"],
            "business_day": issue["business_day"],
            "metric_id": metric_id,
            "scope": scope,
            "status": node["status"],
            "value": node["value"],
            "numerator": node["numerator"],
            "denominator": node["denominator"],
            "window_start": node["window_start"],
            "window_end": node["window_end"],
            "data_cutoff": issue["data_cutoff"],
            "window_complete": issue["window_complete"],
            "definition": definition,
            "detail_row_count": len(detail_ids),
            "detail_row_id_range": [detail_ids[0], detail_ids[-1]] if detail_ids else [],
            "detail_row_ids": detail_ids,
            "sequence": issue["sequence"],
            "original_issue_id": issue["original_issue_id"],
            "correction_reason": issue["correction_reason"],
        }
        if warehouse is not None:
            trace["detail_rows"] = warehouse.by_ids(detail_ids)
        return trace

    # ------------------------------------------------------------- 内部工具
    def _load_original(self, region: str, business_day) -> dict | None:
        chain = self.list_issues(region, business_day)
        return chain[0] if chain else None

    def _load_latest(self, region: str, business_day) -> dict:
        return self.list_issues(region, business_day)[-1]

    def _ledger_path(self, issue_id: str, recipient: str) -> Path:
        key = hashlib.sha256(f"{issue_id}\0{recipient}".encode()).hexdigest()[:16]
        return self.ledger_dir / f"{key}.json"


def _as_date(value):
    from datetime import date as _date, datetime as _datetime
    if isinstance(value, _datetime):
        return value.date()
    if isinstance(value, _date):
        return value
    return _date.fromisoformat(value)


def _utc(value) -> datetime:
    from .metrics import parse_event_time
    return parse_event_time(value)
