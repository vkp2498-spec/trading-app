from collections import deque
from pathlib import Path
import ast
import html
import json
import os
import re
import subprocess
import sys

import pandas as pd
import requests
import streamlit as st

from datetime import time

from banknifty_post_market import run_banknifty_veto_audit
from post_market_review import (
    ask_llm_for_insights,
    build_decision_funnel,
    build_rejection_quality,
    build_review,
    build_symbol_audit,
    build_trade_forensics,
    enrich_replay_decisions,
    summarize,
)
from post_market_score_audit import (
    SCORE_BUCKETS,
    STATUS_FILE as SCORE_AUDIT_STATUS_FILE,
    build_bucket_summary as build_score_bucket_summary,
    build_reason_summary as build_score_reason_summary,
    read_audit as read_score_audit,
)
from strategy_core import now_ist
from dashboard_data import build_live_positions as api_build_live_positions
from dashboard_data import build_trade_performance as api_build_trade_performance
from dashboard_data import dashboard_index_trades
from dashboard_data import normalized_underlying
from dashboard_data import option_type
from dashboard_data import read_trade_history
from trade_forensics import (
    analyze_executed_trades as run_executed_trade_forensics,
    analyze_rejected_signals as run_rejected_signal_forensics,
    build_summary as build_forensic_summary,
    instrument_lookup as forensic_instrument_lookup,
    load_instruments as load_forensic_instruments,
    read_rejected_signals as read_forensic_rejected_signals,
    read_trades as read_forensic_trades,
)
from session_insights import build_session_insights, read_analysis_rows, read_log_lines

BASE_DIR = Path(__file__).resolve().parent
APP_ICON = BASE_DIR / "assets" / "vamsi_icon_v2.jpg"
DATA_DIR = BASE_DIR / "data"
ENV_FILE = BASE_DIR / ".env"
TRADE_HISTORY_FILE = BASE_DIR / "data" / "trade_history.csv"
ANALYSIS_HISTORY_FILE = BASE_DIR / "data" / "analysis_history.csv"
LOG_FILE = BASE_DIR / "logs" / "trade_bot.log"
STOCK_SCANNER_STATUS_FILE = DATA_DIR / "stock_scanner_status.json"
BACKTEST_DIR = DATA_DIR / "backtests"
BACKTEST_STATUS_FILE = BACKTEST_DIR / "status.json"
BACKTEST_LATEST_FILE = BACKTEST_DIR / "latest.json"

SYMBOLS = ["NIFTY", "BANKNIFTY"]
STATE_SLOTS = SYMBOLS + ["STOCK_FUTURE", "GANESH_GAP_NIFTY", "GANESH_GAP_BANKNIFTY"]
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
        background: #ffffff;
        color: #0f172a;
    }
    [data-testid="stHeader"] {
        background: rgba(255, 255, 255, 0.96);
    }
    .block-container {
        padding-top: 1.5rem;
        padding-bottom: 2rem;
        max-width: 1320px;
    }
    .dash-title {
        font-size: 34px;
        font-weight: 800;
        color: #061a35;
        margin-bottom: 4px;
    }
    .dash-subtitle {
        color: #475569;
        font-size: 15px;
        margin-bottom: 20px;
    }
    .navy-section {
        background: #061a35;
        border-radius: 10px;
        color: #ffffff;
        font-size: 18px;
        font-weight: 800;
        margin: 18px 0 12px 0;
        padding: 12px 16px;
    }
    .metric-card {
        background: #f8fafc;
        border: 1px solid #d8e0ea;
        border-radius: 8px;
        padding: 16px;
        min-height: 104px;
    }
    .metric-label {
        color: #061a35;
        font-size: 12px;
        font-weight: 800;
        letter-spacing: 0.04em;
        text-transform: uppercase;
    }
    .metric-value {
        color: #0f172a;
        font-size: 26px;
        font-weight: 850;
        margin-top: 8px;
    }
    .mini-chart-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(220px, 1fr));
        gap: 14px;
        max-width: 760px;
    }
    .mini-chart-card {
        background: #f8fafc;
        border: 1px solid #d8e0ea;
        border-radius: 8px;
        padding: 14px 16px;
    }
    .mini-chart-title {
        color: #061a35;
        font-size: 13px;
        font-weight: 850;
        margin-bottom: 12px;
    }
    .mini-bars {
        align-items: end;
        display: flex;
        gap: 14px;
        height: 120px;
        justify-content: center;
    }
    .mini-bar-wrap {
        align-items: center;
        display: flex;
        flex-direction: column;
        gap: 6px;
        width: 64px;
    }
    .mini-bar {
        border-radius: 5px 5px 0 0;
        min-height: 6px;
        width: 28px;
    }
    .mini-bar.call {
        background: #061a35;
    }
    .mini-bar.put {
        background: #2563eb;
    }
    .mini-bar-label {
        color: #475569;
        font-size: 11px;
        font-weight: 800;
    }
    .mini-bar-value {
        font-size: 12px;
        font-weight: 850;
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
        color: #16a34a;
        font-weight: 800;
    }
    .negative {
        color: #dc2626;
        font-weight: 800;
    }
    .neutral {
        color: #eab308;
        font-weight: 800;
    }
    div[data-testid="stMetric"] {
        background: #f8fafc;
        border: 1px solid #d8e0ea;
        border-radius: 8px;
        padding: 14px;
    }
    div[data-testid="stButton"] button {
        color: #ffffff;
        font-weight: 800;
    }
    div[data-testid="stDataFrame"] {
        border-radius: 12px;
        overflow: hidden;
    }
    .xray-verdict {
        border: 1px solid rgba(148, 163, 184, 0.24);
        border-left: 4px solid #22c55e;
        background: rgba(15, 23, 42, 0.86);
        border-radius: 8px;
        padding: 16px;
        margin: 8px 0 16px 0;
    }
    .xray-verdict-title {
        color: #f8fafc;
        font-size: 19px;
        font-weight: 800;
    }
    .xray-verdict-copy {
        color: #cbd5e1;
        font-size: 14px;
        margin-top: 4px;
    }
    .xray-section-note {
        color: #94a3b8;
        font-size: 13px;
        margin-top: -8px;
        margin-bottom: 12px;
    }
    .insight-line {
        border-left: 3px solid #38bdf8;
        background: rgba(15, 23, 42, 0.72);
        border-radius: 6px;
        color: #dbeafe;
        font-size: 14px;
        margin: 8px 0;
        padding: 11px 13px;
    }
    @media (max-width: 768px) {
        .block-container {
            padding: 0.8rem 0.75rem 1.5rem 0.75rem;
        }
        .dash-title {
            font-size: 28px;
        }
        .mini-chart-grid {
            grid-template-columns: 1fr;
        }
        .xray-verdict-title {
            font-size: 17px;
        }
        div[data-testid="stMetric"] {
            padding: 11px;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

POST_MARKET_REVIEW_TIME = time(15, 30)


def read_csv_if_present(path):
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def actual_trades_for_date(date_text):
    trades = read_csv_if_present(TRADE_HISTORY_FILE)
    if trades.empty or "trade_date" not in trades.columns:
        return pd.DataFrame()
    trade_dates = pd.to_datetime(trades["trade_date"], errors="coerce").dt.date.astype(str)
    trades = trades[trade_dates == date_text].copy()
    if "gross_pnl" in trades.columns:
        trades["gross_pnl"] = pd.to_numeric(trades["gross_pnl"], errors="coerce").fillna(0)
    return trades


def load_saved_replay(date_text):
    decisions = read_csv_if_present(DATA_DIR / f"counterfactual_decisions_{date_text}.csv")
    trades = read_csv_if_present(DATA_DIR / f"counterfactual_trades_{date_text}.csv")
    summary_path = DATA_DIR / f"counterfactual_summary_{date_text}.json"
    summary = read_json(summary_path, {})
    return decisions, trades, summary


def save_replay_artifacts(date_text, decisions, trades, summary):
    decisions_file = DATA_DIR / f"counterfactual_decisions_{date_text}.csv"
    trades_file = DATA_DIR / f"counterfactual_trades_{date_text}.csv"
    summary_file = DATA_DIR / f"counterfactual_summary_{date_text}.json"
    decisions.to_csv(decisions_file, index=False)
    trades.to_csv(trades_file, index=False)
    summary_file.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return decisions_file, trades_file, summary_file


def xray_overview(actual_trades, replay_trades, quality):
    actual_pnl = float(pd.to_numeric(actual_trades.get("gross_pnl", pd.Series(dtype=float)), errors="coerce").fillna(0).sum())
    replay_pnl = float(pd.to_numeric(replay_trades.get("gross_pnl", pd.Series(dtype=float)), errors="coerce").fillna(0).sum())
    correct = int(quality["correct_rejects"].sum()) if not quality.empty else 0
    missed = int(quality["missed_winners"].sum()) if not quality.empty else 0
    decisive = correct + missed
    precision = round((correct / decisive) * 100, 1) if decisive else None
    return {
        "actual_trades": int(len(actual_trades)),
        "actual_pnl": round(actual_pnl, 2),
        "replay_trades": int(len(replay_trades)),
        "replay_pnl": round(replay_pnl, 2),
        "pnl_delta": round(replay_pnl - actual_pnl, 2),
        "correct_rejects": correct,
        "missed_winners": missed,
        "rejection_precision_pct": precision,
    }


def render_xray_trade_forensics(forensics):
    if forensics.empty:
        st.info("No actual closed trades were recorded for this date.")
        return

    for _, row in forensics.sort_values("entry_time").iterrows():
        pnl = float(row.get("gross_pnl") or 0)
        with st.container(border=True):
            st.markdown(f"#### {row.get('symbol', '')} | {row.get('trading_symbol', '')}")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Actual P&L", money(pnl))
            c2.metric("Entry / Exit", f"{number(row.get('actual_entry'))} / {number(row.get('exit_price'))}")
            c3.metric("Exit Reason", str(row.get("exit_reason") or "N/A"))
            revised = str(row.get("revised_decision") or "NO MATCH")
            c4.metric("Revised Rule", revised)

            detail1, detail2 = st.columns(2)
            with detail1:
                st.markdown("**Entry evidence**")
                st.write(
                    f"Score {number(row.get('entry_weighted_score'))} | "
                    f"Grade {row.get('entry_weighted_grade') or 'N/A'} | "
                    f"ATM flow {row.get('atm_option_flow') or 'N/A'}"
                )
                st.caption(
                    f"Expected entry {number(row.get('expected_entry'))}; "
                    f"actual entry {number(row.get('actual_entry'))}; "
                    f"slippage {number(row.get('entry_slippage_pct'))}%"
                )
            with detail2:
                st.markdown("**Revised-rule verdict**")
                st.write(str(row.get("revised_category") or "No revised decision matched"))
                st.caption(str(row.get("revised_reason") or "No replay reason available."))


def render_post_market_review_tab():
    st.markdown('<div class="dash-title">Post-Market X-Ray</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="dash-subtitle">Actual results, revised-rule replay, rejection quality, and trade-level evidence</div>',
        unsafe_allow_html=True,
    )

    today_text = now_ist().strftime("%Y-%m-%d")
    current_time = now_ist().time()

    review_date = st.date_input("Review date", value=now_ist().date())
    review_date_text = review_date.strftime("%Y-%m-%d")

    if review_date_text == today_text and current_time < POST_MARKET_REVIEW_TIME:
        st.warning("Market is still active. Please wait until after 3:30 PM IST for post-trade analysis.")
        return

    use_llm = st.checkbox("Include LLM insight", value=True)

    if st.button("Run Market Debrief", use_container_width=True, type="primary"):
        with st.spinner("Rebuilding the day from saved signals and five-minute option candles..."):
            DATA_DIR.mkdir(exist_ok=True)
            saved_review_file = DATA_DIR / f"post_market_review_{review_date_text}.csv"
            saved_review = read_csv_if_present(saved_review_file)
            try:
                if review_date_text != today_text and not saved_review.empty:
                    review_df = saved_review
                    review_mode = "Saved completed market review"
                else:
                    review_df = build_review(review_date_text)
                    review_mode = "Full review with market-data fetch"
                    if not saved_review.empty and "candle_fetch_error" in review_df.columns:
                        directional = review_df.get("direction", pd.Series(dtype="object")).isin(["BULLISH", "BEARISH"])
                        failed = review_df["candle_fetch_error"].fillna("").astype(str).str.strip().ne("")
                        if directional.any() and failed[directional].all():
                            review_df = saved_review
                            review_mode = "Saved completed market review"
            except Exception as e:
                review_df = read_csv_if_present(saved_review_file)
                review_mode = "Offline saved-data review"
                st.warning(f"Live market-data review was unavailable. Saved evidence is being used. Reason: {e}")

            if review_df.empty:
                summary = {
                    "date": review_date_text,
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

            csv_file = DATA_DIR / f"post_market_review_{review_date_text}.csv"
            summary_file = DATA_DIR / f"post_market_summary_{review_date_text}.json"
            llm_file = DATA_DIR / f"post_market_llm_insights_{review_date_text}.txt"

            review_df.to_csv(csv_file, index=False)
            summary_file.write_text(json.dumps(summary, indent=2, sort_keys=True, default=str))

            replay_error = None
            try:
                from counterfactual_replay import replay_day

                replay_decisions, replay_trades, replay_summary = replay_day(review_date_text)
                replay_mode = "Fresh revised-rule replay"
                replay_files = save_replay_artifacts(
                    review_date_text,
                    replay_decisions,
                    replay_trades,
                    replay_summary,
                )
            except Exception as error:
                replay_error = str(error)
                replay_decisions, replay_trades, replay_summary = load_saved_replay(review_date_text)
                replay_mode = "Saved revised-rule replay"
                replay_files = (
                    DATA_DIR / f"counterfactual_decisions_{review_date_text}.csv",
                    DATA_DIR / f"counterfactual_trades_{review_date_text}.csv",
                    DATA_DIR / f"counterfactual_summary_{review_date_text}.json",
                )

            actual_trades = actual_trades_for_date(review_date_text)
            rejection_quality = build_rejection_quality(replay_decisions, review_df)
            decision_funnel = build_decision_funnel(review_df, replay_decisions)
            symbol_audit = build_symbol_audit(
                review_df,
                replay_decisions,
                replay_trades,
                actual_trades,
            )
            forensics = build_trade_forensics(actual_trades, review_df, replay_decisions)
            overview = xray_overview(actual_trades, replay_trades, rejection_quality)

            llm_insights = None
            if use_llm:
                llm_payload = {
                    **summary,
                    "actual_vs_revised_replay": overview,
                    "revised_rejection_quality": rejection_quality.to_dict(orient="records"),
                    "symbol_audit": symbol_audit.to_dict(orient="records"),
                    "replay_summary": replay_summary,
                    "instruction": (
                        "Prioritize actual P&L, replay P&L, rejection precision, and trade forensics. "
                        "Treat raw missed-signal value as overlapping diagnostic evidence, not realizable profit."
                    ),
                }
                try:
                    llm_insights = ask_llm_for_insights(llm_payload)
                except Exception as error:
                    llm_insights = f"LLM review unavailable: {error}"
                llm_file.write_text(llm_insights)

            st.session_state["post_market_xray_date"] = review_date_text
            st.session_state["post_market_review_df"] = review_df
            st.session_state["post_market_summary"] = summary
            st.session_state["post_market_llm_insights"] = llm_insights
            st.session_state["post_market_replay_decisions"] = replay_decisions
            st.session_state["post_market_replay_trades"] = replay_trades
            st.session_state["post_market_replay_summary"] = replay_summary
            st.session_state["post_market_rejection_quality"] = rejection_quality
            st.session_state["post_market_decision_funnel"] = decision_funnel
            st.session_state["post_market_symbol_audit"] = symbol_audit
            st.session_state["post_market_forensics"] = forensics
            st.session_state["post_market_overview"] = overview
            st.session_state["post_market_replay_mode"] = replay_mode
            st.session_state["post_market_replay_error"] = replay_error
            st.session_state["post_market_files"] = {
                "csv": str(csv_file),
                "summary": str(summary_file),
                "llm": str(llm_file) if use_llm else None,
                "replay_decisions": str(replay_files[0]),
                "replay_trades": str(replay_files[1]),
                "replay_summary": str(replay_files[2]),
            }

    if st.session_state.get("post_market_xray_date") != review_date_text:
        st.info("Run the market debrief to build this date's evidence pack.")
        return

    summary = st.session_state.get("post_market_summary")
    review_df = st.session_state.get("post_market_review_df")
    llm_insights = st.session_state.get("post_market_llm_insights")
    replay_decisions = st.session_state.get("post_market_replay_decisions", pd.DataFrame())
    replay_trades = st.session_state.get("post_market_replay_trades", pd.DataFrame())
    replay_summary = st.session_state.get("post_market_replay_summary", {})
    rejection_quality = st.session_state.get("post_market_rejection_quality", pd.DataFrame())
    decision_funnel = st.session_state.get("post_market_decision_funnel", pd.DataFrame())
    symbol_audit = st.session_state.get("post_market_symbol_audit", pd.DataFrame())
    forensics = st.session_state.get("post_market_forensics", pd.DataFrame())
    overview = st.session_state.get("post_market_overview", {})
    replay_mode = st.session_state.get("post_market_replay_mode", "N/A")
    replay_error = st.session_state.get("post_market_replay_error")
    files = st.session_state.get("post_market_files", {})

    if not summary:
        st.info("Run the market debrief after market close to generate the review.")
        return

    actual_pnl = float(overview.get("actual_pnl", 0) or 0)
    replay_pnl = float(overview.get("replay_pnl", 0) or 0)
    pnl_delta = float(overview.get("pnl_delta", 0) or 0)
    if pnl_delta > 0:
        verdict_title = "Revised rules reduced simulated loss exposure"
        verdict_copy = f"The conservative replay improved the day's gross result by {money(pnl_delta)} versus the actual trades."
    elif pnl_delta < 0:
        verdict_title = "Actual execution outperformed the revised replay"
        verdict_copy = f"The revised replay trailed actual gross P&L by {money(abs(pnl_delta))}. Keep the comparison under observation before changing rules."
    else:
        verdict_title = "No measurable replay advantage for this date"
        verdict_copy = "Actual and revised replay results are equal, or the replay produced no executable setup."

    st.markdown(
        f'<div class="xray-verdict"><div class="xray-verdict-title">{verdict_title}</div>'
        f'<div class="xray-verdict-copy">{verdict_copy}</div></div>',
        unsafe_allow_html=True,
    )
    st.caption(
        f"Review: {summary.get('review_mode', 'N/A')} | Replay: {replay_mode}. "
        "Replay values exclude brokerage, taxes, spread, slippage, and market impact."
    )
    if replay_error:
        st.warning(f"Fresh replay was unavailable; saved replay evidence is shown. Reason: {replay_error}")

    brief_tab, audit_tab, forensic_tab, replay_tab = st.tabs(
        ["Daily Brief", "Decision Audit", "Trade Forensics", "Replay"]
    )

    with brief_tab:
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Actual Gross P&L", money(actual_pnl))
        c2.metric("Revised Replay P&L", money(replay_pnl))
        c3.metric("Replay Delta", money(pnl_delta))
        c4.metric(
            "Trades Actual / Replay",
            f"{overview.get('actual_trades', 0)} / {overview.get('replay_trades', 0)}",
        )
        precision = overview.get("rejection_precision_pct")
        c5.metric("Rejection Precision", f"{precision:.1f}%" if precision is not None else "N/A")

        st.markdown("### Decision Funnel")
        st.markdown('<div class="xray-section-note">How many checks survived each revised-rule gate.</div>', unsafe_allow_html=True)
        if not decision_funnel.empty:
            funnel = decision_funnel.copy()
            first_count = float(funnel.iloc[0]["count"] or 0)
            funnel["share_of_checks"] = funnel["count"].apply(
                lambda value: f"{(float(value) / first_count) * 100:.1f}%" if first_count else "0.0%"
            )
            st.dataframe(funnel, use_container_width=True, hide_index=True)

        st.markdown("### Symbol Comparison")
        if not symbol_audit.empty:
            st.dataframe(symbol_audit, use_container_width=True, hide_index=True)

        st.markdown("### Evidence Quality")
        candle_errors = 0
        if review_df is not None and not review_df.empty and "candle_fetch_error" in review_df.columns:
            errors = review_df["candle_fetch_error"].fillna("").astype(str).str.strip()
            candle_errors = int(errors.ne("").sum())
        matched_trades = int(forensics.get("analysis_matched", pd.Series(dtype=bool)).fillna(False).sum()) if not forensics.empty else 0
        q1, q2, q3, q4 = st.columns(4)
        q1.metric("Analysis Rows", summary.get("total_analysis_rows", 0))
        q2.metric("Replay Decisions", len(replay_decisions))
        q3.metric("Candle Errors", candle_errors)
        q4.metric("Trades Matched", f"{matched_trades}/{len(forensics)}")

        if llm_insights:
            st.markdown("### Independent Review")
            st.write(llm_insights)

    with audit_tab:
        st.markdown("### Rejection Quality Matrix")
        st.markdown(
            '<div class="xray-section-note">Correct rejects versus missed winners by revised rule. Precision excludes no-clear-edge observations.</div>',
            unsafe_allow_html=True,
        )
        if rejection_quality.empty:
            st.info("No revised rejection evidence is available.")
        else:
            display_quality = rejection_quality.rename(
                columns={
                    "rule": "Rule",
                    "checks": "Checks",
                    "correct_rejects": "Correct Rejects",
                    "missed_winners": "Missed Winners",
                    "no_clear_edge": "No Clear Edge",
                    "rejection_precision_pct": "Precision %",
                    "raw_missed_signal_value": "Raw Missed Signal Value",
                }
            )
            st.dataframe(display_quality, use_container_width=True, hide_index=True)
            chart = rejection_quality.set_index("rule")[["correct_rejects", "missed_winners", "no_clear_edge"]]
            st.bar_chart(chart)

        outcome_counts = summary.get("outcome_counts", {})
        if outcome_counts:
            st.markdown("### Observed Signal Outcomes")
            st.dataframe(
                pd.DataFrame([{"Outcome": key, "Count": value} for key, value in outcome_counts.items()]),
                use_container_width=True,
                hide_index=True,
            )

        raw_missed = summary.get("missed_expected_profit_total", 0)
        st.info(
            f"Raw missed signal value: {money(raw_missed)}. This is a diagnostic total across overlapping signals "
            "using the review's assumed quantity; it is not realizable portfolio profit."
        )
        enriched = enrich_replay_decisions(replay_decisions, review_df)
        if not enriched.empty:
            with st.expander("Full Revised Decision Ledger"):
                st.dataframe(enriched, use_container_width=True, hide_index=True)

    with forensic_tab:
        st.markdown("### Actual Trade Forensics")
        st.markdown(
            '<div class="xray-section-note">Each closed trade matched to its entry evidence and the revised rule verdict.</div>',
            unsafe_allow_html=True,
        )
        render_xray_trade_forensics(forensics)

    with replay_tab:
        st.markdown("### Revised-Rule Counterfactual")
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("Simulated Trades", replay_summary.get("simulated_trades", len(replay_trades)))
        r2.metric("Wins / Losses", f"{replay_summary.get('wins', 0)} / {replay_summary.get('losses', 0)}")
        r3.metric("Win Rate", f"{float(replay_summary.get('win_percent', 0) or 0):.1f}%")
        r4.metric("Estimated Gross P&L", money(replay_summary.get("estimated_gross_pnl", replay_pnl)))

        if replay_trades.empty:
            st.info("No trades qualified under the revised rules for this date.")
        else:
            st.dataframe(replay_trades, use_container_width=True, hide_index=True)

        if not replay_decisions.empty:
            st.markdown("### Replay Decision Mix")
            decision_mix = replay_decisions["decision"].fillna("UNKNOWN").value_counts()
            st.bar_chart(decision_mix)

        assumptions = replay_summary.get("assumptions", [])
        if assumptions:
            with st.expander("Replay Assumptions"):
                for assumption in assumptions:
                    st.write(f"- {assumption}")

    if review_df is not None and not review_df.empty:
        with st.expander("Full Original Review Data"):
            st.dataframe(review_df, use_container_width=True, hide_index=True)

    with st.expander("Generated Evidence Files"):
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


def render_score_followthrough_review():
    st.markdown("### Score Follow-Through")
    st.caption(
        "One non-overlapping observation per index every 15 minutes. Movement is measured "
        "from the scan minute through the following 15 minutes; it is research evidence, "
        "not an automatic reason to loosen live gates."
    )

    audit = read_score_audit()
    status = read_backtest_json(SCORE_AUDIT_STATUS_FILE, {})
    if audit.empty:
        st.info(
            "No evening score audit is available yet. The first completed trading-day run "
            "will create it automatically."
        )
        if status:
            st.caption(f"Last audit: {status.get('status', 'UNKNOWN')} | {status.get('message', '')}")
        return

    audit = audit.copy()
    audit["trading_date_dt"] = pd.to_datetime(audit["trading_date"], errors="coerce")
    audit = audit.dropna(subset=["trading_date_dt"])
    minimum_date = audit["trading_date_dt"].min().date()
    maximum_date = audit["trading_date_dt"].max().date()

    f1, f2, f3 = st.columns([1.2, 1.2, 1])
    start_date = f1.date_input(
        "From date",
        value=max(minimum_date, maximum_date - pd.Timedelta(days=30)),
        min_value=minimum_date,
        max_value=maximum_date,
        key="score_audit_start_date",
    )
    end_date = f2.date_input(
        "To date",
        value=maximum_date,
        min_value=minimum_date,
        max_value=maximum_date,
        key="score_audit_end_date",
    )
    minimum_samples = f3.number_input(
        "Usable sample size",
        min_value=5,
        max_value=100,
        value=20,
        step=5,
        key="score_audit_minimum_samples",
    )

    if start_date > end_date:
        st.warning("From date must be before or equal to To date.")
        return

    filtered = audit[
        audit["trading_date_dt"].dt.date.between(start_date, end_date)
    ].copy()
    directional = filtered[filtered["direction"].isin(["BULLISH", "BEARISH"])]
    high_score = directional[pd.to_numeric(directional["score"], errors="coerce") >= 75]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Observations", len(filtered))
    c2.metric("Trading Days", filtered["trading_date"].nunique())
    c3.metric(
        "75+ Avg Favorable",
        f"{pd.to_numeric(high_score.get('favorable_points'), errors='coerce').mean():.1f} pts"
        if not high_score.empty
        else "N/A",
    )
    direction_values = high_score.get("direction_correct", pd.Series(dtype=object))
    direction_rate = direction_values.astype(str).str.lower().map({"true": 1, "false": 0}).mean()
    c4.metric(
        "75+ Direction Right",
        f"{direction_rate * 100:.1f}%" if pd.notna(direction_rate) else "N/A",
    )

    summary = build_score_bucket_summary(filtered, minimum_samples=minimum_samples)
    if summary.empty:
        st.info("No observations match this date range.")
        return

    metrics = [
        "samples",
        "avg_up_points",
        "avg_down_points",
        "avg_favorable_points",
        "avg_adverse_points",
        "direction_accuracy",
        "evidence",
    ]
    wide = summary.pivot(index="score_bucket", columns="symbol", values=metrics)
    wide = wide.reindex(SCORE_BUCKETS)
    wide.columns = [f"{symbol} {metric.replace('_', ' ').title()}" for metric, symbol in wide.columns]
    wide = wide.reset_index().rename(columns={"score_bucket": "Score Bucket"})
    numeric_columns = [
        column
        for column in wide.columns
        if any(term in column for term in ("Avg ", "Accuracy"))
    ]
    for column in numeric_columns:
        wide[column] = pd.to_numeric(wide[column], errors="coerce").round(1)

    st.markdown("#### NIFTY and BANKNIFTY by Score Bucket")
    st.dataframe(wide, use_container_width=True, hide_index=True)
    st.caption(
        f"BUILDING means fewer than {int(minimum_samples)} observations. Do not change live "
        "thresholds from a BUILDING row."
    )

    reason_summary = build_score_reason_summary(filtered, minimum_samples=minimum_samples)
    if not reason_summary.empty:
        st.markdown("#### Rejection Reason Follow-Through")
        reason_summary = reason_summary.rename(
            columns={
                "symbol": "Index",
                "reason_category": "Reason",
                "samples": "Samples",
                "avg_favorable_points": "Avg Favorable Points",
                "avg_adverse_points": "Avg Adverse Points",
                "evidence": "Evidence",
            }
        )
        st.dataframe(reason_summary, use_container_width=True, hide_index=True)

    with st.expander("Observation ledger"):
        columns = [
            "trading_date",
            "signal_time",
            "symbol",
            "score",
            "score_bucket",
            "action",
            "direction",
            "reason_category",
            "reason",
            "reference_price",
            "up_points",
            "down_points",
            "favorable_points",
            "adverse_points",
            "direction_correct",
        ]
        st.dataframe(
            filtered[[column for column in columns if column in filtered.columns]].sort_values(
                "signal_time", ascending=False
            ),
            use_container_width=True,
            hide_index=True,
        )

    if status:
        st.caption(
            f"Last evening audit: {status.get('status', 'UNKNOWN')} | "
            f"{status.get('message', '')} | {str(status.get('updated_at', ''))[:19]}"
        )


def read_backtest_json(path, default):
    try:
        return json.loads(Path(path).read_text()) if Path(path).exists() else default
    except Exception:
        return default


def render_strategy_replay_tab():
    st.markdown('<div class="dash-title">Strategy Replay Lab</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="dash-subtitle">Offline one-month replay using Vamsi Upstox Plus historical candles. No live orders are placed.</div>',
        unsafe_allow_html=True,
    )
    status = read_backtest_json(BACKTEST_STATUS_FILE, {})
    latest = read_backtest_json(BACKTEST_LATEST_FILE, {})
    running = status.get("status") in {"STARTING", "RUNNING"}
    c1, c2, c3 = st.columns(3)
    c1.metric("Replay Status", status.get("status", "NOT RUN"))
    c2.metric("Coverage", status.get("message", "No replay run yet"))
    c3.metric("Latest Run", latest.get("completed_at", "N/A")[:19] if latest.get("completed_at") else "N/A")

    st.caption(
        "Live mode models one account position at a time. Independent mode is a diagnostic view: "
        "it evaluates NIFTY, BANKNIFTY, and stock-futures tracks separately, so its category totals must not be added together."
    )
    mode = st.radio("Replay view", ["live", "independent"], horizontal=True, index=0)
    include_stocks = st.checkbox("Include NIFTY 50 stock-futures fallback", value=True)

    if st.button("Run One-Month Strategy Replay", type="primary", disabled=running, use_container_width=True):
        BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
        log_path = BACKTEST_DIR / "replay_process.log"
        command = [sys.executable, str(BASE_DIR / "run_strategy_replay.py"), "--days", "22", "--portfolio-mode", mode]
        if not include_stocks:
            command.append("--no-stock-futures")
        with log_path.open("a") as handle:
            process = subprocess.Popen(
                command,
                cwd=str(BASE_DIR),
                stdout=handle,
                stderr=subprocess.STDOUT,
                env=os.environ.copy(),
                start_new_session=True,
            )
        st.session_state["backtest_pid"] = process.pid
        st.success(f"Replay started (process {process.pid}). Refresh this tab to follow progress.")
        st.rerun()

    if status.get("status") == "FAILED":
        st.error(status.get("message", "Replay failed"))
    if status.get("status") == "COMPLETE" and latest.get("summary"):
        summary = latest["summary"]
        overall = summary.get("overall", {})
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Trades", overall.get("trades", 0))
        m2.metric("Win Rate", f"{overall.get('win_percent', 0):.1f}%")
        m3.metric("Net P&L", money(overall.get("net_pnl", 0)))
        m4.metric("Max Drawdown", money(overall.get("max_drawdown", 0)))
        run_path = Path(latest.get("path", ""))
        category_path = run_path / "category_summary.csv"
        daily_path = run_path / "daily_summary.csv"
        category_daily_path = run_path / "category_daily_summary.csv"
        if category_path.exists():
            st.markdown("### Strategy Category Summary")
            st.dataframe(pd.read_csv(category_path), use_container_width=True, hide_index=True)
        if daily_path.exists():
            daily = pd.read_csv(daily_path)
            if not daily.empty:
                st.markdown("### Portfolio Daily P&L")
                st.line_chart(daily.set_index("trade_date")["cumulative_net_pnl"])
        if category_daily_path.exists():
            category_daily = pd.read_csv(category_daily_path)
            if not category_daily.empty:
                st.markdown("### Category Cumulative P&L")
                chart = category_daily.pivot(index="trade_date", columns="category", values="cumulative_net_pnl").ffill()
                st.line_chart(chart)
        st.caption(f"Files: {run_path}")

    if st.button("Refresh Replay Status", use_container_width=True):
        st.rerun()


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
        if pd.isna(value):
            return "N/A"
        return f"₹{float(value):,.2f}"
    except Exception:
        return "N/A"


def pnl_class(value):
    try:
        return "positive" if float(value or 0) >= 0 else "negative"
    except Exception:
        return ""


def metric_card(label, value, value_class=""):
    safe_label = html.escape(str(label))
    safe_value = html.escape(str(value))
    class_text = f"metric-value {value_class}".strip()
    st.markdown(
        f"""
        <div class="metric-card">
            <div class="metric-label">{safe_label}</div>
            <div class="{class_text}">{safe_value}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def section_header(title):
    st.markdown(
        f'<div class="navy-section">{html.escape(str(title))}</div>',
        unsafe_allow_html=True,
    )


def dashboard_trades_for_summary():
    return dashboard_index_trades(read_trade_history())


def symbol_summary(trades, symbol):
    matching = [
        trade
        for trade in trades
        if normalized_underlying(trade) == symbol
    ]
    pnl_values = [float(trade.get("grossPnL") or 0) for trade in matching]
    wins = [value for value in pnl_values if value > 0]
    losses = [value for value in pnl_values if value < 0]
    return {
        "trades": len(matching),
        "net_pnl": round(sum(pnl_values), 2),
        "avg_profit": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
    }


def option_totals_chart_data(trades):
    totals = {
        symbol: {"CALL": 0.0, "PUT": 0.0}
        for symbol in SYMBOLS
    }
    for trade in trades:
        symbol = normalized_underlying(trade)
        kind = option_type(trade)
        if symbol in totals and kind in totals[symbol]:
            totals[symbol][kind] += float(trade.get("grossPnL") or 0)
    return pd.DataFrame(
        [
            {
                "Symbol": symbol,
                "CALL": round(values["CALL"], 2),
                "PUT": round(values["PUT"], 2),
            }
            for symbol, values in totals.items()
        ]
    ).set_index("Symbol")


def render_option_totals_mini_chart(trades):
    values = option_totals_chart_data(trades).to_dict("index")
    max_value = max(
        [
            abs(float(group.get(option, 0) or 0))
            for group in values.values()
            for option in ("CALL", "PUT")
        ]
        or [1.0]
    )
    max_value = max(max_value, 1.0)
    cards = []
    for symbol in SYMBOLS:
        group = values.get(symbol, {})
        bars = []
        for option in ("CALL", "PUT"):
            pnl = float(group.get(option, 0) or 0)
            height = max(6, int(abs(pnl) / max_value * 100))
            bars.append(
                f"""
                <div class="mini-bar-wrap">
                    <div class="mini-bar-value {pnl_class(pnl)}">{money(pnl)}</div>
                    <div class="mini-bar {option.lower()}" style="height:{height}px;"></div>
                    <div class="mini-bar-label">{option}</div>
                </div>
                """
            )
        cards.append(
            f"""
            <div class="mini-chart-card">
                <div class="mini-chart-title">{symbol}</div>
                <div class="mini-bars">{''.join(bars)}</div>
            </div>
            """
        )
    st.markdown(
        f'<div class="mini-chart-grid">{"".join(cards)}</div>',
        unsafe_allow_html=True,
    )


def number(value):
    try:
        if pd.isna(value):
            return "N/A"
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


def position_avg_price(position, entry_transaction_type="BUY"):
    keys = ["average_price", "avg_price"]
    if str(entry_transaction_type).upper() == "SELL":
        keys.extend(["sell_price", "day_sell_price"])
    else:
        keys.extend(["buy_price", "day_buy_price"])
    for key in keys:
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

    for symbol in STATE_SLOTS:
        state = read_json(state_file(symbol), {})
        if not state or not state.get("instrument_key"):
            continue

        pos = find_position_by_instrument(positions, state.get("instrument_key"))
        qty = int(float(state.get("quantity") or 0))
        entry = float(state.get("entry_price") or 0)
        target = float(state.get("target_price") or 0)
        stop = float(state.get("stop_loss_price") or 0)
        highest = float(state.get("highest_ltp") or entry or 0)
        lowest = float(state.get("lowest_ltp") or entry or 0)
        entry_transaction = str(state.get("entry_transaction_type") or "BUY").upper()
        is_short = entry_transaction == "SELL"

        ltp = None
        actual_qty = 0

        if pos:
            actual_qty = position_quantity(pos)
            ltp = position_ltp(pos)
            broker_entry = position_avg_price(pos, entry_transaction)
            if broker_entry:
                entry = broker_entry

        live_pnl = None
        target_progress = None
        risk_to_stop = None
        reward_left = None

        if ltp is not None and entry > 0:
            live_pnl = round(((entry - ltp) if is_short else (ltp - entry)) * qty, 2)

        valid_target = target < entry if is_short else target > entry
        if ltp is not None and valid_target:
            target_progress = round(
                (((entry - ltp) / (entry - target)) if is_short else ((ltp - entry) / (target - entry))) * 100,
                1,
            )

        if ltp is not None and stop > 0:
            risk_to_stop = round(((stop - ltp) if is_short else (ltp - stop)) * qty, 2)

        if ltp is not None and target > 0:
            reward_left = round(((ltp - target) if is_short else (target - ltp)) * qty, 2)

        rows.append(
            {
                "symbol": symbol,
                "underlying_symbol": state.get("underlying_symbol", symbol),
                "instrument_class": state.get("instrument_class", "INDEX_OPTION"),
                "trading_symbol": state.get("trading_symbol", ""),
                "state_status": state.get("status", "OPEN"),
                "quantity": qty,
                "broker_quantity": actual_qty,
                "entry_price": entry,
                "ltp": ltp,
                "target_price": target,
                "stop_loss_price": stop,
                "highest_ltp": highest,
                "lowest_ltp": lowest,
                "entry_transaction_type": entry_transaction,
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
<div class="small-label">{row.get("symbol", "")} • {row.get("entry_transaction_type", "BUY")}</div>
<div class="big-value">{row.get("trading_symbol", "")}</div>
<div class="muted">Qty: {row.get("quantity", "")} | Broker Qty: {row.get("broker_quantity", "")}</div>
<br>
<div class="small-label">Live P&L</div>
<div class="big-value {pnl_class}">{money(pnl)}</div>
<br>
<div class="small-label">Trade Levels</div>
<div>Entry: <b>{number(row.get("entry_price"))}</b> | LTP: <b>{number(row.get("ltp"))}</b> | Target: <b>{number(row.get("target_price"))}</b></div>
<div>Stop Loss: <b>{number(row.get("stop_loss_price"))}</b> | Best LTP: <b>{number(row.get("lowest_ltp") if row.get("entry_transaction_type") == "SELL" else row.get("highest_ltp"))}</b></div>
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


def render_trade_forensics_dashboard():
    st.markdown('<div class="dash-title">Trade Forensics</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="dash-subtitle">Peak unrealized P&L, adverse movement, post-exit path, and rejected-signal outcomes</div>',
        unsafe_allow_html=True,
    )

    controls = st.columns([1.4, 1, 1])
    review_date = controls[0].date_input(
        "Trading date",
        value=now_ist().date(),
        key="forensic_review_date",
    )
    forward_minutes = controls[1].selectbox(
        "Rejected-signal window",
        options=[30, 60, 90],
        index=0,
        key="forensic_forward_minutes",
    )
    post_exit_minutes = controls[2].selectbox(
        "Post-exit window",
        options=[30, 60, 90],
        index=0,
        key="forensic_post_exit_minutes",
    )
    date_text = review_date.strftime("%Y-%m-%d")
    if review_date == now_ist().date() and now_ist().time() < POST_MARKET_REVIEW_TIME:
        st.info(
            "Today’s report is provisional while the market is active. Run it again after 3:30 PM IST for the complete path."
        )

    executed_file = DATA_DIR / f"executed_trade_forensics_{date_text}.csv"
    rejected_file = DATA_DIR / f"rejected_signal_forensics_{date_text}.csv"
    summary_file = DATA_DIR / f"trade_forensics_summary_{date_text}.json"

    if st.button(
        "Build Trade Forensic Report",
        type="primary",
        use_container_width=True,
        key="run_trade_forensics",
    ):
        with st.spinner("Reconstructing trades and rejected signals from Upstox candles..."):
            try:
                instruments = load_forensic_instruments()
                by_symbol, by_key = forensic_instrument_lookup(instruments)
                candle_cache = {}
                trades = read_forensic_trades(date_text)
                rejected_signals = read_forensic_rejected_signals(date_text)
                forward_candles = max(int(forward_minutes / 5), 1)
                post_exit_candles = max(int(post_exit_minutes / 5), 1)
                executed = run_executed_trade_forensics(
                    trades,
                    by_symbol,
                    by_key,
                    candle_cache,
                    post_exit_candles,
                )
                rejected = run_rejected_signal_forensics(
                    rejected_signals,
                    by_symbol,
                    by_key,
                    candle_cache,
                    forward_candles,
                )
                summary = build_forensic_summary(
                    date_text,
                    executed,
                    rejected,
                    forward_candles,
                    post_exit_candles,
                )
                analysis = read_analysis_rows(ANALYSIS_HISTORY_FILE, date_text)
                insight_report = build_session_insights(
                    date_text,
                    analysis,
                    executed,
                    rejected,
                    read_log_lines(LOG_FILE, date_text),
                    os.environ,
                )
                DATA_DIR.mkdir(exist_ok=True)
                executed.to_csv(executed_file, index=False)
                rejected.to_csv(rejected_file, index=False)
                summary_file.write_text(
                    json.dumps(summary, indent=2, sort_keys=True, default=str)
                )
                insight_file = DATA_DIR / f"session_insights_{date_text}.json"
                insight_file.write_text(
                    json.dumps(insight_report, indent=2, sort_keys=True, default=str)
                )
                st.session_state["forensic_report_date"] = date_text
                st.session_state["forensic_executed"] = executed
                st.session_state["forensic_rejected"] = rejected
                st.session_state["forensic_summary"] = summary
                st.session_state["forensic_insights"] = insight_report
            except Exception as error:
                st.error(f"Trade forensic report could not be generated: {error}")

    if st.session_state.get("forensic_report_date") == date_text:
        executed = st.session_state.get("forensic_executed", pd.DataFrame())
        rejected = st.session_state.get("forensic_rejected", pd.DataFrame())
        summary = st.session_state.get("forensic_summary", {})
        insights = st.session_state.get("forensic_insights", {})
    elif executed_file.exists() or rejected_file.exists() or summary_file.exists():
        executed = read_csv_if_present(executed_file)
        rejected = read_csv_if_present(rejected_file)
        summary = read_json(summary_file, {})
        insights = read_json(DATA_DIR / f"session_insights_{date_text}.json", {})
    else:
        st.info("Choose a date and build the report. Saved reports are reused automatically.")
        return

    outcomes = summary.get("rejected_forward_outcomes", {}) or {}
    metrics = st.columns(6)
    metrics[0].metric("Executed Trades", summary.get("executed_trades", 0))
    metrics[1].metric("Realized P&L", money(summary.get("realized_pnl", 0)))
    metrics[2].metric(
        "Peak Unrealized",
        money(summary.get("sum_peak_unrealized_pnl", 0)),
    )
    metrics[3].metric(
        "Profit Given Back",
        money(summary.get("sum_profit_given_back", 0)),
    )
    metrics[4].metric(
        "Rejected Analyzed",
        summary.get("rejected_directional_signals_analyzed", 0),
    )
    metrics[5].metric("Missed Winners", outcomes.get("MISSED_WINNER", 0))

    executed_errors = (
        executed.get("analysis_error", pd.Series(index=executed.index, dtype="object"))
        .fillna("")
        .astype(str)
        .str.strip()
    ) if not executed.empty else pd.Series(dtype="object")
    rejected_errors = (
        rejected.get("analysis_error", pd.Series(index=rejected.index, dtype="object"))
        .fillna("")
        .astype(str)
        .str.strip()
    ) if not rejected.empty else pd.Series(dtype="object")
    error_count = int(executed_errors.ne("").sum() + rejected_errors.ne("").sum())
    if error_count:
        st.warning(f"{error_count} row(s) could not be fully analyzed. See the detailed tables for the error text.")

    insight_tab, executed_tab, rejected_tab = st.tabs(
        ["Session Insights", "Executed Trades", "Rejected Signals"]
    )

    with insight_tab:
        if not insights:
            analysis = read_analysis_rows(ANALYSIS_HISTORY_FILE, date_text)
            insights = build_session_insights(
                date_text,
                analysis,
                executed,
                rejected,
                read_log_lines(LOG_FILE, date_text),
                os.environ,
            )

        st.markdown("### What The Session Evidence Says")
        for line in insights.get("narrative", []):
            st.markdown(
                f'<div class="insight-line">{html.escape(str(line))}</div>',
                unsafe_allow_html=True,
            )

        st.markdown("#### Review Priorities")
        for priority in insights.get("review_priorities", []):
            st.write(f"- {priority}")

        signal_mix = pd.DataFrame(insights.get("signal_mix", []))
        blocker_mix = pd.DataFrame(insights.get("blockers", []))
        left, right = st.columns(2)
        with left:
            st.markdown("#### Signal Regime")
            if signal_mix.empty:
                st.info("No saved signal mix was available.")
            else:
                st.dataframe(signal_mix, use_container_width=True, hide_index=True)
        with right:
            st.markdown("#### Dominant Blockers")
            if blocker_mix.empty:
                st.info("No blocker lines were found in the saved log.")
            else:
                st.dataframe(blocker_mix, use_container_width=True, hide_index=True)

        st.markdown("#### Effective Trade Controls")
        controls_frame = pd.DataFrame(
            [
                {"setting": key, "effective_value": value}
                for key, value in insights.get("controls", {}).items()
            ]
        )
        st.dataframe(controls_frame, use_container_width=True, hide_index=True)
        st.caption(str(insights.get("caveat") or ""))

    with executed_tab:
        st.markdown("### Executed Trade Path")
        st.markdown(
            '<div class="xray-section-note">Peak and adverse values are measured while the trade was open. Post-exit values use the selected future window.</div>',
            unsafe_allow_html=True,
        )
        if executed.empty:
            st.info("No closed trades were recorded for this date.")
        else:
            executed_columns = [
                "symbol",
                "trading_symbol",
                "realized_pnl",
                "max_favorable_pnl",
                "max_adverse_pnl",
                "max_favorable_points",
                "max_adverse_points",
                "profit_given_back_from_peak",
                "exit_reason",
                "post_exit_best_pnl_from_entry",
                "post_exit_planned_level_outcome",
                "recovered_to_entry_after_exit",
                "analysis_error",
            ]
            executed_columns = [column for column in executed_columns if column in executed.columns]
            st.dataframe(executed[executed_columns], use_container_width=True, hide_index=True)

    with rejected_tab:
        st.markdown("### Rejected Signal Follow-Through")
        st.markdown(
            '<div class="xray-section-note">Each saved directional rejection is followed through for the selected number of five-minute candles. Repeated checks can overlap and are not independent trades.</div>',
            unsafe_allow_html=True,
        )
        if rejected.empty:
            st.info("No saved directional rejected signals were available for this date.")
        else:
            rejected_columns = [
                "signal_time",
                "symbol",
                "direction",
                "trading_symbol",
                "weighted_score",
                "forward_outcome",
                "max_favorable_pnl",
                "max_adverse_pnl",
                "max_favorable_points",
                "max_adverse_points",
                "rejection_reason",
                "analysis_error",
            ]
            rejected_columns = [column for column in rejected_columns if column in rejected.columns]
            st.dataframe(rejected[rejected_columns], use_container_width=True, hide_index=True)
            if "forward_outcome" in rejected.columns:
                outcome_chart = rejected["forward_outcome"].fillna("UNKNOWN").value_counts()
                st.bar_chart(outcome_chart)

    st.caption(str(summary.get("caveat") or ""))
    with st.expander("Generated report files"):
        st.write(
            {
                "executed_trades": str(executed_file),
                "rejected_signals": str(rejected_file),
                "summary": str(summary_file),
                "session_insights": str(DATA_DIR / f"session_insights_{date_text}.json"),
            }
        )


def render_banknifty_post_market():
    st.markdown('<div class="dash-title">BANKNIFTY Post Market</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="dash-subtitle">Did low option-chain confidence protect us, or suppress technically strong BANKNIFTY opportunities?</div>',
        unsafe_allow_html=True,
    )
    st.info(
        "Research only. This rebuilds evidence from completed candles and does not alter the live option-chain gate."
    )

    controls = st.columns([1.4, 1, 1])
    audit_date = controls[0].date_input(
        "Trading date",
        value=now_ist().date(),
        max_value=now_ist().date(),
        key="banknifty_veto_date",
    )
    forward_minutes = controls[1].selectbox(
        "Follow-through window",
        options=[15, 30, 45, 60],
        index=1,
        format_func=lambda value: f"{value} minutes",
        key="banknifty_veto_forward_minutes",
    )
    minimum_score = controls[2].slider(
        "Strong technical score",
        min_value=50,
        max_value=90,
        value=70,
        step=5,
        key="banknifty_veto_minimum_score",
    )
    date_text = audit_date.isoformat()
    observations_file = DATA_DIR / f"banknifty_veto_observations_{date_text}.csv"
    episodes_file = DATA_DIR / f"banknifty_veto_episodes_{date_text}.csv"
    summary_file = DATA_DIR / f"banknifty_veto_summary_{date_text}.json"

    if st.button(
        "Run BANKNIFTY Veto Audit",
        type="primary",
        use_container_width=True,
        key="run_banknifty_veto_audit",
    ):
        with st.spinner("Rebuilding BANKNIFTY evidence and following the rejected setups..."):
            try:
                observations, episodes, summary = run_banknifty_veto_audit(
                    date_text,
                    forward_candles=max(int(forward_minutes / 5), 1),
                    minimum_score=minimum_score,
                )
                DATA_DIR.mkdir(exist_ok=True)
                observations.to_csv(observations_file, index=False)
                episodes.to_csv(episodes_file, index=False)
                summary_file.write_text(
                    json.dumps(summary, indent=2, sort_keys=True, default=str)
                )
                st.session_state["banknifty_veto_report_date"] = date_text
                st.session_state["banknifty_veto_observations"] = observations
                st.session_state["banknifty_veto_episodes"] = episodes
                st.session_state["banknifty_veto_summary"] = summary
            except Exception as error:
                st.error(f"BANKNIFTY post-market audit could not be generated: {error}")

    if st.session_state.get("banknifty_veto_report_date") == date_text:
        observations = st.session_state.get("banknifty_veto_observations", pd.DataFrame())
        episodes = st.session_state.get("banknifty_veto_episodes", pd.DataFrame())
        summary = st.session_state.get("banknifty_veto_summary", {})
    elif summary_file.exists():
        observations = read_csv_if_present(observations_file)
        episodes = read_csv_if_present(episodes_file)
        summary = read_json(summary_file, {})
    else:
        st.caption("Select the date after market hours and run the audit.")
        return

    if summary.get("message"):
        st.warning(summary["message"])
        return

    metrics = st.columns(6)
    metrics[0].metric("Option-Chain Vetoes", summary.get("veto_checks", 0))
    metrics[1].metric("Technical Direction", summary.get("technically_directional_checks", 0))
    metrics[2].metric("Strong Checks", summary.get("strong_raw_checks", 0))
    metrics[3].metric("Independent Episodes", summary.get("independent_episodes", 0))
    metrics[4].metric("Episode Win %", f"{summary.get('episode_win_percent', 0):.1f}%")
    metrics[5].metric("One-Lot Hypothetical", money(summary.get("one_lot_hypothetical_pnl", 0)))

    outcome_metrics = st.columns(4)
    outcome_metrics[0].metric("Target First", summary.get("target_first", 0))
    outcome_metrics[1].metric(
        "Stop First / Ambiguous", summary.get("stop_first_or_ambiguous", 0)
    )
    outcome_metrics[2].metric("No Clear Edge", summary.get("no_edge_episodes", 0))
    outcome_metrics[3].metric(
        "Underlying Direction Right",
        f"{summary.get('underlying_direction_accuracy', 0):.1f}%",
        help="Whether BANKNIFTY itself finished the selected window in the reconstructed technical direction.",
    )

    verdict = str(summary.get("verdict") or "NO_VERDICT").replace("_", " ").title()
    st.markdown("### Evidence Verdict")
    if summary.get("verdict") == "OPTION_CHAIN_VETO_MAY_BE_TOO_STRICT_FOR_STRONG_TECHNICAL_SETUPS":
        st.success(verdict)
    elif summary.get("verdict") == "OPTION_CHAIN_VETO_WAS_PROTECTIVE_OR_TECHNICAL_EDGE_WAS_WEAK":
        st.warning(verdict)
    else:
        st.info(verdict)
    st.caption(
        f"Target {summary.get('target_index_points')} BANKNIFTY points | "
        f"Stop {summary.get('stop_index_points')} points | "
        f"Delta {summary.get('delta_approximation')} | "
        f"Forward window {summary.get('forward_five_minute_candles', 0) * 5} minutes"
    )

    episode_tab, score_tab, all_checks_tab = st.tabs(
        ["Independent Episodes", "Score Evidence", "All Rejected Checks"]
    )
    with episode_tab:
        st.markdown("#### Non-Overlapping Trade-Like Episodes")
        st.caption(
            "Repeated five-minute checks are collapsed while the prior hypothetical setup remains active. This is the table used for P&L and win rate."
        )
        if episodes.empty:
            st.info("No independent setup met the selected technical-score threshold.")
        else:
            columns = [
                "signal_time",
                "technical_direction",
                "technical_score",
                "trading_symbol",
                "option_type",
                "entry_price",
                "target_price",
                "stop_loss_price",
                "forward_outcome",
                "hypothetical_pnl",
                "max_favorable_pnl",
                "max_adverse_pnl",
                "underlying_signed_move_points",
                "option_flow_bias",
                "option_flow_volume_ratio",
            ]
            st.dataframe(
                episodes[[column for column in columns if column in episodes.columns]],
                use_container_width=True,
                hide_index=True,
            )

    with score_tab:
        st.markdown("#### What Happened At Each Technical-Score Band")
        valid = observations.copy()
        if not valid.empty:
            valid = valid[valid.get("analysis_error", "").fillna("").eq("")]
            valid["score_band"] = pd.cut(
                pd.to_numeric(valid["technical_score"], errors="coerce"),
                bins=[0, 49.999, 59.999, 69.999, 79.999, 89.999, 100],
                labels=["<50", "50-59", "60-69", "70-79", "80-89", "90-100"],
                include_lowest=True,
            )
        if valid.empty:
            st.info("No reconstructed technical observations were available.")
        else:
            score_summary = (
                valid.groupby("score_band", observed=True)
                .agg(
                    checks=("technical_score", "size"),
                    average_score=("technical_score", "mean"),
                    target_first=("forward_outcome", lambda values: int((values == "TARGET_FIRST").sum())),
                    stop_first=("forward_outcome", lambda values: int(values.isin({"STOP_FIRST", "AMBIGUOUS_SAME_CANDLE"}).sum())),
                    average_favorable_pnl=("max_favorable_pnl", "mean"),
                    average_adverse_pnl=("max_adverse_pnl", "mean"),
                )
                .reset_index()
            )
            score_summary["target_first_rate"] = (
                score_summary["target_first"]
                / (score_summary["target_first"] + score_summary["stop_first"]).replace(0, pd.NA)
                * 100
            ).round(1)
            st.dataframe(score_summary, use_container_width=True, hide_index=True)
            chart = score_summary.set_index("score_band")[["target_first", "stop_first"]]
            st.bar_chart(chart)

    with all_checks_tab:
        st.markdown("#### Every Non-HIGH Option-Chain Observation")
        st.caption(
            "These rows overlap and must not be added together as trades. They answer whether the subsequent direction and option-premium excursion supported the independent indicators."
        )
        if observations.empty:
            st.info("No observations were reconstructed.")
        else:
            columns = [
                "signal_time",
                "option_chain_confidence",
                "option_chain_score",
                "technical_direction",
                "technical_score",
                "five_min_bias",
                "five_min_momentum",
                "fifteen_min_bias",
                "two_hour_bias",
                "institutional_bias",
                "option_flow_bias",
                "forward_outcome",
                "hypothetical_pnl",
                "max_favorable_pnl",
                "max_adverse_pnl",
                "underlying_direction_correct",
                "analysis_error",
            ]
            st.dataframe(
                observations[[column for column in columns if column in observations.columns]],
                use_container_width=True,
                hide_index=True,
            )

    st.caption(str(summary.get("caveat") or ""))
    with st.expander("Generated report files"):
        st.write(
            {
                "all_observations": str(observations_file),
                "independent_episodes": str(episodes_file),
                "summary": str(summary_file),
            }
        )


load_env()

if st.button("Refresh Dashboard", use_container_width=True):
    st.rerun()

raw_performance = api_build_trade_performance()


def summary_card_html(title, summary, show_averages=False):
    net_pnl = float(summary.get("netPnL", summary.get("net_pnl", 0)) or 0)
    trades = int(summary.get("trades", 0) or 0)
    win_rate = float(summary.get("winRate", 0) or 0)
    charges = float(summary.get("otherCharges", 0) or 0)
    average_profit = float(summary.get("averageProfit", summary.get("avg_profit", 0)) or 0)
    average_loss = float(summary.get("averageLoss", summary.get("avg_loss", 0)) or 0)
    average_rows = ""
    if show_averages:
        average_rows = f"""
<div style="margin-top:8px;color:#334155;font-weight:700;">
Avg profit <span class="positive">{money(average_profit)}</span>
</div>
<div style="margin-top:4px;color:#334155;font-weight:700;">
Avg loss <span class="negative">{money(average_loss)}</span>
</div>"""
    return f"""
<div class="metric-card">
<div class="metric-label">{html.escape(title)}</div>
<div class="metric-value">{trades} trades</div>
<div class="{pnl_class(net_pnl)}" style="font-size:20px;margin-top:8px;">
Net P&amp;L {money(net_pnl)}
</div>
<div style="margin-top:8px;color:#334155;font-weight:700;">
Win rate {win_rate:.1f}%
</div>
<div style="margin-top:4px;color:#334155;font-weight:700;">
Other charges {money(charges)}
</div>
{average_rows}
</div>"""


def section_stats(payload, total_trades_key, total_pnl_key):
    overall = payload.get("overallStats") or {
        "trades": payload.get(total_trades_key, 0),
        "netPnL": payload.get("netPnL", payload.get(total_pnl_key, 0)),
        "otherCharges": payload.get("otherCharges", 0),
        "winRate": payload.get("winRate", 0),
        "averageProfit": payload.get("averageProfitPerWinningTrade", 0),
        "averageLoss": payload.get("averageLossPerLosingTrade", 0),
    }
    symbol_stats = payload.get("symbolStats") or {}
    return [("Overall", overall)] + [
        (symbol, symbol_stats.get(symbol, {"trades": 0, "netPnL": 0, "otherCharges": 0, "winRate": 0}))
        for symbol in SYMBOLS
    ]

def sequence_table(rows):
    frame = pd.DataFrame(rows or [])
    if frame.empty:
        return pd.DataFrame(
            columns=[
                "sequence",
                "trades",
                "netPnL",
                "winRate",
                "averageProfit",
                "averageLoss",
                "plannedRisk",
            ]
        )
    keep = [
        column
        for column in [
            "sequence",
            "priorOutcome",
            "trades",
            "netPnL",
            "winRate",
            "averageProfit",
            "averageLoss",
        ]
        if column in frame.columns
    ]
    frame = frame[keep].copy()
    rename = {
        "sequence": "Trade",
        "priorOutcome": "Prior Outcome",
        "trades": "Trades",
        "netPnL": "Net P&L",
        "winRate": "Win %",
        "averageProfit": "Avg Win",
        "averageLoss": "Avg Loss",
    }
    frame = frame.rename(columns=rename)
    for column in ["Net P&L", "Avg Win", "Avg Loss"]:
        if column in frame.columns:
            frame[column] = frame[column].map(money)
    if "Win %" in frame.columns:
        frame["Win %"] = frame["Win %"].map(lambda value: f"{float(value or 0):.1f}%")
    return frame


st.markdown('<div class="dash-title">Trading Bot Dashboard</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="dash-subtitle">NIFTY and BANKNIFTY performance and score follow-through research</div>',
    unsafe_allow_html=True,
)

performance_tab, score_review_tab = st.tabs(["Performance", "Post Market Review"])

with performance_tab:
    scale_mode = st.radio(
        "P&L scale",
        ["Per ₹1L", "Raw"],
        horizontal=True,
        index=0,
    )
    performance = (
        raw_performance.get("normalizedPerLakh", raw_performance)
        if scale_mode == "Per ₹1L"
        else raw_performance
    )
    today = performance.get("today", {})
    cumulative = performance.get("cumulative", {})
    t20 = performance.get("t20", {})

    section_header("Today")
    today_cols = st.columns(3)
    for column, (title, summary) in zip(
        today_cols,
        section_stats(today, "closedTrades", "closedPnL"),
    ):
        with column:
            st.markdown(summary_card_html(title, summary), unsafe_allow_html=True)

    section_header("Cumulative")
    summary_cols = st.columns(3)
    for column, (title, summary) in zip(
        summary_cols,
        section_stats(cumulative, "totalTrades", "totalPnL"),
    ):
        with column:
            st.markdown(
                summary_card_html(title, summary, show_averages=True),
                unsafe_allow_html=True,
            )

    section_header("T20 Cumulative")
    st.markdown(
        summary_card_html("T20 Overall", t20),
        unsafe_allow_html=True,
    )

    section_header("Index Trade Sequence")
    seq_today, seq_cumulative = st.columns(2)
    with seq_today:
        st.markdown("**Today**")
        st.dataframe(
            sequence_table(today.get("indexTradeSequencePerformance")),
            use_container_width=True,
            hide_index=True,
        )
    with seq_cumulative:
        st.markdown("**Cumulative**")
        st.dataframe(
            sequence_table(cumulative.get("indexTradeSequencePerformance")),
            use_container_width=True,
            hide_index=True,
        )

    second_context = sequence_table(cumulative.get("secondTradeContextPerformance"))
    if not second_context.empty:
        st.markdown("**Second Trade Context**")
        st.dataframe(second_context, use_container_width=True, hide_index=True)

with score_review_tab:
    render_score_followthrough_review()
