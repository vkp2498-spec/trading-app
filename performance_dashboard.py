from collections import deque
from pathlib import Path
import ast
import json
import os
import re

import pandas as pd
import requests
import streamlit as st

from datetime import time

from post_market_review import build_review, summarize, ask_llm_for_insights, build_loss_review
from strategy_core import now_ist

BASE_DIR = Path(__file__).resolve().parent
APP_ICON = BASE_DIR / "assets" / "vamsi_icon_v2.jpg"
DATA_DIR = BASE_DIR / "data"
ENV_FILE = BASE_DIR / ".env"
TRADE_HISTORY_FILE = BASE_DIR / "data" / "trade_history.csv"
LOG_FILE = BASE_DIR / "logs" / "trade_bot.log"

SYMBOLS = ["NIFTY", "BANKNIFTY"]
UPSTOX_POSITIONS_URL = "https://api.upstox.com/v2/portfolio/short-term-positions"

st.set_page_config(
    page_title="Trading Bot Dashboard",
    page_icon=str(APP_ICON) if APP_ICON.exists() else "📈",
    layout="wide",
)

st.markdown(
    """
    <link rel="apple-touch-icon" sizes="180x180" href="/app/static/vamsi_icon_v2.jpg?v=2">
    <link rel="icon" type="image/jpeg" href="/app/static/vamsi_icon_v2.jpg?v=2">
    """,
    unsafe_allow_html=True,
)


st.markdown(
    """
    <style>
    .stApp {
        background: linear-gradient(135deg, #07111f 0%, #0c1728 45%, #101827 100%);
        color: #f8fafc;
    }
    [data-testid="stHeader"] {
        background: rgba(7, 17, 31, 0.85);
    }
    .block-container {
        padding-top: 1.5rem;
        padding-bottom: 2rem;
        max-width: 1320px;
    }
    .dash-title {
        font-size: 34px;
        font-weight: 800;
        color: #f8fafc;
        margin-bottom: 4px;
    }
    .dash-subtitle {
        color: #94a3b8;
        font-size: 15px;
        margin-bottom: 20px;
    }
    .status-card {
        border: 1px solid rgba(148, 163, 184, 0.22);
        background: rgba(15, 23, 42, 0.88);
        border-radius: 14px;
        padding: 18px;
        min-height: 220px;
        box-shadow: 0 18px 45px rgba(0, 0, 0, 0.22);
    }
    .live-card {
        border: 1px solid rgba(34, 197, 94, 0.25);
        background: rgba(6, 78, 59, 0.28);
        border-radius: 14px;
        padding: 18px;
        box-shadow: 0 18px 45px rgba(0, 0, 0, 0.18);
    }
    .small-label {
        color: #94a3b8;
        font-size: 12px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.04em;
    }
    .big-value {
        color: #f8fafc;
        font-size: 26px;
        font-weight: 800;
        margin-top: 3px;
    }
    .muted {
        color: #94a3b8;
        font-size: 13px;
    }
    .positive {
        color: #22c55e;
        font-weight: 800;
    }
    .negative {
        color: #ef4444;
        font-weight: 800;
    }
    .neutral {
        color: #eab308;
        font-weight: 800;
    }
    div[data-testid="stMetric"] {
        background: rgba(15, 23, 42, 0.78);
        border: 1px solid rgba(148, 163, 184, 0.18);
        border-radius: 12px;
        padding: 14px;
    }
    div[data-testid="stDataFrame"] {
        border-radius: 12px;
        overflow: hidden;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

POST_MARKET_REVIEW_TIME = time(15, 30)


def render_post_market_review_tab():
    st.markdown("### Post-Market X-Ray")
    st.caption("Review rejected signals, missed opportunities, blocker reasons, and LLM insights.")

    today_text = now_ist().strftime("%Y-%m-%d")
    current_time = now_ist().time()

    review_date = st.date_input("Review date", value=now_ist().date())
    review_date_text = review_date.strftime("%Y-%m-%d")

    if review_date_text == today_text and current_time < POST_MARKET_REVIEW_TIME:
        st.warning("Market is still active. Please wait until after 3:30 PM IST for post-trade analysis.")
        return

    use_llm = st.checkbox("Include LLM insight", value=True)

    if st.button("Run Post-Market X-Ray", use_container_width=True):
        with st.spinner("Analyzing signals, rejected trades, missed opportunities, and blockers..."):
            try:
                review_df = build_review(review_date_text)
                review_mode = "Full review with market-data fetch"
            except Exception as e:
                st.warning(f"Full X-Ray failed, switching to offline saved-data review. Reason: {e}")
                review_df = pd.DataFrame()
                review_mode = "Offline saved-data review"

            if review_df.empty:
                summary = {
                    "review_date": review_date_text,
                    "review_mode": review_mode,
                    "total_analysis_rows": 0,
                    "directional_signals": 0,
                    "rejected_signals": 0,
                    "missed_expected_profit_total": 0,
                    "max_possible_profit_total": 0,
                    "outcome_counts": {},
                    "blocker_counts": {},
                    "by_symbol": {},
                    "top_missed_opportunities": [],
                }
            else:
                summary = summarize(review_df, review_date_text)
                summary["review_mode"] = review_mode

            DATA_DIR.mkdir(exist_ok=True)
            csv_file = DATA_DIR / f"post_market_review_{review_date_text}.csv"
            summary_file = DATA_DIR / f"post_market_summary_{review_date_text}.json"
            llm_file = DATA_DIR / f"post_market_llm_insights_{review_date_text}.txt"

            review_df.to_csv(csv_file, index=False)
            summary_file.write_text(json.dumps(summary, indent=2, sort_keys=True))

            llm_insights = None
            if use_llm:
                llm_insights = ask_llm_for_insights(summary)
                llm_file.write_text(llm_insights)

            st.session_state["post_market_review_df"] = review_df
            st.session_state["post_market_summary"] = summary
            st.session_state["post_market_llm_insights"] = llm_insights
            st.session_state["post_market_files"] = {
                "csv": str(csv_file),
                "summary": str(summary_file),
                "llm": str(llm_file) if use_llm else None,
            }

    summary = st.session_state.get("post_market_summary")
    review_df = st.session_state.get("post_market_review_df")
    llm_insights = st.session_state.get("post_market_llm_insights")
    files = st.session_state.get("post_market_files", {})

    if not summary:
        st.info("Click the button after market close to generate today’s review.")
        return

    st.success("Post-market review generated.")
    st.caption(f"Review mode: {summary.get('review_mode', 'N/A')}")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Checks", summary.get("total_analysis_rows", 0))
    c2.metric("Directional", summary.get("directional_signals", 0))
    c3.metric("Rejected", summary.get("rejected_signals", 0))
    c4.metric("Missed Profit", money(summary.get("missed_expected_profit_total", 0)))
    c5.metric("Max Possible", money(summary.get("max_possible_profit_total", 0)))

    st.markdown("### Outcome Summary")
    outcome_counts = summary.get("outcome_counts", {})
    if outcome_counts:
        st.dataframe(
            pd.DataFrame(
                [{"Outcome": key, "Count": value} for key, value in outcome_counts.items()]
            ),
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("### Main Blockers")
    blocker_counts = summary.get("blocker_counts", {})
    if blocker_counts:
        blocker_df = pd.DataFrame(
            [{"Blocker": key, "Count": value} for key, value in blocker_counts.items()]
        )
        st.bar_chart(blocker_df.set_index("Blocker")["Count"])

    st.markdown("### Symbol View")
    by_symbol = summary.get("by_symbol", {})
    if by_symbol:
        st.dataframe(
            pd.DataFrame(
                [{"Symbol": symbol, **values} for symbol, values in by_symbol.items()]
            ),
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("### Top Missed Opportunities")
    missed = summary.get("top_missed_opportunities", [])
    if missed:
        st.dataframe(pd.DataFrame(missed), use_container_width=True, hide_index=True)
    else:
        st.info("No missed target-hitting opportunities found.")

    if llm_insights:
        st.markdown("### LLM Insight")
        st.write(llm_insights)

        st.markdown("### Losing Trade Review")

        trade_file = DATA_DIR / "trade_history.csv"
        analysis_file = DATA_DIR / "analysis_history.csv"

        if not trade_file.exists():
            st.info("No trade history file found for losing trade review.")
        elif not analysis_file.exists():
            st.info("No analysis history file found for losing trade review.")
        else:
            trades_df = pd.read_csv(trade_file)
            analysis_df = pd.read_csv(analysis_file)

            if not trades_df.empty:
                trades_df["trade_date"] = pd.to_datetime(trades_df["trade_date"], errors="coerce").dt.date.astype(str)
                trades_df = trades_df[trades_df["trade_date"] == review_date_text]

            if not analysis_df.empty:
                analysis_df["created_at"] = pd.to_datetime(analysis_df["timestamp"], errors="coerce")
                analysis_df["review_date"] = analysis_df["created_at"].dt.date.astype(str)
                analysis_df = analysis_df[analysis_df["review_date"] == review_date_text]

            loss_reviews = build_loss_review(trades_df, analysis_df)

            if not loss_reviews:
                st.success("No losing trades found for this review date.")
            else:
                st.dataframe(pd.DataFrame(loss_reviews), use_container_width=True, hide_index=True)

    if review_df is not None and not review_df.empty:
        with st.expander("Full Review Data"):
            st.dataframe(review_df, use_container_width=True, hide_index=True)

    with st.expander("Generated Files"):
        st.write(files)


def load_env():
    if not ENV_FILE.exists():
        return

    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def safe_literal_dict(text):
    try:
        return ast.literal_eval(text)
    except Exception:
        return {}


def read_last_lines(path, max_lines=2500):
    if not path.exists():
        return []
    with path.open("r", errors="ignore") as f:
        return list(deque(f, maxlen=max_lines))


def extract_between(line, start, end):
    if start not in line:
        return ""
    part = line.split(start, 1)[1]
    if end and end in part:
        part = part.split(end, 1)[0]
    return part.strip()


def money(value):
    try:
        return f"₹{float(value):,.2f}"
    except Exception:
        return "N/A"


def number(value):
    try:
        return f"{float(value):,.2f}"
    except Exception:
        return "N/A"


def status_class(status):
    status = str(status).upper()
    if status in {"OPEN", "BOUGHT", "APPROVED", "TARGET HIT"}:
        return "positive"
    if status in {"REJECTED", "STOP LOSS HIT", "ERROR"}:
        return "negative"
    return "neutral"


def state_file(symbol):
    return BASE_DIR / f"trade_state_{symbol}.json"


def read_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def upstox_headers():
    token = os.getenv("UPSTOX_ACCESS_TOKEN")
    if not token:
        return None

    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }


def get_upstox_positions():
    headers = upstox_headers()
    if not headers:
        return [], "UPSTOX_ACCESS_TOKEN not set"

    try:
        response = requests.get(UPSTOX_POSITIONS_URL, headers=headers, timeout=12)
        if response.status_code >= 300:
            return [], f"Upstox positions failed {response.status_code}: {response.text[:250]}"
        return response.json().get("data", []) or [], None
    except Exception as e:
        return [], str(e)


def position_quantity(position):
    for key in ["quantity", "net_quantity"]:
        if position.get(key) is not None:
            try:
                return int(float(position.get(key)))
            except Exception:
                pass

    buy_qty = float(position.get("day_buy_quantity") or 0)
    sell_qty = float(position.get("day_sell_quantity") or 0)
    return int(buy_qty - sell_qty)


def position_ltp(position):
    for key in ["last_price", "ltp", "close_price"]:
        if position.get(key) is not None:
            try:
                return float(position.get(key))
            except Exception:
                pass
    return None


def position_avg_price(position):
    for key in ["average_price", "buy_price", "day_buy_price", "avg_price"]:
        if position.get(key) is not None:
            try:
                value = float(position.get(key))
                if value > 0:
                    return value
            except Exception:
                pass
    return None


def find_position_by_instrument(positions, instrument_key):
    for pos in positions:
        pos_key = pos.get("instrument_token") or pos.get("instrument_key")
        if pos_key == instrument_key:
            return pos
    return None


def get_live_bot_positions():
    positions, error = get_upstox_positions()
    rows = []

    for symbol in SYMBOLS:
        state = read_json(state_file(symbol), {})
        if not state or not state.get("instrument_key"):
            continue

        pos = find_position_by_instrument(positions, state.get("instrument_key"))
        qty = int(float(state.get("quantity") or 0))
        entry = float(state.get("entry_price") or 0)
        target = float(state.get("target_price") or 0)
        stop = float(state.get("stop_loss_price") or 0)
        highest = float(state.get("highest_ltp") or entry or 0)

        ltp = None
        actual_qty = 0

        if pos:
            actual_qty = position_quantity(pos)
            ltp = position_ltp(pos)
            broker_entry = position_avg_price(pos)
            if broker_entry:
                entry = broker_entry

        live_pnl = None
        target_progress = None
        risk_to_stop = None
        reward_left = None

        if ltp is not None and entry > 0:
            live_pnl = round((ltp - entry) * qty, 2)

        if ltp is not None and target > entry:
            target_progress = round(((ltp - entry) / (target - entry)) * 100, 1)

        if ltp is not None and stop > 0:
            risk_to_stop = round((ltp - stop) * qty, 2)

        if ltp is not None and target > 0:
            reward_left = round((target - ltp) * qty, 2)

        rows.append(
            {
                "symbol": symbol,
                "trading_symbol": state.get("trading_symbol", ""),
                "state_status": state.get("status", "OPEN"),
                "quantity": qty,
                "broker_quantity": actual_qty,
                "entry_price": entry,
                "ltp": ltp,
                "target_price": target,
                "stop_loss_price": stop,
                "highest_ltp": highest,
                "live_pnl": live_pnl,
                "target_progress": target_progress,
                "risk_to_stop": risk_to_stop,
                "reward_left": reward_left,
                "trailing_stop_active": state.get("trailing_stop_active", False),
                "trailing_stop_reason": state.get("trailing_stop_reason", ""),
                "created_at": state.get("created_at", ""),
            }
        )

    return pd.DataFrame(rows), error

def render_live_trade_cards(live_df):
    if live_df.empty:
        st.info("No open bot-tracked positions right now.")
        return

    cols = st.columns(2)

    for idx, row in live_df.iterrows():
        pnl = row.get("live_pnl")
        pnl_class = "positive" if pnl and pnl > 0 else "negative" if pnl and pnl < 0 else "neutral"

        trailing_active = bool(row.get("trailing_stop_active"))
        trailing_text = "ACTIVE" if trailing_active else "WAITING"
        trailing_class = "positive" if trailing_active else "neutral"

        progress = row.get("target_progress")
        progress_text = f"{progress}%" if pd.notna(progress) else "N/A"

        reason = row.get("trailing_stop_reason") or "Trailing will activate after enough target progress."

        html = f"""
<div class="live-card">
<div class="small-label">{row.get("symbol", "")}</div>
<div class="big-value">{row.get("trading_symbol", "")}</div>
<div class="muted">Qty: {row.get("quantity", "")} | Broker Qty: {row.get("broker_quantity", "")}</div>
<br>
<div class="small-label">Live P&L</div>
<div class="big-value {pnl_class}">{money(pnl)}</div>
<br>
<div class="small-label">Trade Levels</div>
<div>Entry: <b>{number(row.get("entry_price"))}</b> | LTP: <b>{number(row.get("ltp"))}</b> | Target: <b>{number(row.get("target_price"))}</b></div>
<div>Stop Loss: <b>{number(row.get("stop_loss_price"))}</b> | Highest LTP: <b>{number(row.get("highest_ltp"))}</b></div>
<br>
<div class="small-label">Risk View</div>
<div>Target Progress: <b>{progress_text}</b> | Reward Left: <b>{money(row.get("reward_left"))}</b> | Risk To Stop: <b>{money(row.get("risk_to_stop"))}</b></div>
<br>
<div class="small-label">Trailing Stop</div>
<div class="{trailing_class}">{trailing_text}</div>
<div class="muted">{reason}</div>
</div>
"""

        with cols[idx % 2]:
            st.markdown(html, unsafe_allow_html=True)


def parse_latest_bot_status():
    status = {
        symbol: {
            "last_time": "N/A",
            "status": "NO DATA",
            "signal": "N/A",
            "confidence": "N/A",
            "option_score": "N/A",
            "weighted_score": "N/A",
            "weighted_grade": "N/A",
            "atm_option_flow": "N/A",
            "reason": "N/A",
            "position": "N/A",
        }
        for symbol in SYMBOLS
    }

    last_run_time = "N/A"

    for line in read_last_lines(LOG_FILE):
        line = line.strip()
        ts_match = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \|", line)
        if ts_match:
            last_run_time = ts_match.group(1)

        for symbol in SYMBOLS:
            if f"| {symbol} " not in line:
                continue

            if ts_match:
                status[symbol]["last_time"] = ts_match.group(1)

            signal_match = re.search(
                rf"{symbol} signal: ([A-Z]+), confidence=([A-Z]+), score=([-0-9.]+)",
                line,
            )
            if signal_match:
                status[symbol]["status"] = "SIGNAL CHECKED"
                status[symbol]["signal"] = signal_match.group(1)
                status[symbol]["confidence"] = signal_match.group(2)
                status[symbol]["option_score"] = signal_match.group(3)

            if f"{symbol} no trade:" in line:
                status[symbol]["status"] = "REJECTED"
                status[symbol]["reason"] = line.split(f"{symbol} no trade:", 1)[1].strip()

            if f"{symbol} ERROR:" in line:
                status[symbol]["status"] = "ERROR"
                status[symbol]["reason"] = line.split(f"{symbol} ERROR:", 1)[1].strip()

            if f"{symbol} MARKET BUY placed" in line:
                status[symbol]["status"] = "BOUGHT"

            if f"{symbol} POSITION OPEN:" in line:
                status[symbol]["status"] = "OPEN"
                status[symbol]["position"] = line.split(f"{symbol} POSITION OPEN:", 1)[1].strip()

            if f"{symbol} open position active:" in line:
                status[symbol]["status"] = "OPEN"
                status[symbol]["position"] = line.split(f"{symbol} open position active:", 1)[1].strip()

            if f"{symbol} TARGET exit" in line:
                status[symbol]["status"] = "TARGET HIT"

            if f"{symbol} STOP_LOSS exit" in line:
                status[symbol]["status"] = "STOP LOSS HIT"

            if f"{symbol} bot squareoff" in line:
                status[symbol]["status"] = "SQUAREOFF"

            if f"{symbol} analysis:" in line:
                weighted_text = extract_between(line, "weighted=", " llm=")
                weighted = safe_literal_dict(weighted_text)

                if weighted:
                    status[symbol]["weighted_score"] = weighted.get("score", "N/A")
                    status[symbol]["weighted_grade"] = weighted.get("grade", "N/A")

                atm_flow_text = extract_between(line, "atm_option_flow=", "")
                atm_flow = safe_literal_dict(atm_flow_text)

                if atm_flow:
                    status[symbol]["atm_option_flow"] = (
                        f"{atm_flow.get('bias', 'N/A')} | "
                        f"close={atm_flow.get('close', 'N/A')} | "
                        f"vwap={atm_flow.get('vwap', 'N/A')} | "
                        f"vol_ratio={atm_flow.get('volume_ratio', 'N/A')}"
                    )

                llm_text = extract_between(line, "llm=", " atm_option_flow=")
                llm = safe_literal_dict(llm_text)

                if llm:
                    status[symbol]["status"] = "APPROVED" if llm.get("execute_trade") else "REJECTED"
                    status[symbol]["reason"] = llm.get("reason", status[symbol]["reason"])

    return last_run_time, status


def win_percent(data):
    if data.empty:
        return 0.0
    return round((data["gross_pnl"] > 0).mean() * 100, 1)


def pnl_for(data, symbol):
    return round(data[data["symbol"] == symbol]["gross_pnl"].sum(), 2)


load_env()

main_tab, review_tab = st.tabs(["Live Dashboard", "Post-Market X-Ray"])

with main_tab:
    st.markdown('<div class="dash-title">Trading Bot Performance</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="dash-subtitle">Live status, open trade P&L, and closed trade performance</div>',
        unsafe_allow_html=True,
    )

    if st.button("Refresh Dashboard", use_container_width=True):
        st.rerun()

    last_run_time, bot_status = parse_latest_bot_status()
    live_df, live_error = get_live_bot_positions()

    st.markdown("### Live Cockpit")

    top1, top2, top3, top4 = st.columns(4)

    live_pnl_total = 0.0
    if not live_df.empty:
        live_pnl_total = live_df["live_pnl"].fillna(0).sum()

    top1.metric("Last Bot Log", last_run_time)
    top2.metric("Open Bot Trades", len(live_df))
    top3.metric("Live Bot P&L", money(live_pnl_total))
    top4.metric("Upstox Live Data", "OK" if not live_error else "Check")

    if live_error:
        st.warning(live_error)

    status_cols = st.columns(2)

    for idx, symbol in enumerate(SYMBOLS):
        item = bot_status[symbol]
        cls = status_class(item["status"])

        with status_cols[idx]:
            st.markdown(
                f"""
                <div class="status-card">
                    <div class="small-label">{symbol}</div>
                    <div class="big-value {cls}">{item["status"]}</div>
                    <div class="muted">Last update: {item["last_time"]}</div>
                    <br>
                    <div class="small-label">Signal</div>
                    <div>{item["signal"]} | Confidence: {item["confidence"]} | Option score: {item["option_score"]}</div>
                    <br>
                    <div class="small-label">Overall</div>
                    <div>Score: {item["weighted_score"]} | Grade: {item["weighted_grade"]}</div>
                    <br>
                    <div class="small-label">ATM Option Flow</div>
                    <div>{item["atm_option_flow"]}</div>
                    <br>
                    <div class="small-label">Reason</div>
                    <div class="muted">{item["reason"]}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    st.markdown("### Live Bot Trades")
    render_live_trade_cards(live_df)

    st.divider()

    if not TRADE_HISTORY_FILE.exists():
        st.info("No closed trades recorded yet.")
    else:
        df = pd.read_csv(TRADE_HISTORY_FILE)

        if df.empty:
            st.info("No closed trades recorded yet.")
        else:
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
            df["gross_pnl"] = pd.to_numeric(df["gross_pnl"], errors="coerce").fillna(0)
            df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce").fillna(0)

            today = pd.Timestamp.now(tz="Asia/Kolkata").date()
            today_df = df[df["trade_date"] == today]

            st.markdown("### Today")

            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Closed Trades", len(today_df))
            c2.metric("Closed P&L", money(today_df["gross_pnl"].sum()))
            c3.metric("Win %", f"{win_percent(today_df)}%")
            c4.metric("NIFTY P&L", money(pnl_for(today_df, "NIFTY")))
            c5.metric("BANKNIFTY P&L", money(pnl_for(today_df, "BANKNIFTY")))

            st.markdown("### Cumulative")

            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("Total Trades", len(df))
            c2.metric("Total P&L", money(df["gross_pnl"].sum()))
            c3.metric("Win %", f"{win_percent(df)}%")
            c4.metric("NIFTY Total", money(pnl_for(df, "NIFTY")))
            c5.metric("BANKNIFTY Total", money(pnl_for(df, "BANKNIFTY")))

            daily = df.groupby("trade_date", as_index=False)["gross_pnl"].sum()
            daily["cumulative_pnl"] = daily["gross_pnl"].cumsum()

            left, right = st.columns(2)

            with left:
                st.markdown("### Daily P&L")
                st.bar_chart(daily.set_index("trade_date")["gross_pnl"])

            with right:
                st.markdown("### Cumulative Equity Curve")
                st.line_chart(daily.set_index("trade_date")["cumulative_pnl"])

            st.markdown("### Symbol P&L")
            symbol_pnl = df.groupby("symbol", as_index=False)["gross_pnl"].sum()
            st.bar_chart(symbol_pnl.set_index("symbol")["gross_pnl"])

            st.markdown("### Today's Closed Trades")
            st.dataframe(today_df.sort_values("exit_time", ascending=False), use_container_width=True, hide_index=True)

            st.markdown("### All Closed Trades")
            st.dataframe(df.sort_values("exit_time", ascending=False), use_container_width=True, hide_index=True)

with review_tab:
    render_post_market_review_tab()