import unittest

from live_trade_filters import (
    bollinger_exhaustion_reversal,
    classify_market_regime,
    entry_structure_for_direction,
    live_entry_gate,
    structural_invalidation,
    underlying_exit_reason,
)
from signal_score import weighted_alignment_score


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

    def test_neutral_five_minute_can_confirm_timing_without_vetoing_fifteen_minute(self):
        technicals = self.bullish_technicals()
        technicals["five_min"].update(
            bias="NEUTRAL",
            confidence="LOW",
            momentum_score=3,
        )
        regime = classify_market_regime(technicals, extreme_atr_percent=2.0)
        structure = entry_structure_for_direction(technicals, "BULLISH")
        technicals.update(market_regime=regime, entry_structure=structure)

        decision = live_entry_gate(
            "BULLISH",
            technicals,
            82,
            range_minimum_score=80,
        )

        self.assertEqual(structure["type"], "RETEST_HOLD")
        self.assertTrue(structure["five_minute_timing_confirmed"])
        self.assertTrue(structure["qualified"])
        self.assertTrue(decision["allowed"])
        self.assertTrue(any("5M is neutral" in reason for reason in structure["reasons"]))

    def test_low_confidence_opposite_five_minute_is_watch_eligible(self):
        technicals = self.bullish_technicals()
        technicals["five_min"].update(
            bias="BEARISH",
            confidence="LOW",
            momentum_score=-1,
        )
        technicals["market_regime"] = classify_market_regime(
            technicals, extreme_atr_percent=2.0
        )
        technicals["entry_structure"] = entry_structure_for_direction(
            technicals, "BULLISH"
        )

        decision = live_entry_gate("BULLISH", technicals, 82)

        self.assertFalse(decision["allowed"])
        self.assertTrue(decision["watch_eligible"])
        self.assertIn("5M opposes BULLISH", decision["reason"])

    def test_medium_confidence_opposite_five_minute_is_hard_rejection(self):
        technicals = self.bullish_technicals()
        technicals["five_min"].update(
            bias="BEARISH",
            confidence="MEDIUM",
            momentum_score=-2,
        )
        technicals["market_regime"] = classify_market_regime(
            technicals, extreme_atr_percent=2.0
        )
        technicals["entry_structure"] = entry_structure_for_direction(
            technicals, "BULLISH"
        )

        decision = live_entry_gate("BULLISH", technicals, 90)

        self.assertFalse(decision["allowed"])
        self.assertFalse(decision["watch_eligible"])

    def test_fifteen_minute_direction_remains_mandatory(self):
        technicals = self.bullish_technicals()
        technicals["fifteen_min"] = {"bias": "NEUTRAL", "confidence": "LOW"}
        technicals["market_regime"] = classify_market_regime(
            technicals, extreme_atr_percent=2.0
        )
        technicals["entry_structure"] = entry_structure_for_direction(
            technicals, "BULLISH"
        )

        decision = live_entry_gate("BULLISH", technicals, 90)

        self.assertFalse(decision["allowed"])
        self.assertIn("15M direction is not aligned", decision["reason"])

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

    def test_score_58_does_not_bypass_directional_breadth_rejection(self):
        technicals = self.bullish_technicals()
        technicals["nifty_breadth"] = {"bias": "BEARISH", "confidence": "HIGH"}
        technicals["market_regime"] = classify_market_regime(
            technicals, extreme_atr_percent=2.0
        )
        technicals["entry_structure"] = entry_structure_for_direction(
            technicals, "BULLISH"
        )

        decision = live_entry_gate(
            "BULLISH",
            technicals,
            58,
            range_minimum_score=55,
            continuation_minimum_score=55,
        )

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

    def test_upper_band_exhaustion_requires_five_minute_reversal(self):
        technicals = {
            "fifteen_min": {
                "candle_time": "2026-07-29T10:15:00+05:30",
                "open": 112,
                "high": 115,
                "low": 107,
                "close": 109,
                "upper_band": 110,
                "lower_band": 90,
                "atr14": 5,
            },
            "five_min": {
                "open": 108,
                "close": 106,
                "bias": "BEARISH",
                "momentum_score": -3,
            },
        }
        result = bollinger_exhaustion_reversal(technicals)
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["direction"], "BEARISH")
        self.assertGreaterEqual(result["extension_multiple"], 2)

        technicals["five_min"].update(
            open=106,
            close=108,
            bias="BULLISH",
            momentum_score=3,
        )
        result = bollinger_exhaustion_reversal(technicals)
        self.assertFalse(result["confirmed"])

    def test_confirmed_reversal_uses_independent_score(self):
        technicals = {
            "bollinger_reversal": {
                "confirmed": True,
                "direction": "BEARISH",
                "extension_multiple": 2.1,
            },
            "two_hour": {"bias": "NEUTRAL", "confidence": "LOW"},
            "atm_option_flow": {"bias": "BULLISH", "volume_ratio": 1.5},
        }
        result = weighted_alignment_score(
            {
                "bias": "BEARISH",
                "strategy": "BOLLINGER_REVERSAL",
                "chain_bias": "BEARISH",
                "chain_confidence": "MEDIUM",
            },
            technicals,
            {"bias": "NEUTRAL"},
        )
        self.assertEqual(result["strategy"], "BOLLINGER_REVERSAL")
        self.assertEqual(result["grade"], "TRADE")
        self.assertGreaterEqual(result["score"], 75)


if __name__ == "__main__":
    unittest.main()
