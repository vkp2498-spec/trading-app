import unittest

import pandas as pd

from trade_forensics import excursion_metrics, first_level_touch


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


if __name__ == "__main__":
    unittest.main()
