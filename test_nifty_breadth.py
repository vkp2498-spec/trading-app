import unittest

from nifty_breadth import analyze_nifty_breadth


class NiftyBreadthTests(unittest.TestCase):
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

    def test_broad_advance_is_bullish(self):
        symbols = [f"STOCK{index}" for index in range(35)]
        changes = {symbol: 0.7 for symbol in symbols}
        payload, keys = self.quote_payload(changes)
        result = analyze_nifty_breadth(payload, keys, heavyweights={})
        self.assertEqual(result["bias"], "BULLISH")
        self.assertGreater(result["score"], 25)
        self.assertEqual(result["advances"], 35)

    def test_insufficient_coverage_is_neutral(self):
        changes = {f"STOCK{index}": 1.0 for index in range(10)}
        payload, keys = self.quote_payload(changes)
        result = analyze_nifty_breadth(payload, keys, heavyweights={})
        self.assertEqual(result["bias"], "NEUTRAL")
        self.assertEqual(result["confidence"], "LOW")


if __name__ == "__main__":
    unittest.main()
