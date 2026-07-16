import gzip
import json
import os
import tempfile
import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

requests_stub = types.ModuleType("requests")
requests_stub.get = lambda *args, **kwargs: None
sys.modules.setdefault("requests", requests_stub)
connection_stub = types.ModuleType("urllib3.util.connection")
connection_stub.allowed_gai_family = lambda: None
util_stub = types.ModuleType("urllib3.util")
util_stub.connection = connection_stub
urllib3_stub = types.ModuleType("urllib3")
urllib3_stub.util = util_stub
sys.modules.setdefault("urllib3", urllib3_stub)
sys.modules.setdefault("urllib3.util", util_stub)
sys.modules.setdefault("urllib3.util.connection", connection_stub)

import stock_futures_scanner as scanner


IST = ZoneInfo("Asia/Kolkata")


class StockFuturesScannerTests(unittest.TestCase):
    def test_nearest_valid_expiry_is_selected(self):
        today = datetime.now(IST).date()
        instruments = [
            {
                "segment": "NSE_FO",
                "instrument_type": "FUT",
                "underlying_symbol": "RELIANCE",
                "instrument_key": "NSE_FO|LATER",
                "trading_symbol": "RELIANCE LATER FUT",
                "expiry": (today + timedelta(days=35)).isoformat(),
                "lot_size": 500,
            },
            {
                "segment": "NSE_FO",
                "instrument_type": "FUT",
                "underlying_symbol": "RELIANCE",
                "instrument_key": "NSE_FO|NEAR",
                "trading_symbol": "RELIANCE NEAR FUT",
                "expiry": (today + timedelta(days=10)).isoformat(),
                "lot_size": 500,
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "instruments.json.gz"
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                json.dump(instruments, handle)
            selected = scanner.load_nearest_stock_futures(path, 2)

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["instrument_key"], "NSE_FO|NEAR")

    def test_aligned_liquid_future_qualifies(self):
        analysis = {
            "two_hour": {"bias": "BULLISH", "confidence": "MEDIUM"},
            "fifteen_min": {
                "bias": "BULLISH", "confidence": "MEDIUM", "target": 102,
                "stop_loss": 99, "atr14": 1,
            },
            "five_min": {
                "bias": "BULLISH", "confidence": "MEDIUM", "target": 102,
                "stop_loss": 99, "atr14": 1, "vwap_bias": "BULLISH",
                "volume_ratio": 1.5, "momentum_score": 4,
            },
        }
        item = {
            "contract": {
                "underlying_symbol": "RELIANCE",
                "instrument_key": "NSE_FO|TEST",
                "trading_symbol": "RELIANCE FUT",
                "lot_size": 500,
            },
            "last_price": 100,
            "spread_percent": 0.05,
        }
        with (
            patch.object(scanner, "get_instrument_technical_analysis", return_value=analysis),
            patch.dict(os.environ, {"STOCK_FUTURES_MIN_SCORE": "80"}, clear=False),
        ):
            candidate, reason = scanner.evaluate_contract(item)

        self.assertIsNone(reason)
        self.assertEqual(candidate["transaction_type"], "BUY")
        self.assertEqual(candidate["quantity"], 500)
        self.assertGreaterEqual(candidate["signal_score"], 80)

    def test_conflicting_five_and_fifteen_minute_views_are_rejected(self):
        analysis = {
            "two_hour": {"bias": "NEUTRAL", "confidence": "LOW"},
            "fifteen_min": {"bias": "BULLISH", "confidence": "MEDIUM"},
            "five_min": {"bias": "BEARISH", "confidence": "MEDIUM"},
        }
        item = {
            "contract": {
                "underlying_symbol": "TCS",
                "instrument_key": "NSE_FO|TEST",
                "trading_symbol": "TCS FUT",
                "lot_size": 175,
            },
            "last_price": 3000,
            "spread_percent": 0.05,
        }
        with patch.object(scanner, "get_instrument_technical_analysis", return_value=analysis):
            candidate, reason = scanner.evaluate_contract(item)

        self.assertIsNone(candidate)
        self.assertIn("direction", reason)


if __name__ == "__main__":
    unittest.main()
