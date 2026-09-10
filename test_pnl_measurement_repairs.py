import csv
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import sync_upstox_today_trades as sync
import trade_bot
import trade_journal
import vamsi_nifty_option_buy as strategy
from scripts.repair_nifty_measurements import confirmed_empty_dates, repair_rows


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.day = datetime.now(sync.IST)
        self.row = {k: "" for k in sync.COLUMNS}
        self.row.update(trade_date=self.day.strftime("%Y-%m-%d"), symbol="NIFTY",
                        underlying_symbol="NIFTY", trading_symbol="NIFTY 23500 PE 15 SEP 26",
                        strategy=strategy.ENGINE, quantity="65", gross_pnl="-100", status="CLOSED")
        self.history = self.root / "trade_history.csv"
        for name, value in (("DATA_DIR", self.root), ("TRADE_HISTORY_FILE", self.history),
                            ("SYNC_STATUS_FILE", self.root / "status.json")):
            p = patch.object(sync, name, value)
            p.start()
            self.addCleanup(p.stop)
        sync.write_trade_history([self.row], sync.COLUMNS)

    def run_sync(self, rows):
        with patch.object(sync, "fetch_upstox_rows", return_value=[{"quantity": 65, **r} for r in rows]):
            sync.sync(self.day)

    def test_empty_response_keeps_history_byte_for_byte(self):
        before = self.history.read_bytes()
        self.run_sync([])
        self.assertEqual(self.history.read_bytes(), before)
        self.assertEqual(json.loads((self.root / "status.json").read_text())["status"], "PENDING")

    def test_missing_group_and_missing_pnl_never_zero_existing_loss(self):
        before = self.history.read_bytes()
        for rows in ([{"scrip_name": "NIFTY CE", "pnl": 20}], [{"scrip_name": "NIFTY PE"}],
                     [{"scrip_name": "NIFTY PE", "pnl": "nan"}]):
            self.run_sync(rows)
            self.assertEqual(self.history.read_bytes(), before)

    def test_confirmed_zero_is_different_from_missing_data(self):
        self.run_sync([{"scrip_name": "NIFTY PE", "buy_amount": 1000, "sell_amount": 1000}])
        rows, _ = sync.read_trade_history()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["gross_pnl"], "100.00")
        self.run_sync([{"scrip_name": "NIFTY PE", "pnl": 0}])
        repeated, _ = sync.read_trade_history()
        self.assertEqual(len(repeated), 2)
        self.assertEqual(repeated[-1]["gross_pnl"], "100.00")

    def test_paper_results_not_subtracted_from_broker_results(self):
        paper = {**self.row, "strategy": "SELECTIVE_PAPER", "gross_pnl": "500"}
        sync.write_trade_history([self.row, paper], sync.COLUMNS)
        self.run_sync([{"scrip_name": "NIFTY PE", "pnl": -100}])
        self.assertEqual(len(sync.read_trade_history()[0]), 2)

    def test_concurrent_exit_kept_and_not_negated(self):
        def fetched(_):
            sync.write_trade_history([self.row, {**self.row, "gross_pnl": "-200"}], sync.COLUMNS)
            return [{"scrip_name": "NIFTY PE", "pnl": -100, "quantity": 130}]
        with patch.object(sync, "fetch_upstox_rows", side_effect=fetched):
            sync.sync(self.day)
        rows, _ = sync.read_trade_history()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["exit_reason"] != sync.SYNC_REASON for r in rows))

    def test_open_positions_are_not_realized_results(self):
        before = self.history.read_bytes()
        with patch.object(sync, "fetch_upstox_rows", side_effect=RuntimeError("403")), patch.object(
            sync, "fetch_upstox_rows_from_positions", return_value=[{"quantity": 65, "realised": -100}]
        ):
            sync.sync(self.day)
        self.assertEqual(self.history.read_bytes(), before)

    def test_historical_date_never_uses_today_positions(self):
        with patch.object(sync, "fetch_upstox_rows", side_effect=RuntimeError("403")), patch.object(
            sync, "fetch_upstox_rows_from_positions"
        ) as positions:
            sync.sync(datetime(2025, 1, 1, tzinfo=sync.IST))
        positions.assert_not_called()

    def test_dry_run_does_not_change_files(self):
        before = self.history.read_bytes()
        with patch.object(sync, "fetch_upstox_rows", return_value=[]):
            sync.sync(self.day, dry_run=True)
        self.assertEqual(before, self.history.read_bytes())
        self.assertFalse((self.root / "status.json").exists())

    def test_partial_same_group_quantities_preserve_history(self):
        before = self.history.read_bytes()
        self.run_sync([{"scrip_name": "NIFTY PE", "pnl": -50, "quantity": 10}])
        self.assertEqual(before, self.history.read_bytes())


class ExcursionTests(unittest.TestCase):
    def test_extrema_persist_without_stream_or_trailing_activation(self):
        state = {"strategy": strategy.ENGINE, "instrument_class": "INDEX_OPTION", "entry_price": 100,
                 "original_stop_loss_price": 90, "stop_loss_price": 90,
                 "highest_ltp": 100, "lowest_ltp": 100}
        with patch.object(trade_bot, "write_state") as write:
            trade_bot.apply_trailing_stop("NIFTY", state, 104)
            write.assert_called_once()
            write.reset_mock()
            trade_bot.apply_trailing_stop("NIFTY", state, 97)
            write.assert_called_once()
            self.assertEqual((state["highest_ltp"], state["lowest_ltp"]), (104, 97))
            self.assertEqual(state["stop_loss_price"], 90)
            write.reset_mock()
            trade_bot.apply_trailing_stop("NIFTY", state, 99)
            write.assert_not_called()

    def journal(self, state, fill, reason="STOP_LOSS"):
        with tempfile.TemporaryDirectory() as folder, patch.object(trade_journal, "DATA_DIR", Path(folder)), patch.object(
            trade_journal, "TRADE_HISTORY_FILE", Path(folder) / "trades.csv"
        ):
            return trade_journal.record_closed_trade(state, fill, reason)

    def test_broker_fill_is_included_even_if_no_tick_was_seen(self):
        row = self.journal({"entry_price": 100, "quantity": 10, "highest_ltp": 100, "lowest_ltp": 100}, 90)
        self.assertEqual(row["max_adverse_pnl"], -100)
        self.assertEqual(row["lowest_ltp"], 90)
        self.assertEqual(row["gross_pnl"], -100)

    def test_short_exit_and_profitable_trailing_label(self):
        row = self.journal({"entry_price": 100, "quantity": 10, "entry_transaction_type": "SELL"}, 110)
        self.assertEqual(row["max_adverse_pnl"], -100)
        row = self.journal({"entry_price": 100, "quantity": 10, "profit_protection_stage": 2}, 105)
        self.assertEqual(row["exit_reason"], "TRAILING_STOP")
        self.assertEqual(row["max_favorable_pnl"], 50)
        row = self.journal({"entry_price": 100, "quantity": 10, "profit_protection_stage": 2}, 99)
        self.assertEqual(row["exit_reason"], "STOP_LOSS")

    def test_neutral_is_not_reported_as_missing_market_data(self):
        result = strategy.underlying_trade_plan({"direction": "NEUTRAL", "technicals": {
            "five_min": {"close": 23000, "atr14": 20}}})
        self.assertIn("neutral", result["reason"])
        self.assertNotIn("unavailable", result["reason"])


class HistoricalRepairTests(unittest.TestCase):
    def test_repair_requires_logged_empty_response_and_exact_offset(self):
        day = "2026-09-09"
        base = {"trade_date": day, "symbol": "NIFTY", "trading_symbol": "NIFTY PE", "gross_pnl": "-100"}
        adjustment = {**base, "gross_pnl": "100", "exit_reason": sync.SYNC_REASON}
        self.assertEqual(repair_rows([base, adjustment], set())[0], [base, adjustment])
        rows, removed, _ = repair_rows([base, adjustment], {day})
        self.assertEqual(rows, [base])
        self.assertEqual(removed, [adjustment])
        different = {**adjustment, "gross_pnl": "90"}
        self.assertFalse(repair_rows([base, different], {day})[1])

    def test_later_confirmed_report_supersedes_empty_response(self):
        empty = "Date: 2026-09-09\nUpstox rows: 0\nUpstox P&L: 0.00\n"
        self.assertEqual(confirmed_empty_dates([empty]), {"2026-09-09"})
        self.assertEqual(confirmed_empty_dates([empty, "Date: 2026-09-09\nUpstox rows: 2\n"]), set())

    def test_measurement_repair_is_idempotent_and_never_changes_pnl(self):
        row = {"strategy": strategy.ENGINE, "status": "CLOSED", "entry_price": "100", "exit_price": "90",
               "quantity": "10", "gross_pnl": "-100", "exit_reason": "STOP_LOSS"}
        rows, _, changed = repair_rows([row], set())
        self.assertEqual(changed, 1)
        self.assertEqual(rows[0]["gross_pnl"], "-100")
        self.assertEqual(float(rows[0]["max_adverse_pnl"]), -100)
        self.assertEqual(repair_rows(rows, set())[2], 0)


if __name__ == "__main__":
    unittest.main()
