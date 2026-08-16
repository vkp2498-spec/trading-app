"""Minimal index-options dashboard: headline P/L, calendar and scans."""

from __future__ import annotations

import calendar as calendar_module
import html
from datetime import date, datetime
from pathlib import Path

import streamlit as st

from dashboard_data import (
    build_post_market_review,
    build_today_scans,
    build_trade_performance,
)
from strategy_core import now_ist


BASE_DIR = Path(__file__).resolve().parent
APP_ICON = BASE_DIR / "assets" / "vamsi_icon_v2.jpg"
INDEX_COLUMNS = (
    ("NIFTY", "nifty"),
    ("BANKNIFTY", "bankNifty"),
    ("SENSEX", "sensex"),
)

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
    .matrix-wrap { overflow-x: auto; padding: 2px 0 8px; }
    .matrix-table { border-collapse: separate; border-spacing: 0; table-layout: fixed; width: 100%; min-width: 780px; }
    .matrix-table th, .matrix-table td { background: #fff; border-bottom: 1px solid #dde5ef; border-right: 1px solid #dde5ef; padding: 13px 14px; text-align: left; vertical-align: top; }
    .matrix-table th { background: #0b2343; color: #fff; font-size: .76rem; font-weight: 850; }
    .matrix-table th:first-child, .matrix-table td:first-child { border-left: 1px solid #dde5ef; position: sticky; left: 0; width: 142px; z-index: 2; }
    .matrix-table th:first-child { background: #0b2343; }
    .matrix-table td:first-child { background: #eef3f8; color: #0b2343; font-size: .76rem; font-weight: 850; }
    .matrix-table tr:first-child th:first-child { border-top-left-radius: 8px; }
    .matrix-table tr:first-child th:last-child { border-top-right-radius: 8px; }
    .matrix-table tbody tr:last-child td:first-child { border-bottom-left-radius: 8px; }
    .matrix-table tbody tr:last-child td:last-child { border-bottom-right-radius: 8px; }
    .matrix-value { color: #0b2343; font-size: .92rem; font-weight: 850; }
    .matrix-detail { color: #64748b; font-size: .68rem; line-height: 1.35; margin-top: 4px; white-space: normal; }
    .matrix-badge { background: #e8eef6; border-radius: 999px; color: #334b69; display: inline-block; font-size: .62rem; font-weight: 850; padding: 4px 8px; }
    .matrix-badge.entered { background: #dff5e8; color: #13723e; }
    .matrix-badge.unavailable { background: #f0f2f5; color: #7a8799; }
    .symbol-note { color: #cbd8e8; display: block; font-size: .6rem; font-weight: 700; margin-top: 2px; }
    @media (max-width: 720px) {
        .summary-grid { grid-template-columns: 1fr; }
        .page-title { font-size: 1.75rem; }
        .matrix-table { min-width: 680px; }
        .matrix-table th, .matrix-table td { padding: 11px 10px; }
        .matrix-table th:first-child, .matrix-table td:first-child { width: 112px; }
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


def percent(value) -> str:
    return f"{float(value or 0):,.1f}%"


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


def render_index_performance(today: dict, cumulative: dict) -> None:
    today_stats = today.get("symbolStats") or {}
    cumulative_stats = cumulative.get("symbolStats") or {}
    rows = (
        ("Today’s P/L", lambda symbol: money((today_stats.get(symbol) or {}).get("netPnL")), "money", today_stats),
        ("Today’s Trades", lambda symbol: f"{int((today_stats.get(symbol) or {}).get('trades') or 0):,}", "", today_stats),
        ("Today’s Win Rate", lambda symbol: percent((today_stats.get(symbol) or {}).get("winRate")), "", today_stats),
        ("Cumulative P/L", lambda symbol: money((cumulative_stats.get(symbol) or {}).get("netPnL")), "money", cumulative_stats),
        ("Total Trades", lambda symbol: f"{int((cumulative_stats.get(symbol) or {}).get('trades') or 0):,}", "", cumulative_stats),
        ("Cumulative Win Rate", lambda symbol: percent((cumulative_stats.get(symbol) or {}).get("winRate")), "", cumulative_stats),
        ("Average Win", lambda symbol: money((cumulative_stats.get(symbol) or {}).get("averageProfit")), "money", cumulative_stats),
        ("Average Loss", lambda symbol: money(-abs(float((cumulative_stats.get(symbol) or {}).get("averageLoss") or 0))), "loss", cumulative_stats),
    )
    header = "".join(f"<th>{html.escape(symbol)}</th>" for symbol, _key in INDEX_COLUMNS)
    body = []
    for label, formatter, value_type, source in rows:
        cells = []
        for symbol, _key in INDEX_COLUMNS:
            stats = source.get(symbol) or {}
            raw = (
                stats.get("netPnL")
                if "P/L" in label
                else -abs(float(stats.get("averageLoss") or 0))
                if value_type == "loss"
                else stats.get("averageProfit")
                if label == "Average Win"
                else 0
            )
            css_class = value_class(raw) if value_type in {"money", "loss"} else ""
            cells.append(
                f'<td><div class="matrix-value {css_class}">'
                f"{html.escape(formatter(symbol))}</div></td>"
            )
        body.append(f"<tr><td>{html.escape(label)}</td>{''.join(cells)}</tr>")
    st.markdown(
        '<div class="matrix-wrap"><table class="matrix-table"><thead><tr><th>Metric</th>'
        + header
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>",
        unsafe_allow_html=True,
    )


def render_scans(scans: list[dict]) -> None:
    if not any(row.get(key) for row in scans or [] for _symbol, key in INDEX_COLUMNS):
        st.info("Today’s completed five-minute scans will appear here.")
        return
    header = "".join(f"<th>{html.escape(symbol)}</th>" for symbol, _key in INDEX_COLUMNS)
    body = []
    for row in scans or []:
        try:
            timestamp = datetime.fromisoformat(str(row.get("timestamp") or ""))
            time_text = timestamp.strftime("%I:%M %p").lstrip("0")
        except ValueError:
            time_text = "—"
        cells = []
        for _symbol, key in INDEX_COLUMNS:
            decision = row.get(key)
            if not decision:
                cells.append('<td><span class="matrix-badge unavailable">NO DATA</span></td>')
                continue
            status = str(decision.get("decision") or "SCANNED")
            badge_class = "entered" if status in {"ENTERED", "SELECTED"} else ""
            details = []
            score = decision.get("score")
            if score is not None:
                details.append(f"Score {float(score):.1f}")
            direction = str(decision.get("direction") or "").title()
            setup = str(decision.get("setup") or "").title()
            if direction:
                details.append(direction)
            if setup:
                details.append(setup)
            reason = str(decision.get("reason") or "Scan completed")
            instrument = str(decision.get("instrument") or "")
            cells.append(
                "<td>"
                f'<span class="matrix-badge {badge_class}">{html.escape(status)}</span>'
                f'<div class="matrix-value" style="margin-top:7px">{html.escape(" · ".join(details) or "Scanned")}</div>'
                f'<div class="matrix-detail">{html.escape(reason)}</div>'
                + (f'<div class="matrix-detail">{html.escape(instrument)}</div>' if instrument else "")
                + "</td>"
            )
        body.append(f"<tr><td>{html.escape(time_text)}</td>{''.join(cells)}</tr>")
    st.markdown(
        '<div class="matrix-wrap"><table class="matrix-table"><thead><tr><th>Time</th>'
        + header
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>",
        unsafe_allow_html=True,
    )


def render_post_market_review(review: dict) -> None:
    columns = review.get("columns") or []
    rows = review.get("rows") or []
    if not columns:
        st.info(str(review.get("message") or "Post-market evidence will appear after the 4 PM audit."))
        return
    by_symbol = {
        str(row.get("symbol") or "").upper(): row
        for row in rows
    }
    header_cells = []
    for symbol, _key in INDEX_COLUMNS:
        row = by_symbol.get(symbol) or {}
        target = float(row.get("targetPoints") or 0)
        stop = float(row.get("stopPoints") or 0)
        header_cells.append(
            f"<th>{html.escape(symbol)}"
            f'<span class="symbol-note">T/S {target:g}/{stop:g}</span></th>'
        )
    body = []
    for column in columns:
        cells = []
        for symbol, _key in INDEX_COLUMNS:
            symbol_row = by_symbol.get(symbol) or {}
            by_column = {
                str(cell.get("column")): cell
                for cell in symbol_row.get("cells") or []
            }
            cell = by_column.get(str(column)) or {}
            average = cell.get("averageFavorablePoints")
            samples = int(cell.get("samples") or 0)
            value = "—" if average is None else f"{float(average):,.1f} pts"
            cells.append(
                "<td>"
                f'<div class="matrix-value">{html.escape(value)}</div>'
                f'<div class="matrix-detail">{samples} scan{"s" if samples != 1 else ""}</div>'
                "</td>"
            )
        body.append(
            "<tr>"
            f"<td>{html.escape(str(column))}</td>"
            + "".join(cells)
            + "</tr>"
        )
    st.markdown(
        '<div class="matrix-wrap"><table class="matrix-table"><thead><tr><th>Score Bucket</th>'
        + "".join(header_cells)
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>",
        unsafe_allow_html=True,
    )
    st.caption(
        f"Cumulative: {int(review.get('cumulativeObservations') or 0)} directional scans · "
        f"Last audit: {review.get('tradingDate') or '—'} · same-minute target/stop is treated stop-first."
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

st.markdown('<div class="section-title">Index Overview</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="section-subtitle">Today and cumulative results by index</div>',
    unsafe_allow_html=True,
)
render_index_performance(today, cumulative)

st.markdown('<div class="section-title">P/L Calendar</div>', unsafe_allow_html=True)
st.markdown('<div class="section-subtitle">Daily completed-trade results</div>', unsafe_allow_html=True)
render_pnl_calendar(performance.get("pnlCalendar") or [])

st.markdown('<div class="section-title">Today’s Scans</div>', unsafe_allow_html=True)
st.markdown('<div class="section-subtitle">Latest completed five-minute scan first</div>', unsafe_allow_html=True)
render_scans(build_today_scans())

st.markdown('<div class="section-title">Post-Market Review</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="section-subtitle">Average favourable index points before the configured stop, accumulated from every overlapping five-minute scan</div>',
    unsafe_allow_html=True,
)
render_post_market_review(build_post_market_review())
