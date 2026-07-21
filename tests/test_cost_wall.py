from __future__ import annotations

import unittest
from decimal import Decimal

from research.cost_wall import (
    FrictionModel,
    TradeCostInputs,
    compute_cost_wall,
    expectancy_ci,
    required_expectancy_for_monthly_target,
    required_win_rate,
)


FRICTION = FrictionModel(
    per_contract_fee_usd=Decimal("0.15"),
    regulatory_exit_fee_usd=Decimal("0.10"),
    exit_latency_slippage_ticks=1,
    option_tick_size_usd=Decimal("0.01"),
)


class CostWallTests(unittest.TestCase):
    def base_inputs(self, **overrides) -> TradeCostInputs:
        values = dict(
            mid_premium_usd=Decimal("0.50"),
            relative_spread=Decimal("0.08"),
            delta=Decimal("0.40"),
            underlying_price_usd=Decimal("742"),
            friction=FRICTION,
            contracts=1,
            holding_fraction_of_day=Decimal("0"),      # isolate spread+fee+slippage
            theta_per_share_per_day_usd=Decimal("0.02"),
        )
        values.update(overrides)
        return TradeCostInputs(**values)

    def test_spread_fee_slippage_components(self) -> None:
        wall = compute_cost_wall(self.base_inputs())
        # spread = 0.50*0.08 = 0.04; round-trip 0.75*0.04 = 0.03/share -> $3.00
        self.assertEqual(Decimal("3.00"), wall.spread_cost_usd)
        # fees = 0.15*2 + 0.10 = 0.40
        self.assertEqual(Decimal("0.40"), wall.fee_cost_usd)
        # slippage = 1 tick * 0.01 * 100 = 1.00
        self.assertEqual(Decimal("1.00"), wall.slippage_cost_usd)
        self.assertEqual(Decimal("0.00"), wall.theta_cost_usd)
        self.assertEqual(Decimal("4.40"), wall.total_cost_usd)

    def test_cost_pct_and_breakeven_move(self) -> None:
        wall = compute_cost_wall(self.base_inputs())
        # premium paid = (0.50 + 0.25*0.04)*100 = 51.00
        self.assertEqual(Decimal("51.00"), wall.premium_paid_usd)
        # 4.40 / 51.00 * 100 ~= 8.63%
        self.assertEqual(Decimal("8.63"), wall.cost_pct_of_premium)
        # underlying move to break even = 4.40 / (0.40*100) = 0.11 -> /742 ~= 0.0148%
        self.assertEqual(Decimal("0.1100"), wall.breakeven_underlying_move_usd)

    def test_cheaper_contract_has_a_higher_wall(self) -> None:
        expensive = compute_cost_wall(self.base_inputs(mid_premium_usd=Decimal("2.00")))
        cheap = compute_cost_wall(self.base_inputs(mid_premium_usd=Decimal("0.30")))
        self.assertGreater(cheap.cost_pct_of_premium, expensive.cost_pct_of_premium)

    def test_theta_adds_with_holding_time(self) -> None:
        no_theta = compute_cost_wall(self.base_inputs(holding_fraction_of_day=Decimal("0")))
        held = compute_cost_wall(self.base_inputs(holding_fraction_of_day=Decimal("0.5")))
        self.assertGreater(held.theta_cost_usd, no_theta.theta_cost_usd)

    def test_invalid_inputs_rejected(self) -> None:
        with self.assertRaises(ValueError):
            compute_cost_wall(self.base_inputs(mid_premium_usd=Decimal("0")))
        with self.assertRaises(ValueError):
            compute_cost_wall(self.base_inputs(delta=Decimal("0")))


class RequiredEdgeTests(unittest.TestCase):
    def test_cost_raises_the_breakeven_win_rate(self) -> None:
        edge = required_win_rate(
            gross_win_usd=Decimal("20"),
            gross_loss_usd=Decimal("10"),
            total_cost_usd=Decimal("6"),
        )
        # costless = 10/30 = 0.3333; with cost = 16/30 = 0.5333
        self.assertEqual(Decimal("0.3333"), edge.costless_breakeven_win_rate)
        self.assertEqual(Decimal("0.5333"), edge.breakeven_win_rate)
        self.assertEqual(Decimal("0.2000"), edge.win_rate_penalty_from_cost)

    def test_unreachable_target_returns_none(self) -> None:
        edge = required_win_rate(
            gross_win_usd=Decimal("5"),
            gross_loss_usd=Decimal("10"),
            total_cost_usd=Decimal("6"),
            target_per_trade_usd=Decimal("50"),
        )
        self.assertIsNone(edge.win_rate_for_target)

    def test_monthly_target_expectancy(self) -> None:
        # 4% of $300 = $12 over 20 trades = $0.60/trade
        value = required_expectancy_for_monthly_target(
            account_usd=Decimal("300"),
            monthly_target_pct=Decimal("4"),
            trades_per_month=20,
        )
        self.assertEqual(Decimal("0.6000"), value)


class ExpectancyCiTests(unittest.TestCase):
    def test_ci_is_reproducible_and_ordered(self) -> None:
        a = expectancy_ci(
            win_rate=Decimal("0.55"), gross_win_usd=Decimal("20"),
            gross_loss_usd=Decimal("10"), total_cost_usd=Decimal("6"),
            trades=40, samples=500, seed=17,
        )
        b = expectancy_ci(
            win_rate=Decimal("0.55"), gross_win_usd=Decimal("20"),
            gross_loss_usd=Decimal("10"), total_cost_usd=Decimal("6"),
            trades=40, samples=500, seed=17,
        )
        self.assertEqual(a, b)
        mean, low, high = a
        self.assertLessEqual(low, mean)
        self.assertLessEqual(mean, high)


if __name__ == "__main__":
    unittest.main()
