import csv
import tempfile
import unittest
from pathlib import Path

from merge_trade_history import apply_merge, index_audit, merge_sources
from trade_history_schema import COLUMNS


def write_history(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in COLUMNS})


class MergeTradeHistoryTests(unittest.TestCase):
    def test_overlapping_snapshots_are_deduplicated_and_enriched(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            backup = data_dir / "trade_history.2026-07-22.bak"
            current = data_dir / "trade_history.csv"
            duplicate = {
                "trade_date": "2026-07-22",
                "symbol": "NIFTY",
                "underlying_symbol": "NIFTY",
                "instrument_class": "INDEX_OPTION",
                "trading_symbol": "NIFTY 25000 CE",
                "entry_time": "2026-07-22T10:00:00+05:30",
                "exit_time": "2026-07-22T10:30:00+05:30",
                "quantity": "65",
                "gross_pnl": "1500.00",
                "score": "6.2",
                "exit_reason": "TARGET",
                "status": "CLOSED",
            }
            write_history(backup, [duplicate])
            write_history(
                current,
                [
                    {**duplicate, "score": "", "status": "SETTLED"},
                    {
                        **duplicate,
                        "symbol": "BANKNIFTY",
                        "underlying_symbol": "BANKNIFTY",
                        "trading_symbol": "BANKNIFTY 56000 PE",
                        "entry_time": "2026-07-23T11:00:00+05:30",
                        "exit_time": "2026-07-23T11:20:00+05:30",
                        "gross_pnl": "-800",
                        "score": "-5.8",
                    },
                    {
                        **duplicate,
                        "symbol": "STOCK_FUTURE",
                        "underlying_symbol": "RELIANCE",
                        "instrument_class": "STOCK_FUTURE",
                        "trading_symbol": "RELIANCE FUT",
                    },
                    {
                        **duplicate,
                        "symbol": "RELIANCE",
                        "underlying_symbol": "RELIANCE",
                        "instrument_class": "STOCK_OPTION",
                        "trading_symbol": "RELIANCE 3000 CE",
                    },
                    {
                        **duplicate,
                        "trading_symbol": "UPSTOX SYNC NIFTY CALL",
                        "exit_reason": "UPSTOX_SYNC_ADJUSTMENT",
                    },
                ],
            )

            result = merge_sources([backup, current])

            self.assertEqual(len(result.rows), 2)
            self.assertEqual(result.duplicate_count, 1)
            self.assertEqual(result.ignored_row_count, 3)
            nifty = next(row for row in result.rows if row["symbol"] == "NIFTY")
            self.assertEqual(nifty["score"], "6.2")
            self.assertEqual(nifty["status"], "SETTLED")
            audit = {item["symbol"]: item for item in index_audit(result.rows)}
            self.assertEqual(audit["NIFTY"]["pnl"], 1500)
            self.assertEqual(audit["BANKNIFTY"]["pnl"], -800)

    def test_apply_creates_safety_backup_and_sorted_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "trade_history.csv"
            write_history(
                target,
                [
                    {"trade_date": "2026-07-23", "symbol": "NIFTY", "gross_pnl": "20"},
                    {"trade_date": "2026-07-22", "symbol": "BANKNIFTY", "gross_pnl": "10"},
                ],
            )
            original_mtime = target.stat().st_mtime_ns
            result = merge_sources([target])

            backup = apply_merge(target, result, original_mtime)

            self.assertTrue(backup.exists())
            with target.open("r", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([row["trade_date"] for row in rows], ["2026-07-22", "2026-07-23"])


if __name__ == "__main__":
    unittest.main()
