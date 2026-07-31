import sys
import types
import unittest
from unittest.mock import patch


sys.modules.setdefault("requests", types.ModuleType("requests"))

import dashboard_data


def trade(strategy, pnl, symbol="NIFTY"):
    return {
        "tradeDate": "2026-07-31",
        "symbol": symbol,
        "underlyingSymbol": symbol,
        "instrumentClass": "INDEX_OPTION",
        "strategy": strategy,
        "tradingSymbol": f"{symbol} 25000 CE",
        "transactionType": "BUY",
        "positionSide": "LONG_OPTION",
        "quantity": 65,
        "entryPrice": 100.0,
        "exitPrice": 110.0,
        "exitTime": "2026-07-31T10:00:00+05:30",
        "exitReason": "TARGET" if pnl > 0 else "STOP_LOSS",
        "grossPnL": pnl,
        "score": 4,
    }


class T20DashboardTests(unittest.TestCase):
    def test_normalization_preserves_t20_and_defaults_legacy_rows_to_selective(self):
        self.assertEqual(dashboard_data.normalize_trade({"strategy": "t20"})["strategy"], "T20")
        self.assertEqual(dashboard_data.normalize_trade({})["strategy"], "SELECTIVE")

    def test_t20_is_separate_from_selective_performance(self):
        history = [
            trade("SELECTIVE", 1_000),
            trade("", -200, "BANKNIFTY"),
            trade("T20", 500),
            trade("t20", -100, "BANKNIFTY"),
        ]
        with patch.object(dashboard_data, "read_trade_history", return_value=history):
            performance = dashboard_data.build_trade_performance()

        self.assertEqual(performance["cumulative"]["totalTrades"], 2)
        self.assertEqual(performance["cumulative"]["totalPnL"], 800)
        self.assertEqual(performance["t20"]["trades"], 2)
        self.assertEqual(performance["t20"]["grossPnL"], 400)
        self.assertEqual(performance["t20"]["winRate"], 50)
        self.assertLess(performance["t20"]["netPnL"], 400)
        self.assertEqual(len(performance["pnlCalendar"]), 1)
        self.assertEqual(performance["edgeAnalytics"]["totalTrades"], 2)


if __name__ == "__main__":
    unittest.main()
