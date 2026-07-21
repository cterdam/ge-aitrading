from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from execution.raw_data_vault import RawDataVault
from monitoring.market_checks import (
    CheckStatus,
    to_evidence_document,
    verify_market_checks,
)
from monitoring.shadow_readiness import MONDAY_MARKET_CHECKS


def _store_snapshot(root: Path, *, received: datetime, quote_ts: str | None) -> Path:
    option_output = {"quotes": [{"bid": "1.10", "ask": "1.20"}]}
    if quote_ts is not None:
        option_output["quotes"][0]["updated_at"] = quote_ts
    tool_results = [
        {"tool": "get_equity_quotes", "output": {"results": [{"symbol": "SPY"}]}},
        {"tool": "get_equity_historicals", "output": {"bars": [{"close": "742.1"}]}},
        {"tool": "get_option_chains", "output": {"chain": {"id": "abc"}}},
        {"tool": "get_option_instruments", "output": {"instruments": [{"strike": "742"}]}},
        {"tool": "get_option_quotes", "output": option_output},
        {"tool": "get_earnings_results", "output": {"earnings": []}},
    ]
    receipt = RawDataVault(root).store(
        source="ROBINHOOD_OFFICIAL_MCP",
        request={"schema_version": 1, "transport": "CLAUDE_STREAM_JSON_HARVEST", "symbol": "SPY", "tool_calls": []},
        response={"tool_results": tool_results},
        source_updated_at=received - timedelta(seconds=1),
        received_at=received,
    )
    return receipt.path


class MarketChecksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = datetime(2026, 7, 21, 17, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_fresh_quote_snapshot_passes_market_data_checks(self) -> None:
        fresh_ts = (self.now - timedelta(seconds=3)).isoformat()
        path = _store_snapshot(self.root, received=self.now, quote_ts=fresh_ts)
        results = verify_market_checks(path)
        self.assertEqual(set(results), set(MONDAY_MARKET_CHECKS))
        self.assertEqual(results["official_raw_mcp_snapshot"].status, CheckStatus.PASS)
        self.assertEqual(results["raw_to_feature_reproducibility"].status, CheckStatus.PASS)
        self.assertEqual(results["fresh_option_quote"].status, CheckStatus.PASS)

    def test_account_and_session_are_unknown_not_pass(self) -> None:
        fresh_ts = (self.now - timedelta(seconds=3)).isoformat()
        path = _store_snapshot(self.root, received=self.now, quote_ts=fresh_ts)
        results = verify_market_checks(path)
        for name in (
            "official_instrument_session",
            "official_account_cash_reconciliation",
            "official_orders_positions_reconciliation",
        ):
            self.assertEqual(results[name].status, CheckStatus.UNKNOWN, name)
            self.assertFalse(results[name].passed)

    def test_stale_quote_fails_closed(self) -> None:
        stale_ts = (self.now - timedelta(seconds=90)).isoformat()
        path = _store_snapshot(self.root, received=self.now, quote_ts=stale_ts)
        results = verify_market_checks(path, maximum_option_quote_age_seconds=10)
        self.assertEqual(results["fresh_option_quote"].status, CheckStatus.FAIL)
        self.assertIn("QUOTE_STALE", results["fresh_option_quote"].reason or "")

    def test_missing_quote_timestamp_is_unknown(self) -> None:
        path = _store_snapshot(self.root, received=self.now, quote_ts=None)
        results = verify_market_checks(path)
        self.assertEqual(results["fresh_option_quote"].status, CheckStatus.UNKNOWN)

    def test_tampered_snapshot_fails_raw_check(self) -> None:
        fresh_ts = (self.now - timedelta(seconds=3)).isoformat()
        path = _store_snapshot(self.root, received=self.now, quote_ts=fresh_ts)
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["response"]["tool_results"][0]["output"]["results"][0]["symbol"] = "QQQ"
        path.write_bytes(
            json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        )
        results = verify_market_checks(path)
        self.assertEqual(results["official_raw_mcp_snapshot"].status, CheckStatus.FAIL)

    def test_supplied_account_reconciliation_can_pass(self) -> None:
        fresh_ts = (self.now - timedelta(seconds=3)).isoformat()
        path = _store_snapshot(self.root, received=self.now, quote_ts=fresh_ts)
        results = verify_market_checks(
            path,
            account_reconciliation={"reconciled": True, "evidence": ["settled=300; buying_power=300"]},
        )
        self.assertEqual(results["official_account_cash_reconciliation"].status, CheckStatus.PASS)

    def test_evidence_document_only_marks_pass_checks_satisfied(self) -> None:
        fresh_ts = (self.now - timedelta(seconds=3)).isoformat()
        path = _store_snapshot(self.root, received=self.now, quote_ts=fresh_ts)
        document = to_evidence_document(verify_market_checks(path))
        self.assertEqual(document["schema_version"], 1)
        checks = document["checks"]
        self.assertTrue(checks["official_raw_mcp_snapshot"]["passed"])
        self.assertFalse(checks["official_instrument_session"]["passed"])


class MarketChecksReadinessIntegrationTests(unittest.TestCase):
    def test_document_feeds_load_market_check_evidence(self) -> None:
        from monitoring.shadow_readiness import load_market_check_evidence

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fresh = datetime(2026, 7, 21, 17, 0, tzinfo=timezone.utc)
            path = _store_snapshot(root, received=fresh, quote_ts=(fresh - timedelta(seconds=2)).isoformat())
            document = to_evidence_document(verify_market_checks(path))
            evidence_path = root / "market_checks.json"
            evidence_path.write_text(json.dumps(document), encoding="utf-8")
            loaded = load_market_check_evidence(evidence_path)
            # Market-data checks satisfied; account/session still pending.
            self.assertTrue(loaded["official_raw_mcp_snapshot"])
            self.assertFalse(loaded["official_account_cash_reconciliation"])


if __name__ == "__main__":
    unittest.main()
