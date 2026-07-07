import os
import json
import socket
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import urllib3.util.connection as urllib3_cn


def allowed_gai_family():
    return socket.AF_INET


urllib3_cn.allowed_gai_family = allowed_gai_family

IST = ZoneInfo("Asia/Kolkata")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")

NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"
UPSTOX_OPTION_CONTRACT_URL = "https://api.upstox.com/v2/option/contract"
UPSTOX_OPTION_CHAIN_URL = "https://api.upstox.com/v2/option/chain"


def load_env_file():
    if not os.path.exists(ENV_FILE):
        return

    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file()


def now_ist():
    return datetime.now(IST)


def should_use_next_week_expiry():
    # Monday = 0, Tuesday = 1
    return now_ist().weekday() in [0, 1]


def choose_expiry(expiries):
    expiries = sorted([e for e in expiries if e])

    if not expiries:
        raise RuntimeError("No expiries available from Upstox")

    if should_use_next_week_expiry() and len(expiries) >= 2:
        return expiries[1]

    return expiries[0]


def upstox_market_token():
    token = os.getenv("UPSTOX_ANALYTICS_TOKEN") or os.getenv("UPSTOX_ACCESS_TOKEN")

    if not token:
        raise RuntimeError("No Upstox token found. Set UPSTOX_ANALYTICS_TOKEN or UPSTOX_ACCESS_TOKEN in .env")

    return token


def upstox_headers():
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {upstox_market_token()}",
    }


def upstox_get(url, params=None):
    response = requests.get(url, headers=upstox_headers(), params=params, timeout=30)

    if response.status_code >= 300:
        raise RuntimeError(f"Upstox API failed {response.status_code}: {response.text[:500]}")

    return response.json()


def get_nifty_expiries_from_upstox():
    payload = upstox_get(
        UPSTOX_OPTION_CONTRACT_URL,
        params={"instrument_key": NIFTY_INDEX_KEY},
    )

    data = payload.get("data", [])
    expiries = sorted({item.get("expiry") for item in data if item.get("expiry")})

    if not expiries:
        raise RuntimeError("No NIFTY expiries found from Upstox option contract API")

    return expiries


def fetch_upstox_nifty_option_chain(nearby=5):
    expiries = get_nifty_expiries_from_upstox()
    expiry = choose_expiry(expiries)

    payload = upstox_get(
        UPSTOX_OPTION_CHAIN_URL,
        params={
            "instrument_key": NIFTY_INDEX_KEY,
            "expiry_date": expiry,
        },
    )

    rows = []

    for item in payload.get("data", []):
        call = item.get("call_options", {}) or {}
        put = item.get("put_options", {}) or {}

        call_md = call.get("market_data", {}) or {}
        put_md = put.get("market_data", {}) or {}
        call_greeks = call.get("option_greeks", {}) or {}
        put_greeks = put.get("option_greeks", {}) or {}

        rows.append({
            "timestamp": now_ist().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": "NIFTY",
            "expiry": expiry,
            "spot": item.get("underlying_spot_price"),
            "strike": item.get("strike_price"),

            "CE_ltp": call_md.get("ltp"),
            "CE_oi": call_md.get("oi"),
            "CE_previous_oi": None,
            "CE_change_oi": call_md.get("oi") or 0,
            "CE_change_oi_pct": None,
            "CE_volume": call_md.get("volume"),
            "CE_iv": call_greeks.get("iv"),

            "PE_ltp": put_md.get("ltp"),
            "PE_oi": put_md.get("oi"),
            "PE_previous_oi": None,
            "PE_change_oi": put_md.get("oi") or 0,
            "PE_change_oi_pct": None,
            "PE_volume": put_md.get("volume"),
            "PE_iv": put_greeks.get("iv"),
        })

    df_chain = pd.DataFrame(rows)

    if df_chain.empty:
        raise RuntimeError("No Upstox NIFTY option-chain rows found")

    numeric_cols = [
        "spot", "strike",
        "CE_ltp", "CE_oi", "CE_change_oi", "CE_volume", "CE_iv",
        "PE_ltp", "PE_oi", "PE_change_oi", "PE_volume", "PE_iv",
    ]

    for col in numeric_cols:
        if col in df_chain.columns:
            df_chain[col] = pd.to_numeric(df_chain[col], errors="coerce")

    df_chain = df_chain.dropna(subset=["strike"])
    df_chain = df_chain.drop_duplicates(subset=["strike"])
    df_chain = df_chain.sort_values("strike").reset_index(drop=True)

    spot_values = df_chain["spot"].dropna()

    if not spot_values.empty:
        spot = spot_values.iloc[0]
        atm_idx = (df_chain["strike"] - spot).abs().idxmin()
    else:
        df_chain["premium_diff"] = (df_chain["CE_ltp"] - df_chain["PE_ltp"]).abs()
        atm_idx = df_chain["premium_diff"].idxmin()

    start = max(0, atm_idx - nearby)
    end = min(len(df_chain), atm_idx + nearby + 1)

    return (
        df_chain.iloc[[atm_idx]].copy().reset_index(drop=True),
        df_chain.iloc[start:end].copy().reset_index(drop=True),
        df_chain,
    )


def option_chain_signal(row):
    ce_change = 0 if pd.isna(row.get("CE_change_oi", 0)) else row.get("CE_change_oi", 0)
    pe_change = 0 if pd.isna(row.get("PE_change_oi", 0)) else row.get("PE_change_oi", 0)
    ce_oi = 0 if pd.isna(row.get("CE_oi", 0)) else row.get("CE_oi", 0)
    pe_oi = 0 if pd.isna(row.get("PE_oi", 0)) else row.get("PE_oi", 0)
    ce_ltp = 0 if pd.isna(row.get("CE_ltp", 0)) else row.get("CE_ltp", 0)
    pe_ltp = 0 if pd.isna(row.get("PE_ltp", 0)) else row.get("PE_ltp", 0)

    score = 0
    reasons = []

    if pe_change > ce_change:
        score += 2
        reasons.append("PE OI buildup is stronger than CE OI buildup")
    elif ce_change > pe_change:
        score -= 2
        reasons.append("CE OI buildup is stronger than PE OI buildup")

    if pe_oi > ce_oi:
        score += 1
        reasons.append("PE OI is higher than CE OI")
    elif ce_oi > pe_oi:
        score -= 1
        reasons.append("CE OI is higher than PE OI")

    if ce_ltp > pe_ltp:
        score += 1
        reasons.append("CE premium is higher than PE premium")
    elif pe_ltp > ce_ltp:
        score -= 1
        reasons.append("PE premium is higher than CE premium")

    direction = "BULLISH" if score >= 3 else "BEARISH" if score <= -3 else "NEUTRAL"
    confidence = "HIGH" if abs(score) >= 4 else "MEDIUM" if abs(score) >= 2 else "LOW"

    return direction, confidence, score, reasons


def option_chain_target_stoploss(df_nearby, atm_strike, direction):
    df = df_nearby.copy()

    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df["CE_oi"] = pd.to_numeric(df["CE_oi"], errors="coerce").fillna(0)
    df["PE_oi"] = pd.to_numeric(df["PE_oi"], errors="coerce").fillna(0)

    supports = df[df["strike"] < atm_strike].copy()
    resistances = df[df["strike"] > atm_strike].copy()

    support = None
    if not supports.empty:
        support = supports.sort_values(["PE_oi", "strike"], ascending=[False, False]).iloc[0]["strike"]

    resistance = None
    if not resistances.empty:
        resistance = resistances.sort_values(["CE_oi", "strike"], ascending=[False, True]).iloc[0]["strike"]

    if direction == "BULLISH":
        return {
            "target": resistance,
            "stop_loss": support,
            "support": support,
            "resistance": resistance,
        }

    if direction == "BEARISH":
        return {
            "target": support,
            "stop_loss": resistance,
            "support": support,
            "resistance": resistance,
        }

    return {
        "target": None,
        "stop_loss": None,
        "support": support,
        "resistance": resistance,
    }


def expected_atm_option_prices(atm, levels, direction, confidence, signal_score):
    ce_ltp = None if pd.isna(atm.get("CE_ltp", None)) else float(atm.get("CE_ltp"))
    pe_ltp = None if pd.isna(atm.get("PE_ltp", None)) else float(atm.get("PE_ltp"))

    if direction == "BULLISH":
        trade_side = "ATM CALL"
        entry_price = ce_ltp
    elif direction == "BEARISH":
        trade_side = "ATM PUT"
        entry_price = pe_ltp
    else:
        return {
            "trade_side": "NO TRADE",
            "entry_price": None,
            "target_price": None,
            "stop_loss_price": None,
        }

    if entry_price is None:
        return {
            "trade_side": trade_side,
            "entry_price": None,
            "target_price": None,
            "stop_loss_price": None,
        }

    target_price = entry_price * 1.20
    stop_loss_price = entry_price * 0.90

    return {
        "trade_side": trade_side,
        "entry_price": round(entry_price, 0),
        "target_price": round(target_price, 0),
        "stop_loss_price": round(max(stop_loss_price, 0), 0),
    }


def get_nifty_recommendation():
    df_atm, df_nearby, df_chain = fetch_upstox_nifty_option_chain(nearby=5)

    atm = df_atm.iloc[0]

    direction, confidence, score, reasons = option_chain_signal(atm)
    levels = option_chain_target_stoploss(df_nearby, atm["strike"], direction)
    prices = expected_atm_option_prices(atm, levels, direction, confidence, score)

    return {
        "direction": direction,
        "confidence": confidence,
        "score": score,
        "reasons": reasons,
        "atm": atm.to_dict(),
        "levels": levels,
        "prices": prices,
    }