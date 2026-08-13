import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import ml_shadow_v1 as ml


def candles(rows=40, start="2026-08-10 09:15"):
    index = pd.date_range(start, periods=rows, freq="15min", tz=ml.IST)
    values = []
    price = 25000.0
    for number in range(rows):
        close = price + (number % 5) - 2
        values.append(
            {
                "open": price,
                "high": max(price, close) + 4,
                "low": min(price, close) - 3,
                "close": close,
                "volume": 1000 + number * 10,
            }
        )
        price = close
    return pd.DataFrame(values, index=index)


class MlShadowV1Tests(unittest.TestCase):
    def test_features_do_not_change_when_a_future_candle_changes(self):
        original = candles()
        changed = original.copy()
        changed.iloc[20, changed.columns.get_loc("close")] += 500

        original_features = ml.build_feature_frame(original)
        changed_features = ml.build_feature_frame(changed)

        pd.testing.assert_series_equal(
            original_features.loc[original.index[15], ml.FEATURE_COLUMNS],
            changed_features.loc[changed.index[15], ml.FEATURE_COLUMNS],
        )

    def test_forward_label_uses_only_requested_horizon(self):
        frame = candles(rows=12)
        frame.loc[frame.index[1:5], "high"] = frame.iloc[0]["close"] + 20
        features = ml.build_feature_frame(frame)
        labeled = ml.add_forward_labels(features, horizon_candles=4, minimum_move=10)

        self.assertEqual(labeled.iloc[0]["label"], "CALL")
        self.assertAlmostEqual(labeled.iloc[0]["future_up_points"], 20.0)

    def test_action_requires_probability_points_and_reward_risk(self):
        prediction = {
            "call_probability": 0.72,
            "call_target_points": 12,
            "call_stop_points": 10,
            "call_reward_risk": 1.2,
            "put_probability": 0.10,
            "put_target_points": 20,
            "put_stop_points": 10,
            "put_reward_risk": 2.0,
        }
        with patch.dict(
            ml.os.environ,
            {
                "ML_SHADOW_MIN_PROBABILITY": "0.70",
                "ML_SHADOW_MIN_EXPECTED_POINTS": "10",
                "ML_SHADOW_MIN_REWARD_RISK": "0.80",
            },
            clear=False,
        ):
            self.assertEqual(ml.choose_action(prediction)["direction"], "CALL")
            prediction["call_probability"] = 0.69
            self.assertEqual(ml.choose_action(prediction)["action"], "NO_TRADE")

    def test_ambiguous_intrabar_target_and_stop_is_scored_as_stop(self):
        future = pd.DataFrame(
            [{"open": 100, "high": 115, "low": 85, "close": 104}]
        )
        outcome, points = ml._selected_forecast_outcome(
            future, "CALL", entry=100, target=10, stop=10
        )
        self.assertEqual((outcome, points), ("STOP", -10))

    def test_prediction_outcome_is_resolved_after_horizon(self):
        frame = candles(rows=8)
        candle_time = frame.index[0].isoformat()
        with tempfile.TemporaryDirectory() as directory:
            prediction_path = Path(directory) / "predictions.csv"
            row = {column: "" for column in ml.PREDICTION_COLUMNS}
            row.update(
                {
                    "candle_time": candle_time,
                    "underlying_entry_price": frame.iloc[0]["close"],
                    "call_target_points": 10,
                    "call_stop_points": 10,
                    "put_target_points": 10,
                    "put_stop_points": 10,
                    "direction": "CALL",
                    "expected_target_points": 10,
                    "expected_stop_points": 10,
                }
            )
            with prediction_path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=ml.PREDICTION_COLUMNS)
                writer.writeheader()
                writer.writerow(row)
            with patch.object(ml, "PREDICTIONS_FILE", prediction_path), patch.dict(
                ml.os.environ, {"ML_SHADOW_HORIZON_CANDLES": "4"}, clear=False
            ):
                self.assertEqual(ml.resolve_prediction_outcomes(frame), 1)
                with prediction_path.open() as handle:
                    resolved = list(csv.DictReader(handle))[0]
                self.assertTrue(resolved["resolved_at"])
                self.assertTrue(resolved["future_up_points"])

    def test_scan_refuses_live_trading_configuration(self):
        with patch.dict(
            ml.os.environ,
            {"TRADING_ENGINE": "ML_SHADOW_V1", "ENABLE_LIVE_TRADING": "true"},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "refuses"):
                ml.scan()


if __name__ == "__main__":
    unittest.main()
