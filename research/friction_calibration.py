"""Calibrate the cost-wall friction from real observed option quotes.

The cost-wall calculator ships with conservative, uncalibrated friction
assumptions. This module replaces the guesses with the empirical bid/ask spread
and theta actually observed in the immutable raw snapshots we already collect,
so the wall reflects the account's real numbers rather than a placeholder.

It reads option quotes straight out of the vault snapshots (the model never
touches them), restricts to the tradeable delta band, and reports the spread /
theta / mark distributions plus how many contracts also clear the liquidity
filter. Everything is deterministic and read-only; malformed quotes are
skipped, and an empty result is reported rather than invented.
"""

from __future__ import annotations

import glob
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from research.cost_wall import FrictionModel, TradeCostInputs, compute_cost_wall


def _num(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def extract_option_quotes(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect every option-quote dict from a raw snapshot, shape-agnostically.

    A quote is any dict carrying both bid_price and ask_price, wherever it sits
    in the harvested tool_results tree.
    """

    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "bid_price" in node and "ask_price" in node:
                found.append(node)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    response = envelope.get("response")
    walk(response if response is not None else envelope)
    return found


@dataclass(frozen=True)
class QuoteObservation:
    relative_spread: Decimal
    mid_price: Decimal
    abs_delta: Decimal
    abs_theta_per_day: Decimal
    volume: int
    open_interest: int


def _observe(quote: dict[str, Any]) -> QuoteObservation | None:
    bid = _num(quote.get("bid_price"))
    ask = _num(quote.get("ask_price"))
    if bid is None or ask is None or bid < 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / Decimal("2")
    if mid <= 0:
        return None
    delta = _num(quote.get("delta"))
    theta = _num(quote.get("theta"))
    volume = _num(quote.get("volume"))
    oi = _num(quote.get("open_interest"))
    return QuoteObservation(
        relative_spread=(ask - bid) / mid,
        mid_price=mid,
        abs_delta=abs(delta) if delta is not None else Decimal("0"),
        abs_theta_per_day=abs(theta) if theta is not None else Decimal("0"),
        volume=int(volume) if volume is not None else 0,
        open_interest=int(oi) if oi is not None else 0,
    )


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / Decimal("2")


def _quantile(values: list[Decimal], q: Decimal) -> Decimal:
    ordered = sorted(values)
    idx = int((Decimal(len(ordered) - 1) * q).to_integral_value())
    return ordered[idx]


@dataclass(frozen=True)
class CalibratedFriction:
    snapshots_used: int
    total_quotes: int
    delta_eligible_count: int
    affordable_count: int
    liquid_count: int
    basis: str
    sample_size: int
    median_relative_spread: Decimal
    p25_relative_spread: Decimal
    p75_relative_spread: Decimal
    median_mid_price: Decimal
    median_abs_delta: Decimal
    median_abs_theta_per_day: Decimal
    median_volume: int
    median_open_interest: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshots_used": self.snapshots_used,
            "total_quotes": self.total_quotes,
            "delta_eligible_count": self.delta_eligible_count,
            "affordable_count": self.affordable_count,
            "liquid_count": self.liquid_count,
            "distribution_basis": self.basis,
            "sample_size": self.sample_size,
            "median_relative_spread": str(self.median_relative_spread),
            "p25_relative_spread": str(self.p25_relative_spread),
            "p75_relative_spread": str(self.p75_relative_spread),
            "median_mid_price_usd": str(self.median_mid_price),
            "median_abs_delta": str(self.median_abs_delta),
            "median_abs_theta_per_day_usd": str(self.median_abs_theta_per_day),
            "median_volume": self.median_volume,
            "median_open_interest": self.median_open_interest,
        }


def calibrate_friction(
    snapshot_paths: Iterable[str | Path],
    *,
    delta_min: Decimal = Decimal("0.30"),
    delta_max: Decimal = Decimal("0.65"),
    max_relative_spread: Decimal = Decimal("0.05"),
    minimum_volume: int = 500,
    minimum_open_interest: int = 500,
    maximum_premium_usd: Decimal | None = None,
) -> CalibratedFriction:
    paths = [Path(p) for p in snapshot_paths]
    observations: list[QuoteObservation] = []
    total_quotes = 0
    used = 0
    for path in paths:
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        used += 1
        for quote in extract_option_quotes(envelope):
            total_quotes += 1
            observed = _observe(quote)
            if observed is not None:
                observations.append(observed)

    delta_eligible = [
        o for o in observations if delta_min <= o.abs_delta <= delta_max
    ]
    # Affordability is a hard account rule: premium = mid * 100 must fit the ceiling.
    if maximum_premium_usd is not None:
        affordable = [o for o in delta_eligible if o.mid_price * Decimal("100") <= maximum_premium_usd]
    else:
        affordable = delta_eligible
    liquid = [
        o for o in affordable
        if o.volume >= minimum_volume
        and o.open_interest >= minimum_open_interest
        and o.relative_spread <= max_relative_spread
    ]
    if liquid:
        basis, sample = "LIQUID", liquid
    elif affordable:
        basis, sample = "ELIGIBLE_LIQUIDITY_UNMET", affordable
    else:
        raise ValueError(
            "NO_ELIGIBLE_OPTION_QUOTES: "
            f"delta_eligible={len(delta_eligible)}, affordable_under_premium_ceiling={len(affordable)}. "
            "The sampled underlyings have no 0.30-0.65 delta contract within the premium ceiling; "
            "collect raw snapshots on cheaper underlyings."
        )

    spreads = [o.relative_spread for o in sample]
    return CalibratedFriction(
        snapshots_used=used,
        total_quotes=total_quotes,
        delta_eligible_count=len(delta_eligible),
        affordable_count=len(affordable),
        liquid_count=len(liquid),
        basis=basis,
        sample_size=len(sample),
        median_relative_spread=_median(spreads),
        p25_relative_spread=_quantile(spreads, Decimal("0.25")),
        p75_relative_spread=_quantile(spreads, Decimal("0.75")),
        median_mid_price=_median([o.mid_price for o in sample]),
        median_abs_delta=_median([o.abs_delta for o in sample]),
        median_abs_theta_per_day=_median([o.abs_theta_per_day for o in sample]),
        median_volume=int(_median([Decimal(o.volume) for o in sample])),
        median_open_interest=int(_median([Decimal(o.open_interest) for o in sample])),
    )


def calibrated_cost_wall(
    calibration: CalibratedFriction,
    *,
    underlying_price_usd: Decimal,
    friction: FrictionModel,
    holding_fraction_of_day: Decimal = Decimal("0.08"),
    contracts: int = 1,
) -> dict[str, Any]:
    """Run the cost wall on the median observed tradeable contract."""

    inputs = TradeCostInputs(
        mid_premium_usd=calibration.median_mid_price,
        relative_spread=calibration.median_relative_spread,
        delta=calibration.median_abs_delta if calibration.median_abs_delta > 0 else Decimal("0.40"),
        underlying_price_usd=underlying_price_usd,
        friction=friction,
        contracts=contracts,
        holding_fraction_of_day=holding_fraction_of_day,
        theta_per_share_per_day_usd=calibration.median_abs_theta_per_day,
    )
    return compute_cost_wall(inputs).to_dict()


def default_snapshot_paths(root: str | Path = "logs/raw") -> list[str]:
    return sorted(glob.glob(str(Path(root) / "**" / "*.json"), recursive=True))
