import unittest

import pandas as pd

from trade_forensics import (
    excursion_metrics,
    first_level_touch,
    post_exit_trade_diagnosis,
)


class TradeForensicsTests(unittest.TestCase):
    def setUp(self):
        self.candles = pd.DataFrame(
            [
                {"open": 100, "high": 104, "low": 98, "close": 103},
                {"open": 103, "high": 110, "low": 102, "close": 108},
                {"open": 108, "high": 109, "low": 95, "close": 96},
            ],
            index=pd.date_range("2026-07-20 10:00", periods=3, freq="5min", tz="Asia/Kolkata"),
        )

    def test_long_excursion_reports_peak_and_drawdown(self):
        result = excursion_metrics(self.candles, 100, 50, "BUY")
        self.assertEqual(result["max_favorable_points"], 10)
        self.assertEqual(result["max_adverse_points"], 5)
        self.assertEqual(result["max_favorable_pnl"], 500)
        self.assertEqual(result["max_adverse_pnl"], -250)

    def test_short_excursion_inverts_price_direction(self):
        result = excursion_metrics(self.candles, 100, 50, "SELL")
        self.assertEqual(result["max_favorable_points"], 5)
        self.assertEqual(result["max_adverse_points"], 10)
        self.assertEqual(result["max_favorable_pnl"], 250)
        self.assertEqual(result["max_adverse_pnl"], -500)

    def test_first_touch_detects_target_before_later_stop(self):
        outcome, timestamp = first_level_touch(self.candles, 108, 94, "BUY")
        self.assertEqual(outcome, "TARGET_FIRST")
        self.assertIsNotNone(timestamp)

    def test_losing_trade_reports_extra_stop_distance_needed_to_reach_target(self):
        candles = pd.DataFrame(
            [
                {"open": 92, "high": 96, "low": 80, "close": 84},
                {"open": 84, "high": 111, "low": 82, "close": 108},
            ],
            index=pd.date_range("2026-07-20 10:31", periods=2, freq="1min", tz="Asia/Kolkata"),
        )
        result = post_exit_trade_diagnosis(
            candles, 100, 92, 110, 90, -400, "BUY"
        )
        self.assertEqual(result["extra_stop_points_to_target"], 10)
        self.assertEqual(result["required_stop_price_to_target"], 80)
        self.assertEqual(result["loss_path_classification"], "TARGET_AFTER_RELAXING_STOP")

    def test_losing_trade_is_hard_loss_when_target_never_recovers(self):
        candles = pd.DataFrame(
            [
                {"open": 92, "high": 94, "low": 85, "close": 87},
                {"open": 87, "high": 89, "low": 72, "close": 75},
            ],
            index=pd.date_range("2026-07-20 10:31", periods=2, freq="1min", tz="Asia/Kolkata"),
        )
        result = post_exit_trade_diagnosis(
            candles, 100, 92, 110, 90, -400, "BUY"
        )
        self.assertEqual(result["loss_path_classification"], "HARD_LOSS")
        self.assertIsNone(result["extra_stop_points_to_target"])

    def test_best_post_exit_move_excludes_the_stop_touch_candle(self):
        candles = pd.DataFrame(
            [
                {"open": 92, "high": 96, "low": 91, "close": 95},
                {"open": 95, "high": 112, "low": 89, "close": 110},
            ],
            index=pd.date_range("2026-07-20 10:31", periods=2, freq="1min", tz="Asia/Kolkata"),
        )
        result = post_exit_trade_diagnosis(
            candles, 100, 92, 110, 90, 200, "BUY"
        )
        self.assertEqual(result["post_exit_max_move_points_before_stop"], 4)
        self.assertTrue(result["post_exit_stop_touched"])


if __name__ == "__main__":
    unittest.main()
