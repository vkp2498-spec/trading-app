import csv
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import dashboard_data


class NiftyOnlyDashboardTests(unittest.TestCase):
    def test_ml_shadow_payload_keeps_rejected_forecast_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata_path = root / "metadata.json"
            predictions_path = root / "predictions.csv"
            metadata_path.write_text(json.dumps({
                "status": "READY_SHADOW",
                "trained_through": "2026-08-12",
                "training_rows": 2000,
                "training_days": 100,
                "validation": {"accuracy": 0.61, "rows": 400},
            }))
            fieldnames = [
                "candle_time", "call_probability", "put_probability",
                "call_target_percent", "call_stop_percent", "call_reward_risk",
                "put_target_percent", "put_stop_percent", "put_reward_risk",
                "call_action", "call_reason", "put_action", "put_reason",
                "overall_action", "execution_mode", "session_open", "underlying_open",
                "underlying_entry_price", "future_up_percent", "future_down_percent",
                "call_outcome", "call_realized_percent", "put_outcome",
                "put_realized_percent", "resolved_at",
            ]
            with predictions_path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerow({
                    "candle_time": "2026-08-13T10:00:00+05:30",
                    "call_probability": "0.62",
                    "put_probability": "0.38",
                    "call_target_percent": "0.12",
                    "call_stop_percent": "0.09",
                    "call_reward_risk": "1.33",
                    "put_target_percent": "0.08",
                    "put_stop_percent": "0.11",
                    "put_reward_risk": "0.73",
                    "call_action": "PAPER_ENTRY",
                    "call_reason": "qualified",
                    "put_action": "NO_TRADE",
                    "put_reason": "probability below threshold",
                    "overall_action": "PAPER_ENTRY",
                    "execution_mode": "PAPER",
                    "session_open": "24980",
                    "underlying_open": "24995",
                    "underlying_entry_price": "25000",
                    "future_up_percent": "0.15",
                    "future_down_percent": "0.04",
                    "call_outcome": "TARGET",
                    "call_realized_percent": "0.12",
                    "put_outcome": "CLOSE",
                    "put_realized_percent": "-0.03",
                    "resolved_at": "2026-08-13T11:00:00+05:30",
                })

            paper_trade = {
                "strategy": "ML_SHADOW_0920_PERCENT_V3_PAPER",
                "optionType": "CALL",
                "direction": "BULLISH",
            }
            with patch.object(dashboard_data, "ML_SHADOW_METADATA_FILE", metadata_path), \
                 patch.object(dashboard_data, "ML_SHADOW_PREDICTIONS_FILE", predictions_path), \
                 patch.object(dashboard_data, "read_trade_history", return_value=[paper_trade]):
                status = dashboard_data.build_ml_shadow_status()

        forecast = status["recentForecasts"][0]
        self.assertEqual(forecast["callAction"], "PAPER_ENTRY")
        self.assertEqual(forecast["callProbability"], 0.62)
        self.assertEqual(forecast["callTargetPercent"], 0.12)
        self.assertEqual(forecast["sessionOpen"], 24980.0)
        self.assertEqual(status["resolvedForecastCount"], 1)
        self.assertEqual(status["paperTrades"][0]["direction"], "CALL")

    def test_historical_numeric_score_without_version_populates_heatmap(self):
        today = datetime.now(dashboard_data.IST).date().isoformat()
        trade = {
            "tradeDate": today,
            "symbol": "NIFTY",
            "underlyingSymbol": "NIFTY",
            "instrumentClass": "INDEX_OPTION",
            "strategy": "SELECTIVE",
            "tradingSymbol": "NIFTY 25000 CE",
            "transactionType": "BUY",
            "quantity": 65,
            "entryTime": f"{today}T11:15:00+05:30",
            "entryPrice": 100.0,
            "exitTime": f"{today}T11:30:00+05:30",
            "grossPnL": 650.0,
            "score": 55.0,
            "scoreVersion": "",
            "tradeSequence": 1,
        }

        with patch.object(dashboard_data, "read_trade_history", return_value=[trade]):
            performance = dashboard_data.build_trade_performance("real")

        populated = [
            cell
            for cell in performance["edgeAnalytics"]["matrix"]
            if cell["trades"] > 0
        ]
        self.assertEqual(len(populated), 1)
        self.assertEqual(populated[0]["scoreBand"], "50-59")
        self.assertEqual(populated[0]["expectancy"], 10_000.0)

    def test_analytics_scope_can_show_real_only_or_real_and_paper(self):
        today = datetime.now(dashboard_data.IST).date().isoformat()
        base = {
            "tradeDate": today,
            "symbol": "NIFTY",
            "underlyingSymbol": "NIFTY",
            "instrumentClass": "INDEX_OPTION",
            "tradingSymbol": "NIFTY 25000 CE",
            "transactionType": "BUY",
            "quantity": 65,
            "entryTime": f"{today}T11:15:00+05:30",
            "entryPrice": 100.0,
            "exitTime": f"{today}T11:30:00+05:30",
            "score": 55.0,
            "scoreVersion": "VAMSI_UNIFIED_ENTRY_V1",
            "tradeSequence": 1,
        }
        trades = [
            {**base, "strategy": "SELECTIVE", "grossPnL": 650.0},
            {
                **base,
                "strategy": "SELECTIVE_PAPER",
                "grossPnL": -325.0,
                "entryTime": f"{today}T12:15:00+05:30",
                "exitTime": f"{today}T12:30:00+05:30",
            },
        ]

        with patch.object(dashboard_data, "read_trade_history", return_value=trades):
            real = dashboard_data.build_trade_performance("real")
            mixed = dashboard_data.build_trade_performance("mixed")

        self.assertEqual(real["cumulative"]["totalTrades"], 1)
        self.assertEqual(real["cumulative"]["totalPnL"], 650.0)
        self.assertEqual(real["analyticsMode"], "REAL")
        self.assertEqual(mixed["cumulative"]["totalTrades"], 2)
        self.assertEqual(mixed["cumulative"]["totalPnL"], 325.0)
        self.assertEqual(mixed["analyticsMode"], "MIXED")

    def test_all_dashboard_analytics_exclude_banknifty_trades(self):
        today = datetime.now(dashboard_data.IST).date().isoformat()
        trades = [
            {
                "tradeDate": today,
                "symbol": "NIFTY",
                "underlyingSymbol": "NIFTY",
                "instrumentClass": "INDEX_OPTION",
                "tradingSymbol": "NIFTY 25000 CE",
                "transactionType": "BUY",
                "quantity": 65,
                "entryTime": f"{today}T11:15:00+05:30",
                "entryPrice": 100.0,
                "exitTime": f"{today}T11:30:00+05:30",
                "grossPnL": 650.0,
                "score": 55.0,
                "scoreVersion": "VAMSI_UNIFIED_ENTRY_V1",
                "tradeSequence": 1,
            },
            {
                "tradeDate": today,
                "symbol": "BANKNIFTY",
                "underlyingSymbol": "BANKNIFTY",
                "instrumentClass": "INDEX_OPTION",
                "tradingSymbol": "BANKNIFTY 57000 CE",
                "transactionType": "BUY",
                "quantity": 30,
                "entryTime": f"{today}T11:20:00+05:30",
                "entryPrice": 200.0,
                "exitTime": f"{today}T11:35:00+05:30",
                "grossPnL": 6000.0,
                "score": 85.0,
                "scoreVersion": "VAMSI_UNIFIED_ENTRY_V1",
                "tradeSequence": 1,
            },
        ]

        with patch.object(dashboard_data, "read_trade_history", return_value=trades):
            performance = dashboard_data.build_trade_performance()

        self.assertEqual(performance["today"]["closedTrades"], 1)
        self.assertEqual(performance["today"]["closedPnL"], 650.0)
        self.assertEqual(performance["today"]["symbolTrades"]["NIFTY"], 1)
        self.assertEqual(performance["today"]["symbolTrades"].get("BANKNIFTY", 0), 0)
        self.assertEqual(performance["cumulative"]["totalTrades"], 1)
        self.assertEqual(performance["cumulative"]["totalPnL"], 650.0)
        weekday = performance["cumulative"]["dayOfWeekPerformance"]
        self.assertTrue(any(row["symbol"] == "NIFTY" for row in weekday))
        self.assertTrue(
            all(row["netPnL"] == 0 for row in weekday if row["symbol"] == "BANKNIFTY")
        )
        self.assertEqual(performance["edgeAnalytics"]["totalTrades"], 1)
        self.assertEqual(performance["edgeAnalytics"]["symbolTrades"]["NIFTY"], 1)
        self.assertEqual(performance["edgeAnalytics"]["symbolTrades"].get("BANKNIFTY", 0), 0)
        self.assertEqual(len(performance["recentTrades"]), 1)
        self.assertEqual(performance["recentTrades"][0]["underlyingSymbol"], "NIFTY")


if __name__ == "__main__":
    unittest.main()
