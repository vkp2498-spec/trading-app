import os
import re
from PIL import Image
import asyncio
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from datetime import datetime
from playwright.async_api import async_playwright
from zoneinfo import ZoneInfo
import os
import requests

import socket
import urllib3.util.connection as urllib3_cn


def allowed_gai_family():
    return socket.AF_INET


urllib3_cn.allowed_gai_family = allowed_gai_family

def load_env_file(path=".env"):
    if not os.path.exists(path):
        return

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

load_env_file()

NSE_HOME_URL = "https://www.nseindia.com"
NSE_LIVE_ANALYSIS_URL = "https://www.nseindia.com/market-data/live-analysis"
NSE_GAINERS_URL = "https://www.nseindia.com/api/live-analysis-variations?index=gainers&type=FOSec"
NSE_LOSERS_URL = "https://www.nseindia.com/api/live-analysis-variations?index=loosers&type=FOSec"

GROWW_OPTION_URLS = {
    "NIFTY": "https://groww.in/options/nifty",
    "BANKNIFTY": "https://groww.in/options/nifty-bank",
    "ASHOK LEYLAND": "https://groww.in/options/ashok-leyland-ltd",
}

INSTRUMENT_CARDS = {
    "NIFTY": {
        "icon": "📈",
        "label": "NIFTY",
    },
    "BANKNIFTY": {
        "icon": "🏦",
        "label": "BANKNIFTY",
    },
    "ASHOK LEYLAND": {
        "icon": "🚚",
        "label": "ASHOK LEYLAND",
    },
    "TOP GAINER & TOP LOSER": {
        "icon": "⚡",
        "label": "TOP GAINER / LOSER",
    },
}

GROWW_SLUG_MAP = {
    "ADANIENT": "adani-enterprises-ltd",
    "ADANIPORTS": "adani-ports-and-special-economic-zone-ltd",
    "APOLLOHOSP": "apollo-hospitals-enterprise-ltd",
    "ASIANPAINT": "asian-paints-ltd",
    "AXISBANK": "axis-bank-ltd",
    "BAJAJ-AUTO": "bajaj-auto-ltd",
    "BAJFINANCE": "bajaj-finance-ltd",
    "BAJAJFINSV": "bajaj-finserv-ltd",
    "BEL": "bharat-electronics-ltd",
    "BHARTIARTL": "bharti-airtel-ltd",
    "BPCL": "bharat-petroleum-corporation-ltd",
    "BRITANNIA": "britannia-industries-ltd",
    "CIPLA": "cipla-ltd",
    "COALINDIA": "coal-india-ltd",
    "DRREDDY": "dr-reddys-laboratories-ltd",
    "EICHERMOT": "eicher-motors-ltd",
    "GRASIM": "grasim-industries-ltd",
    "HCLTECH": "hcl-technologies-ltd",
    "HDFCBANK": "hdfc-bank-ltd",
    "HDFCLIFE": "hdfc-life-insurance-company-ltd",
    "HEROMOTOCO": "hero-motocorp-ltd",
    "HINDALCO": "hindalco-industries-ltd",
    "HINDUNILVR": "hindustan-unilever-ltd",
    "ICICIBANK": "icici-bank-ltd",
    "INDIGO": "interglobe-aviation-ltd",
    "INFY": "infosys-ltd",
    "ITC": "itc-ltd",
    "JIOFIN": "jio-financial-services-ltd",
    "JSWSTEEL": "jsw-steel-ltd",
    "KOTAKBANK": "kotak-mahindra-bank-ltd",
    "LT": "larsen-toubro-ltd",
    "M&M": "mahindra-and-mahindra-ltd",
    "MARUTI": "maruti-suzuki-india-ltd",
    "MAXHEALTH": "max-healthcare-institute-ltd",
    "NESTLEIND": "nestle-india-ltd",
    "NTPC": "ntpc-ltd",
    "ONGC": "oil-and-natural-gas-corporation-ltd",
    "POWERGRID": "power-grid-corporation-of-india-ltd",
    "RELIANCE": "reliance-industries-ltd",
    "SBILIFE": "sbi-life-insurance-company-ltd",
    "SBIN": "state-bank-of-india",
    "SHRIRAMFIN": "shriram-finance-ltd",
    "SUNPHARMA": "sun-pharmaceutical-industries-ltd",
    "TATACONSUM": "tata-consumer-products-ltd",
    "TATAMOTORS": "tata-motors-ltd",
    "TMPV": "tata-motors-ltd",
    "TATASTEEL": "tata-steel-ltd",
    "TCS": "tata-consultancy-services-ltd",
    "TECHM": "tech-mahindra-ltd",
    "TITAN": "titan-company-ltd",
    "TRENT": "trent-ltd",
    "ULTRACEMCO": "ultratech-cement-ltd",
    "WIPRO": "wipro-ltd",

    # Extra F&O names you already use / may see often
    "ASHOKLEY": "ashok-leyland-ltd",
}


st.set_page_config(
    page_title="Option Strategy",
    page_icon="static/favicon.png",
    layout="wide"
)

# APP_USERNAME = "vkp"
# APP_PASSWORD = "krish999"

# def require_login():
#     if "logged_in" not in st.session_state:
#         st.session_state.logged_in = False

#     if st.session_state.logged_in:
#         return

#     st.markdown("""
#     <style>
#     .login-box {
#         max-width: 360px;
#         margin: 16vh auto 0 auto;
#         padding: 1.4rem;
#         border-radius: 14px;
#         background: rgba(15, 23, 42, 0.88);
#         border: 1px solid rgba(148, 163, 184, 0.28);
#     }
#     </style>
#     """, unsafe_allow_html=True)

#     st.markdown('<div class="login-box">', unsafe_allow_html=True)
#     st.title("NIFTY Option Strategy")
#     username = st.text_input("Username")
#     password = st.text_input("Password", type="password")
#     login = st.button("Login", use_container_width=True)
#     st.markdown("</div>", unsafe_allow_html=True)

#     if login:
#         if username == APP_USERNAME and password == APP_PASSWORD:
#             st.session_state.logged_in = True
#             st.rerun()
#         else:
#             st.error("Invalid username or password")

#     st.stop()

# require_login()

components.html(
    """
    <script>
    const head = window.parent.document.head;

    function addLink(rel, href) {
        let link = window.parent.document.createElement("link");
        link.rel = rel;
        link.href = href;
        head.appendChild(link);
    }

    addLink("apple-touch-icon", "/app/static/apple-touch-icon.png");
    addLink("manifest", "/app/static/manifest.webmanifest");

    let meta = window.parent.document.createElement("meta");
    meta.name = "theme-color";
    meta.content = "#050816";
    head.appendChild(meta);
    </script>
    """,
    height=0,
)

st.markdown("""
<style>
#MainMenu, footer, header {visibility: hidden;}

html, body, [data-testid="stAppViewContainer"], .stApp {
    background:
        radial-gradient(circle at top left, rgba(20, 184, 166, 0.22), transparent 34%),
        linear-gradient(135deg, #07111f 0%, #0b1220 48%, #101827 100%) !important;
    color: #f8fafc !important;
}

[data-testid="stHeader"] {
    background: transparent !important;
}

.block-container {
    max-width: 1180px !important;
    padding-top: 1.1rem !important;
    padding-bottom: 2rem !important;
}

.main-title {
    color: #f8fafc;
    font-size: 2.4rem;
    font-weight: 900;
    margin-top: 1rem;
    letter-spacing: 0;
}

.sub-title {
    color: #cbd5e1;
    font-size: 0.95rem;
    font-weight: 700;
    margin-bottom: 1rem;
}

.stButton > button {
    background: linear-gradient(135deg, #15c8b7, #22d3a6) !important;
    color: #06121f !important;
    border: 1px solid rgba(255,255,255,0.18) !important;
    border-radius: 12px !important;
    font-weight: 800 !important;
    min-height: 56px !important;
    box-shadow: 0 10px 24px rgba(0,0,0,0.22) !important;
    white-space: pre-line !important;
}

.stButton > button:hover {
    background: linear-gradient(135deg, #2ee6d0, #37efb8) !important;
    color: #020617 !important;
    transform: translateY(-1px);
}

div[data-testid="column"] .stButton > button {
    min-height: 78px !important;
    font-size: 0.92rem !important;
    line-height: 1.2 !important;
}

.strategy-card {
    background: rgba(15, 23, 42, 0.84) !important;
    border: 1px solid rgba(148, 163, 184, 0.28) !important;
    border-radius: 12px !important;
    padding: 1.25rem 1.35rem !important;
    margin-top: 1.05rem !important;
    box-shadow: 0 18px 45px rgba(0,0,0,0.22);
}

.card-kicker {
    color: #aeb9ca !important;
    font-size: 0.82rem;
    font-weight: 800;
    margin-bottom: 0.75rem;
}

.signal-text {
    font-size: 1.65rem;
    font-weight: 900;
    line-height: 1.2;
}

.levels-grid {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 1rem;
}

.level-label {
    color: #94a3b8;
    font-size: 0.82rem;
    font-weight: 700;
    margin-bottom: 0.35rem;
}

.level-value {
    color: #f8fafc;
    font-size: 1.35rem;
    font-weight: 900;
}

.green { color: #22c55e !important; }
.red { color: #ef4444 !important; }
.amber { color: #f59e0b !important; }
.muted { color: #94a3b8 !important; }

@media (max-width: 760px) {
    .block-container {
        padding-left: 0.85rem !important;
        padding-right: 0.85rem !important;
        padding-top: 0.65rem !important;
    }

    .main-title {
        font-size: 1.8rem;
        margin-top: 0.7rem;
    }

    .sub-title {
        font-size: 0.84rem;
    }

    div[data-testid="column"] .stButton > button {
        min-height: 64px !important;
        font-size: 0.72rem !important;
        padding: 0.25rem 0.1rem !important;
    }

    .strategy-card {
        padding: 1rem !important;
        border-radius: 10px !important;
    }

    .signal-text {
        font-size: 1.22rem;
    }

    .levels-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 0.85rem;
    }

    .level-value {
        font-size: 1.18rem;
    }
}
</style>
""", unsafe_allow_html=True)

IST = ZoneInfo("Asia/Kolkata")

def now_ist():
    return datetime.now(IST)


def clean_num(x):
    if x is None:
        return None
    x = str(x).replace("₹", "").replace(",", "").strip()
    m = re.search(r"-?\d+(\.\d+)?", x)
    return float(m.group(0)) if m else None


def should_use_next_week_expiry(symbol):
    return str(symbol or "").upper() == "NIFTY"


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
        previous_oi = None
        change_oi = None
    else:
        previous_oi = round(current_oi / (1 + change_pct / 100))
        change_oi = current_oi - previous_oi

    return current_oi, previous_oi, change_oi, change_pct


def detect_expiry(all_text, symbol):
    matches = re.findall(r"\b\d{2}\s+[A-Z][a-z]{2}\b", all_text)

    if not matches:
        return None

    unique_expiries = []
    for expiry in matches:
        if expiry not in unique_expiries:
            unique_expiries.append(expiry)

    if should_use_next_week_expiry(symbol):
        return unique_expiries[1] if len(unique_expiries) >= 2 else None

    return unique_expiries[0]


def detect_spot(all_text, symbol):
    patterns = {
        "NIFTY": [r"NIFTY\s+50\s+([\d,]+\.\d+)", r"NIFTY\s+([\d,]+\.\d+)"],
        "BANKNIFTY": [r"BANKNIFTY\s+([\d,]+\.\d+)", r"BANK\s+NIFTY\s+([\d,]+\.\d+)"],
        "ASHOK LEYLAND": [r"ASHOK\s+LEYLAND\s+([\d,]+\.\d+)"],
    }

    for pattern in patterns.get(symbol, []):
        m = re.search(pattern, all_text, re.IGNORECASE)
        if m:
            return clean_num(m.group(1))

    return None


def nse_symbol_to_groww_url(symbol):
    symbol = symbol.upper().strip()
    slug = GROWW_SLUG_MAP.get(symbol)

    if not slug:
        raise RuntimeError(
            f"NSE top mover symbol '{symbol}' is not in GROWW_SLUG_MAP. "
            f"Add its Groww option slug manually."
        )

    return f"https://groww.in/options/{slug}"


def extract_top_symbol_from_nse_payload(payload, mover_type):
    rows = []

    def walk(obj):
        if isinstance(obj, dict):
            if "symbol" in obj:
                rows.append(obj)

            for value in obj.values():
                walk(value)

        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(payload)

    cleaned = []

    for row in rows:
        symbol = str(row.get("symbol", "")).strip().upper()

        if not symbol:
            continue

        pct = (
            row.get("pChange")
            or row.get("perChange")
            or row.get("percentChange")
            or row.get("%Change")
            or row.get("change")
        )

        try:
            pct = float(str(pct).replace("%", "").replace(",", "").strip())
        except Exception:
            pct = None

        if pct is None:
            continue

        cleaned.append({
            "symbol": symbol,
            "pct": pct,
            "row": row,
        })

    if not cleaned:
        raise RuntimeError("Could not find valid NSE mover rows with symbol and percentage change.")

    if mover_type == "GAINER":
        cleaned = sorted(cleaned, key=lambda x: x["pct"], reverse=True)
    else:
        cleaned = sorted(cleaned, key=lambda x: x["pct"])

    for item in cleaned:
        if item["symbol"] in GROWW_SLUG_MAP:
            return item["symbol"]

    top_symbol = cleaned[0]["symbol"]
    raise RuntimeError(
        f"Top {mover_type.lower()} found as '{top_symbol}', but it is not mapped to Groww. "
        f"Add it to GROWW_SLUG_MAP."
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

    if score >= 3:
        direction = "BULLISH"
    elif score <= -3:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"

    abs_score = abs(score)

    if abs_score >= 4:
        confidence = "HIGH"
    elif abs_score >= 2:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

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
        target = resistance
        stop_loss = support
        strategy = "Bullish option writing bias: PE writers are defending support."
    elif direction == "BEARISH":
        target = support
        stop_loss = resistance
        strategy = "Bearish option writing bias: CE writers are defending resistance."
    else:
        target = None
        stop_loss = None
        strategy = "Neutral option writing bias: no clean directional target."

    return {
        "target": target,
        "stop_loss": stop_loss,
        "support": support,
        "resistance": resistance,
        "strategy": strategy,
    }


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
        trade_side = "ATM CALL"
        entry_price = ce_ltp
        opposing_price = pe_ltp

        if entry_price is None or target is None or stop_loss is None:
            return {"trade_side": trade_side, "entry_price": entry_price, "target_price": None, "stop_loss_price": None, "model_note": "Insufficient data"}

        index_target_points = max(target - atm_strike, 0)
        index_sl_points = max(atm_strike - stop_loss, 0)
        writing_strength = pe_change_oi - ce_change_oi
        oi_balance = pe_oi - ce_oi

    elif direction == "BEARISH":
        trade_side = "ATM PUT"
        entry_price = pe_ltp
        opposing_price = ce_ltp

        if entry_price is None or target is None or stop_loss is None:
            return {"trade_side": trade_side, "entry_price": entry_price, "target_price": None, "stop_loss_price": None, "model_note": "Insufficient data"}

        index_target_points = max(atm_strike - target, 0)
        index_sl_points = max(stop_loss - atm_strike, 0)
        writing_strength = ce_change_oi - pe_change_oi
        oi_balance = ce_oi - pe_oi

    else:
        return {"trade_side": "NO TRADE", "entry_price": None, "target_price": None, "stop_loss_price": None, "model_note": "Neutral signal"}

    if confidence == "HIGH":
        delta = 0.58
        target_multiplier = 1.15
        sl_multiplier = 0.75
    elif confidence == "MEDIUM":
        delta = 0.52
        target_multiplier = 1.00
        sl_multiplier = 0.85
    else:
        delta = 0.45
        target_multiplier = 0.80
        sl_multiplier = 0.95

    premium_ratio = entry_price / opposing_price if opposing_price and opposing_price > 0 else 1

    if premium_ratio >= 1.10:
        premium_factor = 1.08
    elif premium_ratio <= 0.90:
        premium_factor = 0.92
    else:
        premium_factor = 1.00

    oi_factor = 1.00
    oi_factor += 0.08 if writing_strength > 0 else -0.08 if writing_strength < 0 else 0
    oi_factor += 0.05 if oi_balance > 0 else -0.05 if oi_balance < 0 else 0
    oi_factor = min(max(oi_factor, 0.85), 1.15)

    target_premium_move = index_target_points * delta * target_multiplier * premium_factor * oi_factor
    sl_premium_move = index_sl_points * delta * sl_multiplier

    target_premium_move = min(max(target_premium_move, entry_price * 0.18), entry_price * 1.20)
    sl_premium_move = min(max(sl_premium_move, entry_price * 0.22), entry_price * 0.45)

    target_price = entry_price + 20
    stop_loss_price = max(entry_price - 10, 0)

    target_price = entry_price * 1.20
    stop_loss_price = entry_price * 0.90

    return {
        "trade_side": trade_side,
        "entry_price": round(entry_price, 0),
        "target_price": round(target_price, 0),
        "stop_loss_price": round(max(stop_loss_price, 0), 0),
        "model_note": "Fixed intraday rule: target is current option price +20%, stop loss is current option price -10%",
    }


async def fetch_groww_options(symbol, nearby=5, option_url=None):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
        )

        page = await browser.new_page(viewport={"width": 1400, "height": 1000})
        url = option_url or GROWW_OPTION_URLS[symbol]

        await page.goto(url, wait_until="networkidle", timeout=90000)
        await page.wait_for_timeout(5000)

        all_text = await page.locator("body").inner_text()

        elements = await page.evaluate("""
        () => {
            const nodes = Array.from(document.querySelectorAll("body *"));
            return nodes.map(el => {
                const r = el.getBoundingClientRect();
                const text = (el.innerText || "").trim();
                return { text, x: r.x, y: r.y, width: r.width, height: r.height };
            }).filter(e => e.text && e.width > 0 && e.height > 0 && e.text.length < 100);
        }
        """)

        await browser.close()

    expiry = detect_expiry(all_text, symbol)
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

        ce_oi, ce_prev_oi, ce_change_oi, ce_change_pct = parse_oi_with_change(
            ce_oi_texts[-1] if ce_oi_texts else None
        )

        pe_oi, pe_prev_oi, pe_change_oi, pe_change_pct = parse_oi_with_change(
            pe_oi_texts[0] if pe_oi_texts else None
        )

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
            "CE_volume": None,
            "CE_iv": None,
            "PE_ltp": clean_num(pe_prices[0]) if pe_prices else None,
            "PE_oi": pe_oi,
            "PE_previous_oi": pe_prev_oi,
            "PE_change_oi": pe_change_oi,
            "PE_change_oi_pct": pe_change_pct,
            "PE_volume": None,
            "PE_iv": None,
        })

    df_chain = pd.DataFrame(rows)

    if df_chain.empty:
        raise RuntimeError(f"No option-chain rows found for {symbol}. Open Groww once in Chrome and try again.")

    df_chain = df_chain.dropna(subset=["strike"])
    df_chain = df_chain.drop_duplicates(subset=["strike"])
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

    return (
        df_chain.iloc[[atm_idx]].copy().reset_index(drop=True),
        df_chain.iloc[start:end].copy().reset_index(drop=True),
        df_chain,
    )


async def fetch_nse_top_gainer_loser_symbols():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-http2",
            "--ignore-certificate-errors",
            "--disable-features=NetworkService,NetworkServiceInProcess",
        ],
    )

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )

        page = await context.new_page()
        try:
            await page.goto(NSE_LIVE_ANALYSIS_URL, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)
        except Exception:
            pass

        headers = {
            "Accept": "application/json,text/plain,*/*",
            "Referer": NSE_LIVE_ANALYSIS_URL,
        }

        gainer_response = await context.request.get(NSE_GAINERS_URL, headers=headers, timeout=60000)
        loser_response = await context.request.get(NSE_LOSERS_URL, headers=headers, timeout=60000)

        if gainer_response.status != 200:
            body = (await gainer_response.text())[:500]
            raise RuntimeError(f"NSE gainer API failed. Status={gainer_response.status}, Body={body}")

        if loser_response.status != 200:
            body = (await loser_response.text())[:500]
            raise RuntimeError(f"NSE loser API failed. Status={loser_response.status}, Body={body}")

        gainer_payload = await gainer_response.json()
        loser_payload = await loser_response.json()

        await browser.close()

    return {
    "TOP GAINER": extract_top_symbol_from_nse_payload(gainer_payload, "GAINER"),
    "TOP LOSER": extract_top_symbol_from_nse_payload(loser_payload, "LOSER"),
}


async def fetch_top_gainer_loser_data():
    movers = await fetch_nse_top_gainer_loser_symbols()
    output = {}

    for label, symbol in movers.items():
        option_url = nse_symbol_to_groww_url(symbol)

        df_atm, df_nearby, df_chain = await fetch_groww_options(
            symbol=symbol,
            nearby=5,
            option_url=option_url,
        )

        output[label] = {
            "symbol": symbol,
            "url": option_url,
            "df_atm": df_atm,
            "df_nearby": df_nearby,
            "df_chain": df_chain,
        }

    return output


def load_data(symbol):
    return asyncio.run(fetch_groww_options(symbol=symbol, nearby=5))


def load_top_gainer_loser_data():
    return asyncio.run(fetch_top_gainer_loser_data())

# def money_text(value):
#     if value is None:
#         return "N/A"
#     return f"₹{float(value):,.2f}"


# def get_upstox_account_summary():
#     access_token = (
#     os.getenv("UPSTOX_ANALYTICS_TOKEN")
#     or os.getenv("UPSTOX_ACCESS_TOKEN")
#     )

#     if not access_token:
#         return {
#             "available_funds": None,
#             "net_pnl": None,
#             "status": "UPSTOX_ACCESS_TOKEN not set",
#         }

#     headers_v3 = {
#         "Accept": "application/json",
#         "Api-Version": "3.0",
#         "Authorization": f"Bearer {access_token}",
#     }

#     headers_v2 = {
#         "Accept": "application/json",
#         "Content-Type": "application/json",
#         "Authorization": f"Bearer {access_token}",
#     }

#     funds_url = "https://api.upstox.com/v3/user/get-funds-and-margin"
#     positions_url = "https://api.upstox.com/v2/portfolio/short-term-positions"

#     funds_response = requests.get(funds_url, headers=headers_v3, timeout=20)
#     positions_response = requests.get(positions_url, headers=headers_v2, timeout=20)

#     if funds_response.status_code != 200:
#         return {
#             "available_funds": None,
#             "net_pnl": None,
#             "status": f"Funds API failed: {funds_response.status_code} {funds_response.text[:500]}",
#         }

#     if positions_response.status_code != 200:
#         return {
#             "available_funds": None,
#             "net_pnl": None,
#             "status": f"Positions API failed: {positions_response.status_code} {positions_response.text[:500]}",
#         }

#     funds_json = funds_response.json()
#     positions_json = positions_response.json()

#     available_funds = (
#         funds_json
#         .get("data", {})
#         .get("available_to_trade", {})
#         .get("total")
#     )

#     positions = positions_json.get("data", [])
#     net_pnl = sum(float(pos.get("pnl") or 0) for pos in positions)

#     return {
#         "available_funds": available_funds,
#         "net_pnl": net_pnl,
#         "status": "Updated",
#     }


# def render_account_summary(account_summary):
#     available_funds = account_summary.get("available_funds")
#     net_pnl = account_summary.get("net_pnl")
#     status = account_summary.get("status", "")

#     pnl_class = "green" if net_pnl is not None and net_pnl >= 0 else "red"

#     st.markdown(
#         f"""
#         <div class="strategy-card" style="margin-top:0.8rem;">
#             <div class="card-kicker">UPSTOX ACCOUNT</div>
#             <div class="levels-grid" style="grid-template-columns:repeat(3,minmax(0,1fr));">
#                 <div>
#                     <div class="level-label">Available Funds</div>
#                     <div class="level-value">{money_text(available_funds)}</div>
#                 </div>
#                 <div>
#                     <div class="level-label">Net P&L</div>
#                     <div class="level-value {pnl_class}">{money_text(net_pnl)}</div>
#                 </div>
#                 <div>
#                     <div class="level-label">Status</div>
#                     <div class="level-value" style="font-size:1rem;">{status}</div>
#                 </div>
#             </div>
#         </div>
#         """,
#         unsafe_allow_html=True,
#     )


def render_strategy(symbol, df_atm, df_nearby, df_chain, last_refresh):
    atm = df_atm.iloc[0]
    atm_strike = atm["strike"]

    direction, confidence, signal_score, signal_reasons = option_chain_signal(atm)
    levels = option_chain_target_stoploss(df_nearby, atm_strike, direction)

    option_price_levels = expected_atm_option_prices(
        atm=atm,
        levels=levels,
        direction=direction,
        confidence=confidence,
        signal_score=signal_score,
    )

    trade_side = option_price_levels.get("trade_side", "N/A")

    if trade_side == "ATM CALL":
        buy_strike_text = f"{int(atm_strike):,} CE"
    elif trade_side == "ATM PUT":
        buy_strike_text = f"{int(atm_strike):,} PE"
    else:
        buy_strike_text = "N/A"

    signal_class = {
        "BULLISH": "green",
        "BEARISH": "red",
        "NEUTRAL": "amber",
    }.get(direction, "muted")

    st.markdown(
        f"""
        <div class="strategy-card">
            <div class="card-kicker">OPTION CHAIN SIGNAL</div>
            <div class="signal-text {signal_class}">
                {direction} | CONFIDENCE: {confidence}
            </div>
            <div class="muted" style="font-size:0.82rem;margin-top:0.7rem;">Score: {signal_score}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    entry_text = f"₹{option_price_levels['entry_price']:,.2f}" if option_price_levels["entry_price"] is not None else "N/A"
    target_price_text = f"₹{option_price_levels['target_price']:,.2f}" if option_price_levels["target_price"] is not None else "N/A"
    sl_price_text = f"₹{option_price_levels['stop_loss_price']:,.2f}" if option_price_levels["stop_loss_price"] is not None else "N/A"

    st.markdown(
        f"""
        <div class="strategy-card">
            <div class="card-kicker">EXPECTED ATM OPTION PRICE</div>
            <div class="levels-grid">
                <div>
                    <div class="level-label">Buy Strike</div>
                    <div class="level-value">{buy_strike_text}</div>
                </div>
                <div>
                    <div class="level-label">Trade Side</div>
                    <div class="level-value">{trade_side}</div>
                </div>
                <div>
                    <div class="level-label">Current Price</div>
                    <div class="level-value">{entry_text}</div>
                </div>
                <div>
                    <div class="level-label">Target Price</div>
                    <div class="level-value green">{target_price_text}</div>
                </div>
                <div>
                    <div class="level-label">Stop Loss Price</div>
                    <div class="level-value red">{sl_price_text}</div>
                </div>
            </div>
            <div class="muted" style="font-size:0.82rem;margin-top:0.9rem;">
                {option_price_levels["model_note"]}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.expander("Signal Reasoning"):
        if signal_reasons:
            for reason in signal_reasons:
                st.write(f"- {reason}")
        else:
            st.write("No strong signal reason found.")

if "selected_symbol" not in st.session_state:
    st.session_state.selected_symbol = "NIFTY"



cols = st.columns(len(INSTRUMENT_CARDS), gap="small")

for col, (symbol, item) in zip(cols, INSTRUMENT_CARDS.items()):
    with col:
        clicked = st.button(
            f"{item['icon']}\n\n{item['label']}",
            key=f"instrument_{symbol}",
            use_container_width=True,
        )

        if clicked and st.session_state.selected_symbol != symbol:
            st.session_state.selected_symbol = symbol
            st.session_state.pop("df_atm", None)
            st.session_state.pop("df_nearby", None)
            st.session_state.pop("df_chain", None)
            st.session_state.pop("top_movers_data", None)
            st.session_state.pop("last_refresh", None)
            st.rerun()

selected_symbol = st.session_state.selected_symbol

title_symbol = selected_symbol.replace("TOP GAINER & TOP LOSER", "Top Movers")

st.markdown(
    f'<div class="main-title">{title_symbol}</div>',
    unsafe_allow_html=True,
)
st.markdown(
    f'<div class="sub-title">Current time: {now_ist().strftime("%d %b %Y, %I:%M:%S %p")}</div>',
    unsafe_allow_html=True,
)

refresh = st.button("Refresh Data")

# if "account_summary" not in st.session_state or refresh:
#     st.session_state.account_summary = get_upstox_account_summary()

# render_account_summary(st.session_state.account_summary)

if "selected_symbol" not in st.session_state or st.session_state.selected_symbol != selected_symbol:
    st.session_state.selected_symbol = selected_symbol
    st.session_state.pop("df_atm", None)
    st.session_state.pop("df_nearby", None)
    st.session_state.pop("df_chain", None)
    st.session_state.pop("top_movers_data", None)

if selected_symbol == "TOP GAINER & TOP LOSER":
    if "top_movers_data" not in st.session_state or refresh:
        with st.spinner("Fetching NSE top F&O gainer and loser, then loading Groww option chains..."):
            st.session_state.top_movers_data = load_top_gainer_loser_data()
            st.session_state.last_refresh = now_ist().strftime("%d %b %Y, %I:%M:%S %p")

    for label, item in st.session_state.top_movers_data.items():
        st.header(f"{label}: {item['symbol']}")
        st.caption(item["url"])
        render_strategy(
            symbol=item["symbol"],
            df_atm=item["df_atm"],
            df_nearby=item["df_nearby"],
            df_chain=item["df_chain"],
            last_refresh=st.session_state.last_refresh,
        )
        st.divider()

    st.caption(
        "Top gainer/loser is discovered from NSE F&O movers, then Groww option-chain pages are used. Not a trading recommendation."
    )
    st.stop()

if "df_atm" not in st.session_state or refresh:
    with st.spinner(f"Fetching latest {selected_symbol} option-chain data..."):
        st.session_state.df_atm, st.session_state.df_nearby, st.session_state.df_chain = load_data(selected_symbol)
        st.session_state.last_refresh = now_ist().strftime("%d %b %Y, %I:%M:%S %p")

render_strategy(
    symbol=selected_symbol,
    df_atm=st.session_state.df_atm,
    df_nearby=st.session_state.df_nearby,
    df_chain=st.session_state.df_chain,
    last_refresh=st.session_state.last_refresh,
)

st.caption("Heuristic view only. Not a trading recommendation.")
