import os
import unittest
from datetime import datetime
from unittest.mock import patch

import trading_config


class TradingConfigTests(unittest.TestCase):
    def test_default_profile_is_one_lakh(self):
        self.assertEqual(trading_config.DEFAULT_PROFILE_ID, "100000")
        self.assertEqual(
            trading_config._default_config()["profileId"],
            "100000",
        )

    def test_dynamic_limits_scale_from_capital(self):
        with patch.dict(os.environ, {}, clear=False):
            values = trading_config._profile_with_dynamic_limits(
                trading_config.CAPITAL_PROFILES["100000"]
            )

        self.assertEqual(values["optionCapitalPerEntry"], 100000.0)
        self.assertEqual(values["dailyProfitTarget"], 10000.0)
        self.assertEqual(values["dailyMaxLoss"], 15000.0)
        self.assertEqual(values["indexRiskPerTrade"], 15000.0)
        self.assertEqual(values["maxOpenPortfolioRisk"], 17250.0)

    def test_prior_day_selection_resets_before_market(self):
        config = {
            "profileId": "350000",
            "selectedDate": "2026-07-22",
            "selectedAt": "2026-07-22T09:05:00+05:30",
            "resetDoneDate": None,
        }
        current = datetime(2026, 7, 23, 8, 30, tzinfo=trading_config.IST)
        with patch.object(trading_config, "_write"):
            result = trading_config._ensure_automatic_reset(config, current)

        self.assertEqual(result["profileId"], "100000")
        self.assertEqual(result["selectedDate"], "2026-07-23")


if __name__ == "__main__":
    unittest.main()
