import json
import tempfile
import unittest

from digest.mailbox import LocalMailbox, content_hash


class MailboxTests(unittest.TestCase):
    def test_local_delivery_reuses_receipt_across_mailbox_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            mailbox = LocalMailbox(directory)
            receipt = mailbox.deliver("morning", "east", {"value": 1})
            replay = LocalMailbox(directory).deliver("morning", "east", {"value": 1})
            self.assertEqual(receipt, replay)
            self.assertEqual(len(mailbox.messages()), 1)

    def test_delivery_identity_cannot_refer_to_two_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            mailbox = LocalMailbox(directory)
            mailbox.deliver("morning", "east", {"value": 1})
            with self.assertRaises(ValueError):
                mailbox.deliver("morning", "east", {"value": 2})

    def test_envelope_carries_content_hash_and_find_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            mailbox = LocalMailbox(directory)
            mailbox.deliver("morning", "east", {"value": 1})
            mailbox.deliver("morning", "west", {"value": 1})
            mailbox.deliver("evening", "east", {"value": 2})
            self.assertEqual(mailbox.recipients_for("morning"), ["east", "west"])
            found = mailbox.find("morning", "east")
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["content_hash"],
                             content_hash({"value": 1}))
            self.assertEqual(len(mailbox.find("evening")), 1)

    def test_no_partial_files_left_after_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            LocalMailbox(directory).deliver("run", "who", {"a": 1})
            names = [path.name for path in LocalMailbox(directory).directory.iterdir()]
            self.assertTrue(names)
            self.assertFalse(any(".tmp" in name for name in names))
            # 文件本身是合法 JSON
            envelope = json.loads(
                (LocalMailbox(directory).directory / f"{names[0]}").read_text())
            self.assertEqual(envelope["message"], {"a": 1})


if __name__ == "__main__":
    unittest.main()
