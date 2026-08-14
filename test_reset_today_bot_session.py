import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import reset_today_bot_session as reset


class ResetTodayBotSessionTests(unittest.TestCase):
    def test_filter_removes_live_bot_row_but_keeps_manual_and_prior_day(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data = root / "data"
            data.mkdir()
            history = data / "trade_history.csv"
            fields = ["trade_date", "symbol", "instrument_class", "strategy", "gross_pnl"]
            rows = [
                {"trade_date": "2026-08-14", "symbol": "NIFTY", "instrument_class": "INDEX_OPTION", "strategy": "VAMSI_KB_INTRADAY_V1", "gross_pnl": "100"},
                {"trade_date": "2026-08-14", "symbol": "NIFTY", "instrument_class": "INDEX_OPTION", "strategy": "MANUAL_INDEX", "gross_pnl": "200"},
                {"trade_date": "2026-08-13", "symbol": "NIFTY", "instrument_class": "INDEX_OPTION", "strategy": "VAMSI_KB_INTRADAY_V1", "gross_pnl": "300"},
            ]
            with history.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            archive = root / "archive" / "reset"
            with (
                patch.object(reset, "BASE_DIR", root),
                patch.object(reset, "TRADE_HISTORY_FILE", history),
            ):
                removed = reset.filter_trade_history("2026-08-14", archive)
            self.assertEqual(removed, 1)
            with history.open(newline="") as handle:
                retained = list(csv.DictReader(handle))
            self.assertEqual(len(retained), 2)
            self.assertTrue((archive / "data" / "trade_history.csv").exists())

    def test_active_state_refuses_reset(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "trade_state_NIFTY.json").write_text(
                json.dumps({"instrument_key": "NFO|1", "status": "POSITION_OPEN"})
            )
            self.assertEqual(reset.active_local_states(root), [root / "trade_state_NIFTY.json"])


if __name__ == "__main__":
    unittest.main()
