import sys
import types
import unittest


sys.modules.setdefault("requests", types.ModuleType("requests"))

import dashboard_data


class PnLCalendarTests(unittest.TestCase):
    def test_groups_daily_bot_trades_and_deducts_approximate_charges(self):
        trades = [
            {
                "tradeDate": "2026-07-29",
                "grossPnL": 2_000.0,
                "quantity": 65,
                "entryPrice": 100.0,
                "exitPrice": 130.0,
                "transactionType": "BUY",
                "exitReason": "TARGET",
            },
            {
                "tradeDate": "2026-07-29",
                "grossPnL": -500.0,
                "quantity": 65,
                "entryPrice": 120.0,
                "exitPrice": 112.0,
                "transactionType": "BUY",
                "exitReason": "STOP_LOSS",
            },
            {
                "tradeDate": "2026-07-30",
                "grossPnL": 800.0,
                "quantity": 30,
                "entryPrice": 200.0,
                "exitPrice": 230.0,
                "transactionType": "BUY",
                "exitReason": "SQUAREOFF",
            },
        ]

        result = dashboard_data.build_pnl_calendar(trades)

        self.assertEqual([row["date"] for row in result], ["2026-07-29", "2026-07-30"])
        self.assertEqual(result[0]["trades"], 2)
        self.assertEqual(result[0]["grossPnL"], 1_500.0)
        expected_charges = dashboard_data.total_other_charges(trades[:2])
        self.assertEqual(result[0]["otherCharges"], expected_charges)
        self.assertEqual(result[0]["netPnL"], round(1_500.0 - expected_charges, 2))
        self.assertGreater(result[1]["otherCharges"], 88.5)
        self.assertLess(result[1]["netPnL"], 800.0)


if __name__ == "__main__":
    unittest.main()
