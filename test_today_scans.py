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
                    "weighted_alignment": {"score": -4, "grade": "REJECT"}
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
                        "2026-07-29 09:21:05 | NIFTY score -4 reject",
                        "2026-07-29 09:21:06 | NIFTY no trade: Volume confirmation is below threshold.",
                        "2026-07-29 09:22:05 | BANKNIFTY score 0 buy",
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
            self.assertEqual(scans[1]["nifty"]["score"], -4)
            self.assertEqual(scans[1]["nifty"]["reason"], "Volume confirmation below threshold")
            self.assertEqual(scans[1]["bankNifty"]["decision"], "ENTERED")
            self.assertEqual(scans[1]["bankNifty"]["score"], 0)


if __name__ == "__main__":
    unittest.main()
