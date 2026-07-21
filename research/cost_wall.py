"""The cost wall: what edge a single-leg long option trade must clear to profit.

This is an *a priori* feasibility tool, not an empirical measurement. Before a
single trade is collected it answers, from real friction inputs:

- how much a round trip costs (spread + fees + latency slippage + theta),
- how far the option must rise just to break even (as % of premium),
- how far the *underlying* must move to break even (via delta),
- what win rate a given payoff needs just to overcome the wall, and how much
  higher that is than the cost-free breakeven,
- what per-trade expectancy a monthly return target implies.

It exists to screen out parameter regions that are mathematically hopeless
before any Pilot data is spent on them. It makes no profitability claim; a low
wall means "worth testing", never "profitable".

Convention matches the strategy contract: entry fills at MID + 25% of the
bid/ask spread; exit fills at the BID (MID - 50% of spread). Round-trip spread
cost is therefore 0.75 x spread per share. All money is Decimal USD.
"""

from __future__ import annotations

import random
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

HUNDRED = Decimal("100")  # option contract multiplier / percent base


def _decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class FrictionModel:
    per_contract_fee_usd: Decimal
    regulatory_exit_fee_usd: Decimal
    exit_latency_slippage_ticks: int
    option_tick_size_usd: Decimal
    entry_latency_slippage_ticks: int = 0

    @staticmethod
    def from_safety_config(path: str | Path = "config/safety.toml") -> "FrictionModel":
        with open(path, "rb") as handle:
            config = tomllib.load(handle)
        model = config.get("friction_model")
        if not isinstance(model, dict):
            raise ValueError("friction_model section is required in safety.toml")
        return FrictionModel(
            per_contract_fee_usd=_decimal(model["per_contract_fee_usd"]),
            regulatory_exit_fee_usd=_decimal(model["regulatory_exit_fee_usd"]),
            exit_latency_slippage_ticks=int(model["exit_latency_slippage_ticks"]),
            option_tick_size_usd=_decimal(model["option_tick_size_usd"]),
        )


@dataclass(frozen=True)
class TradeCostInputs:
    mid_premium_usd: Decimal          # option mid price per share (e.g. 0.50)
    relative_spread: Decimal          # bid/ask spread as a fraction of mid (e.g. 0.07)
    delta: Decimal                    # option delta magnitude (e.g. 0.40)
    underlying_price_usd: Decimal     # underlying price (e.g. 742.00)
    friction: FrictionModel
    contracts: int = 1
    holding_fraction_of_day: Decimal = Decimal("0.08")  # ~30 min of a 6.5h session
    theta_per_share_per_day_usd: Decimal = Decimal("0.02")  # magnitude, estimate
    entry_markup_of_spread: Decimal = Decimal("0.25")   # MID + 25% of spread
    exit_markup_of_spread: Decimal = Decimal("0.50")    # exit at BID = MID - 50%

    def absolute_spread_usd(self) -> Decimal:
        return self.mid_premium_usd * self.relative_spread

    def entry_price_usd(self) -> Decimal:
        return self.mid_premium_usd + self.entry_markup_of_spread * self.absolute_spread_usd()


@dataclass(frozen=True)
class CostWall:
    contracts: int
    premium_paid_usd: Decimal
    spread_cost_usd: Decimal
    fee_cost_usd: Decimal
    slippage_cost_usd: Decimal
    theta_cost_usd: Decimal
    total_cost_usd: Decimal
    cost_pct_of_premium: Decimal          # total_cost / premium_paid * 100
    breakeven_option_gain_pct: Decimal    # how much the option must rise, %
    breakeven_underlying_move_usd: Decimal
    breakeven_underlying_move_pct: Decimal

    def to_dict(self) -> dict[str, str | int]:
        return {
            "contracts": self.contracts,
            "premium_paid_usd": str(self.premium_paid_usd),
            "spread_cost_usd": str(self.spread_cost_usd),
            "fee_cost_usd": str(self.fee_cost_usd),
            "slippage_cost_usd": str(self.slippage_cost_usd),
            "theta_cost_usd": str(self.theta_cost_usd),
            "total_cost_usd": str(self.total_cost_usd),
            "cost_pct_of_premium": str(self.cost_pct_of_premium),
            "breakeven_option_gain_pct": str(self.breakeven_option_gain_pct),
            "breakeven_underlying_move_usd": str(self.breakeven_underlying_move_usd),
            "breakeven_underlying_move_pct": str(self.breakeven_underlying_move_pct),
        }


def _round(value: Decimal, places: str = "0.0001") -> Decimal:
    return value.quantize(Decimal(places))


def compute_cost_wall(inputs: TradeCostInputs) -> CostWall:
    if inputs.mid_premium_usd <= 0:
        raise ValueError("mid_premium_usd must be positive")
    if inputs.relative_spread < 0:
        raise ValueError("relative_spread cannot be negative")
    if inputs.delta <= 0 or inputs.underlying_price_usd <= 0:
        raise ValueError("delta and underlying_price_usd must be positive")
    if inputs.contracts <= 0:
        raise ValueError("contracts must be positive")

    contracts = Decimal(inputs.contracts)
    spread = inputs.absolute_spread_usd()
    entry_price = inputs.entry_price_usd()
    premium_paid = entry_price * HUNDRED * contracts

    spread_cost_per_share = (inputs.entry_markup_of_spread + inputs.exit_markup_of_spread) * spread
    spread_cost = spread_cost_per_share * HUNDRED * contracts
    fee_cost = inputs.friction.per_contract_fee_usd * Decimal("2") * contracts + (
        inputs.friction.regulatory_exit_fee_usd * contracts
    )
    slippage_ticks = Decimal(
        inputs.friction.entry_latency_slippage_ticks + inputs.friction.exit_latency_slippage_ticks
    )
    slippage_cost = slippage_ticks * inputs.friction.option_tick_size_usd * HUNDRED * contracts
    theta_cost = (
        inputs.theta_per_share_per_day_usd
        * inputs.holding_fraction_of_day
        * HUNDRED
        * contracts
    )
    total_cost = spread_cost + fee_cost + slippage_cost + theta_cost

    cost_pct = total_cost / premium_paid * HUNDRED
    # The option must rise by exactly total_cost in dollars to break even, i.e.
    # cost_pct of the premium paid.
    breakeven_option_gain_pct = cost_pct
    # First-order: option $ gain ~= delta * underlying_move * 100 * contracts.
    breakeven_underlying_move = total_cost / (inputs.delta * HUNDRED * contracts)
    breakeven_underlying_move_pct = breakeven_underlying_move / inputs.underlying_price_usd * HUNDRED

    return CostWall(
        contracts=inputs.contracts,
        premium_paid_usd=_round(premium_paid, "0.01"),
        spread_cost_usd=_round(spread_cost, "0.01"),
        fee_cost_usd=_round(fee_cost, "0.01"),
        slippage_cost_usd=_round(slippage_cost, "0.01"),
        theta_cost_usd=_round(theta_cost, "0.01"),
        total_cost_usd=_round(total_cost, "0.01"),
        cost_pct_of_premium=_round(cost_pct, "0.01"),
        breakeven_option_gain_pct=_round(breakeven_option_gain_pct, "0.01"),
        breakeven_underlying_move_usd=_round(breakeven_underlying_move, "0.0001"),
        breakeven_underlying_move_pct=_round(breakeven_underlying_move_pct, "0.0001"),
    )


@dataclass(frozen=True)
class RequiredEdge:
    gross_win_usd: Decimal
    gross_loss_usd: Decimal
    total_cost_usd: Decimal
    costless_breakeven_win_rate: Decimal   # ignoring costs
    breakeven_win_rate: Decimal            # after the cost wall
    win_rate_penalty_from_cost: Decimal    # how much the wall raises the bar
    target_per_trade_usd: Decimal
    win_rate_for_target: Decimal | None    # None if unreachable (>1)

    def to_dict(self) -> dict[str, str | None]:
        return {
            "gross_win_usd": str(self.gross_win_usd),
            "gross_loss_usd": str(self.gross_loss_usd),
            "total_cost_usd": str(self.total_cost_usd),
            "costless_breakeven_win_rate": str(self.costless_breakeven_win_rate),
            "breakeven_win_rate": str(self.breakeven_win_rate),
            "win_rate_penalty_from_cost": str(self.win_rate_penalty_from_cost),
            "target_per_trade_usd": str(self.target_per_trade_usd),
            "win_rate_for_target": (
                str(self.win_rate_for_target) if self.win_rate_for_target is not None else None
            ),
        }


def required_win_rate(
    *,
    gross_win_usd: Decimal,
    gross_loss_usd: Decimal,
    total_cost_usd: Decimal,
    target_per_trade_usd: Decimal = Decimal("0"),
) -> RequiredEdge:
    """Win rate needed for expectancy >= target, given a gross payoff structure.

    gross_win/gross_loss are the option-value moves on winning/losing trades
    BEFORE costs (both positive magnitudes). Costs are paid on every trade.

    expectancy = p*gross_win - (1-p)*gross_loss - total_cost
    => p_breakeven(target) = (target + total_cost + gross_loss) / (gross_win + gross_loss)
    """

    if gross_win_usd <= 0 or gross_loss_usd <= 0:
        raise ValueError("gross_win_usd and gross_loss_usd must be positive magnitudes")
    span = gross_win_usd + gross_loss_usd
    costless = gross_loss_usd / span
    breakeven = (total_cost_usd + gross_loss_usd) / span
    target_p = (target_per_trade_usd + total_cost_usd + gross_loss_usd) / span
    win_for_target: Decimal | None = target_p if target_p <= Decimal("1") else None
    return RequiredEdge(
        gross_win_usd=gross_win_usd,
        gross_loss_usd=gross_loss_usd,
        total_cost_usd=total_cost_usd,
        costless_breakeven_win_rate=_round(costless, "0.0001"),
        breakeven_win_rate=_round(breakeven, "0.0001"),
        win_rate_penalty_from_cost=_round(breakeven - costless, "0.0001"),
        target_per_trade_usd=target_per_trade_usd,
        win_rate_for_target=_round(win_for_target, "0.0001") if win_for_target is not None else None,
    )


def required_expectancy_for_monthly_target(
    *,
    account_usd: Decimal,
    monthly_target_pct: Decimal,
    trades_per_month: int,
) -> Decimal:
    """Per-trade net expectancy implied by a monthly return target."""

    if trades_per_month <= 0:
        raise ValueError("trades_per_month must be positive")
    target_profit = account_usd * monthly_target_pct / HUNDRED
    return _round(target_profit / Decimal(trades_per_month), "0.0001")


def expectancy_ci(
    *,
    win_rate: Decimal,
    gross_win_usd: Decimal,
    gross_loss_usd: Decimal,
    total_cost_usd: Decimal,
    trades: int,
    samples: int = 2000,
    seed: int = 17,
) -> tuple[Decimal, Decimal, Decimal]:
    """Monte-Carlo (mean, 2.5%, 97.5%) net expectancy for `trades` per-trade draws.

    Shows that even a positive-edge setup has a wide expectancy band at small
    trade counts, so a positive point estimate is not evidence on its own.
    """

    if not Decimal("0") <= win_rate <= Decimal("1"):
        raise ValueError("win_rate must be in [0, 1]")
    if trades <= 0 or samples < 100:
        raise ValueError("trades must be positive and samples >= 100")
    net_win = gross_win_usd - total_cost_usd
    net_loss = -gross_loss_usd - total_cost_usd
    p = float(win_rate)
    rng = random.Random(seed)
    means: list[Decimal] = []
    for _ in range(samples):
        total = Decimal("0")
        for _ in range(trades):
            total += net_win if rng.random() < p else net_loss
        means.append(total / Decimal(trades))
    means.sort()
    mean = sum(means, Decimal("0")) / Decimal(samples)
    low = means[int(samples * 0.025)]
    high = means[min(samples - 1, int(samples * 0.975))]
    return _round(mean, "0.0001"), _round(low, "0.0001"), _round(high, "0.0001")
