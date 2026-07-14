import csv
import json
import os
from pathlib import Path

import requests


BASE_DIR = Path(__file__).resolve().parent
TRADE_HISTORY_FILE = BASE_DIR / "data" / "trade_history.csv"


def plain_money(value):
    try:
        return f"INR {float(value):.2f}"
    except (TypeError, ValueError):
        return "INR 0.00"


def read_today_stats(trade_date):
    stats = {
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "net_pnl": 0.0,
    }

    if not TRADE_HISTORY_FILE.exists():
        return stats

    with TRADE_HISTORY_FILE.open("r", newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)

        for row in reader:
            if str(row.get("trade_date")) != str(trade_date):
                continue

            try:
                pnl = float(row.get("gross_pnl") or 0)
            except (TypeError, ValueError):
                pnl = 0.0

            stats["trades"] += 1
            stats["net_pnl"] += pnl

            if pnl > 0:
                stats["wins"] += 1
            elif pnl < 0:
                stats["losses"] += 1

    return stats


def template_variables(journal_row):
    trade_date = journal_row.get("trade_date")
    stats = read_today_stats(trade_date)

    return {
        "1": str(journal_row.get("symbol") or "N/A"),
        "2": plain_money(journal_row.get("gross_pnl")),
        "3": plain_money(stats["net_pnl"]),
    }


def send_trade_closed_alert(journal_row):
    try:
        if os.getenv("ENABLE_WHATSAPP_ALERTS", "false").lower() != "true":
            print("WhatsApp alert disabled", flush=True)
            return

        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        from_number = os.getenv("TWILIO_WHATSAPP_FROM")
        to_numbers = os.getenv("WHATSAPP_TO_NUMBERS", "")
        content_sid = os.getenv("TWILIO_TRADE_CLOSED_CONTENT_SID")

        required = {
            "TWILIO_ACCOUNT_SID": account_sid,
            "TWILIO_AUTH_TOKEN": auth_token,
            "TWILIO_WHATSAPP_FROM": from_number,
            "WHATSAPP_TO_NUMBERS": to_numbers,
            "TWILIO_TRADE_CLOSED_CONTENT_SID": content_sid,
        }

        missing = [name for name, value in required.items() if not value]

        if missing:
            print(
                f"WhatsApp alert skipped: missing {', '.join(missing)}",
                flush=True,
            )
            return

        url = (
            "https://api.twilio.com/2010-04-01/Accounts/"
            f"{account_sid}/Messages.json"
        )

        variables = template_variables(journal_row)

        recipients = [
            number.strip()
            for number in to_numbers.split(",")
            if number.strip()
        ]

        for recipient in recipients:
            response = requests.post(
                url,
                data={
                    "From": from_number,
                    "To": recipient,
                    "ContentSid": content_sid,
                    "ContentVariables": json.dumps(variables),
                },
                auth=(account_sid, auth_token),
                timeout=20,
            )

            try:
                response_data = response.json()
            except ValueError:
                response_data = {}

            if response.status_code >= 300:
                print(
                    f"WhatsApp alert failed for {recipient}: "
                    f"HTTP {response.status_code} "
                    f"{response.text[:500]}",
                    flush=True,
                )
            else:
                print(
                    f"WhatsApp template accepted for {recipient}: "
                    f"message_sid={response_data.get('sid')} "
                    f"status={response_data.get('status')}",
                    flush=True,
                )

    except Exception as error:
        print(f"WhatsApp alert error: {error}", flush=True)