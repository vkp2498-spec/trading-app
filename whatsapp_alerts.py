import csv
import os
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
TRADE_HISTORY_FILE = BASE_DIR / "data" / "trade_history.csv"


def money(value):
    try:
        value = float(value)
        sign = "+" if value > 0 else ""
        return f"{sign}₹{value:,.2f}"
    except Exception:
        return "N/A"


def number(value):
    try:
        return f"₹{float(value):,.2f}"
    except Exception:
        return "N/A"


def option_side_text(trading_symbol):
    text = str(trading_symbol).upper()
    if " CE" in text or text.endswith("CE"):
        return "CALL"
    if " PE" in text or text.endswith("PE"):
        return "PUT"
    return "OPTION"


def pnl_emoji(value):
    try:
        return "🟢" if float(value) >= 0 else "🔴"
    except Exception:
        return "⚪"


def read_today_stats(trade_date):
    stats = {
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "net_pnl": 0.0,
    }

    if not TRADE_HISTORY_FILE.exists():
        return stats

    with TRADE_HISTORY_FILE.open("r", newline="") as f:
        reader = csv.DictReader(f)

        for row in reader:
            if str(row.get("trade_date")) != str(trade_date):
                continue

            pnl = float(row.get("gross_pnl") or 0)
            stats["trades"] += 1
            stats["net_pnl"] += pnl

            if pnl > 0:
                stats["wins"] += 1
            elif pnl < 0:
                stats["losses"] += 1

    return stats


def build_trade_closed_message(journal_row):
    trade_date = journal_row.get("trade_date")
    stats = read_today_stats(trade_date)

    symbol = journal_row.get("symbol", "N/A")
    trading_symbol = journal_row.get("trading_symbol", "N/A")
    side = option_side_text(trading_symbol)
    pnl = float(journal_row.get("gross_pnl") or 0)

    title_emoji = "🎯" if pnl >= 0 else "🛑"

    return f"""\
{title_emoji} *Trade Closed*

📌 *Index:* {symbol}
🧾 *Contract:* {trading_symbol}
📈 *Side:* {side}

💰 *Entry:* {number(journal_row.get("entry_price"))}
🏁 *Exit:* {number(journal_row.get("exit_price"))}
📍 *Reason:* {journal_row.get("exit_reason", "N/A")}

{pnl_emoji(pnl)} *Trade P&L:* {money(pnl)}

📊 *Today Summary*
Trades: *{stats["trades"]}*
Wins: *{stats["wins"]}*
Losses: *{stats["losses"]}*
Net P&L: *{money(stats["net_pnl"])}*

🙏 Hare Krishna
"""


def send_whatsapp_message(message):
    if os.getenv("ENABLE_WHATSAPP_ALERTS", "false").lower() != "true":
        return

    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_WHATSAPP_FROM")
    to_numbers = os.getenv("WHATSAPP_TO_NUMBERS", "")

    if not sid or not token or not from_number or not to_numbers:
        print("WhatsApp alert skipped: missing Twilio env vars", flush=True)
        return

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

    for to_number in [x.strip() for x in to_numbers.split(",") if x.strip()]:
        response = requests.post(
            url,
            data={
                "From": from_number,
                "To": to_number,
                "Body": message,
            },
            auth=(sid, token),
            timeout=20,
        )

        if response.status_code >= 300:
            print(f"WhatsApp alert failed for {to_number}: {response.status_code} {response.text[:300]}", flush=True)
        else:
            print(f"WhatsApp alert sent to {to_number}", flush=True)


def send_trade_closed_alert(journal_row):
    try:
        message = build_trade_closed_message(journal_row)
        send_whatsapp_message(message)
    except Exception as e:
        print(f"WhatsApp alert error: {e}", flush=True)