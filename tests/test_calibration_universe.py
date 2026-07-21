from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from research.calibration_universe import (
    InvalidCalibrationUniverseError,
    load_calibration_universe,
)


class CalibrationUniverseTests(unittest.TestCase):
    def write(self, directory: str, body: str) -> Path:
        path = Path(directory) / "calib.toml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_committed_file_loads_and_is_research_only(self) -> None:
        symbols = load_calibration_universe("config/calibration_universe.toml")
        self.assertTrue(symbols)
        self.assertTrue(all(s == s.upper() for s in symbols))

    def test_committed_trading_universe_is_rejected_as_calibration_source(self) -> None:
        # The real trading universe must never be usable as a calibration list.
        with self.assertRaises(InvalidCalibrationUniverseError):
            load_calibration_universe("config/universe.toml")

    def test_missing_never_a_trading_universe_flag_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, "\n".join((
                'schema_version = 1',
                'status = "RESEARCH_ONLY"',
                'purpose = "COST_WALL_CALIBRATION"',
                'symbols = ["F", "SOFI"]',
            )))
            with self.assertRaisesRegex(InvalidCalibrationUniverseError, "never_a_trading_universe"):
                load_calibration_universe(path)

    def test_duplicate_or_lowercase_symbols_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(directory, "\n".join((
                'schema_version = 1',
                'status = "RESEARCH_ONLY"',
                'purpose = "COST_WALL_CALIBRATION"',
                'never_a_trading_universe = true',
                'symbols = ["f", "SOFI"]',
            )))
            with self.assertRaises(InvalidCalibrationUniverseError):
                load_calibration_universe(path)


if __name__ == "__main__":
    unittest.main()
