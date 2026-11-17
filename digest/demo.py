"""端到端本地演示：计算 → 发稿 → 中断续投 → 补数更正 → 明细追溯。

完全离线，不连接邮件服务。固定时钟与固定数据，重复运行得到逐字节一致的结果。
运行：python -m digest.demo
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .dispatch import Dispatcher
from .fixtures import DEFINITIONS, DIMENSION, ROSTER, SCHEDULES, sample_events
from .mailbox import LocalMailbox
from .metrics import parse_event_time
from .scheduler import upcoming_runs
from .warehouse import Warehouse

STATE_DIR = Path("demo-state")
FIRST_CUTOFF = "2026-11-02T06:00:00Z"
LATE_CUTOFF = "2026-11-02T08:00:00Z"
FIXED_NOW = datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc)
BUSINESS_DAY = "2026-11-01"


def fixed_clock() -> datetime:
    return FIXED_NOW


def _show(title: str, payload) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> None:
    if STATE_DIR.exists():
        shutil.rmtree(STATE_DIR)
    warehouse = Warehouse(sample_events())
    mailbox = LocalMailbox(STATE_DIR / "mailbox")
    dispatcher = Dispatcher(STATE_DIR / "dispatcher", SCHEDULES, ROSTER,
                            clock=fixed_clock)

    # 1) 跨区域调度：各区域按自己的业务日与本地时刻出稿（含 DST 安排）
    runs = upcoming_runs(SCHEDULES["us-east"],
                         parse_event_time("2026-03-07T00:00:00Z"), 3)
    _show("美东区跨日调度（2026-03 夏令时跳变前后）", runs)

    # 2) 首次发稿：截止点 06:00Z，补数行尚未入库
    original = dispatcher.publish_issue(
        "us-east", BUSINESS_DAY, warehouse, DEFINITIONS,
        FIRST_CUTOFF, DIMENSION)
    _show("原稿（数据截止点 06:00Z，补数未入库）", {
        "issue_id": original["issue_id"],
        "data_cutoff": original["data_cutoff"],
        "window_complete": original["window_complete"],
        "conversion_total": original["reports"][0]["total"],
    })

    # 3) 模拟进程在部分收件人送达后中断：只投第一个收件人
    first_recipient = original["recipients"][0]
    _simulate_crash_after_first(dispatcher, original["issue_id"],
                                first_recipient)
    delivered_before_crash = mailbox.recipients_for(original["issue_id"])

    # 4) 进程重启：重开 Dispatcher，接着完成剩余投递
    restarted = Dispatcher(STATE_DIR / "dispatcher", SCHEDULES, ROSTER,
                           clock=fixed_clock)
    resumed = restarted.deliver_pending(mailbox, original["issue_id"])
    _show("中断后续投结果", {
        "崩溃前已送达": sorted(delivered_before_crash),
        "重启后补投": sorted(r["recipient"] for r in resumed),
        "最终全部送达": sorted(mailbox.recipients_for(original["issue_id"])),
        "原稿投递台账": restarted.delivery_status(original["issue_id"]),
    })

    # 5) 收件人名册变动：旧稿不换内容、不按新名册重发
    restarted.roster["us-east"] = ["us-lead@example.invalid",
                                   "newcomer@example.invalid"]
    rerun = restarted.publish_issue("us-east", BUSINESS_DAY, warehouse,
                                    DEFINITIONS, FIRST_CUTOFF, DIMENSION)
    _show("名册变动后用同一截止点重发（应原样返回，无新稿、无新投递）", {
        "issue_id_unchanged": rerun["issue_id"] == original["issue_id"],
        "收件人仍为发稿快照": rerun["recipients"],
        "邮箱消息数": len(mailbox.messages()),
    })

    # 6) 补数到达：新截止点产生关联原稿的更正，不覆盖已送达内容
    correction = restarted.publish_issue(
        "us-east", BUSINESS_DAY, warehouse, DEFINITIONS,
        LATE_CUTOFF, DIMENSION)
    restarted.deliver_pending(mailbox, correction["issue_id"])
    _show("补数更正（关联原稿，原稿保持不变）", {
        "original_issue_id": correction["original_issue_id"],
        "correction_issue_id": correction["issue_id"],
        "correction_reason": correction["correction_reason"],
        "原稿仍在邮箱": mailbox.find(original["issue_id"]) != [],
        "更正后合计转化率": correction["reports"][0]["total"],
        "更正链": [item["issue_id"]
                  for item in restarted.correction_chain("us-east", BUSINESS_DAY)],
    })

    # 7) 从已送达数字查回明细范围与口径
    trace = restarted.trace_metric(correction["issue_id"],
                                   "conversion_rate_v1",
                                   warehouse=warehouse)
    _show("数字追溯（更正稿合计转化率 → 明细范围与口径）", {
        key: trace[key] for key in (
            "issue_id", "scope", "status", "value", "numerator",
            "denominator", "data_cutoff", "detail_row_id_range",
            "detail_row_count", "definition", "correction_reason")
    })

    # 8) 缺失数据与零分母不伪装成零
    eu_issue = restarted.publish_issue(
        "eu", BUSINESS_DAY, warehouse, DEFINITIONS,
        LATE_CUTOFF, DIMENSION)
    china_issue = restarted.publish_issue(
        "china", BUSINESS_DAY, warehouse, DEFINITIONS,
        LATE_CUTOFF, DIMENSION)
    _show("口径状态：真实 0 / 无数据 / 零分母显式区分", {
        "eu_转化率(无成交但有曝光→真实0)": eu_issue["reports"][0]["total"]["status"],
        "eu_成交金额(无成交行→no_data)": eu_issue["reports"][1]["total"]["status"],
        "cn_比较期(7天前无数据→no_data)":
            china_issue["reports"][0]["total"]["comparison"]["status"],
    })

    print("\n演示完成，落盘目录：", STATE_DIR.resolve())


def _simulate_crash_after_first(dispatcher: Dispatcher, issue_id: str,
                                first_recipient: str) -> None:
    """只投递一个收件人，其余保持 pending，模拟进程中途死掉。"""
    issue = dispatcher.load_issue(issue_id)
    mailbox = LocalMailbox(STATE_DIR / "mailbox")
    mailbox.deliver(issue_id, first_recipient, issue)
    for path in sorted(dispatcher.ledger_dir.glob("*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record["issue_id"] == issue_id and record["recipient"] == first_recipient:
            record["state"] = "delivered"
            record["receipt"] = mailbox._path_for(issue_id, first_recipient).stem
            from .dispatch import _atomic_replace
            _atomic_replace(path, record)
            break


if __name__ == "__main__":
    main()
