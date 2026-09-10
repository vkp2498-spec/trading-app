"""Sync today's Upstox realized P&L into the mobile dashboard trade log.

The mobile app reads data/trade_history.csv through the mobile API. This script
keeps existing bot rows, removes prior Upstox sync adjustment rows for the day,
then writes fresh adjustment rows so the log totals match Upstox account P&L.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from safe_storage import atomic_write_json, file_lock
from trade_history_schema import COLUMNS


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ENV_FILE = BASE_DIR / ".env"
TRADE_HISTORY_FILE = DATA_DIR / "trade_history.csv"
IST = ZoneInfo("Asia/Kolkata")
UPSTOX_TRADE_PNL_URL = "https://api.upstox.com/v2/trade/profit-loss/data"
SYNC_REASON = "UPSTOX_SYNC_ADJUSTMENT"
SYNC_STATUS_FILE = DATA_DIR / "upstox_pnl_sync_status.json"

def load_env() -> None:
    if not ENV_FILE.exists():
        return

    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def safe_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def financial_year(for_day: datetime) -> str:
    if for_day.month >= 4:
        return f"{for_day.year % 100:02d}{(for_day.year + 1) % 100:02d}"
    return f"{(for_day.year - 1) % 100:02d}{for_day.year % 100:02d}"


def fetch_upstox_rows(day: datetime) -> list[dict]:
    token = os.getenv("UPSTOX_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN is not set in .env")

    date_text = day.strftime("%d-%m-%Y")
    params = {
        "from_date": date_text,
        "to_date": date_text,
        "segment": "FO",
        "financial_year": financial_year(day),
        "page_number": 1,
        "page_size": 5000,
    }

    try:
        response = requests.get(
            UPSTOX_TRADE_PNL_URL,
            params=params,
            timeout=30,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "HKTradingMobileSync/1.0",
            },
        )
    except requests.RequestException as error:
        raise RuntimeError(f"Unable to contact Upstox: {type(error).__name__}") from error

    if response.status_code >= 300:
        detail = response.text[:500]
        raise RuntimeError(f"Upstox P&L request failed {response.status_code}: {detail}")

    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError("Upstox returned invalid JSON") from error

    if payload.get("status") != "success":
        raise RuntimeError("Upstox did not confirm a successful P&L response")
    rows = payload.get("data", [])
    if not isinstance(rows, list):
        raise RuntimeError("Upstox returned an unexpected P&L response")
    page_size = (payload.get("metadata") or {}).get("page", {}).get("page_size") or params["page_size"]
    if len(rows) >= int(page_size):
        raise RuntimeError("P&L response may be paginated; refusing to reconcile a partial report")
    return [row for row in rows if isinstance(row, dict)]


def fetch_upstox_rows_from_positions() -> list[dict]:
    token = os.getenv("UPSTOX_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN is not set in .env")

    response = requests.get(
        "https://api.upstox.com/v2/portfolio/short-term-positions",
        timeout=30,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "HKTradingMobileSync/1.0",
        },
    )

    if response.status_code >= 300:
        detail = response.text[:500]
        raise RuntimeError(f"Upstox positions request failed {response.status_code}: {detail}")

    payload = response.json()
    rows = payload.get("data", [])
    if not isinstance(rows, list):
        raise RuntimeError("Upstox returned an unexpected positions response")
    return [row for row in rows if isinstance(row, dict)]


def row_pnl(row: dict) -> float:
    for key in ["pnl", "day_pnl", "profit_and_loss"]:
        if row.get(key) is not None:
            result = float(row[key])
            break
    else:
        if row.get("sell_amount") is None or row.get("buy_amount") is None:
            raise ValueError("Broker row has no realized P&L or buy/sell amounts")
        result = float(row["sell_amount"]) - float(row["buy_amount"])
    if not math.isfinite(result):
        raise ValueError("Broker P&L is not finite")
    return round(result, 2)


def row_symbol(row: dict) -> str:
    text = str(row.get("scrip_name") or row.get("trading_symbol") or row.get("tradingsymbol") or "").upper()
    if "BANKNIFTY" in text:
        return "BANKNIFTY"
    if "NIFTY" in text:
        return "NIFTY"
    symbol = str(row.get("symbol") or row.get("underlying") or "UPSTOX").upper()
    return symbol or "UPSTOX"


def row_option_type(row: dict) -> str:
    text = str(row.get("scrip_name") or row.get("trading_symbol") or row.get("tradingsymbol") or "").upper()
    explicit = str(row.get("option_type") or row.get("optionType") or "").upper()
    if explicit in {"CE", "CALL"} or " CE" in text or text.endswith("CE") or "CALL" in text:
        return "CALL"
    if explicit in {"PE", "PUT"} or " PE" in text or text.endswith("PE") or "PUT" in text:
        return "PUT"
    return "UNKNOWN"


def trade_key_from_log(row: dict) -> tuple[str, str]:
    text = str(row.get("trading_symbol") or "").upper()
    symbol = str(row.get("underlying_symbol") or row.get("symbol") or "").upper()
    if "BANKNIFTY" in symbol or "BANKNIFTY" in text:
        symbol = "BANKNIFTY"
    elif "NIFTY" in symbol or "NIFTY" in text:
        symbol = "NIFTY"
    else:
        symbol = symbol or "UPSTOX"

    if "CALL" in text or text.endswith("CE") or " CE" in text:
        option = "CALL"
    elif "PUT" in text or text.endswith("PE") or " PE" in text:
        option = "PUT"
    else:
        option = "UNKNOWN"
    return symbol, option


def ensure_trade_history() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    if TRADE_HISTORY_FILE.exists():
        return
    with TRADE_HISTORY_FILE.open("w", newline="") as handle:
        csv.DictWriter(handle, fieldnames=COLUMNS).writeheader()


def read_trade_history() -> tuple[list[dict], list[str]]:
    ensure_trade_history()
    with TRADE_HISTORY_FILE.open("r", newline="", errors="ignore") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or COLUMNS
        return list(reader), fieldnames


def write_trade_history(rows: list[dict], fieldnames: list[str]) -> None:
    output_fields = COLUMNS if fieldnames != COLUMNS else fieldnames
    temporary = TRADE_HISTORY_FILE.with_suffix(".tmp")
    with temporary.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in output_fields})
    temporary.replace(TRADE_HISTORY_FILE)


def adjustment_row(day: datetime, symbol: str, option: str, pnl: float) -> dict:
    direction = "BULLISH" if option == "CALL" else "BEARISH" if option == "PUT" else ""
    transaction = "BUY" if pnl >= 0 else "SELL"
    return {
        "trade_date": day.strftime("%Y-%m-%d"),
        "symbol": symbol,
        "underlying_symbol": symbol,
        "instrument_class": "INDEX_OPTION",
        "trading_symbol": f"UPSTOX SYNC {symbol} {option}",
        "direction": direction,
        "transaction_type": transaction,
        "position_side": "LONG_OPTION",
        "quantity": "0",
        "entry_time": day.isoformat(),
        "entry_price": "",
        "exit_time": datetime.now(IST).isoformat(),
        "exit_price": "",
        "target_price": "",
        "stop_loss_price": "",
        "original_stop_loss_price": "",
        "profit_protection_stage": "",
        "profit_booking_price": "",
        "exit_reason": SYNC_REASON,
        "gross_pnl": f"{pnl:.2f}",
        "score": "",
        "status": "CLOSED",
    }


def sync_status(day, status, reason, dry_run=False):
    print(f"Reconciliation {status}: {reason}")
    if not dry_run:
        atomic_write_json(SYNC_STATUS_FILE, {
            "date": day.strftime("%Y-%m-%d"), "status": status,
            "reason": reason, "updated_at": datetime.now(IST).isoformat(),
        })


def sync(day: datetime, dry_run: bool = False, show_rows: bool = False) -> None:
    # Capture before the broker request. An exit arriving during that request
    # must not be negated by an older broker snapshot.
    trade_rows, fieldnames = read_trade_history()
    source = "profit-loss"
    try:
        upstox_rows = fetch_upstox_rows(day)
    except RuntimeError as error:
        if "403" not in str(error) and "1010" not in str(error):
            sync_status(day, "PENDING", "Broker P&L request failed; history preserved", dry_run)
            raise
        if day.date() != datetime.now(IST).date():
            sync_status(day, "PENDING", "Historical P&L unavailable; today's positions cannot reconcile another date", dry_run)
            return
        print(f"Profit-loss endpoint blocked; falling back to positions endpoint. Detail: {error}")
        upstox_rows = fetch_upstox_rows_from_positions()
        source = "positions"
    day_text = day.strftime("%Y-%m-%d")
    if not upstox_rows:
        sync_status(day, "PENDING", "Broker returned no rows; existing trade history preserved", dry_run)
        return

    clean_rows = [
        row
        for row in trade_rows
        if not (row.get("trade_date") == day_text and row.get("exit_reason") == SYNC_REASON)
    ]

    logged = defaultdict(float)
    logged_quantity = defaultdict(float)
    for row in clean_rows:
        if row.get("trade_date") != day_text:
            continue
        if "PAPER" in str(row.get("strategy") or "").upper() or str(row.get("paper_trade") or "").lower() == "true":
            continue
        logged[trade_key_from_log(row)] += safe_float(row.get("gross_pnl"))
        logged_quantity[trade_key_from_log(row)] += abs(safe_float(row.get("quantity")))

    upstox = defaultdict(float)
    broker_quantity = defaultdict(float)
    try:
        for row in upstox_rows:
            if source == "positions":
                # Never reconcile unrealized/open position P&L into closed trades.
                if row.get("quantity") is None or float(row["quantity"]) != 0:
                    raise ValueError("Open or incomplete broker position; wait until closed")
                realized = row.get("realised", row.get("realized"))
                if realized is None:
                    raise ValueError("Closed position has no realized P&L")
                pnl = row_pnl({"pnl": realized})
            else:
                pnl = row_pnl(row)
                quantity = float(row.get("quantity") or 0)
                if not math.isfinite(quantity) or quantity <= 0:
                    raise ValueError("Broker report has no valid closed trade quantity")
                broker_quantity[(row_symbol(row), row_option_type(row))] += quantity
            upstox[(row_symbol(row), row_option_type(row))] += pnl
    except (TypeError, ValueError) as error:
        sync_status(day, "PENDING", str(error), dry_run)
        return
    if set(logged) - set(upstox):
        sync_status(day, "PENDING", "Broker response is missing logged trade groups; history preserved", dry_run)
        return
    if source == "profit-loss" and any(broker_quantity[key] < quantity for key, quantity in logged_quantity.items()):
        sync_status(day, "PENDING", "Broker report does not yet cover all logged trade quantities", dry_run)
        return

    adjustments = []
    for key in sorted(set(logged) | set(upstox)):
        delta = round(upstox.get(key, 0.0) - logged.get(key, 0.0), 2)
        if abs(delta) >= 0.01:
            adjustments.append(adjustment_row(day, key[0], key[1], delta))

    print(f"Date: {day_text}")
    print(f"Upstox source: {source}")
    print(f"Upstox rows: {len(upstox_rows)}")
    print(f"Existing log P&L: {sum(logged.values()):.2f}")
    print(f"Upstox P&L: {sum(upstox.values()):.2f}")
    print(f"Adjustment P&L: {sum(safe_float(row['gross_pnl']) for row in adjustments):.2f}")

    if show_rows:
        for row in upstox_rows:
            print(f"{row_symbol(row):10s} {row_option_type(row):7s} {row_pnl(row):10.2f}  {row.get('scrip_name') or ''}")

    if dry_run:
        print("Dry run only; trade_history.csv was not changed.")
        return

    backup = TRADE_HISTORY_FILE.with_suffix(f".{day_text}.bak")
    if TRADE_HISTORY_FILE.exists() and not backup.exists():
        backup.write_bytes(TRADE_HISTORY_FILE.read_bytes())

    with file_lock(TRADE_HISTORY_FILE.with_suffix(TRADE_HISTORY_FILE.suffix + ".lock")):
        # Re-read while holding the lock so a monitor exit cannot be overwritten
        # by a concurrent reconciliation rewrite.
        current_rows, current_fields = read_trade_history()
        if current_rows != trade_rows:
            sync_status(day, "PENDING", "Trade history changed during broker request; retry with a fresh snapshot", dry_run)
            return
        retained = [
            row for row in current_rows
            if not (row.get("trade_date") == day_text and row.get("exit_reason") == SYNC_REASON)
        ]
        write_trade_history(retained + adjustments, current_fields)
    sync_status(day, "COMPLETE", f"Reconciled {len(upstox_rows)} broker rows", dry_run)
    print(f"Wrote {len(adjustments)} adjustment rows to {TRADE_HISTORY_FILE}")
    print(f"Backup: {backup}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync today's Upstox P&L into data/trade_history.csv")
    parser.add_argument("--date", help="Date to sync in YYYY-MM-DD format. Defaults to today in IST.")
    parser.add_argument("--dry-run", action="store_true", help="Print totals without writing trade_history.csv.")
    parser.add_argument("--show-rows", action="store_true", help="Print fetched Upstox rows.")
    return parser.parse_args()


def main() -> None:
    load_env()
    args = parse_args()
    day = (
        datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=IST)
        if args.date
        else datetime.now(IST)
    )
    sync(day, dry_run=args.dry_run, show_rows=args.show_rows)


if __name__ == "__main__":
    main()
