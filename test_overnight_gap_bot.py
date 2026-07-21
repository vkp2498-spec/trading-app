import unittest

from overnight_gap_bot import premium_levels


class OvernightGapBotTests(unittest.TestCase):
    def test_premium_levels_use_delta(self):
        self.assertEqual(
            premium_levels(100, 30, 20, 0.5),
            {"target_price": 115.0, "stop_loss_price": 90.0},
        )

    def test_levels_never_go_below_option_tick_floor(self):
        self.assertEqual(
            premium_levels(5, 30, 20, 0.5),
            {"target_price": 20.0, "stop_loss_price": 0.05},
        )


if __name__ == "__main__":
    unittest.main()
