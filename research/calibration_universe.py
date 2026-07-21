"""Research-only universe of cheap underlyings for cost-wall calibration.

This list exists ONLY to let friction-calibrate measure the affordable option
universe. It is never a trading universe: the trading path reads
config/universe.toml. The loader enforces that separation — a file that does
not declare itself research-only and non-tradeable is rejected, so it can never
be mistaken for a trade-candidate source.
"""

from __future__ import annotations

import tomllib
from pathlib import Path


class InvalidCalibrationUniverseError(ValueError):
    pass


def load_calibration_universe(
    path: str | Path = "config/calibration_universe.toml",
) -> list[str]:
    with Path(path).open("rb") as handle:
        policy = tomllib.load(handle)
    if policy.get("schema_version") != 1:
        raise InvalidCalibrationUniverseError("schema_version must equal 1")
    if policy.get("status") != "RESEARCH_ONLY":
        raise InvalidCalibrationUniverseError("status must be RESEARCH_ONLY")
    if policy.get("purpose") != "COST_WALL_CALIBRATION":
        raise InvalidCalibrationUniverseError("purpose must be COST_WALL_CALIBRATION")
    if policy.get("never_a_trading_universe") is not True:
        raise InvalidCalibrationUniverseError("never_a_trading_universe must be true")
    symbols = policy.get("symbols")
    if not isinstance(symbols, list) or not symbols or len(symbols) != len(set(symbols)):
        raise InvalidCalibrationUniverseError("symbols must be a non-empty unique list")
    if any(not isinstance(symbol, str) or symbol != symbol.upper() for symbol in symbols):
        raise InvalidCalibrationUniverseError("symbols must be uppercase strings")
    return list(symbols)
