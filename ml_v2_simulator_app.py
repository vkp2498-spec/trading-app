"""Local Streamlit interface for the opening V2 holdout simulator."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ml_v2_simulator import (
    HISTORY_FILE,
    RR_CUTOFFS,
    SimulationAssumptions,
    compare_rr_cutoffs,
    default_test_start,
    fetch_history,
    fit_and_forecast,
    labeled_samples,
    load_history,
    probability_portfolio_curve,
    probability_rr_surface,
    simulate_portfolio,
    simulate_trades,
    summarize,
)


st.set_page_config(page_title="V2 Opening Model Simulator", page_icon="🧪", layout="wide")


def money(value) -> str:
    return f"₹{float(value):,.0f}"


def percent(value) -> str:
    return f"{float(value) * 100:.1f}%"


def surface_figure(surface: pd.DataFrame, direction: str) -> go.Figure:
    side = surface[surface["direction"] == direction]
    pnl = side.pivot(index="rr_cutoff", columns="probability_cutoff", values="net_pnl")
    counts = side.pivot(index="rr_cutoff", columns="probability_cutoff", values="trades")
    drawdowns = side.pivot(index="rr_cutoff", columns="probability_cutoff", values="maximum_drawdown")
    z = pnl.to_numpy(dtype=float, copy=True)
    z[counts.to_numpy(dtype=int) == 0] = np.nan
    finite = np.abs(z[np.isfinite(z)])
    scale = float(finite.max()) if finite.size else 1.0
    figure = go.Figure(go.Surface(
        x=pnl.columns.to_numpy(dtype=float) * 100,
        y=pnl.index.to_numpy(dtype=float),
        z=z,
        customdata=np.stack(
            [counts.to_numpy(dtype=int), drawdowns.to_numpy(dtype=float)], axis=-1
        ),
        colorscale="RdYlGn",
        cmin=-scale,
        cmax=scale,
        colorbar={"title": "Net P&L"},
        hovertemplate=(
            "Probability > %{x:.0f}%<br>Min RR: %{y:.1f}<br>"
            "Portfolio P&L: ₹%{z:,.0f}<br>Trades: %{customdata[0]}<br>"
            "Max drawdown: ₹%{customdata[1]:,.0f}<extra></extra>"
        ),
    ))
    figure.update_layout(
        title=f"{direction} — one cash account",
        height=520,
        margin={"l": 0, "r": 0, "t": 45, "b": 0},
        scene={
            "xaxis_title": "Probability cutoff (%)",
            "yaxis_title": "Minimum predicted RR (0 = Any)",
            "zaxis_title": "Portfolio net P&L (₹)",
            "camera": {"eye": {"x": 1.45, "y": -1.55, "z": 1.15}},
        },
    )
    return figure


def probability_figure(curve: pd.DataFrame, direction: str) -> go.Figure:
    side = curve[curve["direction"] == direction].sort_values("probability_cutoff")
    custom = np.stack(
        [side["executed_trades"].to_numpy(), side["maximum_drawdown"].to_numpy()], axis=-1
    )
    figure = go.Figure(go.Scatter(
        x=side["probability_cutoff"] * 100,
        y=side["net_profit"],
        customdata=custom,
        mode="lines+markers",
        line={"width": 3},
        marker={"size": 8},
        hovertemplate=(
            "Probability > %{x:.0f}%<br>Portfolio P&L: ₹%{y:,.0f}<br>"
            "Executed: %{customdata[0]}<br>Max drawdown: ₹%{customdata[1]:,.0f}<extra></extra>"
        ),
    ))
    figure.add_hline(y=0, line_dash="dash", line_color="gray")
    figure.update_layout(
        title=f"{direction} — fixed NIFTY exits",
        height=420,
        margin={"l": 0, "r": 0, "t": 45, "b": 0},
        xaxis_title="Minimum probability (%)",
        yaxis_title="Portfolio net P&L (₹)",
    )
    return figure


@st.cache_data(show_spinner=False)
def cached_history(path_text: str, modified_ns: int) -> pd.DataFrame:
    del modified_ns
    return load_history(Path(path_text))


@st.cache_data(show_spinner="Training on 18 months and scoring the next six months…")
def cached_forecast(samples: pd.DataFrame, test_start) -> tuple[pd.DataFrame, dict]:
    return fit_and_forecast(samples, test_start)


st.title("V2 Opening Model Simulator")
st.caption(
    "Train once on 18 months of NIFTY first-4H candles, then evaluate the untouched next six months. "
    "CALL and PUT are tested independently."
)

with st.sidebar:
    st.header("Study controls")
    refresh = st.button("Refresh two-year market data", use_container_width=True)
    st.caption("Refresh requires a valid Upstox token in this MacBook's `.env`. Cached data works offline.")

if refresh:
    progress_box = st.empty()
    try:
        with st.spinner("Downloading first-4H NIFTY candles…"):
            frame = fetch_history(progress=lambda message: progress_box.info(message))
        cached_history.clear()
        cached_forecast.clear()
        progress_box.success(f"Saved {len(frame)} daily candles")
    except Exception as error:
        progress_box.error(f"Could not refresh history: {error}")

if not HISTORY_FILE.exists():
    st.warning(
        "Local history is not present yet. Use **Refresh two-year market data** after placing a valid "
        "Upstox access token in `.env`, or copy the cached history from the Vamsi instance."
    )
    st.stop()

candles = cached_history(str(HISTORY_FILE), HISTORY_FILE.stat().st_mtime_ns)
samples = labeled_samples(candles)
proposed_start = default_test_start(samples)
minimum_start = pd.Timestamp(samples.index.min()).tz_localize(None) + pd.DateOffset(months=18)
maximum_start = pd.Timestamp(samples.index.max()).tz_localize(None) - pd.DateOffset(months=6) + pd.Timedelta(days=1)
if maximum_start < minimum_start:
    st.error("The cache does not yet contain a complete 18-month training plus six-month test window.")
    st.stop()
proposed_start = min(max(proposed_start, minimum_start), maximum_start)

with st.sidebar:
    test_start = st.date_input(
        "Out-of-sample period starts",
        value=proposed_start.date(),
        min_value=minimum_start.date(),
        max_value=maximum_start.date(),
        help="The model trains only on the preceding 18 months and remains frozen for the following six months.",
    )
    probability_cutoff = st.slider("Minimum probability", 0.50, 0.90, 0.50, 0.01)
    exit_mode = st.radio(
        "Target and stop source",
        ["Fixed NIFTY points", "Model predicted"],
        horizontal=True,
    )
    if exit_mode == "Fixed NIFTY points":
        fixed_target_points = st.number_input("NIFTY target points", 1.0, 500.0, 30.0, 5.0)
        fixed_stop_points = st.number_input("NIFTY stop points", 1.0, 500.0, 30.0, 5.0)
        selected_rr = None
        st.caption(f"Applied underlying RR: {fixed_target_points / fixed_stop_points:.2f}")
    else:
        fixed_target_points = None
        fixed_stop_points = None
        rr_labels = ["Any"] + [f"{value:g}" for value in RR_CUTOFFS if value is not None]
        selected_rr_label = st.selectbox("Minimum predicted reward/risk", rr_labels, index=0)
        selected_rr = None if selected_rr_label == "Any" else float(selected_rr_label)

    st.header("Option approximation")
    option_premium = st.number_input("Fixed entry premium", 25.0, 500.0, 150.0, 5.0)
    option_delta = st.slider("ATM delta assumption", 0.20, 0.80, 0.50, 0.05)
    capital = st.number_input("Starting account capital", 10_000.0, 1_000_000.0, 100_000.0, 10_000.0)
    lot_size = st.number_input("NIFTY option lot size", 1, 500, 65, 1)
    allocation_percent = st.slider("Maximum equity deployed", 10, 100, 100, 5)
    cost_percent = st.number_input(
        "Estimated round-trip costs (% of premium deployed)", 0.0, 5.0, 0.25, 0.05
    )

try:
    forecasts, window = cached_forecast(samples, test_start)
except Exception as error:
    st.error(str(error))
    st.stop()

assumptions = SimulationAssumptions(
    probability_cutoff=probability_cutoff,
    rr_cutoff=selected_rr,
    option_premium=option_premium,
    option_delta=option_delta,
    capital_per_trade=capital,
    round_trip_cost=0.0,
    round_trip_cost_percent=cost_percent,
    lot_size=int(lot_size),
    allocation_fraction=allocation_percent / 100,
    fixed_target_underlying_points=fixed_target_points,
    fixed_stop_underlying_points=fixed_stop_points,
)
trades = simulate_trades(forecasts, assumptions, rule="rr")
metrics = summarize(trades)
portfolio_ledger, portfolio = simulate_portfolio(trades, assumptions)

st.info(
    f"Training: **{window['train_start'].date()} to {window['train_end'].date()}** "
    f"({window['training_sessions']} sessions) · Holdout: **{window['test_start'].date()} to "
    f"{window['test_end'].date()}** ({window['test_sessions']} sessions)"
)

top = st.columns(6)
top[0].metric("Starting equity", money(portfolio["starting_capital"]))
top[1].metric("Final equity", money(portfolio["final_equity"]))
top[2].metric("Portfolio P&L", money(portfolio["net_profit"]), f"{portfolio['return_percent']:+.1f}%")
top[3].metric("Executed", portfolio["executed_trades"], f"{portfolio['skipped_trades']} skipped")
top[4].metric("Maximum drawdown", money(portfolio["maximum_drawdown"]), f"{portfolio['maximum_drawdown_percent']:.1f}%")
top[5].metric(
    "Account status",
    "SURVIVED" if portfolio["survived"] else "DEPLETED",
    "All signals completed" if portfolio["completed_all_signals"] else "Capital constraint skipped signals",
)

secondary = st.columns(4)
secondary[0].metric("Profitable executed trades", percent(portfolio["win_probability"]))
secondary[1].metric("Target / stop signals", f"{metrics['targets']} / {metrics['stops']}")
secondary[2].metric("Minimum account equity", money(portfolio["minimum_equity"]))
secondary[3].metric("Sizing", f"{int(lot_size)} per lot", f"Up to {allocation_percent}% current equity")

portfolio_tab, comparison_tab, trades_tab, evidence_tab, assumptions_tab = st.tabs(
    ["Portfolio path", "RR comparison", "Selected signals", "Probability evidence", "Assumptions"]
)

with portfolio_tab:
    st.subheader("Cash-constrained account path")
    if portfolio_ledger.empty:
        st.info("No qualifying signals were available for this setting.")
    else:
        executed_ledger = portfolio_ledger[portfolio_ledger["status"] == "EXECUTED"].copy()
        if not executed_ledger.empty:
            daily_equity = executed_ledger.assign(
                Date=pd.to_datetime(executed_ledger["timestamp"]).dt.date
            ).groupby("Date")["equity_after"].last()
            initial = pd.Series(
                [portfolio["starting_capital"]],
                index=[pd.Timestamp(window["test_start"]).date()],
            )
            st.line_chart(pd.concat([initial, daily_equity]).sort_index().rename("Account equity"))
        ledger_display = portfolio_ledger.copy()
        ledger_display["timestamp"] = pd.to_datetime(ledger_display["timestamp"]).dt.date
        ledger_display = ledger_display.rename(columns={
            "timestamp": "Date", "direction": "Side", "status": "Status", "lots": "Lots",
            "quantity": "Quantity", "deployed_capital": "Deployed", "estimated_cost": "Costs",
            "portfolio_pnl": "Trade P&L",
            "equity_before": "Equity before", "equity_after": "Equity after", "outcome": "Outcome",
        })
        st.dataframe(
            ledger_display[["Date", "Side", "Status", "Lots", "Quantity", "Deployed", "Costs", "Outcome", "Trade P&L", "Equity before", "Equity after"]],
            use_container_width=True,
            hide_index=True,
        )

with comparison_tab:
    model_assumptions = replace(
        assumptions,
        rr_cutoff=None,
        fixed_target_underlying_points=None,
        fixed_stop_underlying_points=None,
    )
    curve_target_points = float(fixed_target_points or 30)
    curve_stop_points = float(fixed_stop_points or 30)
    curve_assumptions = replace(
        assumptions,
        rr_cutoff=None,
        fixed_target_underlying_points=curve_target_points,
        fixed_stop_underlying_points=curve_stop_points,
    )
    fixed_curve = probability_portfolio_curve(forecasts, curve_assumptions)
    comparison = compare_rr_cutoffs(forecasts, model_assumptions)
    surface = probability_rr_surface(forecasts, model_assumptions)
    current_v2_trades = simulate_trades(forecasts, model_assumptions, rule="ev", minimum_ev_r=0.10)
    current_v2 = summarize(current_v2_trades)
    _current_v2_ledger, current_v2_portfolio = simulate_portfolio(current_v2_trades, model_assumptions)
    st.subheader("Fixed 30/30-style exits — probability comparison")
    fixed_call_chart, fixed_put_chart = st.columns(2)
    fixed_call_chart.plotly_chart(probability_figure(fixed_curve, "CALL"), use_container_width=True)
    fixed_put_chart.plotly_chart(probability_figure(fixed_curve, "PUT"), use_container_width=True)
    curve_display = fixed_curve.copy()
    curve_display["probability_cutoff"] = (curve_display["probability_cutoff"] * 100).round(0)
    curve_display = curve_display.rename(columns={
        "direction": "Side", "probability_cutoff": "Probability > %", "signals": "Signals",
        "executed_trades": "Executed", "skipped_trades": "Skipped", "final_equity": "Final equity",
        "net_profit": "Portfolio P&L", "return_percent": "Return %",
        "maximum_drawdown": "Max drawdown", "survived": "Survived",
    })
    st.dataframe(curve_display, use_container_width=True, hide_index=True)
    st.caption(
        f"The curves use a {curve_target_points:g}-point NIFTY target and "
        f"{curve_stop_points:g}-point stop, whole lots, current equity and scaled costs."
    )
    st.subheader("Optional model-predicted target/stop comparison")
    call_chart, put_chart = st.columns(2)
    call_chart.plotly_chart(surface_figure(surface, "CALL"), use_container_width=True)
    put_chart.plotly_chart(surface_figure(surface, "PUT"), use_container_width=True)
    st.caption(
        "Each side starts with the selected account capital and uses whole lots. RR 0 means no RR cutoff; "
        "zero-trade combinations are blank. Hover to see trade count and drawdown."
    )
    st.subheader("Model-predicted reward/risk cutoffs")
    display = comparison[[
        "rr_cutoff", "trades", "calls", "puts", "targets", "stops",
        "win_probability", "average_target_points", "average_stop_points",
        "portfolio_final_equity", "portfolio_net_profit", "portfolio_return_percent",
        "portfolio_maximum_drawdown", "portfolio_executed_trades", "portfolio_skipped_trades",
        "portfolio_survived",
    ]].copy()
    display.columns = [
        "Min RR", "Orders", "CALL", "PUT", "Targets", "Stops", "Profitable %",
        "Avg target pts", "Avg stop pts", "Final equity", "Portfolio P&L", "Return %",
        "Max drawdown", "Executed", "Skipped", "Survived",
    ]
    display["Profitable %"] = display["Profitable %"].map(lambda value: round(value * 100, 1))
    st.dataframe(display, use_container_width=True, hide_index=True)
    chart = comparison.set_index("rr_cutoff")[["portfolio_net_profit", "portfolio_maximum_drawdown"]].rename(
        columns={"portfolio_net_profit": "Portfolio P&L", "portfolio_maximum_drawdown": "Max drawdown"}
    )
    st.bar_chart(chart)
    st.subheader("Current live V2 policy benchmark")
    st.caption("Probability > selected cutoff and expected-value proxy ≥ +0.10R.")
    bench = st.columns(5)
    bench[0].metric("Orders", current_v2["trades"])
    bench[1].metric("Profitable", percent(current_v2["win_probability"]))
    bench[2].metric("Target / stop", f"{current_v2['targets']} / {current_v2['stops']}")
    bench[3].metric("Portfolio P&L", money(current_v2_portfolio["net_profit"]))
    bench[4].metric("Final equity", money(current_v2_portfolio["final_equity"]))

with trades_tab:
    if trades.empty:
        st.info("No CALL or PUT forecast passed the selected filters.")
    else:
        trade_display = trades.copy()
        trade_display["timestamp"] = pd.to_datetime(trade_display["timestamp"]).dt.date
        trade_display["probability"] = (trade_display["probability"] * 100).round(1)
        trade_display = trade_display.rename(columns={
            "timestamp": "Date", "direction": "Side", "probability": "Probability %",
            "applied_reward_risk": "Applied RR", "option_target_points": "Target option pts",
            "option_stop_points": "Stop option pts", "outcome": "Outcome", "net_pnl": "Normalized P&L",
        })
        st.dataframe(
            trade_display[["Date", "Side", "Probability %", "Applied RR", "Target option pts", "Stop option pts", "Outcome", "Normalized P&L"]],
            use_container_width=True,
            hide_index=True,
        )
        daily = trades.assign(Date=pd.to_datetime(trades["timestamp"]).dt.date).groupby("Date")["net_pnl"].sum().cumsum()
        st.line_chart(daily.rename("Normalized cumulative P&L — fresh capital per signal"))

with evidence_tab:
    probability_rows = []
    for direction in ("CALL", "PUT"):
        prefix = direction.lower()
        frame = forecasts[[f"{prefix}_probability", f"future_{'up' if direction == 'CALL' else 'down'}_percent"]].copy()
        frame["bucket"] = pd.cut(
            frame[f"{prefix}_probability"], bins=np.arange(0, 1.01, 0.1), include_lowest=True
        ).astype(str)
        grouped = frame.groupby("bucket", observed=True).agg(
            Samples=(f"{prefix}_probability", "size"),
            Average_probability=(f"{prefix}_probability", "mean"),
            Average_favorable_move=(f"future_{'up' if direction == 'CALL' else 'down'}_percent", "mean"),
        ).reset_index()
        grouped.insert(0, "Side", direction)
        probability_rows.append(grouped)
    evidence = pd.concat(probability_rows, ignore_index=True)
    evidence["Average_probability"] = (evidence["Average_probability"] * 100).round(1)
    evidence["Average_favorable_move"] = evidence["Average_favorable_move"].round(3)
    st.dataframe(evidence, use_container_width=True, hide_index=True)
    st.caption(
        "V2 probability predicts whether NIFTY moves at least 0.10% in that direction; it is not directly "
        "the probability that the model-specific target beats its stop."
    )

with assumptions_tab:
    st.markdown(
        """
        - Entry is approximated at the 09:15 NIFTY open. Live V2 normally enters later, so this study does not model opening execution drift.
        - Every option is approximated at the selected fixed premium and delta. IV, gamma, theta, spread and slippage are not reconstructed.
        - The portfolio begins with the selected starting capital, deploys only whole lots, and compounds actual remaining equity.
        - Simultaneous CALL and PUT signals split that day's deployable equity equally; unused cash remains in the account.
        - A signal is skipped when its equal capital share cannot afford one lot. The simulator never adds fresh capital.
        - Estimated transaction costs scale with premium deployed; adjust the percentage in the sidebar.
        - Predicted target and stop are NIFTY percentages converted to option points through the selected delta.
        - Fixed-point mode replaces model exits with the selected NIFTY target and stop; probability still comes from V2.
        - If target and stop both occur inside one 4-hour candle, the simulator conservatively records the stop first.
        - A trade that reaches neither level exits at the first candle's close.
        - Results are out-of-sample for the selected six months, but repeated cutoff testing on the same holdout can still overfit it.
        """
    )
