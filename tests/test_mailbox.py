import tempfile
import unittest

from digest.dispatch import Dispatcher
from digest.mailbox import LocalMailbox, send_digest


class MailboxTests(unittest.TestCase):
    def test_local_delivery_reuses_receipt_across_mailbox_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            dispatcher = Dispatcher()
            mailbox = LocalMailbox(directory)
            rows = [{"day": "2026-11-01", "count": 3}]
            definition = {"name": "volume", "day": "2026-11-01", "field": "count", "aggregation": "sum"}
            receipts = send_digest(dispatcher, mailbox, "morning", rows, definition, ["east", "east", "west"])
            self.assertEqual(len(receipts), 2)
            replay = send_digest(dispatcher, LocalMailbox(directory), "morning", rows, definition, ["east", "west"])
            self.assertEqual(receipts, replay)
            self.assertEqual(len(mailbox.messages()), 2)

    def test_delivery_identity_cannot_refer_to_two_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            mailbox = LocalMailbox(directory)
            mailbox.deliver("morning", "east", {"value": 1})
            with self.assertRaises(ValueError):
                mailbox.deliver("morning", "east", {"value": 2})
