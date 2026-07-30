import tempfile
import unittest
from pathlib import Path

import pandas as pd

from post_market_score_audit import (
    build_bucket_summary,
    evaluate_followthrough,
    non_overlapping_scans,
    parse_log_scans,
    reason_category,
    score_bucket,
    upsert_audit,
)


class PostMarketScoreAuditTests(unittest.TestCase):
    def test_score_buckets_have_extra_resolution_near_entry_threshold(self):
        self.assertEqual(score_bucket(49.9), "00-49")
        self.assertEqual(score_bucket(65), "65-69")
        self.assertEqual(score_bucket(74.9), "70-74")
        self.assertEqual(score_bucket(75), "75-79")
        self.assertEqual(score_bucket(92.5), "90-100")

    def test_log_parser_keeps_final_score_direction_and_reason(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "trade_bot.log"
            log_file.write_text(
                "\n".join(
                    [
                        "2026-07-30 10:05:02 | NIFTY signal: NEUTRAL, confidence=LOW, score=0, strike=24300.0, expiry=2026-08-04",
                        "2026-07-30 10:05:04 | NIFTY neutral-chain override candidate: direction=BULLISH; the complete 100-point score will decide",
                        "2026-07-30 10:05:06 | NIFTY BUY candidate: allowed=False score=43.7 reason=weighted score 43.7 does not qualify contract=NIFTY 24300 CE 04 AUG 26 contract_rank=0",
                        "2026-07-30 10:05:08 | NIFTY score 43.7 reject",
                        "2026-07-30 10:05:08 | NIFTY no trade: BUY structure did not pass deterministic gates.",
                    ]
                )
            )
            parsed = parse_log_scans(log_file, "2026-07-30")

        self.assertEqual(len(parsed), 1)
        row = parsed.iloc[0]
        self.assertEqual(row["direction"], "BULLISH")
        self.assertEqual(row["score"], 43.7)
        self.assertIn("weighted score", row["reason"])

    def test_overlapping_scans_are_not_double_counted(self):
        scans = pd.DataFrame(
            [
                {"timestamp": pd.Timestamp("2026-07-30 09:20", tz="Asia/Kolkata"), "symbol": "NIFTY"},
                {"timestamp": pd.Timestamp("2026-07-30 09:25", tz="Asia/Kolkata"), "symbol": "NIFTY"},
                {"timestamp": pd.Timestamp("2026-07-30 09:35", tz="Asia/Kolkata"), "symbol": "NIFTY"},
                {"timestamp": pd.Timestamp("2026-07-30 09:20", tz="Asia/Kolkata"), "symbol": "BANKNIFTY"},
            ]
        )
        selected = non_overlapping_scans(scans, horizon_minutes=15)
        nifty_times = selected[selected["symbol"] == "NIFTY"]["timestamp"].dt.strftime("%H:%M").tolist()
        self.assertEqual(nifty_times, ["09:20", "09:35"])
        self.assertEqual(len(selected), 3)

    def test_followthrough_math_is_direction_aware(self):
        index = pd.date_range("2026-07-30 10:05", periods=15, freq="min", tz="Asia/Kolkata")
        candles = pd.DataFrame(
            {
                "open": [100] * 15,
                "high": [101] * 14 + [112],
                "low": [99] * 14 + [96],
                "close": [100] * 14 + [108],
            },
            index=index,
        )
        scan = {
            "timestamp": pd.Timestamp("2026-07-30 10:05:08", tz="Asia/Kolkata"),
            "symbol": "NIFTY",
            "score": 78,
            "action": "reject",
            "direction": "BULLISH",
            "chain_direction": "NEUTRAL",
            "chain_confidence": "LOW",
            "reason": "weighted score below entry threshold",
            "source": "long_log",
        }
        row = evaluate_followthrough(scan, candles, horizon_minutes=15)
        self.assertEqual(row["up_points"], 12)
        self.assertEqual(row["down_points"], 4)
        self.assertEqual(row["favorable_points"], 12)
        self.assertTrue(row["direction_correct"])
        self.assertTrue(row["nifty_10_point_hit"])
        self.assertFalse(row["nifty_20_point_hit"])

    def test_upsert_is_idempotent(self):
        index = pd.date_range("2026-07-30 10:05", periods=15, freq="min", tz="Asia/Kolkata")
        candles = pd.DataFrame(
            {"open": 100, "high": 105, "low": 98, "close": 103},
            index=index,
        )
        scan = {
            "timestamp": pd.Timestamp("2026-07-30 10:05", tz="Asia/Kolkata"),
            "symbol": "NIFTY",
            "score": 75,
            "action": "reject",
            "direction": "BULLISH",
            "reason": "weighted score",
            "source": "long_log",
        }
        row = evaluate_followthrough(scan, candles)
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_file = Path(temp_dir) / "audit.csv"
            upsert_audit([row], audit_file)
            combined = upsert_audit([row], audit_file)
        self.assertEqual(len(combined), 1)

    def test_summary_marks_small_samples_as_building(self):
        frame = pd.DataFrame(
            [
                {
                    "observation_id": "1",
                    "score_bucket": "75-79",
                    "symbol": "NIFTY",
                    "up_points": 12,
                    "down_points": 5,
                    "favorable_points": 12,
                    "adverse_points": 5,
                    "direction_correct": True,
                }
            ]
        )
        summary = build_bucket_summary(frame, minimum_samples=20)
        self.assertEqual(summary.iloc[0]["evidence"], "BUILDING")
        self.assertEqual(reason_category("Technical reward/risk is too low"), "REWARD_RISK")


if __name__ == "__main__":
    unittest.main()
