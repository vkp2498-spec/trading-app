from pathlib import Path
import ast
import re

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent
TRADE_HISTORY_FILE = BASE_DIR / "data" / "trade_history.csv"
LOG_FILE = BASE_DIR / "logs" / "trade_bot.log"

SYMBOLS = ["NIFTY", "BANKNIFTY"]

st.set_page_config(page_title="Bot Performance", page_icon="📈", layout="wide")
st.title("Bot Performance Dashboard")

if st.button("Refresh"):
    st.rerun()


def safe_literal_dict(text):
    try:
        return ast.literal_eval(text)
    except Exception:
        return {}


def extract_between(line, start, end):
    if start not in line:
        return ""
    part = line.split(start, 1)[1]
    if end and end in part:
        part = part.split(end, 1)[0]
    return part.strip()


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

    if not LOG_FILE.exists():
        return last_run_time, status

    lines = LOG_FILE.read_text(errors="ignore").splitlines()[-2000:]

    for line in lines:
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

            if f"{symbol} MARKET BUY placed" in line:
                status[symbol]["status"] = "BOUGHT"

            if f"{symbol} POSITION OPEN:" in line:
                status[symbol]["status"] = "OPEN"
                status[symbol]["position"] = line.split(f"{symbol} POSITION OPEN:", 1)[1].strip()

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
                    if llm.get("execute_trade"):
                        status[symbol]["status"] = "APPROVED"
                    else:
                        status[symbol]["status"] = "REJECTED"
                    status[symbol]["reason"] = llm.get("reason", status[symbol]["reason"])

    return last_run_time, status


last_run_time, bot_status = parse_latest_bot_status()

st.subheader("Live Bot Status")
st.caption(f"Last log time: {last_run_time}")

cols = st.columns(2)

for idx, symbol in enumerate(SYMBOLS):
    item = bot_status[symbol]

    with cols[idx]:
        st.markdown(f"### {symbol}")

        c1, c2, c3 = st.columns(3)
        c1.metric("Status", item["status"])
        c2.metric("Signal", item["signal"])
        c3.metric("Confidence", item["confidence"])

        c1, c2, c3 = st.columns(3)
        c1.metric("Option Score", item["option_score"])
        c2.metric("Overall Score", item["weighted_score"])
        c3.metric("Grade", item["weighted_grade"])

        st.write("**ATM Option Flow:**", item["atm_option_flow"])
        st.write("**Latest Reason:**", item["reason"])

        if item["position"] != "N/A":
            st.write("**Position:**", item["position"])

st.divider()

if not TRADE_HISTORY_FILE.exists():
    st.info("No closed trades recorded yet.")
    st.stop()

df = pd.read_csv(TRADE_HISTORY_FILE)

if df.empty:
    st.info("No closed trades recorded yet.")
    st.stop()

df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
df["gross_pnl"] = pd.to_numeric(df["gross_pnl"], errors="coerce").fillna(0)
df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce").fillna(0)

today = pd.Timestamp.now(tz="Asia/Kolkata").date()
today_df = df[df["trade_date"] == today]


def win_percent(data):
    if data.empty:
        return 0.0
    return round((data["gross_pnl"] > 0).mean() * 100, 1)


def pnl_for(data, symbol):
    return round(data[data["symbol"] == symbol]["gross_pnl"].sum(), 2)


st.subheader("Today")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Trades", len(today_df))
c2.metric("Net P&L", f"₹{today_df['gross_pnl'].sum():.2f}")
c3.metric("Win %", f"{win_percent(today_df)}%")
c4.metric("NIFTY P&L", f"₹{pnl_for(today_df, 'NIFTY'):.2f}")
c5.metric("BANKNIFTY P&L", f"₹{pnl_for(today_df, 'BANKNIFTY'):.2f}")

st.subheader("Cumulative")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total Trades", len(df))
c2.metric("Total P&L", f"₹{df['gross_pnl'].sum():.2f}")
c3.metric("Win %", f"{win_percent(df)}%")
c4.metric("NIFTY Total", f"₹{pnl_for(df, 'NIFTY'):.2f}")
c5.metric("BANKNIFTY Total", f"₹{pnl_for(df, 'BANKNIFTY'):.2f}")

daily = df.groupby("trade_date", as_index=False)["gross_pnl"].sum()
daily["cumulative_pnl"] = daily["gross_pnl"].cumsum()

st.subheader("Daily P&L")
st.bar_chart(daily.set_index("trade_date")["gross_pnl"])

st.subheader("Cumulative Equity Curve")
st.line_chart(daily.set_index("trade_date")["cumulative_pnl"])

st.subheader("Symbol P&L")
symbol_pnl = df.groupby("symbol", as_index=False)["gross_pnl"].sum()
st.bar_chart(symbol_pnl.set_index("symbol")["gross_pnl"])

st.subheader("Today's Trades")
st.dataframe(today_df.sort_values("exit_time", ascending=False), use_container_width=True)

st.subheader("All Closed Trades")
st.dataframe(df.sort_values("exit_time", ascending=False), use_container_width=True)