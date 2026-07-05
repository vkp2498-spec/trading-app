import re
from PIL import Image
import asyncio
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from datetime import datetime
from playwright.async_api import async_playwright

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

st.markdown(
    """
    <style>
    .stApp {
        background:
            radial-gradient(circle at top left, rgba(20,184,166,0.18), transparent 28%),
            radial-gradient(circle at top right, rgba(59,130,246,0.16), transparent 30%),
            linear-gradient(135deg, #07111f 0%, #0d1729 48%, #111827 100%);
        color: #f8fafc;
    }
    .main-title {
        font-size: 42px;
        font-weight: 800;
        color: #f8fafc;
        margin-bottom: 4px;
    }
    .sub-title {
        color: #cbd5e1;
        font-size: 16px;
        margin-bottom: 24px;
    }
    [data-testid="stMetricValue"] { color: #f8fafc; }
    [data-testid="stMetricLabel"] { color: #cbd5e1; }
    div.stButton > button {
        background: #14b8a6;
        color: #03111f;
        border: 0;
        border-radius: 8px;
        font-weight: 700;
        padding: 0.55rem 1.1rem;
    }
    div.stButton > button:hover {
        background: #2dd4bf;
        color: #03111f;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def clean_num(x):
    if x is None:
        return None
    x = str(x).replace("₹", "").replace(",", "").strip()
    m = re.search(r"-?\d+(\.\d+)?", x)
    return float(m.group(0)) if m else None


def should_use_next_week_expiry():
    return datetime.now().weekday() in [0, 1]


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


def detect_expiry(all_text):
    matches = re.findall(r"\b\d{2}\s+[A-Z][a-z]{2}\b", all_text)

    if not matches:
        return None

    unique_expiries = []
    for expiry in matches:
        if expiry not in unique_expiries:
            unique_expiries.append(expiry)

    if should_use_next_week_expiry() and len(unique_expiries) >= 2:
        return unique_expiries[1]

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

    return {
        "trade_side": trade_side,
        "entry_price": round(entry_price, 2),
        "target_price": round(entry_price + target_premium_move, 2),
        "stop_loss_price": round(max(entry_price - sl_premium_move, 0), 2),
        "model_note": (
            f"Intraday heuristic using delta={delta}, confidence={confidence}, "
            f"premium factor={premium_factor:.2f}, OI factor={oi_factor:.2f}"
        ),
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

        ce_oi, ce_prev_oi, ce_change_oi, ce_change_pct = parse_oi_with_change(
            ce_oi_texts[-1] if ce_oi_texts else None
        )

        pe_oi, pe_prev_oi, pe_change_oi, pe_change_pct = parse_oi_with_change(
            pe_oi_texts[0] if pe_oi_texts else None
        )

        rows.append({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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


def render_strategy(symbol, df_atm, df_nearby, df_chain, last_refresh):
    atm = df_atm.iloc[0]
    direction, confidence, signal_score, signal_reasons = option_chain_signal(atm)

    levels = option_chain_target_stoploss(df_nearby, atm["strike"], direction)

    option_price_levels = expected_atm_option_prices(
        atm=atm,
        levels=levels,
        direction=direction,
        confidence=confidence,
        signal_score=signal_score,
    )

    col1, col2, col3, col4, col5, col6 = st.columns(6)
    col1.metric("Spot", f"{atm['spot']:,.2f}" if pd.notna(atm["spot"]) else "N/A")
    col2.metric("ATM Strike", f"{atm['strike']:,.0f}")
    col3.metric("Expiry", atm["expiry"] if atm["expiry"] else "N/A")
    col4.metric("Bias", direction)
    col5.metric("Confidence", confidence)
    col6.metric("Last Refresh", last_refresh)

    signal_color = {"BULLISH": "#22c55e", "BEARISH": "#ef4444", "NEUTRAL": "#f59e0b"}.get(direction, "#f8fafc")

    st.markdown(
        f"""
        <div style="margin:12px 0 20px 0;padding:16px 18px;border-radius:8px;
        background:rgba(15,23,42,0.78);border:1px solid rgba(148,163,184,0.25);">
            <div style="font-size:14px;color:#cbd5e1;">OPTION CHAIN SIGNAL</div>
            <div style="font-size:30px;font-weight:800;color:{signal_color};">
                {direction} | CONFIDENCE: {confidence}
            </div>
            <div style="font-size:13px;color:#94a3b8;margin-top:6px;">Score: {signal_score}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    target_text = f"{levels['target']:,.0f}" if levels["target"] is not None else "N/A"
    sl_text = f"{levels['stop_loss']:,.0f}" if levels["stop_loss"] is not None else "N/A"
    support_text = f"{levels['support']:,.0f}" if levels["support"] is not None else "N/A"
    resistance_text = f"{levels['resistance']:,.0f}" if levels["resistance"] is not None else "N/A"

    st.markdown(
        f"""
        <div style="margin:12px 0 24px 0;padding:16px 18px;border-radius:8px;
        background:rgba(2,6,23,0.72);border:1px solid rgba(148,163,184,0.25);">
            <div style="font-size:14px;color:#cbd5e1;">OPTION WRITING LEVELS</div>
            <div style="display:grid;grid-template-columns:repeat(4,minmax(120px,1fr));gap:14px;margin-top:12px;">
                <div><div style="color:#94a3b8;font-size:13px;">Immediate Target</div><div style="color:#22c55e;font-size:28px;font-weight:800;">{target_text}</div></div>
                <div><div style="color:#94a3b8;font-size:13px;">Immediate Stop Loss</div><div style="color:#ef4444;font-size:28px;font-weight:800;">{sl_text}</div></div>
                <div><div style="color:#94a3b8;font-size:13px;">Nearest Support</div><div style="color:#f8fafc;font-size:24px;font-weight:700;">{support_text}</div></div>
                <div><div style="color:#94a3b8;font-size:13px;">Nearest Resistance</div><div style="color:#f8fafc;font-size:24px;font-weight:700;">{resistance_text}</div></div>
            </div>
            <div style="font-size:13px;color:#94a3b8;margin-top:10px;">{levels["strategy"]}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    entry_text = f"₹{option_price_levels['entry_price']:,.2f}" if option_price_levels["entry_price"] is not None else "N/A"
    target_price_text = f"₹{option_price_levels['target_price']:,.2f}" if option_price_levels["target_price"] is not None else "N/A"
    sl_price_text = f"₹{option_price_levels['stop_loss_price']:,.2f}" if option_price_levels["stop_loss_price"] is not None else "N/A"

    st.markdown(
        f"""
        <div style="margin:12px 0 24px 0;padding:16px 18px;border-radius:8px;
        background:rgba(15,23,42,0.82);border:1px solid rgba(148,163,184,0.25);">
            <div style="font-size:14px;color:#cbd5e1;">EXPECTED ATM OPTION PRICE</div>
            <div style="display:grid;grid-template-columns:repeat(4,minmax(120px,1fr));gap:14px;margin-top:12px;">
                <div><div style="color:#94a3b8;font-size:13px;">Trade Side</div><div style="color:#f8fafc;font-size:24px;font-weight:800;">{option_price_levels["trade_side"]}</div></div>
                <div><div style="color:#94a3b8;font-size:13px;">Current Price</div><div style="color:#f8fafc;font-size:24px;font-weight:800;">{entry_text}</div></div>
                <div><div style="color:#94a3b8;font-size:13px;">Target Price</div><div style="color:#22c55e;font-size:28px;font-weight:800;">{target_price_text}</div></div>
                <div><div style="color:#94a3b8;font-size:13px;">Stop Loss Price</div><div style="color:#ef4444;font-size:28px;font-weight:800;">{sl_price_text}</div></div>
            </div>
            <div style="font-size:13px;color:#94a3b8;margin-top:10px;">{option_price_levels["model_note"]}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.expander("Signal Reasoning"):
        for reason in signal_reasons:
            st.write(f"- {reason}")

if "selected_symbol" not in st.session_state:
    st.session_state.selected_symbol = "NIFTY"

st.markdown(
    """
    <style>
    div[data-testid="column"] button {
        width: 100%;
        min-height: 92px;
        border-radius: 10px;
        background: rgba(15, 23, 42, 0.78);
        border: 1px solid rgba(148, 163, 184, 0.25);
        color: #f8fafc;
        font-weight: 800;
    }
    div[data-testid="column"] button:hover {
        border-color: #14b8a6;
        background: rgba(20, 184, 166, 0.16);
    }
    </style>
    """,
    unsafe_allow_html=True,
)

cols = st.columns(len(INSTRUMENT_CARDS))

for col, (symbol, item) in zip(cols, INSTRUMENT_CARDS.items()):
    with col:
        clicked = st.button(
            f"{item['icon']}\n\n{item['label']}",
            key=f"instrument_{symbol}",
        )

        if clicked:
            st.session_state.selected_symbol = symbol

selected_symbol = st.session_state.selected_symbol

st.markdown(f'<div class="main-title">{selected_symbol} Option Strategy</div>', unsafe_allow_html=True)
st.markdown(
    f'<div class="sub-title">Current time: {datetime.now().strftime("%d %b %Y, %I:%M:%S %p")}</div>',
    unsafe_allow_html=True,
)

refresh = st.button("Refresh Data")

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
            st.session_state.last_refresh = datetime.now().strftime("%d %b %Y, %I:%M:%S %p")

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
        st.session_state.last_refresh = datetime.now().strftime("%d %b %Y, %I:%M:%S %p")

render_strategy(
    symbol=selected_symbol,
    df_atm=st.session_state.df_atm,
    df_nearby=st.session_state.df_nearby,
    df_chain=st.session_state.df_chain,
    last_refresh=st.session_state.last_refresh,
)

st.caption(
    "Signal, target, stop loss, and expected option prices are simple option-writing heuristics. Not a trading recommendation."
)
