#!/usr/bin/env python3
"""Archive today's bot trades and reset only today's bot entry controls."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from zoneinfo import ZoneInfo

from safe_storage import atomic_write_json, file_lock


IST = ZoneInfo("Asia/Kolkata")
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ARCHIVE_DIR = BASE_DIR / "archive"
TRADE_HISTORY_FILE = DATA_DIR / "trade_history.csv"
TRADE_COUNT_FILE = BASE_DIR / "daily_trade_count.json"
DAY_RISK_STATE_FILE = DATA_DIR / "day_risk_state.json"
SYMBOLS = {"NIFTY", "BANKNIFTY"}
ACTIVE_STATUSES = {
    "POSITION_OPEN",
    "EXIT_PENDING",
    "BUY_PLACED_NOT_COMPLETE",
    "SELL_PLACED_NOT_COMPLETE",
    "GTT_SUBMITTING",
    "GTT_ACTIVE",
    "GTT_SUBMISSION_UNKNOWN",
    "SQUAREOFF_SENT",
}


def active_local_states(base_dir: Path = BASE_DIR) -> list[Path]:
    active = []
    for path in sorted(base_dir.glob("trade_state_*.json")):
        try:
            state = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            state = {}
        if state.get("instrument_key") and state.get("status") in ACTIVE_STATUSES:
            active.append(path)
    return active


def is_bot_index_row(row: dict[str, str], trade_date: str) -> bool:
    if str(row.get("trade_date")) != trade_date:
        return False
    if str(row.get("symbol") or "").upper() not in SYMBOLS:
        return False
    if str(row.get("instrument_class") or "INDEX_OPTION").upper() != "INDEX_OPTION":
        return False
    exit_reason = str(row.get("exit_reason") or "").strip().upper()
    trading_symbol = str(row.get("trading_symbol") or "").strip().upper()
    strategy = str(row.get("strategy") or "").strip().upper()
    manual_override = str(row.get("manual_override") or "").strip().lower()
    return not (
        exit_reason == "UPSTOX_SYNC"
        or trading_symbol.startswith("UPSTOX SYNC")
        or strategy in {"MANUAL", "MANUAL_INDEX", "MOBILE_MANUAL", "SELECTIVE_PAPER"}
        or strategy.endswith("_PAPER")
        or manual_override in {"1", "true", "yes", "on"}
    )


def archive_existing(source: Path, archive_root: Path) -> None:
    if not source.exists():
        return
    destination = archive_root / source.relative_to(BASE_DIR)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def filter_trade_history(trade_date: str, archive_root: Path) -> int:
    if not TRADE_HISTORY_FILE.exists():
        return 0
    lock_path = TRADE_HISTORY_FILE.with_suffix(TRADE_HISTORY_FILE.suffix + ".lock")
    with file_lock(lock_path):
        with TRADE_HISTORY_FILE.open("r", newline="", errors="ignore") as handle:
            reader = csv.DictReader(handle)
            fieldnames = reader.fieldnames or []
            rows = list(reader)
        removed = [row for row in rows if is_bot_index_row(row, trade_date)]
        if not removed:
            return 0
        archive_existing(TRADE_HISTORY_FILE, archive_root)
        retained = [row for row in rows if not is_bot_index_row(row, trade_date)]
        with NamedTemporaryFile(
            "w",
            newline="",
            dir=TRADE_HISTORY_FILE.parent,
            prefix=TRADE_HISTORY_FILE.name + ".",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            writer = csv.DictWriter(temporary, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(retained)
            temporary_path = Path(temporary.name)
        temporary_path.replace(TRADE_HISTORY_FILE)
        return len(removed)


def reset_today(confirm: bool = False) -> tuple[Path, int]:
    active = active_local_states()
    if active:
        names = ", ".join(path.name for path in active)
        raise RuntimeError(f"Active bot state found: {names}; reset refused")

    now = datetime.now(IST)
    trade_date = now.strftime("%Y-%m-%d")
    archive_root = ARCHIVE_DIR / f"today_session_reset_{now.strftime('%Y%m%d_%H%M%S')}"
    if not confirm:
        return archive_root, 0

    archive_root.mkdir(parents=True, exist_ok=True)
    removed = filter_trade_history(trade_date, archive_root)
    for source in (TRADE_COUNT_FILE, DAY_RISK_STATE_FILE):
        archive_existing(source, archive_root)
    for symbol in SYMBOLS:
        archive_existing(BASE_DIR / f"reentry_guard_{symbol}.json", archive_root)

    atomic_write_json(TRADE_COUNT_FILE, {"date": trade_date, "counts": {}})
    atomic_write_json(DAY_RISK_STATE_FILE, {"date": trade_date, "peak_pnl": 0.0})
    for symbol in SYMBOLS:
        atomic_write_json(BASE_DIR / f"reentry_guard_{symbol}.json", {})
    return archive_root, removed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    archive_root, removed = reset_today(confirm=args.confirm)
    if not args.confirm:
        print(f"Dry run: today's bot session would be reset; archive={archive_root}")
        return
    print(f"Reset today's bot session; archived_rows={removed}; archive={archive_root}")


if __name__ == "__main__":
    main()
