import unittest
from datetime import datetime
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import vamsi_opening_pulse as pulse


def snapshot(
    *,
    spot=22120,
    today_open=22080,
    previous_close=22050,
    five_open=22080,
    five_close=22115,
):
    return {
        "spot": spot,
        "today_open": today_open,
        "previous_close": previous_close,
        "latest_completed_5m": {
            "open": five_open,
            "close": five_close,
        },
        "pivots": {
            "P": 22070,
            "R1": 22170,
            "R2": 22240,
            "S1": 22000,
            "S2": 21930,
        },
        "bollinger": {
            "middle": 22090,
            "upper": 22200,
            "lower": 21980,
        },
    }


class OpeningPulseTests(unittest.TestCase):
    def test_bullish_opening_path_always_selects_call(self):
        result = pulse.opening_pulse(snapshot())

        self.assertEqual(result["direction"], "BULLISH")
        self.assertEqual(result["option_direction"], "CALL")
        self.assertGreater(result["vote"], 0)

    def test_bearish_opening_path_selects_put(self):
        result = pulse.opening_pulse(
            snapshot(
                spot=21950,
                today_open=22020,
                previous_close=22050,
                five_open=22020,
                five_close=21955,
            )
        )

        self.assertEqual(result["direction"], "BEARISH")
        self.assertEqual(result["option_direction"], "PUT")
        self.assertLess(result["vote"], 0)

    def test_perfect_tie_still_resolves_to_a_trade_direction(self):
        tied = snapshot(
            spot=22000,
            today_open=22000,
            previous_close=22000,
            five_open=22000,
            five_close=22000,
        )
        tied["pivots"]["P"] = 22000
        tied["bollinger"]["middle"] = 22000

        result = pulse.opening_pulse(tied)

        self.assertEqual(result["direction"], "BULLISH")
        self.assertEqual(result["option_direction"], "CALL")

    def test_balanced_pair_prefers_near_named_levels(self):
        levels = [
            {"name": "R1", "level": 101},
            {"name": "R2", "level": 110},
            {"name": "S1", "level": 99},
            {"name": "S2", "level": 90},
        ]

        result = pulse.balanced_level_pair(
            "BULLISH",
            100,
            levels,
            minimum_distance=0.1,
        )

        self.assertEqual(result["target_name"], "R1")
        self.assertEqual(result["stop_name"], "S1")
        self.assertEqual(result["reward_risk"], 1.0)
        self.assertTrue(result["balanced_named_pair"])

    def test_unbalanced_levels_choose_closest_ratio_without_rejection(self):
        result = pulse.balanced_level_pair(
            "BEARISH",
            100,
            [
                {"name": "S1", "level": 98},
                {"name": "S2", "level": 92},
                {"name": "R1", "level": 105},
            ],
            minimum_distance=0.1,
        )

        self.assertEqual(result["target_name"], "S2")
        self.assertEqual(result["stop_name"], "R1")
        self.assertAlmostEqual(result["reward_risk"], 1.6)
        self.assertFalse(result["balanced_named_pair"])

    def test_gtt_is_immediate_and_has_no_trailing_stop(self):
        payload = pulse.gtt_payload(
            {"instrument_key": "NSE_FO|1", "entry_price": 100},
            {"target_price": 115, "stop_loss_price": 85},
            65,
        )

        self.assertEqual(payload["type"], "MULTIPLE")
        self.assertEqual(payload["quantity"], 65)
        self.assertTrue(all(rule["trigger_type"] == "IMMEDIATE" for rule in payload["rules"]))
        stop = next(rule for rule in payload["rules"] if rule["strategy"] == "STOPLOSS")
        self.assertNotIn("trailing_gap", stop)

    def test_scan_places_one_max_allocation_gtt_without_strategy_gates(self):
        fixed_now = datetime(2026, 8, 26, 9, 20, 5, tzinfo=ZoneInfo("Asia/Kolkata"))
        option = {
            "instrument_key": "NSE_FO|123",
            "trading_symbol": "NIFTY26AUG22100CE",
            "underlying_symbol": "NIFTY",
            "option_type": "CE",
            "strike": 22100,
            "expiry": "2026-08-26",
            "entry_price": 100.0,
            "delta": 0.5,
            "lot_size": 65,
        }
        response = Mock(status_code=200)
        response.json.return_value = {
            "status": "success",
            "data": {"gtt_order_ids": ["GTT-1"]},
        }
        saved_state = {}

        def read_state(_slot):
            return dict(saved_state)

        def write_state(_slot, value):
            saved_state.clear()
            saved_state.update(value)

        with TemporaryDirectory() as temporary, patch.dict(
            pulse.os.environ,
            {
                "ENABLE_LIVE_TRADING": "true",
                "VAMSI_OPENING_PULSE_LIVE_ENABLED": "true",
                "UPSTOX_ACCESS_TOKEN": "test-token",
            },
            clear=False,
        ), patch.object(pulse, "CLAIM_FILE", pulse.Path(temporary) / "claim.json"), patch.object(
            pulse, "LOCK_FILE", pulse.Path(temporary) / "entry.lock"
        ), patch.object(pulse, "now_ist", return_value=fixed_now), patch.object(
            pulse.trade_bot, "load_env"
        ), patch.object(
            pulse.trade_bot, "trading_engine", return_value=pulse.ENGINE
        ), patch.object(
            pulse.trade_bot, "read_state", side_effect=read_state
        ), patch.object(
            pulse.trade_bot, "write_state", side_effect=write_state
        ), patch.object(
            pulse.trade_bot, "state_is_active", return_value=False
        ), patch.object(
            pulse.trade_bot, "ganesh_gap_market_snapshot", return_value=snapshot()
        ), patch.object(
            pulse, "select_atm_option", return_value=option
        ), patch.object(
            pulse, "max_allocation_quantity", return_value=(650, 65_000.0)
        ), patch.object(
            pulse.requests, "post", return_value=response
        ) as post, patch.object(
            pulse.trade_bot, "increment_trade_count"
        ) as increment, patch.object(
            pulse.trade_bot, "send_apple_trade_entered_alert"
        ):
            result = pulse.scan()

        self.assertEqual(result["action"], "LIVE_GTT")
        self.assertEqual(saved_state["status"], "GTT_ACTIVE")
        self.assertEqual(saved_state["quantity"], 650)
        self.assertFalse(saved_state["trailing_stop_active"])
        increment.assert_called_once_with("NIFTY")
        submitted = post.call_args.kwargs["json"]
        self.assertEqual(submitted["quantity"], 650)
        self.assertNotIn("trailing_gap", str(submitted))


if __name__ == "__main__":
    unittest.main()
