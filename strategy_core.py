import re
import asyncio
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright

IST = ZoneInfo("Asia/Kolkata")

GROWW_OPTION_URLS = {
    "NIFTY": "https://groww.in/options/nifty",
}

def now_ist():
    return datetime.now(IST)

def clean_num(x):
    if x is None:
        return None
    x = str(x).replace("₹", "").replace(",", "").strip()
    m = re.search(r"-?\d+(\.\d+)?", x)
    return float(m.group(0)) if m else None

def should_use_next_week_expiry():
    return now_ist().weekday() in [0, 1]

def parse_oi_with_change(text):
    if not text:
        return None, None, None, None
    s = str(text).replace(",", "").strip()
    m = re.search(r"(\d+)\s*([+-]\d+(\.\d+)?)%", s)
    if not m:
        m = re.search(r"(\d+)", s)
        current_oi = int(m.group(1)) if m else None
        return current_oi, None, None, None
    current_oi = int(m.group(1))
    change_pct = float(m.group(2))
    if change_pct == -100:
        return current_oi, None, None, change_pct
    previous_oi = round(current_oi / (1 + change_pct / 100))
    change_oi = current_oi - previous_oi
    return current_oi, previous_oi, change_oi, change_pct

def detect_expiry(all_text):
    matches = re.findall(r"\b\d{2}\s+[A-Z][a-z]{2}\b", all_text)
    if not matches:
        return None
    unique = []
    for expiry in matches:
        if expiry not in unique:
            unique.append(expiry)
    if should_use_next_week_expiry() and len(unique) >= 2:
        return unique[1]
    return unique[0]

def detect_spot(all_text, symbol):
    patterns = {
        "NIFTY": [r"NIFTY\s+50\s+([\d,]+\.\d+)", r"NIFTY\s+([\d,]+\.\d+)"],
    }
    for pattern in patterns.get(symbol, []):
        m = re.search(pattern, all_text, re.IGNORECASE)
        if m:
            return clean_num(m.group(1))
    return None

async def fetch_groww_options(symbol="NIFTY", nearby=5):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = await browser.new_page(viewport={"width": 1400, "height": 1000})
        await page.goto(GROWW_OPTION_URLS[symbol], wait_until="networkidle", timeout=90000)
        await page.wait_for_timeout(5000)

        all_text = await page.locator("body").inner_text()
        elements = await page.evaluate("""
        () => Array.from(document.querySelectorAll("body *")).map(el => {
            const r = el.getBoundingClientRect();
            const text = (el.innerText || "").trim();
            return { text, x: r.x, y: r.y, width: r.width, height: r.height };
        }).filter(e => e.text && e.width > 0 && e.height > 0 && e.text.length < 100)
        """)
        await browser.close()

    expiry = detect_expiry(all_text)
    spot = detect_spot(all_text, symbol)
    strike_re = re.compile(r"^\d{1,3}(,\d{3})*(\.\d+)?$")
    strike_elements = [e for e in elements if strike_re.match(e["text"])]
    rows = []

    for strike_el in strike_elements:
        strike = clean_num(strike_el["text"])
        y = strike_el["y"]
        strike_x = strike_el["x"]
        if strike is None:
            continue

        same_row = [e for e in elements if abs(e["y"] - y) <= 12]
        left = sorted([e for e in same_row if e["x"] < strike_x], key=lambda e: e["x"])
        right = sorted([e for e in same_row if e["x"] > strike_x], key=lambda e: e["x"])

        ce_prices = [e["text"] for e in left if "₹" in e["text"]]
        pe_prices = [e["text"] for e in right if "₹" in e["text"]]
        if not ce_prices and not pe_prices:
            continue

        ce_oi_texts = [e["text"] for e in left if "₹" not in e["text"] and "%" in e["text"]]
        pe_oi_texts = [e["text"] for e in right if "₹" not in e["text"] and "%" in e["text"]]

        ce_oi, ce_prev_oi, ce_change_oi, ce_change_pct = parse_oi_with_change(ce_oi_texts[-1] if ce_oi_texts else None)
        pe_oi, pe_prev_oi, pe_change_oi, pe_change_pct = parse_oi_with_change(pe_oi_texts[0] if pe_oi_texts else None)

        rows.append({
            "timestamp": now_ist().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "expiry": expiry,
            "spot": spot,
            "strike": strike,
            "CE_ltp": clean_num(ce_prices[-1]) if ce_prices else None,
            "CE_oi": ce_oi,
            "CE_previous_oi": ce_prev_oi,
            "CE_change_oi": ce_change_oi,
            "CE_change_oi_pct": ce_change_pct,
            "PE_ltp": clean_num(pe_prices[0]) if pe_prices else None,
            "PE_oi": pe_oi,
            "PE_previous_oi": pe_prev_oi,
            "PE_change_oi": pe_change_oi,
            "PE_change_oi_pct": pe_change_pct,
        })

    df_chain = pd.DataFrame(rows)
    if df_chain.empty:
        raise RuntimeError("No NIFTY option-chain rows found.")

    df_chain = df_chain.dropna(subset=["strike"]).drop_duplicates(subset=["strike"])
    df_chain = df_chain.sort_values("strike").reset_index(drop=True)
    df_chain["CE_ltp"] = pd.to_numeric(df_chain["CE_ltp"], errors="coerce")
    df_chain["PE_ltp"] = pd.to_numeric(df_chain["PE_ltp"], errors="coerce")

    if spot is not None:
        atm_idx = (df_chain["strike"] - spot).abs().idxmin()
    else:
        df_chain["premium_diff"] = (df_chain["CE_ltp"] - df_chain["PE_ltp"]).abs()
        atm_idx = df_chain["premium_diff"].idxmin()

    start = max(0, atm_idx - nearby)
    end = min(len(df_chain), atm_idx + nearby + 1)
    return df_chain.iloc[[atm_idx]].copy().reset_index(drop=True), df_chain.iloc[start:end].copy().reset_index(drop=True), df_chain

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

    support = None if supports.empty else supports.sort_values(["PE_oi", "strike"], ascending=[False, False]).iloc[0]["strike"]
    resistance = None if resistances.empty else resistances.sort_values(["CE_oi", "strike"], ascending=[False, True]).iloc[0]["strike"]

    if direction == "BULLISH":
        return {"target": resistance, "stop_loss": support, "support": support, "resistance": resistance}
    if direction == "BEARISH":
        return {"target": support, "stop_loss": resistance, "support": support, "resistance": resistance}
    return {"target": None, "stop_loss": None, "support": support, "resistance": resistance}

def expected_atm_option_prices(atm, levels, direction, confidence, signal_score):
    atm_strike = atm["strike"]
    ce_ltp = None if pd.isna(atm.get("CE_ltp", None)) else float(atm.get("CE_ltp"))
    pe_ltp = None if pd.isna(atm.get("PE_ltp", None)) else float(atm.get("PE_ltp"))

    ce_change_oi = 0 if pd.isna(atm.get("CE_change_oi", 0)) else float(atm.get("CE_change_oi", 0))
    pe_change_oi = 0 if pd.isna(atm.get("PE_change_oi", 0)) else float(atm.get("PE_change_oi", 0))
    ce_oi = 0 if pd.isna(atm.get("CE_oi", 0)) else float(atm.get("CE_oi", 0))
    pe_oi = 0 if pd.isna(atm.get("PE_oi", 0)) else float(atm.get("PE_oi", 0))

    target = levels.get("target")
    stop_loss = levels.get("stop_loss")

    if direction == "BULLISH":
        trade_side, entry_price, opposing_price = "ATM CALL", ce_ltp, pe_ltp
        if entry_price is None or target is None or stop_loss is None:
            return {"trade_side": trade_side, "entry_price": entry_price, "target_price": None, "stop_loss_price": None}
        index_target_points = max(target - atm_strike, 0)
        index_sl_points = max(atm_strike - stop_loss, 0)
        writing_strength = pe_change_oi - ce_change_oi
        oi_balance = pe_oi - ce_oi
    elif direction == "BEARISH":
        trade_side, entry_price, opposing_price = "ATM PUT", pe_ltp, ce_ltp
        if entry_price is None or target is None or stop_loss is None:
            return {"trade_side": trade_side, "entry_price": entry_price, "target_price": None, "stop_loss_price": None}
        index_target_points = max(atm_strike - target, 0)
        index_sl_points = max(stop_loss - atm_strike, 0)
        writing_strength = ce_change_oi - pe_change_oi
        oi_balance = ce_oi - pe_oi
    else:
        return {"trade_side": "NO TRADE", "entry_price": None, "target_price": None, "stop_loss_price": None}

    delta, target_multiplier, sl_multiplier = (0.58, 1.15, 0.75) if confidence == "HIGH" else (0.52, 1.0, 0.85)
    premium_ratio = entry_price / opposing_price if opposing_price and opposing_price > 0 else 1
    premium_factor = 1.08 if premium_ratio >= 1.10 else 0.92 if premium_ratio <= 0.90 else 1.00
    oi_factor = 1.00 + (0.08 if writing_strength > 0 else -0.08 if writing_strength < 0 else 0)
    oi_factor += 0.05 if oi_balance > 0 else -0.05 if oi_balance < 0 else 0
    oi_factor = min(max(oi_factor, 0.85), 1.15)

    target_move = min(max(index_target_points * delta * target_multiplier * premium_factor * oi_factor, entry_price * 0.18), entry_price * 1.20)
    sl_move = min(max(index_sl_points * delta * sl_multiplier, entry_price * 0.22), entry_price * 0.45)

    return {
        "trade_side": trade_side,
        "entry_price": round(entry_price, 2),
        "target_price": round(entry_price + target_move, 2),
        "stop_loss_price": round(max(entry_price - sl_move, 0), 2),
    }

def get_nifty_recommendation():
    df_atm, df_nearby, df_chain = asyncio.run(fetch_groww_options("NIFTY", nearby=5))
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