import unittest

from sensex_breadth import analyze_sensex_breadth


class SensexBreadthTests(unittest.TestCase):
    def quote_payload(self, changes):
        data = {}
        keys = {}
        for index, (symbol, change_percent) in enumerate(changes.items(), start=1):
            key = f"NSE_EQ|{index}"
            keys[symbol] = key
            previous = 100.0
            data[key.replace("|", ":", 1)] = {
                "instrument_token": key,
                "last_price": previous * (1 + change_percent / 100),
                "ohlc": {"close": previous},
            }
        return {"data": data}, keys

    def test_broad_constituent_advance_is_bullish(self):
        changes = {f"TEST{number}": 0.6 for number in range(22)}
        payload, keys = self.quote_payload(changes)
        result = analyze_sensex_breadth(payload, keys)
        self.assertEqual(result["bias"], "BULLISH")
        self.assertEqual(result["coverage"], 22)
        self.assertGreater(result["score"], 45)

    def test_thin_quote_coverage_is_neutral(self):
        changes = {f"TEST{number}": 1.0 for number in range(10)}
        payload, keys = self.quote_payload(changes)
        result = analyze_sensex_breadth(payload, keys)
        self.assertEqual(result["bias"], "NEUTRAL")
        self.assertEqual(result["confidence"], "LOW")


if __name__ == "__main__":
    unittest.main()
