import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from zoneinfo import ZoneInfo

import vamsi_kb_intraday as kb


IST = ZoneInfo("Asia/Kolkata")


def qualified_candidate(direction="BULLISH", current=None, symbol="NIFTY"):
    current = current or datetime(2026, 8, 14, 11, 6, tzinfo=IST)
    breadth_score = 45 if direction == "BULLISH" else -45
    option_type = "CE" if direction == "BULLISH" else "PE"
    return {
        "symbol": symbol,
        "direction": direction,
        "entry_price": 150.0,
        "instrument": {
            "instrument_key": "NSE_FO|1",
            "trading_symbol": f"NIFTY ATM {option_type}",
            "lot_size": 65,
        },
        "option_summary": {
            "option_type": option_type,
            "chain_bias": direction,
            "chain_confidence": "HIGH",
            "option_market_quality": {
                "entry_allowed": True,
                "ltp": 149.5,
                "spread_percent": 0.8,
                "delta": 0.52 if direction == "BULLISH" else -0.52,
                "depth_bias": "NEUTRAL",
            },
        },
        "technicals": {
            "market_regime": {
                "regime": "TREND",
                "direction": direction,
            },
            "entry_structure": {
                "qualified": True,
                "type": "PULLBACK_HOLD",
                "signed_momentum": 4,
            },
            "bollinger_reversal": {"confirmed": False},
            "five_min": {
                "bias": direction,
                "confidence": "HIGH",
                "candle_time": (current - timedelta(minutes=6)).isoformat(),
            },
            "fifteen_min": {"bias": direction, "confidence": "HIGH"},
            "nifty_breadth": {
                "bias": direction,
                "confidence": "HIGH",
                "score": breadth_score,
                "coverage": 50,
            },
            "execution_atm_option_flow": {
                "bias": "BULLISH",
                "confidence": "HIGH",
                "close": 150,
                "vwap": 147,
                "volume_ratio": 1.5,
            },
            "trade_feasibility": {"entry_extension_percent": 0.2},
        },
    }


class VamsiKnowledgeEngineTests(unittest.TestCase):
    def test_all_independent_families_are_required(self):
        current = datetime(2026, 8, 14, 11, 6, tzinfo=IST)
        result = kb.evaluate_knowledge_setup(qualified_candidate(current=current), current)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["score"], 100.0)
        self.assertEqual(result["setup"], "PULLBACK_HOLD")

        candidate = qualified_candidate(current=current)
        candidate["technicals"]["nifty_breadth"]["bias"] = "BEARISH"
        rejected = kb.evaluate_knowledge_setup(candidate, current)
        self.assertFalse(rejected["allowed"])
        self.assertIn("breadth", " ".join(rejected["blockers"]).lower())

    def test_put_uses_oriented_negative_breadth_and_bought_put_flow(self):
        current = datetime(2026, 8, 14, 11, 6, tzinfo=IST)
        result = kb.evaluate_knowledge_setup(
            qualified_candidate("BEARISH", current), current
        )
        self.assertTrue(result["allowed"])

    def test_symbol_specific_breadth_coverage_is_not_nifty_hardcoded(self):
        current = datetime(2026, 8, 14, 11, 6, tzinfo=IST)
        bank = qualified_candidate(current=current, symbol="BANKNIFTY")
        bank["technicals"]["banknifty_breadth"] = bank["technicals"].pop(
            "nifty_breadth"
        )
        bank["technicals"]["banknifty_breadth"]["coverage"] = 5
        self.assertTrue(kb.evaluate_knowledge_setup(bank, current)["allowed"])

        sensex = qualified_candidate(current=current, symbol="SENSEX")
        sensex["technicals"]["sensex_breadth"] = sensex["technicals"].pop(
            "nifty_breadth"
        )
        sensex["technicals"]["sensex_breadth"]["coverage"] = 25
        self.assertTrue(kb.evaluate_knowledge_setup(sensex, current)["allowed"])

    def test_neutral_chain_is_rejected_by_default(self):
        current = datetime(2026, 8, 14, 11, 6, tzinfo=IST)
        candidate = qualified_candidate(current=current)
        candidate["option_summary"]["chain_bias"] = "NEUTRAL"
        with patch.dict(kb.os.environ, {"VAMSI_KB_ALLOW_NEUTRAL_CHAIN": "false"}):
            result = kb.evaluate_knowledge_setup(candidate, current)
        self.assertFalse(result["allowed"])
        self.assertFalse(result["evidence"]["option_chain"]["passed"])

    def test_contract_quality_requires_live_delta_and_tight_spread(self):
        current = datetime(2026, 8, 14, 11, 6, tzinfo=IST)
        candidate = qualified_candidate(current=current)
        candidate["option_summary"]["option_market_quality"]["spread_percent"] = 2.1
        result = kb.evaluate_knowledge_setup(candidate, current)
        self.assertFalse(result["allowed"])
        self.assertFalse(result["evidence"]["contract_quality"]["passed"])

    def test_prepared_trade_uses_actual_delta_and_fixed_nifty_points(self):
        current = datetime(2026, 8, 14, 11, 6, tzinfo=IST)
        candidate = qualified_candidate(current=current)
        decision = kb.evaluate_knowledge_setup(candidate, current)
        prepared = kb.prepare_candidate(candidate, decision)
        self.assertEqual(prepared["target_points"], 30)
        self.assertEqual(prepared["stop_points"], 30)
        self.assertEqual(prepared["target_price"], 165.6)
        self.assertEqual(prepared["stop_loss_price"], 134.4)
        self.assertEqual(prepared["entry_score"]["score_version"], kb.SCORE_VERSION)

    def test_prepared_trade_uses_index_specific_bank_and_sensex_points(self):
        current = datetime(2026, 8, 14, 11, 6, tzinfo=IST)
        with patch.dict(
            kb.os.environ,
            {
                "VAMSI_KB_BANKNIFTY_TARGET_POINTS": "60",
                "VAMSI_KB_BANKNIFTY_STOP_POINTS": "60",
                "VAMSI_KB_SENSEX_TARGET_POINTS": "40",
                "VAMSI_KB_SENSEX_STOP_POINTS": "40",
            },
        ):
            bank_candidate = qualified_candidate(current=current, symbol="BANKNIFTY")
            bank_candidate["technicals"]["banknifty_breadth"] = bank_candidate["technicals"].pop("nifty_breadth")
            bank = kb.prepare_candidate(
                bank_candidate, kb.evaluate_knowledge_setup(bank_candidate, current)
            )
            sensex_candidate = qualified_candidate(current=current, symbol="SENSEX")
            sensex_candidate["technicals"]["sensex_breadth"] = sensex_candidate["technicals"].pop("nifty_breadth")
            sensex = kb.prepare_candidate(
                sensex_candidate, kb.evaluate_knowledge_setup(sensex_candidate, current)
            )
        self.assertEqual((bank["target_points"], bank["stop_points"]), (60, 60))
        self.assertEqual((sensex["target_points"], sensex["stop_points"]), (40, 40))

    def test_scan_evaluates_all_indices_and_executes_highest_qualified_score(self):
        candidates = {
            symbol: {
                "symbol": symbol,
                "direction": "BULLISH",
                "entry_price": 100.0,
                "stop_loss_price": 85.0,
                "contract_selection_rank": 5.0,
                "instrument": {
                    "instrument_key": f"TEST|{symbol}",
                    "trading_symbol": f"{symbol} CE",
                    "lot_size": 10,
                },
            }
            for symbol in kb.INDEX_SYMBOLS
        }
        rank = {"NIFTY": 75.0, "BANKNIFTY": 82.0, "SENSEX": 88.0}

        def evaluate(symbol, **_kwargs):
            return candidates[symbol]

        def decision(candidate, current=None):
            return {
                "allowed": True,
                "direction": "BULLISH",
                "setup": "PULLBACK_HOLD",
                "score": 100.0,
                "blockers": [],
                "evidence": {},
            }

        def prepare(candidate, result):
            return {
                **candidate,
                "target_price": 115.0,
                "stop_loss_price": 85.0,
                "target_points": 30.0,
                "stop_points": 30.0,
                "knowledge_decision": result,
            }

        with (
            patch.object(kb.trade_bot, "load_env"),
            patch.object(kb.trade_bot, "trading_engine", return_value=kb.ENGINE),
            patch.object(kb, "_entry_window_ok", return_value=True),
            patch.object(kb, "_observation_window_ok", return_value=True),
            patch.object(kb, "_read_scan_state", return_value={}),
            patch.object(kb, "atomic_write_json"),
            patch.object(kb.trade_bot, "read_state", return_value={}),
            patch.object(kb.trade_bot, "state_is_active", return_value=False),
            patch.object(kb.trade_bot, "index_trade_count_today", return_value=0),
            patch.object(kb.trade_bot, "daily_index_entry_block_reason", return_value=""),
            patch.object(kb.trade_bot, "evaluate_symbol_buy_or_sell", side_effect=evaluate) as evaluate_mock,
            patch.object(kb, "evaluate_knowledge_setup", side_effect=decision),
            patch.object(kb, "selection_score", side_effect=lambda item: rank[item["symbol"]]),
            patch.object(kb, "prepare_candidate", side_effect=prepare),
            patch.object(kb.trade_bot, "order_quantity_for", return_value=20),
            patch.object(kb.trade_bot, "execute_selected_candidate", return_value=True) as execute,
            patch.object(kb, "_record_scan"),
        ):
            result = kb.scan()

        self.assertEqual(evaluate_mock.call_count, 3)
        self.assertEqual(result["symbol"], "SENSEX")
        self.assertEqual(execute.call_args.args[0]["symbol"], "SENSEX")

    def test_daily_trade_limit_blocks_orders_but_not_evidence_scans(self):
        with (
            patch.object(kb.trade_bot, "load_env"),
            patch.object(kb.trade_bot, "trading_engine", return_value=kb.ENGINE),
            patch.object(kb, "_entry_window_ok", return_value=True),
            patch.object(kb, "_observation_window_ok", return_value=True),
            patch.object(kb, "_read_scan_state", return_value={}),
            patch.object(kb, "atomic_write_json"),
            patch.object(kb.trade_bot, "read_state", return_value={}),
            patch.object(kb.trade_bot, "state_is_active", return_value=False),
            patch.object(kb.trade_bot, "index_trade_count_today", return_value=1),
            patch.object(
                kb.trade_bot,
                "evaluate_symbol_buy_or_sell",
                return_value=None,
            ) as evaluate,
            patch.object(kb.trade_bot, "execute_selected_candidate") as execute,
            patch.object(kb, "_record_scan"),
        ):
            result = kb.scan()

        self.assertEqual(evaluate.call_count, 3)
        self.assertEqual(result["action"], "NO_QUALIFIED_CANDIDATE")
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
