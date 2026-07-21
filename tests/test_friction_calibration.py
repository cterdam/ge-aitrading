from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from execution.raw_data_vault import RawDataVault
from research.friction_calibration import (
    calibrate_friction,
    extract_option_quotes,
    default_snapshot_paths,
)


def _quote(bid, ask, delta, theta, volume, oi):
    return {
        "bid_price": bid, "ask_price": ask, "delta": delta, "theta": theta,
        "volume": volume, "open_interest": oi, "mark_price": str((float(bid) + float(ask)) / 2),
    }


def _store(root: Path, quotes):
    # Mirror the real nested shape: data -> results -> [{quote: {...}}]
    tool_results = [
        {"tool": "get_option_quotes", "output": {"data": {"results": [{"quote": q} for q in quotes]}}},
    ]
    return RawDataVault(root).store(
        source="ROBINHOOD_OFFICIAL_MCP",
        request={"schema_version": 1, "transport": "CLAUDE_STREAM_JSON_HARVEST", "symbol": "SPY", "tool_calls": []},
        response={"tool_results": tool_results},
        source_updated_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
        received_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
    ).path


class ExtractQuotesTests(unittest.TestCase):
    def test_finds_quotes_regardless_of_nesting(self) -> None:
        envelope = {"response": {"tool_results": [
            {"tool": "get_option_quotes", "output": {"data": {"results": [
                {"quote": {"bid_price": "1.10", "ask_price": "1.20"}},
            ]}}},
        ]}}
        quotes = extract_option_quotes(envelope)
        self.assertEqual(1, len(quotes))
        self.assertEqual("1.10", quotes[0]["bid_price"])


class CalibrateFrictionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_liquid_basis_when_volume_and_oi_present(self) -> None:
        # Two 0.40-delta liquid contracts with 10% and 6% spreads.
        path = _store(self.root, [
            _quote("0.45", "0.55", "0.40", "-0.02", 800, 900),   # spread 0.10/0.50=20%? recompute
            _quote("0.48", "0.52", "0.42", "-0.03", 600, 700),
        ])
        cal = calibrate_friction([path], max_relative_spread=Decimal("1"))
        self.assertEqual(cal.basis, "LIQUID")
        self.assertEqual(cal.liquid_count, 2)
        self.assertGreater(cal.median_relative_spread, Decimal("0"))
        self.assertEqual(cal.median_abs_theta_per_day, Decimal("0.025"))

    def test_falls_back_to_delta_eligible_when_illiquid(self) -> None:
        # After-hours: volume/oi zero, so liquidity filter matches nothing.
        path = _store(self.root, [
            _quote("0.45", "0.55", "0.40", "-0.02", 0, 0),
        ])
        cal = calibrate_friction([path])
        self.assertEqual(cal.basis, "ELIGIBLE_LIQUIDITY_UNMET")
        self.assertEqual(cal.liquid_count, 0)
        self.assertEqual(cal.delta_eligible_count, 1)

    def test_delta_band_filters_out_of_range_contracts(self) -> None:
        path = _store(self.root, [
            _quote("0.45", "0.55", "0.95", "-0.02", 0, 0),   # deep ITM, out of band
        ])
        with self.assertRaisesRegex(ValueError, "NO_ELIGIBLE_OPTION_QUOTES"):
            calibrate_friction([path])

    def test_premium_ceiling_excludes_expensive_contracts(self) -> None:
        # A $3.73 mid = $373 premium, above a $120 ceiling -> no affordable contract.
        path = _store(self.root, [
            _quote("3.60", "3.86", "0.40", "-0.30", 1400, 700),
        ])
        with self.assertRaisesRegex(ValueError, "NO_ELIGIBLE_OPTION_QUOTES"):
            calibrate_friction([path], max_relative_spread=Decimal("1"), maximum_premium_usd=Decimal("120"))

    def test_affordable_contract_passes_ceiling(self) -> None:
        path = _store(self.root, [
            _quote("0.45", "0.55", "0.40", "-0.02", 800, 900),   # $50 premium, under ceiling
        ])
        cal = calibrate_friction([path], max_relative_spread=Decimal("1"), maximum_premium_usd=Decimal("120"))
        self.assertEqual(cal.affordable_count, 1)
        self.assertEqual(cal.liquid_count, 1)

    def test_malformed_quote_is_skipped(self) -> None:
        path = _store(self.root, [
            {"bid_price": "abc", "ask_price": "0.55", "delta": "0.40"},   # bad bid
            _quote("0.45", "0.55", "0.40", "-0.02", 0, 0),
        ])
        cal = calibrate_friction([path])
        self.assertEqual(cal.delta_eligible_count, 1)


class DefaultSnapshotPathsTests(unittest.TestCase):
    def test_globs_recursively(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "2026-07-21").mkdir()
            (root / "2026-07-21" / "a.json").write_text("{}", encoding="utf-8")
            paths = default_snapshot_paths(root)
            self.assertEqual(1, len(paths))


if __name__ == "__main__":
    unittest.main()
