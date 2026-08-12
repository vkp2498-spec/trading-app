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

    def test_edge_expectancy_is_always_normalized_and_excludes_unscalable_rows(self):
        small = trade(pnl=1_000.0, quantity=100, entry_price=100.0)
        large = trade(pnl=10_000.0, quantity=1_000, entry_price=100.0)
        missing_size = trade(pnl=50_000.0, quantity=0, entry_price=100.0)
        for item in (small, large, missing_size):
            item.update(
                {
                    "entryTime": "2026-07-31T10:15:00+05:30",
                    "score": 55,
                    "scoreVersion": "VAMSI_UNIFIED_ENTRY_V1",
                }
            )

        with patch.object(
            dashboard_data,
            "read_trade_history",
            return_value=[small, large, missing_size],
        ):
            performance = dashboard_data.build_trade_performance()

        analytics = performance["edgeAnalytics"]
        cell = next(
            item
            for item in analytics["matrix"]
            if item["timeBucket"] == "morning" and item["scoreBand"] == "50-59"
        )
        self.assertEqual(performance["cumulative"]["totalPnL"], 61_000.0)
        self.assertEqual(cell["expectancy"], 10_000.0)
        self.assertEqual(cell["trades"], 2)
        self.assertEqual(analytics["normalizationBasis"], "PER_LAKH_ENTRY_PREMIUM")
        self.assertEqual(analytics["excludedUnscalableTrades"], 1)
        self.assertEqual(
            performance["normalizedPerLakh"]["edgeAnalytics"],
            analytics,
        )

    def test_edge_heatmap_includes_low_scores_and_removes_unscored_unknown_time(self):
        valid = trade(pnl=1_000.0, quantity=100, entry_price=100.0)
        below_fifty = trade(pnl=20_000.0, quantity=100, entry_price=100.0)
        unscored = trade(pnl=30_000.0, quantity=100, entry_price=100.0)
        unknown_time = trade(pnl=40_000.0, quantity=100, entry_price=100.0)
        valid.update(
            {
                "entryTime": "2026-07-31T10:15:00+05:30",
                "score": 55,
                "scoreVersion": "VAMSI_UNIFIED_ENTRY_V1",
            }
        )
        below_fifty.update(valid, score=49)
        unscored.update(valid, score=None)
        unknown_time.update(valid, entryTime="")

        analytics = dashboard_data.normalized_edge_analytics_per_lakh(
            [valid, below_fifty, unscored, unknown_time]
        )

        self.assertEqual(analytics["totalTrades"], 2)
        self.assertEqual(analytics["excludedHiddenCategoryTrades"], 2)
        self.assertIn("40-49", analytics["scoreBands"])
        self.assertNotIn("Unscored", analytics["scoreBands"])
        self.assertNotIn("unknown", {item["id"] for item in analytics["timeBuckets"]})
        self.assertTrue(
            all(
                cell["scoreBand"] != "Unscored"
                and cell["timeBucket"] != "unknown"
                for cell in analytics["matrix"]
            )
        )

    def test_index_history_does_not_filter_retired_strategy_labels(self):
        historical = trade(pnl=2_500.0)
        historical["strategy"] = "RETIRED_EXPERIMENT"

        with patch.object(
            dashboard_data,
            "read_trade_history",
            return_value=[historical],
        ):
            performance = dashboard_data.build_trade_performance()

        self.assertEqual(performance["cumulative"]["totalTrades"], 1)
        self.assertEqual(performance["cumulative"]["totalPnL"], 2_500.0)

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
