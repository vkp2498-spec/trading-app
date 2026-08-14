import unittest

import pandas as pd

from ml_v2_simulator import (
    SimulationAssumptions,
    compare_rr_cutoffs,
    probability_rr_surface,
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
