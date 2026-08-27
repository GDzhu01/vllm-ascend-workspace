from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from npu_fleet_monitor.db import Database
from npu_fleet_monitor.scheduler import AdaptiveScheduler
from npu_fleet_monitor.settings import Settings


class SchedulerTests(unittest.TestCase):
    def test_fastest_visible_viewer_controls_interval(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            settings = Settings(Path(state), Path(state), "127.0.0.1", 8789, 120, 30, 60, 90, 4, 12, 8192)
            db = Database(Path(state) / "test.sqlite3")
            db.initialize()
            scheduler = AdaptiveScheduler(settings, db, object())  # type: ignore[arg-type]
            self.assertEqual(scheduler.effective_interval(), 120)
            scheduler.heartbeat("viewer_00000001", 10, True)
            scheduler.heartbeat("viewer_00000002", 1, True)
            self.assertEqual(scheduler.effective_interval(), 1)
            scheduler.remove_lease("viewer_00000002")
            self.assertEqual(scheduler.effective_interval(), 10)
            scheduler.heartbeat("viewer_00000001", 10, False)
            self.assertEqual(scheduler.effective_interval(), 120)
            db.close()


if __name__ == "__main__":
    unittest.main()
