import csv
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


sys.modules.setdefault("requests", types.ModuleType("requests"))

import dashboard_data


IST = ZoneInfo("Asia/Kolkata")


class TodayScansTests(unittest.TestCase):
    def test_combines_analysis_reasons_with_concise_log_decisions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            analysis_file = root / "analysis_history.csv"
            log_file = root / "trade_bot.log"
            raw = {
                "option_summary": {
                    "unified_entry_score": {
                        "score": 53.7,
                        "score_version": "VAMSI_UNIFIED_ENTRY_V1",
                        "grade": "SKIP",
                    }
                },
                "llm_decision": {
                    "execute_trade": False,
                    "decision": "NO_TRADE",
                    "reason": "Volume confirmation is below the strategy threshold.",
                },
            }
            with analysis_file.open("w", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["timestamp", "symbol", "llm_reason", "raw_json"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "timestamp": "2026-07-29T09:21:04+05:30",
                        "symbol": "NIFTY",
                        "llm_reason": raw["llm_decision"]["reason"],
                        "raw_json": json.dumps(raw),
                    }
                )
            log_file.write_text(
                "\n".join(
                    [
                        "2026-07-29 09:21:05 | NIFTY score 53.7 reject version=VAMSI_UNIFIED_ENTRY_V1",
                        "2026-07-29 09:21:06 | NIFTY no trade: Volume confirmation is below threshold.",
                        "2026-07-29 09:22:05 | BANKNIFTY score 68 buy version=VAMSI_UNIFIED_ENTRY_V1",
                        "2026-07-29 09:26:05 | NIFTY score used reject",
                    ]
                )
            )

            with (
                patch.object(dashboard_data, "ANALYSIS_HISTORY_FILE", analysis_file),
                patch.object(dashboard_data, "LOG_FILE", log_file),
            ):
                scans = dashboard_data.build_today_scans(
                    datetime(2026, 7, 29, 12, 0, tzinfo=IST)
                )

            self.assertEqual(len(scans), 2)
            self.assertTrue(scans[0]["timestamp"].startswith("2026-07-29T09:25"))
            self.assertEqual(scans[0]["nifty"]["reason"], "Daily trade limit reached")
            self.assertIsNone(scans[0]["bankNifty"])
            self.assertEqual(scans[1]["nifty"]["decision"], "REJECTED")
            self.assertEqual(scans[1]["nifty"]["score"], 53.7)
            self.assertEqual(scans[1]["nifty"]["scoreVersion"], "VAMSI_UNIFIED_ENTRY_V1")
            self.assertEqual(scans[1]["nifty"]["reason"], "Volume confirmation below threshold")
            self.assertEqual(scans[1]["bankNifty"]["decision"], "ENTERED")
            self.assertEqual(scans[1]["bankNifty"]["score"], 68)
            self.assertEqual(scans[1]["bankNifty"]["scoreVersion"], "VAMSI_UNIFIED_ENTRY_V1")

    def test_knowledge_engine_ledger_is_authoritative_for_current_scans(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            analysis_file = root / "analysis_history.csv"
            log_file = root / "trade_bot.log"
            scan_file = root / "vamsi_kb_intraday" / "scans.csv"
            scan_file.parent.mkdir()
            analysis_file.write_text("")
            log_file.write_text("")
            with scan_file.open("w", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "scan_time", "scan_slot", "action", "direction", "setup",
                        "knowledge_score", "instrument", "blockers",
                    ],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "scan_time": "2026-08-14T11:36:04+05:30",
                        "scan_slot": "2026-08-14T11:35:00+05:30",
                        "action": "REJECT",
                        "direction": "BEARISH",
                        "setup": "NONE",
                        "knowledge_score": "42.9",
                        "instrument": "NIFTY2681424500PE",
                        "blockers": "breadth does not confirm | option flow below VWAP",
                    }
                )

            with (
                patch.object(dashboard_data, "ANALYSIS_HISTORY_FILE", analysis_file),
                patch.object(dashboard_data, "LOG_FILE", log_file),
                patch.object(dashboard_data, "VAMSI_KB_SCAN_FILE", scan_file),
            ):
                scans = dashboard_data.build_today_scans(
                    datetime(2026, 8, 14, 12, 0, tzinfo=IST)
                )

            self.assertEqual(len(scans), 1)
            decision = scans[0]["nifty"]
            self.assertEqual(decision["decision"], "REJECTED")
            self.assertEqual(decision["direction"], "BEARISH")
            self.assertEqual(decision["score"], 42.9)
            self.assertEqual(decision["setup"], "NONE")
            self.assertIn("breadth does not confirm", decision["reason"])


if __name__ == "__main__":
    unittest.main()
