import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from adaptive_score_calibration import (
    build_daily_config,
    calibrate_symbol,
    read_effective_score_rule,
)


IST = ZoneInfo("Asia/Kolkata")


def history_rows(symbol, score_outcomes, days=10, start=date(2026, 7, 1)):
    rows = []
    for offset in range(days):
        trading_date = start + timedelta(days=offset)
        for score, success in score_outcomes:
            rows.append(
                {
                    "observation_id": f"{symbol}-{offset}-{score}",
                    "trading_date": trading_date.isoformat(),
                    "symbol": symbol,
                    "score": score,
                    "direction_correct": success,
                    "favorable_points": 30 if success else 4,
                    "adverse_points": 5 if success else 25,
                }
            )
    return rows


class AdaptiveScoreCalibrationTests(unittest.TestCase):
    def test_selects_minimum_when_all_higher_scores_are_reliable(self):
        frame = pd.DataFrame(
            history_rows(
                "NIFTY",
                [(10, False), (20, False), (35, True), (45, True), (55, True), (70, True), (85, True)],
            )
        )
        rule = calibrate_symbol(
            frame,
            "NIFTY",
            date(2026, 8, 1),
            minimum_samples=10,
            minimum_trading_days=5,
        )

        self.assertEqual(rule["status"], "ADAPTIVE")
        self.assertEqual(rule["mode"], "MIN")
        self.assertEqual(rule["min_score"], 35)
        self.assertIsNone(rule["max_score"])

    def test_selects_range_when_high_scores_perform_materially_worse(self):
        frame = pd.DataFrame(
            history_rows(
                "BANKNIFTY",
                [(20, False), (40, True), (50, True), (60, True), (70, False), (85, False)],
            )
        )
        rule = calibrate_symbol(
            frame,
            "BANKNIFTY",
            date(2026, 8, 1),
            minimum_samples=10,
            minimum_trading_days=5,
        )

        self.assertEqual(rule["status"], "ADAPTIVE")
        self.assertEqual(rule["mode"], "RANGE")
        self.assertEqual(rule["min_score"], 40)
        self.assertEqual(rule["max_score"], 64.9)

    def test_insufficient_history_keeps_static_fallback(self):
        frame = pd.DataFrame(history_rows("NIFTY", [(45, True)], days=3))
        rule = calibrate_symbol(
            frame,
            "NIFTY",
            date(2026, 8, 1),
            fallback_minimum=20,
            minimum_samples=20,
            minimum_trading_days=5,
        )

        self.assertEqual(rule["status"], "FALLBACK_INSUFFICIENT_HISTORY")
        self.assertEqual(rule["min_score"], 20)

    def test_current_day_is_not_used_for_calibration(self):
        effective_date = date(2026, 8, 1)
        rows = history_rows("NIFTY", [(35, True)], days=5)
        rows.extend(
            {
                "observation_id": f"current-{index}",
                "trading_date": effective_date.isoformat(),
                "symbol": "NIFTY",
                "score": 35,
                "direction_correct": False,
                "favorable_points": 0,
                "adverse_points": 30,
            }
            for index in range(20)
        )
        rule = calibrate_symbol(
            pd.DataFrame(rows),
            "NIFTY",
            effective_date,
            minimum_samples=5,
            minimum_trading_days=5,
        )

        self.assertEqual(rule["status"], "ADAPTIVE")
        self.assertEqual(rule["success_rate"], 1.0)

    def test_runtime_only_reads_adaptive_rule_for_effective_date(self):
        frame = pd.DataFrame(
            history_rows("NIFTY", [(35, True), (45, True)], days=10)
            + history_rows("BANKNIFTY", [(40, True), (55, True)], days=10)
        )
        config = build_daily_config(
            frame,
            date(2026, 8, 3),
            minimum_samples=10,
            minimum_trading_days=5,
            generated_at=datetime(2026, 8, 3, 9, 0, tzinfo=IST),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "adaptive.json"
            path.write_text(json.dumps(config))
            current = read_effective_score_rule("NIFTY", date(2026, 8, 3), path)
            stale = read_effective_score_rule("NIFTY", date(2026, 8, 4), path)

        self.assertEqual(current["status"], "ADAPTIVE")
        self.assertIsNone(stale)


if __name__ == "__main__":
    unittest.main()
