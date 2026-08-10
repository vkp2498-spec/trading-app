import json
import unittest
from datetime import date, timedelta

import pandas as pd

from adaptive_exit_shadow import (
    build_shadow_config,
    calibrate_band,
    daily_change_cap,
    prepare_episodes,
    simulate_first_touch,
)
from unified_entry_score import UNIFIED_SCORE_VERSION


def candle(timestamp, open_price=100, high=122, low=98, close=115):
    return {
        "timestamp": pd.Timestamp(timestamp),
        "open": float(open_price),
        "high": float(high),
        "low": float(low),
        "close": float(close),
    }


def episode(index, day):
    return {
        "observation_id": f"episode-{index}",
        "signal_time": pd.Timestamp(f"{day.isoformat()} 11:00", tz="Asia/Kolkata"),
        "trading_date": day.isoformat(),
        "score": 66.0,
        "score_band": "65-69",
        "direction": "BULLISH",
        "reference_price": 100.0,
        "path": [candle(f"{day.isoformat()} 11:00+05:30")],
    }


class AdaptiveExitShadowTests(unittest.TestCase):
    def test_first_touch_is_direction_aware_and_same_minute_is_conservative(self):
        bullish_target = [candle("2026-08-01 11:00+05:30", high=121, low=99)]
        bearish_target = [candle("2026-08-01 11:00+05:30", high=101, low=79)]
        ambiguous = [candle("2026-08-01 11:00+05:30", high=121, low=89)]

        self.assertEqual(
            simulate_first_touch(bullish_target, "BULLISH", 100, 20, 10), 20
        )
        self.assertEqual(
            simulate_first_touch(bearish_target, "BEARISH", 100, 20, 10), 20
        )
        self.assertEqual(
            simulate_first_touch(ambiguous, "BULLISH", 100, 20, 10), -10
        )

    def test_prepare_episodes_purges_overlapping_research_windows(self):
        day = date(2026, 8, 3)
        path = json.dumps(
            [{"t": "2026-08-03T09:15:00+05:30", "o": 100, "h": 102, "l": 99, "c": 101}]
        )
        frame = pd.DataFrame(
            [
                {
                    "observation_id": str(index),
                    "trading_date": day.isoformat(),
                    "signal_time": f"2026-08-03T{time}:00+05:30",
                    "symbol": "NIFTY",
                    "score": 66,
                    "score_version": UNIFIED_SCORE_VERSION,
                    "action": "buy",
                    "direction": "BULLISH",
                    "reference_price": 100,
                    "minute_path_json": path,
                }
                for index, time in enumerate(("09:15", "09:30", "10:15"))
            ]
        )

        episodes = prepare_episodes(
            frame,
            date(2026, 8, 4),
            horizon_minutes=60,
            entry_start_time="09:15",
            entry_end_time="15:25",
        )

        self.assertEqual([item["observation_id"] for item in episodes], ["0", "2"])

    def test_insufficient_data_remains_building(self):
        result = calibrate_band([], minimum_samples=40, minimum_trading_days=10)
        self.assertEqual(result["status"], "BUILDING")
        self.assertFalse(result["execution_applied"])
        self.assertIsNone(result["proposed_target_points"])

    def test_held_out_positive_candidate_is_still_shadow_only_and_capped(self):
        start = date(2026, 6, 1)
        episodes = [episode(index, start + timedelta(days=index // 4)) for index in range(40)]

        result = calibrate_band(
            episodes,
            current_target=30,
            current_stop=30,
            target_grid=(10, 20),
            stop_grid=(10, 20),
            minimum_samples=40,
            minimum_trading_days=10,
            minimum_validation_samples=10,
            maximum_daily_change_percent=10,
        )

        self.assertEqual(result["status"], "SHADOW_VALIDATED")
        self.assertEqual(result["raw_proposed_target_points"], 20)
        self.assertEqual(result["raw_proposed_stop_points"], 10)
        self.assertEqual(result["proposed_target_points"], 27)
        self.assertEqual(result["proposed_stop_points"], 27)
        self.assertFalse(result["execution_applied"])

    def test_daily_change_cap_limits_proposal(self):
        self.assertEqual(daily_change_cap(10, 30, 10), 27)
        self.assertEqual(daily_change_cap(50, 30, 10), 33)

    def test_config_is_explicitly_non_executable(self):
        config = build_shadow_config(pd.DataFrame(), date(2026, 8, 10))
        self.assertEqual(config["mode"], "SHADOW_ONLY")
        self.assertFalse(config["execution_applied"])


if __name__ == "__main__":
    unittest.main()
