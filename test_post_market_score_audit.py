import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from post_market_score_audit import (
    build_bucket_summary,
    build_knowledge_summary,
    evaluate_knowledge_followthrough,
    evaluate_followthrough,
    knowledge_categories,
    non_overlapping_scans,
    parse_log_scans,
    read_audit,
    reason_category,
    score_bucket,
    upsert_audit,
)
from unified_entry_score import UNIFIED_SCORE_VERSION


class PostMarketScoreAuditTests(unittest.TestCase):
    def test_score_buckets_use_ten_point_dashboard_ranges(self):
        self.assertEqual(score_bucket(0), "0-9")
        self.assertEqual(score_bucket(9.9), "0-9")
        self.assertEqual(score_bucket(10), "10-19")
        self.assertEqual(score_bucket(19.9), "10-19")
        self.assertEqual(score_bucket(20), "20-29")
        self.assertEqual(score_bucket(29.9), "20-29")
        self.assertEqual(score_bucket(30), "30-39")
        self.assertEqual(score_bucket(49.9), "40-49")
        self.assertEqual(score_bucket(50), "50-59")
        self.assertEqual(score_bucket(65), "60-69")
        self.assertEqual(score_bucket(79.9), "70-79")
        self.assertEqual(score_bucket(80), "80-89")
        self.assertEqual(score_bucket(92.5), "90-100")

    def test_read_audit_rebuckets_existing_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            audit_file = Path(temp_dir) / "score_followthrough_audit.csv"
            pd.DataFrame(
                [
                    {
                        "observation_id": "legacy-1",
                        "score": 22.5,
                        "score_bucket": "00-49",
                    }
                ]
            ).to_csv(audit_file, index=False)

            audit = read_audit(audit_file)

        self.assertEqual(audit.iloc[0]["score_bucket"], "20-29")

    def test_log_parser_keeps_final_score_direction_and_reason(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_file = Path(temp_dir) / "trade_bot.log"
            log_file.write_text(
                "\n".join(
                    [
                        "2026-07-30 10:05:02 | NIFTY signal: NEUTRAL, confidence=LOW, score=0, strike=24300.0, expiry=2026-08-04",
                        "2026-07-30 10:05:04 | NIFTY neutral-chain override candidate: direction=BULLISH; the complete 100-point score will decide",
                        f"2026-07-30 10:05:06 | NIFTY BUY candidate: allowed=False score=43.7 version={UNIFIED_SCORE_VERSION} reason=unified entry score 43.7 does not qualify contract=NIFTY 24300 CE 04 AUG 26 contract_rank=0",
                        f"2026-07-30 10:05:08 | NIFTY score 43.7 reject version={UNIFIED_SCORE_VERSION}",
                        "2026-07-30 10:05:08 | NIFTY no trade: BUY structure did not pass deterministic gates.",
                    ]
                )
            )
            parsed = parse_log_scans(log_file, "2026-07-30")

        self.assertEqual(len(parsed), 1)
        row = parsed.iloc[0]
        self.assertEqual(row["direction"], "BULLISH")
        self.assertEqual(row["score"], 43.7)
        self.assertEqual(row["score_version"], UNIFIED_SCORE_VERSION)
        self.assertIn("unified entry score", row["reason"])

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

    def test_followthrough_stores_longer_minute_path_for_shadow_exits(self):
        index = pd.date_range(
            "2026-07-30 10:05", periods=60, freq="min", tz="Asia/Kolkata"
        )
        candles = pd.DataFrame(
            {"open": 100, "high": 102, "low": 98, "close": 101}, index=index
        )
        scan = {
            "timestamp": index[0],
            "symbol": "NIFTY",
            "score": 66,
            "score_version": UNIFIED_SCORE_VERSION,
            "direction": "BULLISH",
        }

        row = evaluate_followthrough(
            scan, candles, horizon_minutes=15, exit_path_minutes=60
        )
        path = json.loads(row["minute_path_json"])

        self.assertEqual(row["path_horizon_minutes"], 60)
        self.assertEqual(len(path), 60)
        self.assertEqual(path[0]["o"], 100.0)
        self.assertEqual(path[-1]["c"], 101.0)

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
                    "score_bucket": "70-79",
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

    def test_knowledge_audit_keeps_overlaps_and_stops_before_ambiguous_bar(self):
        index = pd.date_range(
            "2026-08-14 10:00", periods=4, freq="min", tz="Asia/Kolkata"
        )
        candles = pd.DataFrame(
            {
                "open": [100, 100, 100, 100],
                "high": [108, 140, 110, 112],
                "low": [99, 69, 98, 97],
                "close": [106, 75, 105, 108],
            },
            index=index,
        )
        scan = {
            "scan_slot_timestamp": index[0],
            "scan_time": index[0].isoformat(),
            "symbol": "NIFTY",
            "action": "REJECT",
            "direction": "BULLISH",
            "knowledge_score": 71.4,
            "selection_score": 63,
            "blockers": "breadth failed",
            "evidence": json.dumps(
                {
                    "breadth": {"passed": False},
                    "option_chain": {"passed": False},
                }
            ),
        }
        with patch.dict(
            os.environ,
            {
                "VAMSI_KB_NIFTY_TARGET_POINTS": "30",
                "VAMSI_KB_NIFTY_STOP_POINTS": "30",
            },
        ):
            row = evaluate_knowledge_followthrough(scan, candles)

        self.assertEqual(row["favorable_points_before_stop"], 8)
        self.assertTrue(row["stop_hit"])
        self.assertFalse(row["target_hit_before_stop"])
        self.assertIn("SCORE 70-79", json.loads(row["categories_json"]))
        self.assertIn("REJECT · CONSTITUENT BREADTH", json.loads(row["categories_json"]))
        self.assertIn("REJECT · OPTION CHAIN", knowledge_categories(scan))

    def test_knowledge_summary_has_three_index_rows_and_sample_counts(self):
        frame = pd.DataFrame(
            [
                {
                    "symbol": "NIFTY",
                    "categories_json": json.dumps(["SCORE 70-79", "REJECT · OPTION CHAIN"]),
                    "favorable_points_before_stop": 12,
                    "target_hit_before_stop": False,
                },
                {
                    "symbol": "NIFTY",
                    "categories_json": json.dumps(["SCORE 70-79"]),
                    "favorable_points_before_stop": 18,
                    "target_hit_before_stop": False,
                },
            ]
        )
        summary = build_knowledge_summary(
            frame, trading_date=pd.Timestamp("2026-08-14").date()
        )
        self.assertEqual([row["symbol"] for row in summary["rows"]], ["NIFTY", "BANKNIFTY", "SENSEX"])
        nifty = summary["rows"][0]
        score_cell = next(cell for cell in nifty["cells"] if cell["column"] == "SCORE 70-79")
        self.assertEqual(score_cell["samples"], 2)
        self.assertEqual(score_cell["averageFavorablePoints"], 15)


if __name__ == "__main__":
    unittest.main()
