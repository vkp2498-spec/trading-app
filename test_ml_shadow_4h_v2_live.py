import unittest
from unittest.mock import patch

import ml_shadow_4h_v2_live as v2
import ml_shadow_v1 as execution


class MlShadowV2LiveTests(unittest.TestCase):
    def test_probability_alone_does_not_qualify_negative_payoff(self):
        prediction = {
            "call_probability": 0.786,
            "call_target_percent": 0.27,
            "call_stop_percent": 1.0,
            "call_reward_risk": 0.27,
            "put_probability": 0.807,
            "put_target_percent": 0.95,
            "put_stop_percent": 1.0,
            "put_reward_risk": 0.95,
        }
        with patch.dict(v2.os.environ, {
            "ML_SHADOW_MIN_PROBABILITY": "0.50",
            "ML_SHADOW_MIN_EXPECTED_VALUE_R": "0.10",
        }, clear=False):
            decisions = v2.choose_actions(prediction)
        self.assertFalse(decisions["CALL"]["qualified"])
        self.assertTrue(decisions["PUT"]["qualified"])
        self.assertAlmostEqual(decisions["CALL"]["expected_value_r"], -0.00178)
        self.assertAlmostEqual(decisions["PUT"]["expected_value_r"], 0.57365)

    def test_execution_drift_rechecks_expected_value(self):
        prediction = {
            "underlying_open": 100.0,
            "underlying_entry_price": 100.0,
            "minimum_execution_reward_risk": 0.0,
            "minimum_expected_value_r": 0.10,
        }
        decision = {
            "direction": "CALL",
            "probability": 0.60,
            "target_percent": 0.50,
            "stop_percent": 1.0,
            "reward_risk": 0.50,
        }
        option = {"entry_price": 100.0, "delta": 0.50}
        with self.assertRaisesRegex(RuntimeError, "remaining EV proxy"):
            execution.option_levels(prediction, decision, option)

    def test_state_records_v2_model_identity(self):
        prediction = {
            "model_version": v2.MODEL_VERSION,
            "underlying_open": 100.0,
            "underlying_entry_price": 100.0,
            "model_hash": "hash",
            "model_trained_through": "2026-08-13",
            "candle_time": "2026-08-14T09:15:00+05:30",
        }
        decision = {
            "direction": "PUT", "probability": 0.80,
            "target_percent": 1.0, "stop_percent": 1.0,
            "reward_risk": 1.0, "expected_value_r": 0.60,
        }
        option = {
            "instrument_key": "NSE_FO|1", "trading_symbol": "NIFTY PE",
            "option_type": "PE", "lot_size": 65, "entry_price": 100.0,
        }
        levels = {
            "target_price": 110.0, "stop_loss_price": 90.0,
            "trailing_gap": 2.5, "underlying_target_price": 99.0,
            "underlying_stop_price": 101.0, "option_target_percent": 10.0,
            "option_stop_percent": 10.0, "execution_reward_risk": 1.0,
        }
        state = execution._base_state(prediction, decision, option, levels, "LIVE_GTT")
        self.assertEqual(state["entry_score_version"], v2.MODEL_VERSION)
        self.assertEqual(state["ml_expected_value_r"], 0.60)


if __name__ == "__main__":
    unittest.main()
