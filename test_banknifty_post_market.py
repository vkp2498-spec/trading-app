import unittest

import pandas as pd

from banknifty_post_market import (
    _deduplicate_episodes,
    choose_technical_direction,
    technical_only_score,
)


class BankNiftyPostMarketTests(unittest.TestCase):
    def bullish_technicals(self):
        return {
            "five_min": {
                "bias": "BULLISH",
                "confidence": "HIGH",
                "momentum_score": 4,
            },
            "fifteen_min": {"bias": "BULLISH", "confidence": "HIGH"},
            "two_hour": {"bias": "NEUTRAL", "confidence": "LOW"},
        }

    def test_strong_bullish_evidence_selects_call_direction(self):
        direction, bullish, bearish = choose_technical_direction(
            self.bullish_technicals(),
            {"bias": "BULLISH", "confidence": "MEDIUM"},
        )
        self.assertEqual(direction, "BULLISH")
        self.assertGreater(bullish, bearish)

    def test_option_flow_can_raise_technical_only_score(self):
        without_flow, _ = technical_only_score(
            "BULLISH",
            self.bullish_technicals(),
            {"bias": "NEUTRAL", "confidence": "LOW"},
        )
        with_flow, components = technical_only_score(
            "BULLISH",
            self.bullish_technicals(),
            {"bias": "NEUTRAL", "confidence": "LOW"},
            {"bias": "BULLISH", "volume_confirmed": True},
        )
        self.assertGreater(with_flow, without_flow)
        self.assertEqual(components["option_VWAP_volume"], 10)

    def test_overlapping_checks_become_one_episode(self):
        frame = pd.DataFrame(
            [
                {
                    "signal_time": "2026-07-20T10:00:00+05:30",
                    "technical_score": 82,
                    "outcome_time": "2026-07-20T10:20:00+05:30",
                    "analysis_error": "",
                },
                {
                    "signal_time": "2026-07-20T10:05:00+05:30",
                    "technical_score": 86,
                    "outcome_time": "2026-07-20T10:15:00+05:30",
                    "analysis_error": "",
                },
                {
                    "signal_time": "2026-07-20T10:25:00+05:30",
                    "technical_score": 80,
                    "outcome_time": None,
                    "analysis_error": "",
                },
            ]
        )
        episodes = _deduplicate_episodes(frame, minimum_score=70, forward_candles=6)
        self.assertEqual(len(episodes), 2)


if __name__ == "__main__":
    unittest.main()

