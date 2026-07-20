import unittest
from datetime import date
from unittest.mock import Mock, patch

import pandas as pd

from banknifty_post_market import (
    _fetch_range,
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

    @patch("banknifty_post_market.upstox_headers", return_value={})
    @patch("banknifty_post_market.requests.get")
    def test_minute_history_is_split_into_api_safe_windows(self, get, _headers):
        response = Mock(status_code=200)
        response.json.return_value = {"data": {"candles": []}}
        get.return_value = response

        _fetch_range(
            "NSE_INDEX|Nifty Bank",
            "minutes",
            15,
            date(2026, 1, 1),
            date(2026, 3, 1),
            {},
        )

        self.assertEqual(get.call_count, 3)
        requested_urls = [call.args[0] for call in get.call_args_list]
        self.assertIn("2026-01-28/2026-01-01", requested_urls[0])
        self.assertIn("2026-02-25/2026-01-29", requested_urls[1])
        self.assertIn("2026-03-01/2026-02-26", requested_urls[2])


if __name__ == "__main__":
    unittest.main()
