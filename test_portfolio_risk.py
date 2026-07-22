import unittest

from portfolio_risk import (
    aggregate_risk_decision,
    correlation_decision,
    remaining_position_risk,
)


def position(symbol, direction, entry=100, stop=90, quantity=10, score=85, underlying=None):
    return {
        "symbol": symbol,
        "underlying_symbol": underlying or symbol,
        "direction": direction,
        "entry_transaction_type": "BUY",
        "entry_price": entry,
        "stop_loss_price": stop,
        "quantity": quantity,
        "weighted_score": score,
        "instrument_key": f"NSE_FO|{symbol}",
        "status": "POSITION_OPEN",
    }


class PortfolioRiskTests(unittest.TestCase):
    def test_profit_locked_stop_releases_open_risk(self):
        state = position("NIFTY", "BULLISH", entry=100, stop=105, quantity=65)
        self.assertEqual(remaining_position_risk(state), 0)

    def test_aggregate_gate_includes_buffer(self):
        existing = position("NIFTY", "BULLISH", entry=100, stop=90, quantity=65)
        result = aggregate_risk_decision(
            [existing], 200, 180, 30, "BUY", risk_limit=1300, buffer_percent=15
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["current_risk"], 650)
        self.assertEqual(result["proposed_risk"], 600)
        self.assertEqual(result["buffer_risk"], 90)

    def test_same_direction_indices_require_both_high_scores(self):
        existing = position("NIFTY", "BULLISH", score=76)
        result = correlation_decision(
            {"symbol": "BANKNIFTY", "direction": "BULLISH", "weighted_score": 90},
            [existing],
            same_direction_index_min_score=80,
        )
        self.assertFalse(result["allowed"])
        self.assertIn("both scores", result["reason"])

    def test_third_same_direction_position_is_rejected(self):
        states = [
            position("NIFTY", "BULLISH", score=90),
            position("BANKNIFTY", "BULLISH", score=90),
        ]
        result = correlation_decision(
            {"symbol": "NIFTY", "direction": "BULLISH", "weighted_score": 90},
            states,
            max_same_direction_positions=2,
        )
        self.assertFalse(result["allowed"])


if __name__ == "__main__":
    unittest.main()
