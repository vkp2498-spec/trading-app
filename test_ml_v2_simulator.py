import unittest

import pandas as pd

from ml_v2_simulator import (
    SimulationAssumptions,
    compare_rr_cutoffs,
    probability_portfolio_curve,
    probability_rr_surface,
    simulate_portfolio,
    simulate_trades,
    split_samples,
    summarize,
)


class MlV2SimulatorTests(unittest.TestCase):
    def forecasts(self):
        index = pd.to_datetime(["2026-01-02 09:15", "2026-01-05 09:15"])
        return pd.DataFrame({
            "open": [100.0, 100.0],
            "high": [102.0, 101.2],
            "low": [98.0, 99.8],
            "close": [101.0, 100.5],
            "future_up_percent": [2.0, 1.2],
            "future_down_percent": [2.0, 0.2],
            "call_probability": [0.8, 0.8],
            "call_target_percent": [1.0, 1.0],
            "call_stop_percent": [1.0, 1.0],
            "call_reward_risk": [1.0, 1.0],
            "call_expected_value_r": [0.6, 0.6],
            "put_probability": [0.4, 0.4],
            "put_target_percent": [1.0, 1.0],
            "put_stop_percent": [1.0, 1.0],
            "put_reward_risk": [1.0, 1.0],
            "put_expected_value_r": [-0.2, -0.2],
        }, index=index)

    def test_both_hit_is_conservatively_a_stop(self):
        trades = simulate_trades(self.forecasts(), SimulationAssumptions())
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades.iloc[0]["outcome"], "BOTH_STOP_FIRST")
        self.assertLess(trades.iloc[0]["net_pnl"], 0)
        self.assertEqual(trades.iloc[1]["outcome"], "TARGET")
        self.assertGreater(trades.iloc[1]["net_pnl"], 0)

    def test_rr_cutoff_changes_trade_count(self):
        forecasts = self.forecasts()
        forecasts.loc[forecasts.index[0], "call_reward_risk"] = 0.4
        comparison = compare_rr_cutoffs(
            forecasts,
            SimulationAssumptions(),
            cutoffs=(None, 0.5),
        )
        self.assertEqual(comparison.iloc[0]["trades"], 2)
        self.assertEqual(comparison.iloc[1]["trades"], 1)

    def test_summary_reports_target_and_stop(self):
        summary = summarize(simulate_trades(self.forecasts(), SimulationAssumptions()))
        self.assertEqual(summary["targets"], 1)
        self.assertEqual(summary["stops"], 1)
        self.assertEqual(summary["win_probability"], 0.5)

    def test_probability_rr_surface_separates_call_and_put(self):
        surface = probability_rr_surface(
            self.forecasts(),
            SimulationAssumptions(),
            probability_cutoffs=(0.5, 0.9),
            rr_cutoffs=(0.0, 1.5),
        )
        self.assertEqual(len(surface), 8)
        self.assertEqual(set(surface["direction"]), {"CALL", "PUT"})
        call_default = surface[
            (surface["direction"] == "CALL")
            & (surface["probability_cutoff"] == 0.5)
            & (surface["rr_cutoff"] == 0.0)
        ].iloc[0]
        self.assertEqual(call_default["trades"], 2)
        self.assertEqual(
            surface[(surface["probability_cutoff"] == 0.9)]["trades"].sum(),
            0,
        )

    def test_portfolio_uses_whole_lots_and_current_cash(self):
        trades = simulate_trades(
            self.forecasts().iloc[[1]],
            SimulationAssumptions(round_trip_cost=250, round_trip_cost_percent=0),
        )
        ledger, portfolio = simulate_portfolio(
            trades,
            SimulationAssumptions(round_trip_cost=250, round_trip_cost_percent=0),
        )
        self.assertEqual(ledger.iloc[0]["lots"], 10)
        self.assertEqual(ledger.iloc[0]["quantity"], 650)
        self.assertEqual(ledger.iloc[0]["deployed_capital"], 97_500)
        self.assertEqual(portfolio["executed_trades"], 1)
        self.assertAlmostEqual(
            portfolio["final_equity"],
            100_000 + 0.5 * 650 - 250,
        )

    def test_simultaneous_signals_split_available_equity(self):
        forecasts = self.forecasts().iloc[[1]].copy()
        forecasts.loc[:, "put_probability"] = 0.8
        forecasts.loc[:, "put_expected_value_r"] = 0.6
        trades = simulate_trades(
            forecasts,
            SimulationAssumptions(round_trip_cost=0, round_trip_cost_percent=0),
        )
        ledger, portfolio = simulate_portfolio(
            trades,
            SimulationAssumptions(round_trip_cost=0, round_trip_cost_percent=0),
        )
        self.assertEqual(len(ledger), 2)
        self.assertEqual(set(ledger["lots"]), {5})
        self.assertEqual(set(ledger["deployed_capital"]), {48_750})
        self.assertTrue(portfolio["completed_all_signals"])

    def test_portfolio_skips_signal_when_one_lot_is_unaffordable(self):
        trades = simulate_trades(
            self.forecasts().iloc[[1]],
            SimulationAssumptions(round_trip_cost=0, round_trip_cost_percent=0),
        )
        ledger, portfolio = simulate_portfolio(
            trades,
            SimulationAssumptions(
                capital_per_trade=9_000,
                round_trip_cost=0,
                round_trip_cost_percent=0,
            ),
        )
        self.assertEqual(ledger.iloc[0]["status"], "SKIPPED_INSUFFICIENT_CAPITAL")
        self.assertEqual(portfolio["executed_trades"], 0)
        self.assertEqual(portfolio["skipped_trades"], 1)
        self.assertFalse(portfolio["completed_all_signals"])

    def test_fixed_nifty_points_replace_model_exit_levels(self):
        assumptions = SimulationAssumptions(
            fixed_target_underlying_points=30,
            fixed_stop_underlying_points=30,
            round_trip_cost_percent=0,
        )
        trades = simulate_trades(self.forecasts().iloc[[1]], assumptions)
        self.assertEqual(trades.iloc[0]["exit_source"], "FIXED_NIFTY_POINTS")
        self.assertEqual(trades.iloc[0]["underlying_target_points"], 30)
        self.assertEqual(trades.iloc[0]["underlying_stop_points"], 30)
        self.assertEqual(trades.iloc[0]["applied_reward_risk"], 1)
        self.assertEqual(trades.iloc[0]["option_target_points"], 15)
        self.assertEqual(trades.iloc[0]["option_stop_points"], 15)

    def test_probability_curve_keeps_call_and_put_separate(self):
        forecasts = self.forecasts().copy()
        forecasts.loc[:, "put_probability"] = 0.8
        curve = probability_portfolio_curve(
            forecasts,
            SimulationAssumptions(
                fixed_target_underlying_points=30,
                fixed_stop_underlying_points=30,
                round_trip_cost_percent=0,
            ),
            probability_cutoffs=(0.5, 0.85),
        )
        self.assertEqual(len(curve), 4)
        self.assertEqual(set(curve["direction"]), {"CALL", "PUT"})
        self.assertEqual(curve[curve["probability_cutoff"] == 0.5]["signals"].sum(), 4)
        self.assertEqual(curve[curve["probability_cutoff"] == 0.85]["signals"].sum(), 0)

    def test_fixed_train_and_test_windows_do_not_overlap(self):
        index = pd.date_range("2023-01-02", periods=520, freq="B", tz="Asia/Kolkata")
        samples = pd.DataFrame({
            "call_label": [index_value % 2 for index_value in range(len(index))],
            "put_label": [(index_value + 1) % 2 for index_value in range(len(index))],
        }, index=index)
        training, testing, bounds = split_samples(samples, "2024-07-01")
        self.assertLess(training.index.max(), testing.index.min())
        self.assertEqual(bounds[1], pd.Timestamp("2024-07-01"))
        self.assertGreaterEqual(len(training), 240)
        self.assertGreaterEqual(len(testing), 60)


if __name__ == "__main__":
    unittest.main()
