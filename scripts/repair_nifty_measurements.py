"""Backed-up, evidence-limited repair of the NIFTY journal (dry run by default)."""
import argparse
import csv
import io
import json
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from safe_storage import file_lock
from scripts.sync_core_env import active_state_files, write_atomic
from sync_upstox_today_trades import SYNC_REASON, trade_key_from_log

ENGINE = "VAMSI_NIFTY_OPTION_BUY_V1"


def confirmed_empty_dates(logs):
    dates = set()
    for text in logs:
        for day, block in re.findall(r"Date: (\d{4}-\d{2}-\d{2})\n(.*?)(?=Date: |\Z)", text, re.S):
            if "Upstox rows: 0\n" in block and "Upstox P&L: 0.00\n" in block:
                dates.add(day)
            elif "Upstox rows:" in block:
                dates.discard(day)
    return dates


def repair_rows(rows, empty_dates):
    totals = defaultdict(float)
    for row in rows:
        if row.get("exit_reason") != SYNC_REASON:
            totals[(row.get("trade_date"), trade_key_from_log(row))] += float(row.get("gross_pnl") or 0)
    retained, removed, changed = [], [], 0
    for original in rows:
        row = dict(original)
        key = (row.get("trade_date"), trade_key_from_log(row))
        if (row.get("exit_reason") == SYNC_REASON and row.get("trade_date") in empty_dates
                and abs(totals[key]) > 0.005
                and abs(float(row.get("gross_pnl") or 0) + totals[key]) < 0.005):
            removed.append(row)
            continue
        if row.get("strategy") == ENGINE and row.get("status") == "CLOSED":
            entry, exit_price = float(row["entry_price"]), float(row["exit_price"])
            qty = float(row["quantity"])
            high = max(float(row.get("highest_ltp") or entry), entry, exit_price)
            low = min(float(row.get("lowest_ltp") or entry), entry, exit_price)
            short = row.get("transaction_type") == "SELL"
            favourable = max(entry - low if short else high - entry, 0) * qty
            adverse = -max(high - entry if short else entry - low, 0) * qty
            for name, value in (("highest_ltp", high), ("lowest_ltp", low),
                                ("max_favorable_pnl", favourable), ("max_adverse_pnl", adverse)):
                if abs(float(row.get(name) or 0) - value) >= 0.005:
                    row[name] = str(round(value, 2))
            if (row.get("exit_reason") == "STOP_LOSS" and float(row.get("gross_pnl") or 0) > 0
                    and float(row.get("profit_protection_stage") or 0) > 0):
                row["exit_reason"] = "TRAILING_STOP"
        changed += row != original
        retained.append(row)
    return retained, removed, changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    root = args.app_dir.resolve()
    path = root / "data/trade_history.csv"
    dates = confirmed_empty_dates(p.read_text(errors="replace") for p in sorted(
        (root / "logs").glob("upstox_trade_sync.log*"), key=lambda p: p.stat().st_mtime) if p.is_file())
    with file_lock(path.with_suffix(".csv.lock")):
        if args.confirm and active_state_files(root):
            raise SystemExit("Refusing historical repair while bot state is active")
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames
            rows = list(reader)
        repaired, removed, changed = repair_rows(rows, dates)
        print(f"Proven empty-response dates: {sorted(dates)}")
        print(f"Invalid adjustments: {len(removed)}; measurement/label repairs: {changed}")
        print("Missing historical ticks cannot be reconstructed; excursions remain observed lower bounds.")
        if not args.confirm or not (removed or changed):
            return
        backup = root / "archive" / ("measurement_repair_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
        backup.mkdir(parents=True, mode=0o700)
        shutil.copy2(path, backup / "trade_history.csv")
        (backup / "audit.json").write_text(json.dumps({"empty_response_dates": sorted(dates),
            "removed_adjustments": removed, "modified_rows": changed,
            "excursions": "Observed bounds including fills; missing ticks not reconstructed"}, indent=2))
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(repaired)
        write_atomic(path, output.getvalue())
        print(f"Repair complete; original journal and audit saved in {backup}")


if __name__ == "__main__":
    main()
