import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from digest.dispatch import Dispatcher
from digest.fixtures import DEFINITIONS, DIMENSION, ROSTER, SCHEDULES, sample_events
from digest.mailbox import LocalMailbox, content_hash
from digest.warehouse import Warehouse

FIRST_CUTOFF = "2026-11-02T06:00:00Z"
LATE_CUTOFF = "2026-11-02T08:00:00Z"
DAY = "2026-11-01"


def fixed_clock():
    return datetime(2026, 11, 2, 14, tzinfo=timezone.utc)


class DispatcherEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.warehouse = Warehouse(sample_events())
        self.mailbox = LocalMailbox(self.root / "mailbox")

    def tearDown(self):
        self.tmp.cleanup()

    def _dispatcher(self, roster=None):
        return Dispatcher(self.root / "state", SCHEDULES,
                          roster or ROSTER, clock=fixed_clock)

    def test_original_issue_carries_cutoff_definition_and_detail_trace(self):
        dispatcher = self._dispatcher()
        issue = dispatcher.publish_issue(
            "us-east", DAY, self.warehouse, DEFINITIONS, FIRST_CUTOFF, DIMENSION)
        # 补数行 e-us-007-late 在 06:00Z 截止点下不可见
        total = issue["reports"][0]["total"]
        self.assertEqual(total["numerator"], 3)
        self.assertEqual(total["denominator"], 5)
        self.assertNotIn("e-us-007-late", total["detail_row_ids"])
        # 截止点 06:00Z 已越过窗口结束 04:00Z，窗口完整
        self.assertTrue(issue["window_complete"])
        self.assertEqual(issue["data_cutoff"], FIRST_CUTOFF)
        self.assertEqual(issue["metric_definitions"], DEFINITIONS)
        self.assertEqual(issue["sequence"], 0)
        self.assertIsNone(issue["original_issue_id"])

    def test_backfill_creates_correction_linked_to_original_without_overwrite(self):
        dispatcher = self._dispatcher()
        original = dispatcher.publish_issue(
            "us-east", DAY, self.warehouse, DEFINITIONS, FIRST_CUTOFF, DIMENSION)
        dispatcher.deliver_pending(self.mailbox, original["issue_id"])
        original_files = {
            path.name: path.read_text()
            for path in self.mailbox.directory.glob("*.json")
        }

        correction = dispatcher.publish_issue(
            "us-east", DAY, self.warehouse, DEFINITIONS, LATE_CUTOFF, DIMENSION)
        self.assertNotEqual(correction["issue_id"], original["issue_id"])
        self.assertEqual(correction["original_issue_id"], original["issue_id"])
        self.assertEqual(correction["supersedes"], original["issue_id"])
        self.assertEqual(correction["sequence"], 1)
        self.assertIn("补数", correction["correction_reason"])
        self.assertIn("e-us-007-late", correction["correction_reason"])
        # 新数据：分母 6、分子 4
        total = correction["reports"][0]["total"]
        self.assertEqual((total["numerator"], total["denominator"]), (4, 6))
        self.assertIn("e-us-007-late", total["detail_row_ids"])

        dispatcher.deliver_pending(self.mailbox, correction["issue_id"])
        # 原稿文件字节不变
        for name, content in original_files.items():
            self.assertEqual((self.mailbox.directory / name).read_text(), content)
        # 链条完整
        chain = dispatcher.correction_chain("us-east", DAY)
        self.assertEqual([item["sequence"] for item in chain], [0, 1])

    def test_same_cutoff_republish_is_idempotent_without_new_issue(self):
        dispatcher = self._dispatcher()
        first = dispatcher.publish_issue(
            "us-east", DAY, self.warehouse, DEFINITIONS, FIRST_CUTOFF, DIMENSION)
        second = dispatcher.publish_issue(
            "us-east", DAY, self.warehouse, DEFINITIONS, FIRST_CUTOFF, DIMENSION)
        self.assertEqual(first["issue_id"], second["issue_id"])
        self.assertEqual(len(dispatcher.correction_chain("us-east", DAY)), 1)

    def test_resume_after_partial_delivery_across_restart(self):
        dispatcher = self._dispatcher()
        issue = dispatcher.publish_issue(
            "us-east", DAY, self.warehouse, DEFINITIONS, FIRST_CUTOFF, DIMENSION)
        recipients = issue["recipients"]
        self.assertEqual(len(recipients), 2)

        # 只送达第一个收件人后崩溃：新进程只看到落盘状态
        first = recipients[0]
        self.mailbox.deliver(issue["issue_id"], first, issue)
        for path in dispatcher.ledger_dir.glob("*.json"):
            record = json.loads(path.read_text())
            if record["issue_id"] == issue["issue_id"] and record["recipient"] == first:
                record["state"] = "delivered"
                record["receipt"] = self.mailbox._path_for(
                    issue["issue_id"], first).stem
                from digest.dispatch import _atomic_replace
                _atomic_replace(path, record)

        # 重启分发器（内存状态全部丢失），续投
        restarted = self._dispatcher()
        resumed = restarted.deliver_pending(self.mailbox, issue["issue_id"])
        self.assertEqual([r["recipient"] for r in resumed], [recipients[1]])
        status = restarted.delivery_status(issue["issue_id"])
        self.assertEqual({r["state"] for r in status}, {"delivered"})
        self.assertEqual(sorted(self.mailbox.recipients_for(issue["issue_id"])),
                         sorted(recipients))
        # 再跑一次不产生重复投递
        again = restarted.deliver_pending(self.mailbox, issue["issue_id"])
        self.assertEqual(again, [])
        self.assertEqual(len(self.mailbox.messages()), 2)

    def test_recipient_change_does_not_resend_or_mutate_old_issue(self):
        dispatcher = self._dispatcher()
        issue = dispatcher.publish_issue(
            "us-east", DAY, self.warehouse, DEFINITIONS, FIRST_CUTOFF, DIMENSION)
        dispatcher.deliver_pending(self.mailbox, issue["issue_id"])
        snapshot = list(issue["recipients"])

        new_roster = {**ROSTER, "us-east": ["us-lead@example.invalid",
                                            "newcomer@example.invalid"]}
        restarted = self._dispatcher(new_roster)
        rerun = restarted.publish_issue(
            "us-east", DAY, self.warehouse, DEFINITIONS, FIRST_CUTOFF, DIMENSION)
        self.assertEqual(rerun["issue_id"], issue["issue_id"])
        self.assertEqual(rerun["recipients"], snapshot)  # 仍是发稿时快照
        self.assertEqual(len(self.mailbox.messages()), len(snapshot))
        # 新人不会收到旧稿；其首次出现是在更正稿里
        correction = restarted.publish_issue(
            "us-east", DAY, self.warehouse, DEFINITIONS, LATE_CUTOFF, DIMENSION)
        self.assertEqual(correction["recipients"],
                         ["newcomer@example.invalid", "us-lead@example.invalid"])
        restarted.deliver_pending(self.mailbox, correction["issue_id"])
        self.assertEqual(
            self.mailbox.recipients_for(correction["issue_id"]),
            ["newcomer@example.invalid", "us-lead@example.invalid"])

    def test_delivered_message_matches_issue_content_hash(self):
        dispatcher = self._dispatcher()
        issue = dispatcher.publish_issue(
            "us-east", DAY, self.warehouse, DEFINITIONS, FIRST_CUTOFF, DIMENSION)
        dispatcher.deliver_pending(self.mailbox, issue["issue_id"])
        for envelope in self.mailbox.find(issue["issue_id"]):
            self.assertEqual(envelope["content_hash"], content_hash(issue))
            self.assertEqual(envelope["message"]["issue_id"], issue["issue_id"])
            self.assertEqual(envelope["message"]["data_cutoff"], FIRST_CUTOFF)

    def test_trace_metric_recovers_detail_scope_and_correction_reason(self):
        dispatcher = self._dispatcher()
        dispatcher.publish_issue(
            "us-east", DAY, self.warehouse, DEFINITIONS, FIRST_CUTOFF, DIMENSION)
        correction = dispatcher.publish_issue(
            "us-east", DAY, self.warehouse, DEFINITIONS, LATE_CUTOFF, DIMENSION)
        trace = dispatcher.trace_metric(
            correction["issue_id"], "conversion_rate_v1",
            warehouse=self.warehouse)
        self.assertEqual(trace["scope"], "total")
        self.assertEqual(trace["value"], round(4 / 6, 6))
        self.assertEqual(trace["data_cutoff"], LATE_CUTOFF)
        self.assertEqual(trace["definition"]["numerator"]["distinct"], "user_id")
        self.assertIn("e-us-007-late", trace["detail_row_ids"])
        self.assertEqual(len(trace["detail_rows"]), 7)
        self.assertIsNotNone(trace["correction_reason"])
        # 渠道维度追溯
        web = dispatcher.trace_metric(
            correction["issue_id"], "conversion_rate_v1",
            dimension_value="web")
        self.assertEqual((web["numerator"], web["denominator"]), (2, 3))

    def test_missing_and_zero_states_survive_publishing(self):
        dispatcher = self._dispatcher()
        eu = dispatcher.publish_issue(
            "eu", DAY, self.warehouse, DEFINITIONS, LATE_CUTOFF, DIMENSION)
        cvr = eu["reports"][0]["total"]
        amt = eu["reports"][1]["total"]
        # 欧洲区只有曝光无成交：转化率是真实 0（2 个去重用户，0 成交）
        self.assertEqual(cvr["status"], "ok")
        self.assertEqual(cvr["value"], 0.0)
        self.assertEqual((cvr["numerator"], cvr["denominator"]), (0, 2))
        # 成交金额没有任何成交行可求和：显式 no_data 而不是 0
        self.assertEqual(amt["status"], "no_data")
        self.assertIsNone(amt["value"])

    def test_state_survives_full_process_restart(self):
        dispatcher = self._dispatcher()
        issue = dispatcher.publish_issue(
            "china", DAY, self.warehouse, DEFINITIONS, LATE_CUTOFF, DIMENSION)
        restarted = self._dispatcher()
        reloaded = restarted.load_issue(issue["issue_id"])
        self.assertEqual(reloaded["content_fingerprint"], issue["content_fingerprint"])
        self.assertEqual(restarted.correction_chain("china", DAY)[0]["issue_id"],
                         issue["issue_id"])

    def test_empty_roster_publishes_issue_without_ledger_entries(self):
        dispatcher = self._dispatcher({"us-east": []})
        issue = dispatcher.publish_issue(
            "us-east", DAY, self.warehouse, DEFINITIONS, FIRST_CUTOFF, DIMENSION)
        self.assertEqual(issue["recipients"], [])
        self.assertEqual(dispatcher.delivery_status(issue["issue_id"]), [])
        self.assertEqual(dispatcher.deliver_pending(self.mailbox), [])

    def test_undelivered_correction_is_resumed_after_restart(self):
        dispatcher = self._dispatcher()
        original = dispatcher.publish_issue(
            "us-east", DAY, self.warehouse, DEFINITIONS, FIRST_CUTOFF, DIMENSION)
        dispatcher.deliver_pending(self.mailbox, original["issue_id"])
        correction = dispatcher.publish_issue(
            "us-east", DAY, self.warehouse, DEFINITIONS, LATE_CUTOFF, DIMENSION)
        # 更正稿一条都没投，进程就死了
        before = self.mailbox.recipients_for(correction["issue_id"])
        self.assertEqual(before, [])
        restarted = self._dispatcher()
        resumed = restarted.deliver_pending(self.mailbox, correction["issue_id"])
        self.assertEqual(len(resumed), len(correction["recipients"]))
        self.assertEqual(
            sorted(self.mailbox.recipients_for(correction["issue_id"])),
            sorted(correction["recipients"]))

    def test_fingerprint_includes_cutoff_so_identical_numbers_still_correct_on_cutoff_move(self):
        dispatcher = self._dispatcher()
        first = dispatcher.publish_issue(
            "china", DAY, self.warehouse, DEFINITIONS, FIRST_CUTOFF, DIMENSION)
        # 中国区 06:00Z 与 08:00Z 截止点可见行相同，但截止点不同 -> 指纹不同，出更正
        second = dispatcher.publish_issue(
            "china", DAY, self.warehouse, DEFINITIONS, LATE_CUTOFF, DIMENSION)
        self.assertNotEqual(first["issue_id"], second["issue_id"])
        self.assertEqual(second["original_issue_id"], first["issue_id"])


if __name__ == "__main__":
    unittest.main()
