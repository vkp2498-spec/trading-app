import os
import socket
from pathlib import Path

import requests
import urllib3.util.connection as urllib3_cn

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"


def force_ipv4():
    def allowed_gai_family():
        return socket.AF_INET

    urllib3_cn.allowed_gai_family = allowed_gai_family


def load_env_file():
    if not ENV_FILE.exists():
        raise RuntimeError(f".env file not found at {ENV_FILE}")

    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def upstox_get(path):
    token = os.getenv("UPSTOX_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN not set in .env")

    url = f"https://api.upstox.com{path}"
    response = requests.get(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
        timeout=30,
    )

    if response.status_code != 200:
        raise RuntimeError(f"Upstox API failed {response.status_code}: {response.text[:1000]}")

    return response.json()


def as_float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def main():
    force_ipv4()
    load_env_file()

    data = upstox_get("/v2/portfolio/short-term-positions")
    positions = data.get("data") or []

    total_pnl = 0.0
    open_positions = []

    print("\nToday's Positions")
    print("-" * 80)

    for position in positions:
        symbol = (
            position.get("tradingsymbol")
            or position.get("trading_symbol")
            or position.get("instrument_token")
            or position.get("instrument_key")
            or "UNKNOWN"
        )

        qty = as_float(position.get("quantity") or position.get("net_quantity"))
        pnl = as_float(position.get("pnl"))
        avg_price = position.get("average_price") or position.get("buy_price") or "N/A"
        ltp = position.get("last_price") or position.get("ltp") or "N/A"

        total_pnl += pnl

        if qty != 0:
            open_positions.append(position)

        print(f"{symbol}")
        print(f"  Qty: {qty:g}")
        print(f"  Avg: {avg_price}")
        print(f"  LTP: {ltp}")
        print(f"  P&L: Rs {pnl:.2f}")
        print("-" * 80)

    print(f"\nNet P&L Today: Rs {total_pnl:.2f}")
    print(f"Open Positions: {len(open_positions)}\n")


if __name__ == "__main__":
    main()
