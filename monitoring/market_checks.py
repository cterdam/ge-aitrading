"""Deterministic local adjudication of the six official market-time checks.

Until now the six checks that gate formal Shadow (see
monitoring.shadow_readiness.MONDAY_MARKET_CHECKS) were reported by the market-
hours agent itself. This module lets deterministic local code independently
decide each check from the immutable raw snapshot, so `monday_go` no longer
rests on the agent's self-report.

Design honesty: the raw collector is market-data-only (it deliberately excludes
every account/order/position/session tool so no identifier can enter the
vault). Checks that need those domains therefore return UNKNOWN with a precise
reason unless a separately-obtained, already-reconciled result is supplied.
UNKNOWN and FAIL both fail closed: neither can satisfy a check.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from execution.official_mcp_collector import (
    RAW_REQUIRED_TOOLS,
    _ISO_TIMESTAMP,
    _parse_iso_aware,
)
from execution.raw_data_vault import RawDataVault
from monitoring.shadow_readiness import MONDAY_MARKET_CHECKS


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class MarketCheckResult:
    name: str
    status: CheckStatus
    evidence: tuple[str, ...]
    reason: str | None

    @property
    def passed(self) -> bool:
        return self.status is CheckStatus.PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "status": self.status.value,
            "evidence": list(self.evidence),
            "reason": self.reason,
        }


def _load_envelope(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _tool_results(envelope: Mapping[str, Any]) -> list[dict[str, Any]]:
    response = envelope.get("response")
    if not isinstance(response, dict):
        return []
    results = response.get("tool_results")
    return [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []


def _market_projection_digest(envelope: Mapping[str, Any]) -> str:
    """Deterministic digest of the harvested market data.

    Canonicalizes the tool_results into sorted-key JSON and hashes it. Two
    independent parses of the same immutable snapshot must yield the same
    digest, which is what 'identical raw snapshot -> identical features without
    an LLM' means at the transport-to-normalized-structure boundary.
    """

    projection = [
        {"tool": r.get("tool"), "output": r.get("output")}
        for r in _tool_results(envelope)
    ]
    canonical = json.dumps(projection, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _check_raw_snapshot(path: Path) -> MarketCheckResult:
    name = "official_raw_mcp_snapshot"
    try:
        receipt = RawDataVault.verify(path)
    except ValueError as error:
        return MarketCheckResult(name, CheckStatus.FAIL, (), f"VAULT_VERIFY_FAILED:{error}")
    try:
        envelope = _load_envelope(path)
    except (OSError, json.JSONDecodeError) as error:
        return MarketCheckResult(name, CheckStatus.FAIL, (), f"SNAPSHOT_UNREADABLE:{error}")
    if envelope.get("source") != "ROBINHOOD_OFFICIAL_MCP":
        return MarketCheckResult(name, CheckStatus.FAIL, (), "SOURCE_NOT_OFFICIAL_MCP")
    called = {r.get("tool") for r in _tool_results(envelope)}
    missing = RAW_REQUIRED_TOOLS - called
    if missing:
        return MarketCheckResult(
            name, CheckStatus.FAIL, (), "MISSING_TOOLS:" + ",".join(sorted(missing))
        )
    return MarketCheckResult(
        name,
        CheckStatus.PASS,
        (f"snapshot_id={receipt.snapshot_id}", f"sha256={receipt.content_sha256}"),
        None,
    )


def _check_reproducibility(path: Path, raw_ok: bool) -> MarketCheckResult:
    name = "raw_to_feature_reproducibility"
    if not raw_ok:
        return MarketCheckResult(name, CheckStatus.UNKNOWN, (), "DEPENDS_ON_RAW_SNAPSHOT")
    try:
        first = _market_projection_digest(_load_envelope(path))
        second = _market_projection_digest(_load_envelope(path))
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return MarketCheckResult(name, CheckStatus.FAIL, (), f"PROJECTION_FAILED:{error}")
    if first != second:
        return MarketCheckResult(name, CheckStatus.FAIL, (), "NON_DETERMINISTIC_PROJECTION")
    return MarketCheckResult(name, CheckStatus.PASS, (f"projection_sha256={first}",), None)


def _received_at(envelope: Mapping[str, Any]) -> datetime | None:
    value = envelope.get("received_at")
    if not isinstance(value, str):
        return None
    return _parse_iso_aware(value)


def _check_fresh_option_quote(path: Path, raw_ok: bool, max_age_seconds: int) -> MarketCheckResult:
    name = "fresh_option_quote"
    if not raw_ok:
        return MarketCheckResult(name, CheckStatus.UNKNOWN, (), "DEPENDS_ON_RAW_SNAPSHOT")
    try:
        envelope = _load_envelope(path)
    except (OSError, json.JSONDecodeError) as error:
        return MarketCheckResult(name, CheckStatus.UNKNOWN, (), f"SNAPSHOT_UNREADABLE:{error}")
    received = _received_at(envelope)
    if received is None:
        return MarketCheckResult(name, CheckStatus.UNKNOWN, (), "NO_TRUSTED_RECEIPT_TIME")
    quote_result = next(
        (r for r in _tool_results(envelope) if r.get("tool") == "get_option_quotes"), None
    )
    if quote_result is None:
        return MarketCheckResult(name, CheckStatus.UNKNOWN, (), "NO_OPTION_QUOTE_RESULT")
    text = json.dumps(quote_result.get("output"), ensure_ascii=False)
    freshest: datetime | None = None
    for match in _ISO_TIMESTAMP.findall(text):
        parsed = _parse_iso_aware(match)
        if parsed is None or parsed > received:
            continue
        if freshest is None or parsed > freshest:
            freshest = parsed
    if freshest is None:
        return MarketCheckResult(name, CheckStatus.UNKNOWN, (), "NO_QUOTE_TIMESTAMP")
    age = (received - freshest).total_seconds()
    if age > max_age_seconds:
        return MarketCheckResult(
            name, CheckStatus.FAIL, (), f"QUOTE_STALE:{age:.1f}s>{max_age_seconds}s"
        )
    return MarketCheckResult(
        name, CheckStatus.PASS, (f"quote_age_seconds={age:.3f}", f"quote_updated_at={freshest.isoformat()}"), None
    )


def _domain_result(name: str, reconciliation: Mapping[str, Any] | None) -> MarketCheckResult:
    """Account/orders-positions checks: UNKNOWN unless a reconciled result is fed.

    The market-data raw snapshot never contains these domains (by design), so
    they can only PASS when a caller supplies an already-verified reconciliation
    object of the form {"reconciled": true, "evidence": ["...", ...]}.
    """

    if reconciliation is None:
        return MarketCheckResult(
            name, CheckStatus.UNKNOWN, (),
            "REQUIRES_ACCOUNT_DOMAIN_READ_ABSENT_FROM_MARKET_DATA_SNAPSHOT",
        )
    if reconciliation.get("reconciled") is not True:
        reason = str(reconciliation.get("reason") or "NOT_RECONCILED")
        return MarketCheckResult(name, CheckStatus.FAIL, (), reason)
    evidence = reconciliation.get("evidence")
    if not isinstance(evidence, list) or not evidence or not all(
        isinstance(item, str) and item.strip() for item in evidence
    ):
        return MarketCheckResult(name, CheckStatus.FAIL, (), "RECONCILIATION_EVIDENCE_MISSING")
    return MarketCheckResult(name, CheckStatus.PASS, tuple(evidence), None)


def verify_market_checks(
    snapshot_path: str | Path,
    *,
    maximum_option_quote_age_seconds: int = 10,
    account_reconciliation: Mapping[str, Any] | None = None,
    orders_positions_reconciliation: Mapping[str, Any] | None = None,
) -> dict[str, MarketCheckResult]:
    """Adjudicate all six market checks deterministically from a raw snapshot."""

    path = Path(snapshot_path)
    raw = _check_raw_snapshot(path)
    raw_ok = raw.status is CheckStatus.PASS
    results = {
        "official_raw_mcp_snapshot": raw,
        "raw_to_feature_reproducibility": _check_reproducibility(path, raw_ok),
        "official_instrument_session": MarketCheckResult(
            "official_instrument_session",
            CheckStatus.UNKNOWN,
            (),
            "NO_OFFICIAL_SESSION_TOOL_IN_MARKET_DATA_SNAPSHOT",
        ),
        "official_account_cash_reconciliation": _domain_result(
            "official_account_cash_reconciliation", account_reconciliation
        ),
        "official_orders_positions_reconciliation": _domain_result(
            "official_orders_positions_reconciliation", orders_positions_reconciliation
        ),
        "fresh_option_quote": _check_fresh_option_quote(path, raw_ok, maximum_option_quote_age_seconds),
    }
    # Guard against drift from the canonical check set.
    assert set(results) == set(MONDAY_MARKET_CHECKS)
    return results


def to_evidence_document(results: Mapping[str, MarketCheckResult]) -> dict[str, Any]:
    """Render results in the schema that shadow-readiness --market-checks loads."""

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "checks": {name: result.to_dict() for name, result in results.items()},
        "note": "Deterministic local adjudication. UNKNOWN/FAIL both fail closed; "
        "a check counts as satisfied only when status is PASS with evidence.",
    }
