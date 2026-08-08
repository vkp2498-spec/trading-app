import unittest
from datetime import datetime
from unittest.mock import patch

import dashboard_data


class NiftyOnlyDashboardTests(unittest.TestCase):
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
