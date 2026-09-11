import unittest
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import vamsi_nifty_option_buy as strategy


def candidate(direction="BULLISH", *, target=120.0, volume_ratio=1.6):
    bullish = direction == "BULLISH"
    five = {
        "bias": direction,
        "close": 100.0,
        "high": 102.0,
        "low": 96.0,
        "pivot": 97.0 if bullish else 103.0,
        "middle_band": 96.0 if bullish else 104.0,
        "upper_band": target if bullish else 115.0,
        "lower_band": 85.0 if bullish else target,
        "recent_swing_high": target if bullish else 106.0,
        "recent_swing_low": 94.0 if bullish else target,
        "target": target,
        "atr14": 10.0,
        "volume_ratio": volume_ratio,
        "vwap": 97.0 if bullish else 103.0,
        "vwap_bias": direction,
    }
    fifteen = {
        "bias": direction,
        "recent_swing_high": 130.0,
        "recent_swing_low": 70.0,
        "upper_band": 135.0,
        "lower_band": 65.0,
        "target": 140.0 if bullish else 60.0,
    }
    option_type = "CE" if bullish else "PE"
    return {
        "symbol": "NIFTY",
        "direction": direction,
        "entry_price": 150.0,
        "instrument": {
            "instrument_key": "NSE_FO|1",
            "trading_symbol": f"NIFTY ATM {option_type}",
            "lot_size": 65,
        },
        "technicals": {
            "five_min": five,
            "fifteen_min": fifteen,
            "entry_structure": {
                "qualified": True,
                "type": "RETEST_HOLD",
                "reference": five["pivot"],
            },
            "market_regime": {
                "regime": "TREND",
                "direction": direction,
                "atr_percent": 0.10,
                "days_to_expiry": 2,
            },
            "nifty_breadth": {"bias": direction, "score": 45, "coverage": 45},
        },
        "option_summary": {
            "option_type": option_type,
            "chain_bias": direction,
            "chain_confidence": "MEDIUM",
            "option_market_quality": {
                "entry_allowed": True,
                "delta": 0.5,
                "spread_percent": 1.0,
                "ltp": 150.0,
                "iv": 14.0,
            },
        },
    }


class NiftyOptionBuyTests(unittest.TestCase):
    def test_scan_schedule_has_23_quarter_hour_slots(self):
        start = datetime(2026, 9, 10, tzinfo=ZoneInfo("Asia/Kolkata"))
        with patch.dict(strategy.os.environ, {}, clear=True):
            times = [start + timedelta(minutes=i) for i in range(24 * 60)]
            due = [value for value in times if strategy.scan_due(value)]
        self.assertEqual(len(due), 23)
        self.assertEqual(due[0].strftime("%H:%M"), "09:15")
        self.assertEqual(due[1].strftime("%H:%M"), "09:30")
        self.assertEqual(due[-1].strftime("%H:%M"), "14:45")
        self.assertTrue(all(b - a == timedelta(minutes=15) for a, b in zip(due, due[1:])))
        self.assertEqual(strategy.completed_scan_slot(due[0]), "2026-09-10T09:15:00+05:30")
        self.assertEqual(strategy.completed_scan_slot(due[-1]), "2026-09-10T14:45:00+05:30")

    def test_last_scan_minute_and_candle_publication_delay(self):
        last = datetime(2026, 9, 10, 14, 45, 59, tzinfo=ZoneInfo("Asia/Kolkata"))
        with patch.dict(strategy.os.environ, {}, clear=True):
            self.assertTrue(strategy.scan_due(last))
            self.assertFalse(strategy.scan_due(last + timedelta(seconds=1)))
            self.assertFalse(strategy.scan_due(last.replace(minute=46)))
            self.assertFalse(strategy.scan_due(last.replace(day=12)))
        self.assertEqual(strategy.completed_scan_slot(last), "2026-09-10T14:45:00+05:30")

    def test_publication_wait_is_bounded_and_only_on_schedule(self):
        current = datetime(2026, 9, 10, 9, 30, 2, tzinfo=ZoneInfo("Asia/Kolkata"))
        with patch.dict(strategy.os.environ, {}, clear=True), patch.object(strategy.time, "sleep") as sleep:
            strategy.wait_for_candle_publication(current)
            sleep.assert_called_once_with(8)
            sleep.reset_mock()
            strategy.wait_for_candle_publication(current.replace(second=20))
            strategy.wait_for_candle_publication(current.replace(minute=31))
            sleep.assert_not_called()

    def test_opening_scan_records_wait_and_never_places_order(self):
        current = datetime(2026, 9, 10, 9, 15, 10, tzinfo=ZoneInfo("Asia/Kolkata"))
        with tempfile.TemporaryDirectory() as folder, patch.dict(strategy.os.environ, {}, clear=True), patch.object(
            strategy, "DATA_DIR", Path(folder)
        ), patch.object(strategy, "SCAN_STATE_FILE", Path(folder) / "state.json"), patch.object(
            strategy, "SCAN_LOCK_FILE", Path(folder) / "lock"
        ), patch.object(strategy.trade_bot, "load_env"), patch.object(
            strategy.trade_bot, "trading_engine", return_value=strategy.ENGINE
        ), patch.object(strategy.trade_bot, "now_ist", return_value=current), patch.object(
            strategy, "record_scan"
        ) as record, patch.object(strategy, "collect_candidate") as collect, patch.object(
            strategy.trade_bot, "execute_selected_candidate"
        ) as execute, patch.object(strategy, "log"):
            self.assertEqual(strategy.scan()["action"], "WAITING_FOR_CANDLE")
            self.assertEqual(strategy.scan()["action"], "DUPLICATE")
        record.assert_called_once()
        collect.assert_not_called()
        execute.assert_not_called()

    def test_off_schedule_invocation_never_collects_or_executes(self):
        current = datetime(2026, 9, 10, 10, 6, tzinfo=ZoneInfo("Asia/Kolkata"))
        with patch.object(strategy.trade_bot, "load_env"), patch.object(
            strategy.trade_bot, "trading_engine", return_value=strategy.ENGINE
        ), patch.object(strategy.trade_bot, "now_ist", return_value=current), patch.object(
            strategy, "collect_candidate"
        ) as collect, patch.object(strategy.trade_bot, "execute_selected_candidate") as execute, patch.object(strategy, "log"):
            self.assertEqual(strategy.scan()["action"], "OUTSIDE_SCAN_SCHEDULE")
        collect.assert_not_called()
        execute.assert_not_called()

    def test_engine_is_accepted_by_central_runtime_validator(self):
        with patch.dict(
            strategy.trade_bot.os.environ,
            {"TRADING_ENGINE": strategy.ENGINE},
            clear=False,
        ):
            self.assertEqual(strategy.trade_bot.trading_engine(), strategy.ENGINE)

    def test_strategy_forces_max_capital_even_if_saved_profile_is_one_lot(self):
        environment = {
            "TRADING_ENGINE": strategy.ENGINE,
            "OPTION_CAPITAL_PER_ENTRY": "1",
            "ACCOUNT_MAX_OPTION_CAPITAL": "0",
        }
        with patch.dict(strategy.trade_bot.os.environ, environment, clear=False), patch.object(
            strategy.trade_bot,
            "active_value",
            return_value=1,
        ):
            self.assertEqual(strategy.trade_bot.option_capital_per_entry(), "MAX")

    def test_prior_daily_trades_do_not_block_a_new_sequential_opportunity(self):
        with patch.object(strategy.trade_bot, "read_state", return_value={}), patch.object(
            strategy.trade_bot,
            "state_is_active",
            return_value=False,
        ), patch.object(
            strategy.trade_bot,
            "daily_index_entry_block_reason",
            return_value="",
        ), patch.object(
            strategy.trade_bot,
            "index_trade_count_today",
            return_value=99,
        ) as trade_count:
            self.assertEqual(strategy.live_entry_block_reason(), "")
            trade_count.assert_not_called()

    def test_active_position_still_blocks_overlapping_max_allocation(self):
        with patch.object(strategy.trade_bot, "read_state", return_value={"status": "POSITION_OPEN"}), patch.object(
            strategy.trade_bot,
            "state_is_active",
            return_value=True,
        ):
            self.assertEqual(
                strategy.live_entry_block_reason(),
                "NIFTY bot position is already active",
            )

    def test_full_alignment_earns_one_hundred_points(self):
        result = strategy.weighted_signal(candidate())

        self.assertEqual(result["direction"], "BULLISH")
        self.assertEqual(result["option_direction"], "CALL")
        self.assertEqual(result["score"], 100.0)
        self.assertEqual(result["magnitude"], 100.0)

    def test_bearish_alignment_is_a_negative_put_score(self):
        result = strategy.weighted_signal(candidate("BEARISH", target=80.0))

        self.assertEqual(result["option_direction"], "PUT")
        self.assertEqual(result["score"], -100.0)

    def test_structure_stop_is_extended_to_half_atr(self):
        result = strategy.underlying_trade_plan(candidate())

        self.assertTrue(result["allowed"])
        self.assertEqual(result["entry"], 100.0)
        self.assertEqual(result["stop"], 95.0)
        self.assertEqual(result["target"], 120.0)
        self.assertEqual(result["stop_points"], 5.0)
        self.assertEqual(result["reward_risk"], 4.0)

    def test_nearby_target_rejects_poor_reward_risk(self):
        result = strategy.underlying_trade_plan(candidate(target=104.0))

        self.assertFalse(result["allowed"])
        self.assertIn("below 1.50", result["reason"])

    def test_candidate_requires_score_volatility_liquidity_and_rr(self):
        current = datetime(2026, 8, 31, 10, 1, tzinfo=ZoneInfo("Asia/Kolkata"))
        result = strategy.evaluate_candidate(candidate(), current=current)

        self.assertTrue(result["allowed"])
        self.assertEqual(result["blockers"], [])

    def test_candidate_rejects_weak_volume_score_and_wrong_contract(self):
        value = candidate(volume_ratio=0.5)
        value["option_summary"]["option_type"] = "PE"
        value["technicals"]["nifty_breadth"]["bias"] = "BEARISH"
        value["option_summary"]["chain_bias"] = "BEARISH"
        current = datetime(2026, 8, 31, 10, 1, tzinfo=ZoneInfo("Asia/Kolkata"))

        result = strategy.evaluate_candidate(value, current=current)

        self.assertFalse(result["allowed"])
        self.assertIn("contract PE does not express BULLISH", result["blockers"])

    def test_prepare_uses_max_allocation_and_underlying_levels(self):
        decision = strategy.evaluate_candidate(candidate())
        prepared = strategy.prepare_candidate(candidate(), decision)

        self.assertEqual(prepared["capital_override"], "MAX")
        self.assertEqual(prepared["strategy"], strategy.ENGINE)
        self.assertEqual(prepared["target_points"], 20.0)
        self.assertEqual(prepared["stop_points"], 5.0)
        self.assertEqual(prepared["target_price"], 160.0)
        self.assertEqual(prepared["stop_loss_price"], 147.5)
        self.assertEqual(
            prepared["structural_invalidation"]["stop_underlying"], 95.0
        )

    def test_risk_based_trailing_moves_to_break_even_at_one_r(self):
        state = {
            "strategy": strategy.ENGINE,
            "instrument_class": "INDEX_OPTION",
            "entry_transaction_type": "BUY",
            "entry_price": 100.0,
            "target_price": 130.0,
            "planned_target_price": 130.0,
            "stop_loss_price": 90.0,
            "original_stop_loss_price": 90.0,
        }
        with patch.object(strategy.trade_bot, "write_state"):
            result = strategy.trade_bot.apply_trailing_stop("NIFTY", state, 110.0)

        self.assertEqual(result["stop_loss_price"], 100.0)
        self.assertEqual(result["profit_protection_stage"], 1)


if __name__ == "__main__":
    unittest.main()
