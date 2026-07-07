import os
import sys
import json
import gzip
import urllib.request
from pathlib import Path
from datetime import datetime, time
from zoneinfo import ZoneInfo

import requests

from strategy_core import get_nifty_recommendation, now_ist

import socket
import urllib3.util.connection as urllib3_cn


def allowed_gai_family():
    return socket.AF_INET


urllib3_cn.allowed_gai_family = allowed_gai_family

IST = ZoneInfo("Asia/Kolkata")
BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "trade_state.json"
ENV_FILE = BASE_DIR / ".env"
INSTRUMENT_CACHE = BASE_DIR / "upstox_complete.json.gz"

UPSTOX_GTT_PLACE_URL = "https://api.upstox.com/v3/order/gtt/place"
UPSTOX_GTT_GET_URL = "https://api.upstox.com/v3/order/gtt"
UPSTOX_GTT_CANCEL_URL = "https://api.upstox.com/v3/order/gtt/cancel"
UPSTOX_EXIT_POSITIONS_URL = "https://api.upstox.com/v2/order/positions/exit"
UPSTOX_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"

def load_env():
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

def log(msg):
    print(f"{now_ist().strftime('%Y-%m-%d %H:%M:%S')} | {msg}", flush=True)

def round_tick(value, tick=0.05):
    return round(round(float(value) / tick) * tick, 2)

def read_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}

def write_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))

def clear_state():
    write_state({})

def upstox_headers():
    token = os.getenv("UPSTOX_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN not set in .env")
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }

def upstox_request(method, url, **kwargs):
    response = requests.request(method, url, headers=upstox_headers(), timeout=30, **kwargs)
    if response.status_code >= 300:
        raise RuntimeError(f"Upstox API failed {response.status_code}: {response.text[:500]}")
    return response.json()

def get_gtt_details(gtt_order_id):
    return upstox_request("GET", UPSTOX_GTT_GET_URL, params={"gtt_order_id": gtt_order_id})

def is_gtt_still_active(gtt_order_id):
    try:
        data = get_gtt_details(gtt_order_id).get("data", [])
    except Exception as e:
        log(f"GTT lookup failed, assuming inactive: {e}")
        return False

    if not data:
        return False

    statuses = []
    for item in data:
        for rule in item.get("rules", []):
            statuses.append(str(rule.get("status", "")).upper())

    active_statuses = {"SCHEDULED", "OPEN", "PENDING", "TRIGGERED", "INACTIVE"}
    return any(status in active_statuses for status in statuses)

def cancel_gtt(gtt_order_id):
    return upstox_request("DELETE", UPSTOX_GTT_CANCEL_URL, json={"gtt_order_id": gtt_order_id})

def exit_nse_fo_positions():
    return upstox_request("POST", UPSTOX_EXIT_POSITIONS_URL, params={"segment": "NSE_FO"})

def ensure_instruments_file():
    if INSTRUMENT_CACHE.exists():
        return
    log("Downloading Upstox instrument file...")
    urllib.request.urlretrieve(UPSTOX_INSTRUMENTS_URL, INSTRUMENT_CACHE)

def parse_expiry(expiry_text):
    # Example: "07 Jul"
    dt = datetime.strptime(f"{expiry_text} {now_ist().year}", "%d %b %Y").date()
    if dt < now_ist().date():
        dt = datetime.strptime(f"{expiry_text} {now_ist().year + 1}", "%d %b %Y").date()
    return dt

def find_nifty_option_instrument(expiry_text, strike, option_type):
    ensure_instruments_file()
    wanted_expiry = parse_expiry(expiry_text)
    wanted_strike = float(strike)

    with gzip.open(INSTRUMENT_CACHE, "rt", encoding="utf-8") as f:
        instruments = json.load(f)

    matches = []
    for item in instruments:
        if item.get("segment") != "NSE_FO":
            continue
        if item.get("underlying_symbol") != "NIFTY":
            continue
        if item.get("instrument_type") != option_type:
            continue
        if float(item.get("strike_price", -1)) != wanted_strike:
            continue

        expiry_raw = item.get("expiry")
        expiry_date = datetime.fromtimestamp(expiry_raw / 1000, IST).date() if isinstance(expiry_raw, int) else None
        if expiry_date == wanted_expiry:
            matches.append(item)

    if not matches:
        raise RuntimeError(f"No Upstox instrument found for NIFTY {int(strike)} {option_type} {expiry_text}")

    return sorted(matches, key=lambda x: x.get("lot_size", 0))[0]

def place_gtt_order(instrument, entry_price, target_price, stop_loss_price):
    payload = {
        "type": "MULTIPLE",
        "quantity": int(instrument["lot_size"]),
        "product": "I",
        "instrument_token": instrument["instrument_key"],
        "transaction_type": "BUY",
        "rules": [
            {
                "strategy": "ENTRY",
                "trigger_type": "IMMEDIATE",
                "trigger_price": round_tick(entry_price),
            },
            {
                "strategy": "TARGET",
                "trigger_type": "IMMEDIATE",
                "trigger_price": round_tick(target_price),
            },
            {
                "strategy": "STOPLOSS",
                "trigger_type": "IMMEDIATE",
                "trigger_price": round_tick(stop_loss_price),
            },
        ],
    }
    return upstox_request("POST", UPSTOX_GTT_PLACE_URL, json=payload), payload

def market_window_ok():
    now = now_ist().time()
    return time(9, 30) <= now <= time(15, 15)

def run_squareoff():
    state = read_state()
    gtt_order_id = state.get("gtt_order_id")

    if gtt_order_id:
        try:
            log(f"Cancelling active GTT {gtt_order_id}")
            cancel_gtt(gtt_order_id)
        except Exception as e:
            log(f"GTT cancel failed: {e}")

    try:
        log("Exiting NSE_FO positions")
        result = exit_nse_fo_positions()
        log(f"Squareoff response: {result}")
    except Exception as e:
        log(f"Squareoff failed: {e}")

    clear_state()

def run_signal_check():
    if not market_window_ok():
        log("Outside trading window. No action.")
        return

    state = read_state()
    gtt_order_id = state.get("gtt_order_id")

    if gtt_order_id and is_gtt_still_active(gtt_order_id):
        log(
            "Existing GTT still active: "
            f"{gtt_order_id}. "
            f"symbol={state.get('trading_symbol')}, "
            f"direction={state.get('direction')}, "
            f"entry={state.get('entry_price')}, "
            f"target={state.get('target_price')}, "
            f"stop_loss={state.get('stop_loss_price')}. "
            "No new order."
        )
        return

    if gtt_order_id:
        log("Previous GTT no longer active. Clearing state.")
        clear_state()

    rec = get_nifty_recommendation()
    direction = rec["direction"]
    confidence = rec["confidence"]
    score = rec["score"]
    atm = rec["atm"]
    prices = rec["prices"]

    log(f"Signal: {direction}, confidence={confidence}, score={score}, strike={atm['strike']}, expiry={atm['expiry']}")

    if direction not in {"BULLISH", "BEARISH"}:
        log("No trade: neutral signal.")
        return

    if confidence != "HIGH" or abs(score) < 4:
        log("No trade: signal is not strong HIGH confidence.")
        return

    option_type = "CE" if direction == "BULLISH" else "PE"
    entry_price = prices.get("entry_price")
    target_price = prices.get("target_price")
    stop_loss_price = prices.get("stop_loss_price")

    if not entry_price or not target_price or not stop_loss_price:
        log("No trade: missing entry/target/stop-loss price.")
        return

    if target_price <= entry_price or stop_loss_price >= entry_price:
        log("No trade: invalid target/stop-loss relationship.")
        return

    instrument = find_nifty_option_instrument(atm["expiry"], atm["strike"], option_type)
    live = os.getenv("ENABLE_LIVE_TRADING", "false").lower() == "true"

    log(
        f"Prepared order: {instrument['trading_symbol']} qty={instrument['lot_size']} "
        f"entry={entry_price} target={target_price} sl={stop_loss_price} live={live}"
    )

    if not live:
        log("DRY RUN ONLY. Set ENABLE_LIVE_TRADING=true in .env to place real orders.")
        return

    result, payload = place_gtt_order(instrument, entry_price, target_price, stop_loss_price)
    gtt_ids = result.get("data", {}).get("gtt_order_ids", [])

    if not gtt_ids:
        raise RuntimeError(f"GTT placed but no ID returned: {result}")

    write_state({
        "date": now_ist().strftime("%Y-%m-%d"),
        "gtt_order_id": gtt_ids[0],
        "instrument_key": instrument["instrument_key"],
        "trading_symbol": instrument["trading_symbol"],
        "direction": direction,
        "confidence": confidence,
        "score": score,
        "entry_price": entry_price,
        "target_price": target_price,
        "stop_loss_price": stop_loss_price,
        "created_at": now_ist().isoformat(),
        "payload": payload,
    })

    log(
    f"LIVE GTT placed: {gtt_ids[0]} "
    f"symbol={instrument['trading_symbol']} "
    f"qty={instrument['lot_size']} "
    f"direction={direction} "
    f"entry={entry_price} "
    f"target={target_price} "
    f"stop_loss={stop_loss_price}"
)

def main():
    load_env()
    if "--squareoff" in sys.argv:
        run_squareoff()
    else:
        run_signal_check()

if __name__ == "__main__":
    main()