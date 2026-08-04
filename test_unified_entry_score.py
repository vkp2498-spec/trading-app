import unittest

from unified_entry_score import (
    COMPONENT_WEIGHTS,
    UNIFIED_SCORE_VERSION,
    unified_entry_score,
)


class UnifiedEntryScoreTests(unittest.TestCase):
    def technicals(self, breadth_score=70, structure_qualified=True):
        return {
            "five_min": {"bias": "BULLISH"},
            "fifteen_min": {"bias": "BULLISH"},
            "two_hour": {"bias": "BULLISH"},
            "entry_structure": {
                "type": "RETEST_HOLD" if structure_qualified else "NONE",
                "qualified": structure_qualified,
                "watch_eligible": False,
            },
            "market_regime": {
                "regime": "TREND",
                "direction": "BULLISH",
            },
            "nifty_breadth": {
                "bias": "BULLISH" if breadth_score >= 30 else "BEARISH",
                "score": breadth_score,
            },
        }

    def feasibility(self, allowed=True):
        return {
            "allowed": allowed,
            "technical_target_candidates": (
                [{"timeframe": "15M", "target_price": 110}] if allowed else []
            ),
            "technical_reward_risk": 1.0 if allowed else 0.2,
            "minimum_reward_risk": 0.8,
            "entry_extension_percent": 0.5,
            "maximum_entry_extension_percent": 1.5,
        }

    def test_weights_total_one_hundred(self):
        self.assertEqual(sum(COMPONENT_WEIGHTS.values()), 100)

    def test_aligned_setup_gets_high_unified_score(self):
        result = unified_entry_score(
            {"score": 80},
            self.technicals(),
            {"bias": "BULLISH", "score": 60},
            "BULLISH",
            feasibility=self.feasibility(),
            days_to_expiry=7,
            live_gate={"allowed": True, "reason": "TREND with RETEST_HOLD"},
        )

        self.assertEqual(result["score_version"], UNIFIED_SCORE_VERSION)
        self.assertGreaterEqual(result["score"], 85)
        self.assertEqual(result["components"]["direction_and_structure"], 25)

    def test_conflicting_breadth_reduces_score_instead_of_being_a_veto(self):
        aligned = unified_entry_score(
            {"score": 80},
            self.technicals(breadth_score=70),
            {"bias": "NEUTRAL", "score": 0},
            "BULLISH",
            feasibility=self.feasibility(),
            days_to_expiry=7,
        )
        conflicting = unified_entry_score(
            {"score": 80},
            self.technicals(breadth_score=-70),
            {"bias": "NEUTRAL", "score": 0},
            "BULLISH",
            feasibility=self.feasibility(),
            days_to_expiry=7,
            live_gate={
                "allowed": False,
                "reason": "constituent breadth materially conflicts",
            },
        )

        self.assertLess(conflicting["score"], aligned["score"])
        self.assertGreater(conflicting["score"], 0)
        self.assertFalse(conflicting["details"]["former_live_gate_allowed"])

    def test_structure_and_reward_risk_failures_lower_same_base_score(self):
        strong = unified_entry_score(
            {"score": 75},
            self.technicals(),
            {"bias": "NEUTRAL", "score": 0},
            "BULLISH",
            feasibility=self.feasibility(),
        )
        weak = unified_entry_score(
            {"score": 75},
            self.technicals(structure_qualified=False),
            {"bias": "NEUTRAL", "score": 0},
            "BULLISH",
            feasibility=self.feasibility(allowed=False),
        )

        self.assertLess(weak["score"], strong["score"])
        self.assertEqual(weak["components"]["trade_feasibility"], 3.5)

    def test_bearish_direction_orients_negative_context_as_support(self):
        technicals = self.technicals(breadth_score=-70)
        for timeframe in ("five_min", "fifteen_min", "two_hour"):
            technicals[timeframe]["bias"] = "BEARISH"
        technicals["entry_structure"].update(direction="BEARISH")
        technicals["market_regime"]["direction"] = "BEARISH"
        result = unified_entry_score(
            {"score": 70},
            technicals,
            {"bias": "BEARISH", "score": -60},
            "BEARISH",
            feasibility=self.feasibility(),
        )

        self.assertGreater(result["components"]["breadth"], 15)
        self.assertGreater(result["components"]["institutional"], 7)


if __name__ == "__main__":
    unittest.main()
