import unittest

from banknifty_breadth import analyze_banknifty_breadth
from signal_score import banknifty_neutral_chain_direction, weighted_alignment_score


class BankNiftyBreadthTests(unittest.TestCase):
    def quote_payload(self, changes):
        data = {}
        keys = {}
        for index, (symbol, change_percent) in enumerate(changes.items(), start=1):
            key = f"NSE_EQ|{index}"
            keys[symbol] = key
            previous = 100.0
            last = previous * (1 + change_percent / 100)
            data[key.replace("|", ":", 1)] = {
                "instrument_token": key,
                "last_price": last,
                "ohlc": {"close": previous},
            }
        return {"data": data}, keys

    def bullish_technicals(self):
        return {
            "five_min": {
                "bias": "BULLISH",
                "confidence": "HIGH",
                "momentum_score": 4,
                "volume_confirmed": True,
            },
            "fifteen_min": {"bias": "BULLISH", "confidence": "HIGH"},
            "two_hour": {"bias": "NEUTRAL", "confidence": "LOW"},
            "atm_option_flow": {
                "bias": "BULLISH",
                "volume_ratio": 1.6,
                "volume_confirmed": True,
            },
            "banknifty_breadth": {
                "bias": "BULLISH",
                "confidence": "HIGH",
                "score": 70,
            },
        }

    def test_weighted_constituents_produce_bullish_breadth(self):
        changes = {
            "HDFCBANK": 0.8,
            "ICICIBANK": 0.6,
            "SBIN": 0.4,
            "KOTAKBANK": -0.1,
            "AXISBANK": 0.5,
        }
        payload, keys = self.quote_payload(changes)
        breadth = analyze_banknifty_breadth(payload, keys)
        self.assertEqual(breadth["bias"], "BULLISH")
        self.assertIn(breadth["confidence"], {"MEDIUM", "HIGH"})
        self.assertGreater(breadth["score"], 25)

    def test_neutral_chain_override_requires_aligned_breadth(self):
        direction, blockers = banknifty_neutral_chain_direction(self.bullish_technicals())
        self.assertEqual(direction, "BULLISH")
        self.assertEqual(blockers, [])

        conflicting = self.bullish_technicals()
        conflicting["banknifty_breadth"] = {
            "bias": "BEARISH",
            "confidence": "HIGH",
        }
        direction, blockers = banknifty_neutral_chain_direction(conflicting)
        self.assertIsNone(direction)
        self.assertTrue(any("breadth" in blocker for blocker in blockers))

    def test_neutral_chain_can_reach_strict_threshold_only_with_strong_confirmation(self):
        score = weighted_alignment_score(
            {
                "symbol": "BANKNIFTY",
                "bias": "BULLISH",
                "confidence": "MEDIUM",
                "chain_bias": "NEUTRAL",
                "chain_confidence": "MEDIUM",
            },
            self.bullish_technicals(),
            {"bias": "NEUTRAL"},
        )
        self.assertGreaterEqual(score["score"], 75)


if __name__ == "__main__":
    unittest.main()
