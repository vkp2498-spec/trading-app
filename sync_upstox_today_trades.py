"""Sync today's Upstox realized P&L into the mobile dashboard trade log.

The mobile app reads data/trade_history.csv through the mobile API. This script
keeps existing bot rows, removes prior Upstox sync adjustment rows for the day,
then writes fresh adjustment rows so the log totals match Upstox account P&L.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ENV_FILE = BASE_DIR / ".env"
TRADE_HISTORY_FILE = DATA_DIR / "trade_history.csv"
IST = ZoneInfo("Asia/Kolkata")
UPSTOX_TRADE_PNL_URL = "https://api.upstox.com/v2/trade/profit-loss/data"
SYNC_REASON = "UPSTOX_SYNC_ADJUSTMENT"

COLUMNS = [
    "trade_date",
    "symbol",
    "underlying_symbol",
    "instrument_class",
    "trading_symbol",
    "direction",
    "transaction_type",
    "position_side",
    "quantity",
    "entry_time",
    "entry_price",
    "exit_time",
    "exit_price",
    "target_price",
    "stop_loss_price",
    "original_stop_loss_price",
    "profit_protection_stage",
    "profit_booking_price",
    "exit_reason",
    "gross_pnl",
    "score",
    "status",
]


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
    params = urlencode(
        {
            "from_date": date_text,
            "to_date": date_text,
            "segment": "FO",
            "financial_year": financial_year(day),
            "page_number": 1,
            "page_size": 5000,
        }
    )
    request = Request(
        f"{UPSTOX_TRADE_PNL_URL}?{params}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )

    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="ignore")[:500]
        raise RuntimeError(f"Upstox P&L request failed {error.code}: {detail}") from error
    except URLError as error:
        raise RuntimeError(f"Unable to contact Upstox: {error.reason}") from error

    rows = payload.get("data", [])
    if not isinstance(rows, list):
        raise RuntimeError("Upstox returned an unexpected P&L response")
    return [row for row in rows if isinstance(row, dict)]


def row_pnl(row: dict) -> float:
    return round(safe_float(row.get("sell_amount")) - safe_float(row.get("buy_amount")), 2)


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


def sync(day: datetime, dry_run: bool = False, show_rows: bool = False) -> None:
    upstox_rows = fetch_upstox_rows(day)
    trade_rows, fieldnames = read_trade_history()
    day_text = day.strftime("%Y-%m-%d")

    clean_rows = [
        row
        for row in trade_rows
        if not (row.get("trade_date") == day_text and row.get("exit_reason") == SYNC_REASON)
    ]

    logged = defaultdict(float)
    for row in clean_rows:
        if row.get("trade_date") != day_text:
            continue
        logged[trade_key_from_log(row)] += safe_float(row.get("gross_pnl"))

    upstox = defaultdict(float)
    for row in upstox_rows:
        upstox[(row_symbol(row), row_option_type(row))] += row_pnl(row)

    adjustments = []
    for key in sorted(set(logged) | set(upstox)):
        delta = round(upstox.get(key, 0.0) - logged.get(key, 0.0), 2)
        if abs(delta) >= 0.01:
            adjustments.append(adjustment_row(day, key[0], key[1], delta))

    print(f"Date: {day_text}")
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

    write_trade_history(clean_rows + adjustments, fieldnames)
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
