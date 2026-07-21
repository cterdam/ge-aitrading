from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from monitoring.daily_schedule import DAILY_SLOTS
from scripts.prepare_observation_day import prepare


class PrepareObservationDayTests(unittest.TestCase):
    def test_writes_both_plists_and_registers_every_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            launch_agents = base / "LaunchAgents"
            expected = base / "expected"
            result = prepare(
                date(2026, 7, 22),
                launch_agents=launch_agents,
                expected_directory=expected,
            )
            worker = Path(result["worker_plist"])
            watchdog = Path(result["watchdog_plist"])
            self.assertTrue(worker.exists())
            self.assertTrue(watchdog.exists())
            # Worker is date-pinned; watchdog runs on an interval.
            self.assertIn("<key>Day</key><integer>22</integer>", worker.read_text())
            self.assertIn("StartInterval", watchdog.read_text())
            # Every slot registered as an expectation the watchdog can verify.
            self.assertEqual(len(DAILY_SLOTS), result["expected_run_count"])
            self.assertEqual(
                len(DAILY_SLOTS), len(list(expected.glob("*.expected.json")))
            )

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            launch_agents = base / "LaunchAgents"
            expected = base / "expected"
            prepare(
                date(2026, 7, 22),
                launch_agents=launch_agents,
                expected_directory=expected,
                write=False,
            )
            self.assertFalse(launch_agents.exists())
            self.assertFalse(expected.exists())


if __name__ == "__main__":
    unittest.main()
