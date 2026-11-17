import unittest

from digest.dispatch import Dispatcher
from digest.scheduler import next_run
from datetime import datetime, timezone


class DigestTests(unittest.TestCase):
    def test_retry_of_same_run_is_idempotent(self):
        dispatcher = Dispatcher()
        first = dispatcher.send("r1", [], {"day": "d", "field": "n", "name": "m"}, ["a"])
        second = dispatcher.send("r1", [], {"day": "other", "field": "n", "name": "m"}, ["b"])
        self.assertEqual(first, second)

    def test_next_run_advances_after_scheduled_time(self):
        after = datetime(2026, 11, 1, 10, tzinfo=timezone.utc)
        self.assertEqual(next_run(after, 9).day, 2)


if __name__ == "__main__":
    unittest.main()
