import json
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import dashboard_data


class OpeningPulseDashboardTests(unittest.TestCase):
    def test_same_day_retired_nifty_claim_is_not_shown_as_sensex(self):
        today = datetime.now(dashboard_data.IST).date().isoformat()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim_file = root / "claim.json"
            state_file = root / "sensex.json"
            history_file = root / "trades.csv"
            claim_file.write_text(
                json.dumps(
                    {
                        "date": today,
                        "status": "GTT_ACTIVE",
                        "direction": "BEARISH",
                        "trading_symbol": "NIFTY 24300 PE",
                    }
                )
            )

            with patch.object(
                dashboard_data, "OPENING_PULSE_CLAIM_FILE", claim_file
            ), patch.object(
                dashboard_data, "TRADE_HISTORY_FILE", history_file
            ), patch.object(
                dashboard_data, "state_file", return_value=state_file
            ):
                result = dashboard_data.build_opening_pulse_summary()

        self.assertFalse(result["hasDecision"])
        self.assertIsNone(result["tradingSymbol"])

    def test_performance_excludes_retired_indices_and_engines(self):
        today = datetime.now(dashboard_data.IST).date().isoformat()
        base = {
            "tradeDate": today,
            "instrumentClass": "INDEX_OPTION",
            "paperTrade": False,
            "grossPnL": 500.0,
            "exitTime": f"{today}T10:00:00+05:30",
            "entryTime": f"{today}T09:20:00+05:30",
            "tradingSymbol": "SENSEX OPTION",
            "optionType": "CALL",
            "entryPrice": 100.0,
            "quantity": 20,
        }
        rows = [
            {
                **base,
                "symbol": "SENSEX",
                "underlyingSymbol": "SENSEX",
                "strategy": dashboard_data.OPENING_PULSE_ENGINE,
            },
            {
                **base,
                "symbol": "NIFTY",
                "underlyingSymbol": "NIFTY",
                "strategy": dashboard_data.OPENING_PULSE_ENGINE,
            },
            {
                **base,
                "symbol": "SENSEX",
                "underlyingSymbol": "SENSEX",
                "strategy": "OLD_ENGINE",
            },
        ]

        with patch.object(dashboard_data, "read_trade_history", return_value=rows):
            result = dashboard_data.build_opening_pulse_performance()

        self.assertEqual(result["cumulative"]["totalTrades"], 1)
        self.assertEqual(result["cumulative"]["totalPnL"], 500.0)

    def test_summary_exposes_safe_sensex_pulse_evidence(self):
        today = datetime.now(dashboard_data.IST).date().isoformat()
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim_file = root / "claim.json"
            state_file = root / "sensex.json"
            history_file = root / "trades.csv"
            claim_file.write_text(json.dumps({"date": today, "status": "GTT_ACTIVE"}))
            state_file.write_text(
                json.dumps(
                    {
                        "date": today,
                        "strategy": dashboard_data.OPENING_PULSE_ENGINE,
                        "status": "GTT_ACTIVE",
                        "direction": "BULLISH",
                        "option_type": "CE",
                        "trading_symbol": "SENSEX 77500 CE",
                        "quantity": 40,
                        "entry_price": 200,
                        "target_price": 240,
                        "stop_loss_price": 160,
                        "option_reward_risk": 1,
                        "effective_option_loss_percent": 20,
                        "pulse": {
                            "vote": 7,
                            "strength": 41.2,
                            "components": {
                                "developing_opening_15m_body": {
                                    "points": 52,
                                    "vote": 4,
                                }
                            },
                            "option_chain": {
                                "direction": "BULLISH",
                                "confidence": "MEDIUM",
                                "score": 2,
                                "vote": 2,
                            },
                            "market_depth": {
                                "call_depth_ratio": 0.3,
                                "put_depth_ratio": -0.1,
                                "difference": 0.4,
                                "vote": 2,
                            },
                        },
                    }
                )
            )

            with patch.object(
                dashboard_data, "OPENING_PULSE_CLAIM_FILE", claim_file
            ), patch.object(
                dashboard_data, "TRADE_HISTORY_FILE", history_file
            ), patch.object(
                dashboard_data, "state_file", return_value=state_file
            ):
                result = dashboard_data.build_opening_pulse_summary()

        self.assertEqual(result["symbol"], "SENSEX")
        self.assertEqual(result["optionDirection"], "CALL")
        self.assertEqual(result["status"], "GTT_ACTIVE")
        self.assertEqual(result["chain"]["score"], 2)
        self.assertEqual(result["depth"]["vote"], 2)
        self.assertEqual(result["fifteenMinuteComponents"][0]["vote"], 4)


if __name__ == "__main__":
    unittest.main()
