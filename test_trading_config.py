import os
import unittest
from datetime import datetime
from unittest.mock import patch

import trading_config


class TradingConfigTests(unittest.TestCase):
    def test_default_profile_can_be_configured_safely(self):
        self.assertEqual(trading_config.DEFAULT_PROFILE_ID, "MAX")
        with patch.dict(os.environ, {"DEFAULT_TRADING_PROFILE": "1_LOT"}):
            self.assertEqual(trading_config._default_config()["profileId"], "1_LOT")

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
        with (
            patch.object(trading_config, "_write"),
            patch.dict(os.environ, {"DEFAULT_TRADING_PROFILE": "1_LOT"}),
        ):
            result = trading_config._ensure_automatic_reset(config, current)

        self.assertEqual(result["profileId"], "1_LOT")
        self.assertEqual(result["dailyProfitTarget"], 5000)
        self.assertEqual(result["dailyMaxLoss"], 5000)
        self.assertEqual(result["selectedDate"], "2026-07-23")

    def test_selected_risk_limits_override_capital_derived_values(self):
        config = {
            "profileId": "MAX",
            "dailyProfitTarget": 25_000,
            "dailyMaxLoss": 15_000,
            "selectedDate": "2026-07-23",
            "selectedAt": "2026-07-23T09:05:00+05:30",
            "resetDoneDate": None,
        }
        current = datetime(2026, 7, 23, 9, 10, tzinfo=trading_config.IST)
        with (
            patch.object(trading_config, "_read", return_value=config),
            patch.object(trading_config, "now_ist", return_value=current),
            patch.dict(os.environ, {"DYNAMIC_CAPITAL_RISK_ENABLED": "true"}),
        ):
            result = trading_config.get_config()

        self.assertEqual(result["profile"]["optionCapitalPerEntry"], -1)
        self.assertEqual(result["profile"]["dailyProfitTarget"], 25_000)
        self.assertEqual(result["profile"]["dailyMaxLoss"], 15_000)
        self.assertEqual(result["riskLimitOptions"], [5000, 10000, 15000, 20000, 25000, 30000])

    def test_risk_limit_selection_rejects_values_outside_five_thousand_steps(self):
        current = datetime(2026, 7, 23, 9, 10, tzinfo=trading_config.IST)
        config = {
            "profileId": "1_LOT",
            "dailyProfitTarget": 5_000,
            "dailyMaxLoss": 5_000,
            "selectedDate": "2026-07-23",
            "selectedAt": None,
            "resetDoneDate": None,
        }
        with (
            patch.object(trading_config, "now_ist", return_value=current),
            patch.object(trading_config, "_read", return_value=config),
            patch.object(trading_config, "_write"),
        ):
            with self.assertRaisesRegex(ValueError, "steps of 5000"):
                trading_config.select_profile("1_LOT", 7_500, 5_000)

    def test_dynamic_limits_respect_absolute_caps(self):
        with patch.dict(
            os.environ,
            {
                "DAILY_MAX_LOSS_ABSOLUTE_CAP": "9000",
                "INDEX_RISK_PER_TRADE_ABSOLUTE_CAP": "5000",
            },
        ):
            values = trading_config._profile_with_dynamic_limits(
                trading_config.CAPITAL_PROFILES["350000"]
            )
        self.assertEqual(values["dailyMaxLoss"], 9000)
        self.assertEqual(values["indexRiskPerTrade"], 5000)

    def test_one_lot_limits_respect_absolute_caps(self):
        with patch.dict(
            os.environ,
            {
                "DAILY_MAX_LOSS": "15000",
                "DAILY_MAX_LOSS_ABSOLUTE_CAP": "8000",
                "MAX_OPEN_PORTFOLIO_RISK": "12000",
                "MAX_OPEN_PORTFOLIO_RISK_ABSOLUTE_CAP": "7000",
            },
        ):
            values = trading_config._profile_with_dynamic_limits(
                trading_config.CAPITAL_PROFILES["1_LOT"]
            )

        self.assertEqual(values["dailyMaxLoss"], 8000)
        self.assertEqual(values["maxOpenPortfolioRisk"], 7000)


if __name__ == "__main__":
    unittest.main()
