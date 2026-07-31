import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import reset_tracking_data


class ResetTrackingDataTests(unittest.TestCase):
    def test_reset_archives_stats_but_preserves_config_and_devices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            logs = root / "logs"
            data.mkdir()
            logs.mkdir()
            (data / "trade_history.csv").write_text("trade\n")
            (data / "analysis_history.csv").write_text("analysis\n")
            (data / "trading_config.json").write_text("{}")
            (data / "apns_devices.json").write_text("{}")
            (logs / "trade_bot.log").write_text("old log\n")
            (root / "daily_trade_count.json").write_text("{}")

            with (
                patch.object(reset_tracking_data, "BASE_DIR", root),
                patch.object(reset_tracking_data, "DATA_DIR", data),
                patch.object(reset_tracking_data, "LOG_DIR", logs),
                patch.object(reset_tracking_data, "ARCHIVE_DIR", root / "archive"),
            ):
                archive, moved = reset_tracking_data.run_reset(confirm=True)

            self.assertEqual(len(moved), 4)
            self.assertFalse((data / "trade_history.csv").exists())
            self.assertFalse((data / "analysis_history.csv").exists())
            self.assertFalse((logs / "trade_bot.log").exists())
            self.assertTrue((data / "trading_config.json").exists())
            self.assertTrue((data / "apns_devices.json").exists())
            self.assertTrue((archive / "data" / "trade_history.csv").exists())

    def test_reset_refuses_active_position(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            logs = root / "logs"
            data.mkdir()
            logs.mkdir()
            state = root / "trade_state_NIFTY.json"
            state.write_text(
                json.dumps({"instrument_key": "NSE_FO|1", "status": "POSITION_OPEN"})
            )
            with (
                patch.object(reset_tracking_data, "BASE_DIR", root),
                patch.object(reset_tracking_data, "DATA_DIR", data),
                patch.object(reset_tracking_data, "LOG_DIR", logs),
                patch.object(reset_tracking_data, "ARCHIVE_DIR", root / "archive"),
            ):
                with self.assertRaises(RuntimeError):
                    reset_tracking_data.run_reset(confirm=True)


if __name__ == "__main__":
    unittest.main()
