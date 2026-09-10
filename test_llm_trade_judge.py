import json
import os
import tempfile
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

import llm_trade_judge as judge
import vamsi_nifty_option_buy as strategy
from test_nifty_rejection_repairs import NOW, row
from test_vamsi_nifty_option_buy import candidate
import pandas as pd


class JudgeTests(unittest.TestCase):
    def data(self):
        c = candidate()
        c["api_key"] = "DO_NOT_SEND"
        c["technicals"]["fifteen_min"]["secret"] = "DO_NOT_SEND"
        return judge.snapshot(c, strategy.evaluate_candidate(c, NOW), NOW)

    def response(self, verdict="PASS", **extra):
        output = {"verdict": verdict, "reason": "Review of supplied entry levels", "evidence_fields": ["plan.target_points"], **extra}
        return Mock(status_code=200, json=Mock(return_value={"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(output)}]}]}))

    def test_snapshot_is_allowlisted_and_total_score_hidden(self):
        text = json.dumps(self.data())
        self.assertNotIn("DO_NOT_SEND", text)
        self.assertNotIn("magnitude", text)
        self.assertNotIn("capital", text)

    def test_pass_veto_and_abstain_use_validated_schema(self):
        for verdict in ("PASS", "VETO", "ABSTAIN"):
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-secret"}), patch.object(
                judge.requests, "post", return_value=self.response(verdict)
            ) as post:
                result = judge.review(self.data())
            self.assertEqual(result["verdict"], verdict)
            self.assertFalse(post.call_args.kwargs["json"]["store"])
            self.assertNotIn("tools", post.call_args.kwargs["json"])
            self.assertNotIn("test-secret", json.dumps(result))

    def test_missing_key_timeout_http_and_invalid_evidence_fail_closed(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}), patch.object(judge.requests, "post") as post:
            self.assertEqual(judge.review(self.data())["verdict"], "ABSTAIN")
            post.assert_not_called()
        for response in (Mock(status_code=401), self.response(evidence_fields=["invented.price"]),
                         Mock(status_code=200, json=Mock(return_value={"status": "incomplete"}))):
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-secret"}), patch.object(judge.requests, "post", return_value=response):
                self.assertEqual(judge.review(self.data())["verdict"], "ABSTAIN")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-secret"}), patch.object(
            judge.requests, "post", side_effect=RuntimeError("test-secret")
        ):
            result = judge.review(self.data())
        self.assertEqual(result["verdict"], "ABSTAIN")
        self.assertNotIn("test-secret", json.dumps(result))

    def test_approval_expires_and_cannot_move_to_another_contract(self):
        c = candidate()
        c["llm_judge"] = {"verdict": "PASS", "started_at_epoch": 100, "version": judge.VERSION, "binding": judge.binding(c)}
        self.assertTrue(judge.approval_valid(c, now=120))
        self.assertFalse(judge.approval_valid(c, now=146))
        c["instrument"]["instrument_key"] = "OTHER"
        self.assertFalse(judge.approval_valid(c, now=120))

    def test_veto_never_refreshes_or_executes(self):
        c = candidate()
        with tempfile.TemporaryDirectory() as folder, patch.object(strategy, "DATA_DIR", Path(folder)), patch.object(
            judge, "review", return_value={"verdict": "VETO", "reason": "stop inside noise"}
        ), patch.object(strategy.trade_bot, "fetch_upstox_option_chain") as fetch:
            _, _, block = strategy.judge_entry(c, strategy.evaluate_candidate(c, NOW))
        self.assertIn("LLM VETO", block)
        fetch.assert_not_called()

    def test_scan_rejection_is_before_judge_or_execution(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(strategy, "DATA_DIR", Path(folder)), patch.object(
            strategy, "SCAN_STATE_FILE", Path(folder) / "state.json"
        ), patch.object(strategy, "SCAN_LOCK_FILE", Path(folder) / "lock"), patch.object(
            strategy.trade_bot, "load_env"
        ), patch.object(strategy.trade_bot, "trading_engine", return_value=strategy.ENGINE), patch.object(
            strategy.trade_bot, "now_ist", return_value=NOW
        ), patch.object(strategy, "live_entry_block_reason", return_value=""), patch.object(
            strategy.trade_bot, "reentry_block_reason", return_value=""
        ), patch.object(strategy, "collect_candidate", return_value=(candidate(), {
            "allowed": False, "option_direction": "CALL", "score": 20, "blockers": ["weak"]}
        )), patch.object(strategy, "record_scan"), patch.object(strategy, "log"), patch.object(
            strategy, "judge_entry"
        ) as review, patch.object(strategy.trade_bot, "execute_selected_candidate") as execute:
            self.assertEqual(strategy.scan()["action"], "REJECT")
        review.assert_not_called()
        execute.assert_not_called()

    def refreshed_review(self, spot=100, ask=150.5):
        c = candidate()
        c["option_summary"].update(expiry="2026-09-15", strike=25000, contract_selection_role="NEXT_EXPIRY_ATM")
        r = {**row(), "spot": spot, "CE_ask_price": ask}
        review = {"verdict": "PASS", "reason": "qualified", "version": judge.VERSION, "started_at_epoch": time.time()}
        with tempfile.TemporaryDirectory() as folder, patch.object(strategy, "DATA_DIR", Path(folder)), patch.object(
            judge, "review", return_value=review
        ), patch.object(strategy.trade_bot, "now_ist", return_value=NOW), patch.object(
            strategy.trade_bot, "fetch_upstox_option_chain", return_value=(pd.DataFrame([r]), pd.DataFrame([r]), None)
        ), patch.object(strategy.trade_bot, "find_index_option_instrument", return_value=c["instrument"]):
            return strategy.judge_entry(c, strategy.evaluate_candidate(c, NOW))

    def test_pass_refreshes_same_contract_without_changing_absolute_levels(self):
        c, decision, block = self.refreshed_review(spot=101)
        self.assertEqual(block, "")
        self.assertEqual(c["entry_price"], 150.5)
        self.assertEqual(decision["plan"]["entry"], 101)
        self.assertEqual(decision["plan"]["target"], 120)
        self.assertEqual(decision["plan"]["stop"], 95)

    def test_pass_cannot_override_price_drift(self):
        self.assertTrue(self.refreshed_review(spot=105)[2])
        self.assertTrue(self.refreshed_review(ask=160)[2])

    def test_extra_model_fields_cannot_change_order_size(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-secret"}), patch.object(
            judge.requests, "post", return_value=self.response(quantity=999)
        ):
            self.assertEqual(judge.review(self.data())["verdict"], "ABSTAIN")

    def test_execution_layer_blocks_live_order_without_pass(self):
        c = candidate()
        prepared = strategy.prepare_candidate(c, strategy.evaluate_candidate(c, NOW))
        prepared["transaction_type"] = "BUY"
        bot = strategy.trade_bot
        with patch.dict(os.environ, {"NIFTY_LLM_JUDGE_ENABLED": "true", "ENABLE_LIVE_TRADING": "true"}), patch.object(
            bot, "paper_after_first_outcome", return_value=False
        ), patch.object(bot, "order_quantity_for", return_value=65), patch.object(
            bot, "planned_trade_context", return_value={}
        ), patch.object(bot, "write_stream_instruments"), patch.object(
            bot, "portfolio_entry_lock", return_value=nullcontext()
        ), patch.object(bot, "pre_order_portfolio_decision", return_value={"allowed": True, "risk": {}}), patch.object(
            bot, "place_market_order"
        ) as place, patch.object(bot, "log"), patch.object(bot, "verbose_log"):
            self.assertFalse(bot._execute_selected_candidate_locked(prepared))
        place.assert_not_called()
        self.assertIn("LLM PASS", prepared["execution_rejection_reason"])


if __name__ == "__main__":
    unittest.main()
