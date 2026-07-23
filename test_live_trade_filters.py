import unittest

from live_trade_filters import (
    classify_market_regime,
    entry_structure_for_direction,
    live_entry_gate,
    structural_invalidation,
    underlying_exit_reason,
)


class LiveTradeFilterTests(unittest.TestCase):
    def bullish_technicals(self):
        return {
            "five_min": {
                "bias": "BULLISH",
                "confidence": "HIGH",
                "close": 101.0,
                "high": 102.0,
                "low": 99.8,
                "prev_close": 100.5,
                "pivot": 100.0,
                "middle_band": 99.5,
                "upper_band": 103.0,
                "lower_band": 96.0,
                "vwap": 100.2,
                "vwap_bias": "BULLISH",
                "momentum_score": 4,
                "atr14": 1.0,
                "recent_swing_low": 98.8,
                "recent_swing_high": 102.0,
            },
            "fifteen_min": {"bias": "BULLISH", "confidence": "HIGH"},
            "two_hour": {"bias": "BULLISH", "confidence": "MEDIUM"},
            "nifty_breadth": {"bias": "BULLISH", "confidence": "MEDIUM"},
        }

    def test_trending_retest_is_allowed(self):
        technicals = self.bullish_technicals()
        regime = classify_market_regime(technicals, extreme_atr_percent=2.0)
        structure = entry_structure_for_direction(technicals, "BULLISH")
        technicals.update(market_regime=regime, entry_structure=structure)
        decision = live_entry_gate("BULLISH", technicals, 82)
        self.assertEqual(regime["regime"], "TREND")
        self.assertEqual(structure["type"], "RETEST_HOLD")
        self.assertTrue(decision["allowed"])

    def test_conflicting_breadth_rejects_entry(self):
        technicals = self.bullish_technicals()
        technicals["nifty_breadth"] = {"bias": "BEARISH", "confidence": "HIGH"}
        technicals["market_regime"] = classify_market_regime(
            technicals, extreme_atr_percent=2.0
        )
        technicals["entry_structure"] = entry_structure_for_direction(technicals, "BULLISH")
        decision = live_entry_gate("BULLISH", technicals, 90)
        self.assertFalse(decision["allowed"])
        self.assertIn("breadth", decision["reason"])

    def test_structural_stop_uses_nearest_defensible_level(self):
        result = structural_invalidation(self.bullish_technicals(), "BULLISH", atr_buffer=0.2)
        self.assertEqual(result["reference"], "VWAP")
        self.assertAlmostEqual(result["stop_underlying"], 100.0)

    def test_underlying_structural_and_time_stops(self):
        state = {
            "direction": "BULLISH",
            "underlying_entry_price": 100,
            "underlying_structural_stop": 98,
            "target_points": 10,
        }
        self.assertEqual(
            underlying_exit_reason(state, 97.9, 5),
            "STRUCTURAL_STOP",
        )
        self.assertEqual(
            underlying_exit_reason(state, 100.5, 21),
            "TIME_STOP",
        )
        self.assertIsNone(underlying_exit_reason(state, 102, 21))


if __name__ == "__main__":
    unittest.main()
