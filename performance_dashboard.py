from pathlib import Path

import pandas as pd
import streamlit as st

BASE_DIR = Path(__file__).resolve().parent
TRADE_HISTORY_FILE = BASE_DIR / "data" / "trade_history.csv"

st.set_page_config(page_title="Bot Performance", page_icon="📈", layout="wide")

st.title("Bot Performance Dashboard")

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