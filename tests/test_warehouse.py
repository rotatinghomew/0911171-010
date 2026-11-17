import unittest

from digest.metrics import parse_event_time
from digest.warehouse import Warehouse


def ev(rid, event_time, received_at, **fields):
    data = {"id": rid, "event_time": event_time, "received_at": received_at}
    data.update(fields)
    return data


class WarehouseTests(unittest.TestCase):
    def setUp(self):
        self.events = [
            ev("r1", "2026-11-01T01:00Z", "2026-11-02T05:00Z", region="us", n=1),
            ev("r2-late", "2026-11-01T02:00Z", "2026-11-02T07:30Z", region="us", n=2),
            ev("r3", "2026-11-03T02:00Z", "2026-11-03T03:00Z", region="cn", n=3),
        ]
        self.warehouse = Warehouse(self.events)

    def test_cutoff_hides_late_rows_but_reveals_them_later(self):
        early = self.warehouse.visible("2026-11-02T06:00:00Z")
        self.assertEqual([row["id"] for row in early], ["r1"])
        later = self.warehouse.visible("2026-11-02T08:00:00Z")
        self.assertEqual([row["id"] for row in later], ["r1", "r2-late"])

    def test_window_completion_reflects_cutoff_versus_window_end(self):
        window_end = parse_event_time("2026-11-02T00:00:00Z")
        self.assertFalse(self.warehouse.window_complete(
            window_end, "2026-11-01T23:00:00Z"))
        self.assertTrue(self.warehouse.window_complete(
            window_end, "2026-11-02T00:00:00Z"))

    def test_by_ids_returns_traceable_detail_rows(self):
        rows = self.warehouse.by_ids(["r2-late", "r3"])
        self.assertEqual({row["id"] for row in rows}, {"r2-late", "r3"})

    def test_duplicate_event_id_rejected(self):
        with self.assertRaises(ValueError):
            Warehouse([
                ev("r1", "2026-11-01T01:00Z", "2026-11-02T05:00Z"),
                ev("r1", "2026-11-01T02:00Z", "2026-11-02T05:01Z"),
            ])

    def test_naive_event_time_rejected(self):
        with self.assertRaises(ValueError):
            Warehouse([ev("r1", "2026-11-01T01:00:00", "2026-11-02T05:00Z")])

    def test_roundtrip_save_load(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.json"
            self.warehouse.save(path)
            reloaded = Warehouse.load(path)
            self.assertEqual(
                [row["id"] for row in reloaded.visible("2026-11-03T00:00:00Z")],
                ["r1", "r2-late"])


if __name__ == "__main__":
    unittest.main()
