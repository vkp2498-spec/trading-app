"""Minimal index-options dashboard: headline P/L, calendar and scans."""

from __future__ import annotations

import calendar as calendar_module
import html
from datetime import date, datetime
from pathlib import Path

import streamlit as st

from dashboard_data import build_today_scans, build_trade_performance
from strategy_core import now_ist


BASE_DIR = Path(__file__).resolve().parent
APP_ICON = BASE_DIR / "assets" / "vamsi_icon_v2.jpg"

st.set_page_config(
    page_title="Index Options Trading",
    page_icon=str(APP_ICON) if APP_ICON.exists() else "📈",
    layout="wide",
)

st.markdown(
    """
    <style>
    .stApp { background: #f7f9fc; color: #10233f; }
    [data-testid="stHeader"] { background: rgba(247, 249, 252, 0.94); }
    .block-container { max-width: 1120px; padding-top: 2rem; padding-bottom: 3rem; }
    .page-title { color: #0b2343; font-size: 2.1rem; font-weight: 850; margin: 0; }
    .page-subtitle { color: #64748b; font-size: .93rem; margin: .35rem 0 1.5rem; }
    .summary-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
    .summary-card { background: #fff; border: 1px solid #dde5ef; border-radius: 14px; padding: 20px; box-shadow: 0 8px 24px rgba(15, 35, 64, .05); }
    .summary-label { color: #64748b; font-size: .75rem; font-weight: 800; letter-spacing: .06em; text-transform: uppercase; }
    .summary-value { color: #0b2343; font-size: 1.85rem; font-weight: 850; margin-top: 8px; }
    .summary-detail { color: #64748b; font-size: .76rem; margin-top: 5px; }
    .positive { color: #16834b !important; } .negative { color: #cf3f4f !important; }
    .section-title { color: #0b2343; font-size: 1.25rem; font-weight: 820; margin: 2rem 0 .25rem; }
    .section-subtitle { color: #64748b; font-size: .84rem; margin-bottom: .9rem; }
    .calendar-wrap { overflow-x: auto; padding-bottom: 6px; }
    .calendar-grid { display: grid; grid-template-columns: repeat(7, minmax(92px, 1fr)); gap: 7px; min-width: 720px; }
    .weekday { color: #738197; font-size: .68rem; font-weight: 800; padding: 4px; text-align: center; }
    .day { background: #fff; border: 1px solid #dde5ef; border-radius: 10px; min-height: 76px; padding: 9px; }
    .day.empty { background: transparent; border-color: transparent; }
    .day.profit { background: #eaf8f0; border-color: #a7dfbf; }
    .day.loss { background: #fff0f2; border-color: #f2b8c0; }
    .day-number { color: #64748b; font-size: .7rem; font-weight: 750; }
    .day-pnl { color: #0b2343; font-size: .88rem; font-weight: 850; margin-top: 12px; }
    .day-trades { color: #738197; font-size: .65rem; margin-top: 3px; }
    .scan-card { background: #fff; border: 1px solid #dde5ef; border-radius: 11px; margin-bottom: 9px; padding: 14px 16px; }
    .scan-head { align-items: center; display: flex; gap: 10px; justify-content: space-between; }
    .scan-time { color: #0b2343; font-size: .9rem; font-weight: 800; }
    .scan-badge { background: #e8eef6; border-radius: 999px; color: #334b69; font-size: .66rem; font-weight: 850; padding: 5px 9px; }
    .scan-badge.entered { background: #dff5e8; color: #13723e; }
    .scan-detail { color: #64748b; font-size: .78rem; margin-top: 8px; }
    .scan-meta { color: #334b69; font-size: .72rem; font-weight: 700; margin-top: 7px; }
    @media (max-width: 720px) {
        .summary-grid { grid-template-columns: 1fr; }
        .page-title { font-size: 1.75rem; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def money(value) -> str:
    amount = float(value or 0)
    sign = "-" if amount < 0 else ""
    return f"{sign}₹{abs(amount):,.2f}"


def value_class(value) -> str:
    amount = float(value or 0)
    return "positive" if amount > 0 else "negative" if amount < 0 else ""


def summary_card(label: str, value: str, detail: str = "", css_class: str = "") -> str:
    return (
        '<div class="summary-card">'
        f'<div class="summary-label">{html.escape(label)}</div>'
        f'<div class="summary-value {css_class}">{html.escape(value)}</div>'
        f'<div class="summary-detail">{html.escape(detail)}</div>'
        "</div>"
    )


def parse_calendar_days(days: list[dict]) -> list[dict]:
    parsed = []
    for item in days or []:
        try:
            trade_date = date.fromisoformat(str(item.get("date") or ""))
        except ValueError:
            continue
        parsed.append(
            {
                "date": trade_date,
                "pnl": float(item.get("netPnL", item.get("grossPnL", 0)) or 0),
                "trades": int(item.get("trades", 0) or 0),
            }
        )
    return parsed


def render_pnl_calendar(days: list[dict]) -> None:
    parsed = parse_calendar_days(days)
    if not parsed:
        st.info("Completed index-option trades will populate the P/L calendar.")
        return

    available = sorted({(item["date"].year, item["date"].month) for item in parsed})
    month_keys = [f"{year:04d}-{month:02d}" for year, month in available]
    selected = st.selectbox(
        "Month",
        month_keys,
        index=len(month_keys) - 1,
        format_func=lambda key: datetime.strptime(key, "%Y-%m").strftime("%B %Y"),
        label_visibility="collapsed",
    )
    year, month = (int(part) for part in selected.split("-"))
    by_date = {item["date"]: item for item in parsed}
    month_rows = [item for item in parsed if item["date"].year == year and item["date"].month == month]
    month_pnl = sum(item["pnl"] for item in month_rows)
    st.caption(f"{datetime(year, month, 1).strftime('%B %Y')} · {money(month_pnl)}")

    cells = [f'<div class="weekday">{day}</div>' for day in ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")]
    for week in calendar_module.Calendar(firstweekday=0).monthdatescalendar(year, month):
        for day in week:
            if day.month != month:
                cells.append('<div class="day empty"></div>')
                continue
            item = by_date.get(day)
            pnl = item["pnl"] if item else None
            state = "profit" if pnl is not None and pnl > 0 else "loss" if pnl is not None and pnl < 0 else ""
            pnl_text = money(pnl) if pnl is not None else "—"
            trades_text = f"{item['trades']} trade{'s' if item and item['trades'] != 1 else ''}" if item else "No trades"
            cells.append(
                f'<div class="day {state}"><div class="day-number">{day.day}</div>'
                f'<div class="day-pnl">{html.escape(pnl_text)}</div>'
                f'<div class="day-trades">{html.escape(trades_text)}</div></div>'
            )
    st.markdown(
        '<div class="calendar-wrap"><div class="calendar-grid">'
        + "".join(cells)
        + "</div></div>",
        unsafe_allow_html=True,
    )


def render_scans(scans: list[dict]) -> None:
    symbol_keys = (
        ("NIFTY", "nifty"),
        ("BANKNIFTY", "bankNifty"),
        ("SENSEX", "sensex"),
    )
    visible = [
        (row, symbol, row.get(key))
        for row in scans or []
        for symbol, key in symbol_keys
        if row.get(key)
    ]
    if not visible:
        st.info("Today’s completed five-minute scans will appear here.")
        return
    for row, symbol, decision in visible:
        try:
            timestamp = datetime.fromisoformat(str(row.get("timestamp") or ""))
            time_text = timestamp.strftime("%I:%M %p").lstrip("0")
        except ValueError:
            time_text = "—"
        status = str(decision.get("decision") or "SCANNED")
        badge_class = "entered" if status in {"ENTERED", "SELECTED"} else ""
        metadata = [
            symbol,
            str(decision.get("direction") or "").title(),
            str(decision.get("setup") or "").title(),
        ]
        score = decision.get("score")
        if score is not None:
            metadata.append(f"Knowledge {float(score):.1f}")
        selection = decision.get("selectionScore")
        if selection is not None:
            metadata.append(f"Rank {float(selection):.1f}")
        instrument = decision.get("instrument")
        if instrument:
            metadata.append(str(instrument))
        metadata = [item for item in metadata if item]
        st.markdown(
            '<div class="scan-card">'
            f'<div class="scan-head"><div class="scan-time">{html.escape(time_text)}</div>'
            f'<div class="scan-badge {badge_class}">{html.escape(status)}</div></div>'
            f'<div class="scan-detail">{html.escape(str(decision.get("reason") or "Scan completed"))}</div>'
            f'<div class="scan-meta">{html.escape(" · ".join(metadata))}</div>'
            "</div>",
            unsafe_allow_html=True,
        )


performance = build_trade_performance(analytics_mode="real")
today = performance.get("today") or {}
cumulative = performance.get("cumulative") or {}
today_pnl = float(today.get("netPnL", today.get("closedPnL", 0)) or 0)
cumulative_pnl = float(cumulative.get("netPnL", cumulative.get("totalPnL", 0)) or 0)
today_trades = int(today.get("closedTrades", 0) or 0)
total_trades = int(cumulative.get("totalTrades", 0) or 0)

st.markdown('<div class="page-title">Index Options Trading</div>', unsafe_allow_html=True)
st.markdown(
    f'<div class="page-subtitle">Updated {now_ist().strftime("%d %b %Y · %I:%M:%S %p")} IST</div>',
    unsafe_allow_html=True,
)

if st.button("Refresh", icon="🔄"):
    st.rerun()

st.markdown(
    '<div class="summary-grid">'
    + summary_card("Today’s P/L", money(today_pnl), f"{today_trades} trade{'s' if today_trades != 1 else ''} today", value_class(today_pnl))
    + summary_card("Cumulative P/L", money(cumulative_pnl), "Recorded live bot trades", value_class(cumulative_pnl))
    + summary_card("Number of Trades", f"{total_trades:,}", "Cumulative completed trades")
    + "</div>",
    unsafe_allow_html=True,
)

st.markdown('<div class="section-title">P/L Calendar</div>', unsafe_allow_html=True)
st.markdown('<div class="section-subtitle">Daily completed-trade results</div>', unsafe_allow_html=True)
render_pnl_calendar(performance.get("pnlCalendar") or [])

st.markdown('<div class="section-title">Today’s Scans</div>', unsafe_allow_html=True)
st.markdown('<div class="section-subtitle">Latest completed five-minute scan first</div>', unsafe_allow_html=True)
render_scans(build_today_scans())
