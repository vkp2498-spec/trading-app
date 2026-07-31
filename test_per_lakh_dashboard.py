import sys
import types
import unittest
from unittest.mock import patch


sys.modules.setdefault("requests", types.ModuleType("requests"))

import dashboard_data


def trade(pnl=10_000.0, quantity=200, entry_price=1_000.0):
    return {
        "tradeDate": "2026-07-31",
        "symbol": "NIFTY",
        "underlyingSymbol": "NIFTY",
        "instrumentClass": "INDEX_OPTION",
        "strategy": "SELECTIVE",
        "tradingSymbol": "NIFTY 25000 CE",
        "transactionType": "BUY",
        "positionSide": "LONG_OPTION",
        "quantity": quantity,
        "entryPrice": entry_price,
        "exitPrice": entry_price + 50,
        "exitTime": "2026-07-31T10:00:00+05:30",
        "exitReason": "TARGET",
        "grossPnL": pnl,
        "score": 4,
    }


class PerLakhDashboardTests(unittest.TestCase):
    def test_scales_pnl_and_charges_from_total_entry_value(self):
        raw = trade()
        normalized = dashboard_data.normalize_trade_per_lakh(raw)

        self.assertEqual(normalized["tradeValueAtEntry"], 200_000.0)
        self.assertEqual(normalized["normalizationFactor"], 0.5)
        self.assertEqual(normalized["grossPnL"], 5_000.0)
        self.assertEqual(
            dashboard_data.approximate_other_charges(normalized),
            round(dashboard_data.approximate_other_charges(raw) * 0.5, 2),
        )

    def test_performance_keeps_raw_and_per_lakh_views(self):
        history = [trade()]
        with patch.object(dashboard_data, "read_trade_history", return_value=history):
            performance = dashboard_data.build_trade_performance()

        normalized = performance["normalizedPerLakh"]
        self.assertEqual(performance["cumulative"]["totalPnL"], 10_000.0)
        self.assertEqual(normalized["cumulative"]["totalPnL"], 5_000.0)
        self.assertEqual(normalized["cumulative"]["totalTrades"], 1)
        self.assertEqual(normalized["cumulative"]["winRate"], 100.0)
        self.assertEqual(normalized["recentTrades"][0]["grossPnL"], 5_000.0)

    def test_live_pnl_uses_the_same_entry_value_factor(self):
        live = {
            "totalLivePnL": 4_000.0,
            "positions": [
                {
                    "entryPrice": 1_000.0,
                    "quantity": 200,
                    "livePnL": 4_000.0,
                    "riskToStop": 2_000.0,
                    "rewardLeft": 6_000.0,
                }
            ],
        }

        normalized = dashboard_data.normalize_live_positions_per_lakh(live)

        self.assertEqual(normalized["totalLivePnL"], 2_000.0)
        self.assertEqual(normalized["positions"][0]["riskToStop"], 1_000.0)
        self.assertEqual(normalized["positions"][0]["rewardLeft"], 3_000.0)


if __name__ == "__main__":
    unittest.main()
