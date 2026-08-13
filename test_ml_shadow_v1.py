import csv
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import ml_shadow_v1 as ml


def daily_candles(rows=90, start=None):
    start = start or (date.today() - timedelta(days=rows + 120)).isoformat()
    days = pd.bdate_range(start, periods=rows, tz=ml.IST)
    index = days + pd.Timedelta(hours=9, minutes=15)
    values = []
    price = 25000.0
    for number in range(rows):
        opening = price + (number % 3) - 1
        close = opening + (number % 7) - 3
        values.append({
            "open": opening,
            "high": max(opening, close) + 20 + number % 4,
            "low": min(opening, close) - 14 - number % 5,
            "close": close,
            "volume": 100000 + number * 100,
        })
        price = close
    return pd.DataFrame(values, index=index)


class MlShadowV1Tests(unittest.TestCase):
    def test_features_use_only_prior_candles_and_current_open(self):
        original = daily_candles()
        changed = original.copy()
        changed.iloc[-1, changed.columns.get_loc("high")] += 500
        changed.iloc[-1, changed.columns.get_loc("low")] -= 500
        changed.iloc[-1, changed.columns.get_loc("close")] += 300

        original_features = ml.build_feature_frame(original)
        changed_features = ml.build_feature_frame(changed)
        pd.testing.assert_series_equal(
            original_features.iloc[-1][ml.FEATURE_COLUMNS],
            changed_features.iloc[-1][ml.FEATURE_COLUMNS],
        )

    def test_first_candle_per_day_ignores_second_candle(self):
        first = daily_candles(rows=3)
        second = first.copy()
        second.index = second.index + pd.Timedelta(hours=4)
        second["high"] += 1000
        selected = ml.first_candle_per_day(pd.concat([first, second]).sort_index())
        self.assertEqual(len(selected), 3)
        self.assertEqual(selected.index.hour.unique().tolist(), [9])

    def test_labels_are_percentage_excursions_from_open(self):
        frame = daily_candles(rows=1)
        frame.iloc[0, frame.columns.get_loc("open")] = 100
        frame.iloc[0, frame.columns.get_loc("high")] = 101
        frame.iloc[0, frame.columns.get_loc("low")] = 99.5
        labeled = ml.add_forward_labels(ml.build_feature_frame(frame), minimum_move=0.75)
        self.assertAlmostEqual(labeled.iloc[0]["future_up_percent"], 1.0)
        self.assertAlmostEqual(labeled.iloc[0]["future_down_percent"], 0.5)
        self.assertEqual(labeled.iloc[0]["call_label"], 1)
        self.assertEqual(labeled.iloc[0]["put_label"], 0)

    def test_call_and_put_qualify_independently(self):
        prediction = {
            "call_probability": 0.51, "call_target_percent": 0.30,
            "call_stop_percent": 0.30, "call_reward_risk": 1.0,
            "put_probability": 0.61, "put_target_percent": 0.20,
            "put_stop_percent": 0.25, "put_reward_risk": 0.8,
        }
        with patch.dict(ml.os.environ, {
            "ML_SHADOW_MIN_PROBABILITY": "0.50",
            "ML_SHADOW_MIN_REWARD_RISK": "0.75",
        }, clear=False):
            decisions = ml.choose_actions(prediction)
        self.assertTrue(decisions["CALL"]["qualified"])
        self.assertTrue(decisions["PUT"]["qualified"])

    def test_probability_must_be_strictly_above_half(self):
        prediction = {
            "call_probability": 0.50, "call_target_percent": 0.3,
            "call_stop_percent": 0.2, "call_reward_risk": 1.5,
            "put_probability": 0.1, "put_target_percent": 0.3,
            "put_stop_percent": 0.2, "put_reward_risk": 1.5,
        }
        with patch.dict(ml.os.environ, {
            "ML_SHADOW_MIN_PROBABILITY": "0.50",
            "ML_SHADOW_MIN_REWARD_RISK": "0.75",
        }, clear=False):
            self.assertFalse(ml.choose_actions(prediction)["CALL"]["qualified"])

    def test_ambiguous_intrabar_target_and_stop_is_conservative(self):
        outcome = ml._direction_outcome("CALL", 1.0, 1.0, 0.2, 0.5, 0.5)
        self.assertEqual(outcome, ("STOP", -0.5))

    def test_prediction_resolves_both_directions_in_percent(self):
        frame = daily_candles(rows=1)
        candle_time = frame.index[0].isoformat()
        with tempfile.TemporaryDirectory() as directory:
            prediction_path = Path(directory) / "predictions.csv"
            row = {column: "" for column in ml.PREDICTION_COLUMNS}
            row.update({
                "candle_time": candle_time,
                "call_target_percent": 0.05, "call_stop_percent": 1.0,
                "put_target_percent": 1.0, "put_stop_percent": 1.0,
            })
            with prediction_path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=ml.PREDICTION_COLUMNS)
                writer.writeheader(); writer.writerow(row)
            with patch.object(ml, "PREDICTIONS_FILE", prediction_path):
                self.assertEqual(ml.resolve_prediction_outcomes(frame), 1)
            with prediction_path.open() as handle:
                resolved = list(csv.DictReader(handle))[0]
            self.assertTrue(resolved["resolved_at"])
            self.assertTrue(resolved["future_up_percent"])
            self.assertIn(resolved["call_outcome"], {"TARGET", "STOP", "CLOSE"})
            self.assertIn(resolved["put_outcome"], {"TARGET", "STOP", "CLOSE"})

    def test_live_requires_two_matching_switches(self):
        with patch.dict(ml.os.environ, {
            "TRADING_ENGINE": "ML_SHADOW_V1",
            "ENABLE_LIVE_TRADING": "true",
            "ML_SHADOW_LIVE_TRADING_ENABLED": "false",
        }, clear=False):
            with self.assertRaisesRegex(RuntimeError, "must match"):
                ml.scan()


if __name__ == "__main__":
    unittest.main()
