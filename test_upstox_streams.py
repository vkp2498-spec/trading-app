import unittest

from upstox_streams import _normalise_feed


class UpstoxStreamNormalisationTests(unittest.TestCase):
    def test_unwraps_v3_full_feed_and_market_feed(self):
        normalized = _normalise_feed(
            {
                "fullFeed": {
                    "marketFF": {
                        "ltpc": {"ltp": 173.25, "cp": 168.0, "ltt": "123", "ltq": "65"},
                        "marketLevel": {
                            "bidAskQuote": [
                                {"bidP": 173.2, "askP": 173.3, "bidQ": "130", "askQ": "195"}
                            ]
                        },
                        "optionGreeks": {"delta": 0.51},
                        "oi": 12345,
                        "vtt": "4567",
                        "tbq": 890,
                        "tsq": 321,
                    }
                }
            }
        )

        self.assertEqual(normalized["ltp"], 173.25)
        self.assertEqual(normalized["close"], 168.0)
        self.assertEqual(normalized["bid_price"], 173.2)
        self.assertEqual(normalized["ask_price"], 173.3)
        self.assertEqual(normalized["bid_qty"], "130")
        self.assertEqual(normalized["ask_qty"], "195")
        self.assertEqual(normalized["oi"], 12345)
        self.assertEqual(normalized["volume"], "4567")
        self.assertEqual(normalized["total_buy_quantity"], 890)
        self.assertEqual(normalized["total_sell_quantity"], 321)
        self.assertEqual(normalized["recent_ticks"][-1]["ltp"], 173.25)

    def test_unwraps_v3_full_feed_and_index_feed(self):
        normalized = _normalise_feed(
            {"fullFeed": {"indexFF": {"ltpc": {"ltp": 24471.7, "cp": 24583.8}}}}
        )

        self.assertEqual(normalized["ltp"], 24471.7)
        self.assertEqual(normalized["close"], 24583.8)


if __name__ == "__main__":
    unittest.main()
