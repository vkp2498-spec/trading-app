import os
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

UPSTOX_OPTION_CONTRACT_URL = "https://api.upstox.com/v2/option/contract"
UPSTOX_OPTION_CHAIN_URL = "https://api.upstox.com/v2/option/chain"

INDEX_CONFIG = {
    "NIFTY": {
        "instrument_key": "NSE_INDEX|Nifty 50",
        "expiry_offset": 1,
    },
    "BANKNIFTY": {
        "instrument_key": "NSE_INDEX|Nifty Bank",
        "expiry_offset": 0,
    },
}


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


def should_use_next_week_expiry(symbol):
    return int(INDEX_CONFIG[symbol].get("expiry_offset", 0)) == 1


def choose_expiry(symbol, expiries):
    expiries = sorted([e for e in expiries if e])

    if not expiries:
        raise RuntimeError(f"No expiries available for {symbol}")

    expiry_offset = int(INDEX_CONFIG[symbol].get("expiry_offset", 0))
    if len(expiries) <= expiry_offset:
        label = "next-week" if expiry_offset == 1 else "nearest"
        raise RuntimeError(
            f"No {label} expiry available for {symbol}; received {expiries}"
        )

    return expiries[expiry_offset]


def upstox_market_token():
    token = os.getenv("UPSTOX_ANALYTICS_TOKEN") or os.getenv("UPSTOX_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("No Upstox token found in .env")
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


def get_expiries_from_upstox(symbol):
    payload = upstox_get(
        UPSTOX_OPTION_CONTRACT_URL,
        params={"instrument_key": INDEX_CONFIG[symbol]["instrument_key"]},
    )

    data = payload.get("data", [])
    expiries = sorted({item.get("expiry") for item in data if item.get("expiry")})

    if not expiries:
        raise RuntimeError(f"No expiries found for {symbol}")

    return expiries


def fetch_upstox_option_chain(symbol, nearby=5):
    expiries = get_expiries_from_upstox(symbol)
    expiry = choose_expiry(symbol, expiries)

    payload = upstox_get(
        UPSTOX_OPTION_CHAIN_URL,
        params={
            "instrument_key": INDEX_CONFIG[symbol]["instrument_key"],
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

        call_oi = call_md.get("oi")
        call_previous_oi = call_md.get("prev_oi")
        put_oi = put_md.get("oi")
        put_previous_oi = put_md.get("prev_oi")

        call_change_oi = (
            float(call_oi) - float(call_previous_oi)
            if call_oi is not None and call_previous_oi is not None
            else 0
        )
        put_change_oi = (
            float(put_oi) - float(put_previous_oi)
            if put_oi is not None and put_previous_oi is not None
            else 0
        )

        rows.append({
            "timestamp": now_ist().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "expiry": expiry,
            "spot": item.get("underlying_spot_price"),
            "strike": item.get("strike_price"),

            "CE_ltp": call_md.get("ltp"),
            "CE_instrument_key": call.get("instrument_key"),
            "CE_bid_price": call_md.get("bid_price"),
            "CE_ask_price": call_md.get("ask_price"),
            "CE_bid_qty": call_md.get("bid_qty"),
            "CE_ask_qty": call_md.get("ask_qty"),
            "CE_oi": call_oi,
            "CE_previous_oi": call_previous_oi,
            "CE_change_oi": call_change_oi,
            "CE_change_oi_pct": (
                round(call_change_oi / float(call_previous_oi) * 100, 2)
                if call_previous_oi
                else None
            ),
            "CE_volume": call_md.get("volume"),
            "CE_iv": call_greeks.get("iv"),
            "CE_delta": call_greeks.get("delta"),
            "CE_gamma": call_greeks.get("gamma"),
            "CE_theta": call_greeks.get("theta"),
            "CE_vega": call_greeks.get("vega"),
            "CE_pop": call_greeks.get("pop"),

            "PE_ltp": put_md.get("ltp"),
            "PE_instrument_key": put.get("instrument_key"),
            "PE_bid_price": put_md.get("bid_price"),
            "PE_ask_price": put_md.get("ask_price"),
            "PE_bid_qty": put_md.get("bid_qty"),
            "PE_ask_qty": put_md.get("ask_qty"),
            "PE_oi": put_oi,
            "PE_previous_oi": put_previous_oi,
            "PE_change_oi": put_change_oi,
            "PE_change_oi_pct": (
                round(put_change_oi / float(put_previous_oi) * 100, 2)
                if put_previous_oi
                else None
            ),
            "PE_volume": put_md.get("volume"),
            "PE_iv": put_greeks.get("iv"),
            "PE_delta": put_greeks.get("delta"),
            "PE_gamma": put_greeks.get("gamma"),
            "PE_theta": put_greeks.get("theta"),
            "PE_vega": put_greeks.get("vega"),
            "PE_pop": put_greeks.get("pop"),
        })

    df_chain = pd.DataFrame(rows)

    if df_chain.empty:
        raise RuntimeError(f"No Upstox option-chain rows found for {symbol}")

    numeric_cols = [
        "spot", "strike",
        "CE_ltp", "CE_oi", "CE_previous_oi", "CE_change_oi",
        "CE_change_oi_pct", "CE_volume", "CE_iv", "CE_delta", "CE_gamma",
        "CE_theta", "CE_vega", "CE_pop", "CE_bid_price", "CE_ask_price",
        "CE_bid_qty", "CE_ask_qty",
        "PE_ltp", "PE_oi", "PE_previous_oi", "PE_change_oi",
        "PE_change_oi_pct", "PE_volume", "PE_iv", "PE_delta", "PE_gamma",
        "PE_theta", "PE_vega", "PE_pop", "PE_bid_price", "PE_ask_price",
        "PE_bid_qty", "PE_ask_qty",
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
        return {"target": resistance, "stop_loss": support, "support": support, "resistance": resistance}

    if direction == "BEARISH":
        return {"target": support, "stop_loss": resistance, "support": support, "resistance": resistance}

    return {"target": None, "stop_loss": None, "support": support, "resistance": resistance}


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
        return {"trade_side": "NO TRADE", "entry_price": None, "target_price": None, "stop_loss_price": None}

    if entry_price is None:
        return {"trade_side": trade_side, "entry_price": None, "target_price": None, "stop_loss_price": None}

    target_percent = float(os.getenv("NORMAL_TARGET_PERCENT", "10"))
    stop_percent = float(os.getenv("NORMAL_STOP_PERCENT", "7.5"))
    if not 0 < target_percent < 100 or not 0 < stop_percent < 100:
        raise RuntimeError("NORMAL_TARGET_PERCENT and NORMAL_STOP_PERCENT must be between 0 and 100")

    return {
        "trade_side": trade_side,
        "entry_price": round(entry_price, 0),
        "target_price": round(entry_price * (1 + target_percent / 100), 0),
        "stop_loss_price": round(max(entry_price * (1 - stop_percent / 100), 0), 0),
    }


def option_contract_quality(atm, option_type, stream_quote=None):
    """Normalize spread, depth and Greeks for the selected ATM contract."""
    prefix = "CE" if str(option_type).upper() == "CE" else "PE"
    stream_quote = stream_quote or {}

    def value(name, default=None):
        stream_value = stream_quote.get(name)
        return stream_value if stream_value is not None else atm.get(f"{prefix}_{name}", default)

    def number(name, default=None):
        try:
            raw = value(name, default)
            return default if raw is None else float(raw)
        except (TypeError, ValueError):
            return default

    ltp = number("ltp")
    bid = number("bid_price")
    ask = number("ask_price")
    bid_qty = number("bid_qty", 0) or 0
    ask_qty = number("ask_qty", 0) or 0
    spread = round(ask - bid, 4) if bid is not None and ask is not None and ask >= bid else None
    spread_percent = round(spread / ltp * 100, 3) if spread is not None and ltp and ltp > 0 else None
    total_depth = bid_qty + ask_qty
    depth_ratio = round((bid_qty - ask_qty) / total_depth, 4) if total_depth > 0 else None
    depth_bias = (
        "BULLISH" if depth_ratio is not None and depth_ratio >= 0.20
        else "BEARISH" if depth_ratio is not None and depth_ratio <= -0.20
        else "NEUTRAL"
    )

    greeks = stream_quote.get("option_greeks") or {}
    delta = greeks.get("delta")
    if delta is None:
        delta = atm.get(f"{prefix}_delta")
    iv = greeks.get("iv") if greeks.get("iv") is not None else atm.get(f"{prefix}_iv")
    pop = greeks.get("pop") if greeks.get("pop") is not None else atm.get(f"{prefix}_pop")
    try:
        delta = float(delta) if delta is not None else None
    except (TypeError, ValueError):
        delta = None

    reasons = []
    if spread_percent is not None:
        reasons.append(f"spread={spread_percent:.3f}%")
    if depth_ratio is not None:
        reasons.append(f"depth={depth_bias} ratio={depth_ratio:.3f}")
    if delta is not None:
        reasons.append(f"delta={delta:.3f}")

    return {
        "option_type": prefix,
        "ltp": ltp,
        "bid_price": bid,
        "ask_price": ask,
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "spread": spread,
        "spread_percent": spread_percent,
        "depth_ratio": depth_ratio,
        "depth_bias": depth_bias,
        "delta": delta,
        "iv": iv,
        "pop": pop,
        "stream_used": bool(stream_quote),
        "reasons": reasons,
    }


def get_index_recommendation(symbol):
    df_atm, df_nearby, df_chain = fetch_upstox_option_chain(symbol, nearby=5)

    atm = df_atm.iloc[0]

    direction, confidence, score, reasons = option_chain_signal(atm)
    levels = option_chain_target_stoploss(df_nearby, atm["strike"], direction)
    prices = expected_atm_option_prices(atm, levels, direction, confidence, score)
    total_ce_oi = float(df_chain["CE_oi"].fillna(0).sum())
    total_pe_oi = float(df_chain["PE_oi"].fillna(0).sum())
    total_ce_volume = float(df_chain["CE_volume"].fillna(0).sum())
    total_pe_volume = float(df_chain["PE_volume"].fillna(0).sum())

    nearby_flow = {
        "ce_oi": float(df_nearby["CE_oi"].fillna(0).sum()),
        "pe_oi": float(df_nearby["PE_oi"].fillna(0).sum()),
        "ce_change_oi": float(df_nearby["CE_change_oi"].fillna(0).sum()),
        "pe_change_oi": float(df_nearby["PE_change_oi"].fillna(0).sum()),
        "ce_volume": float(df_nearby["CE_volume"].fillna(0).sum()),
        "pe_volume": float(df_nearby["PE_volume"].fillna(0).sum()),
    }

    chain_totals = {
        "total_ce_oi": total_ce_oi,
        "total_pe_oi": total_pe_oi,
        "pcr_oi": round(total_pe_oi / total_ce_oi, 4) if total_ce_oi else None,
        "total_ce_volume": total_ce_volume,
        "total_pe_volume": total_pe_volume,
        "pcr_volume": round(total_pe_volume / total_ce_volume, 4) if total_ce_volume else None,
    }

    return {
        "symbol": symbol,
        "direction": direction,
        "confidence": confidence,
        "score": score,
        "reasons": reasons,
        "atm": atm.to_dict(),
        "nearby_contracts": df_nearby.to_dict("records"),
        "levels": levels,
        "prices": prices,
        "chain_totals": chain_totals,
        "nearby_flow": nearby_flow,
    }


def get_nifty_recommendation():
    return get_index_recommendation("NIFTY")


def get_banknifty_recommendation():
    return get_index_recommendation("BANKNIFTY")
