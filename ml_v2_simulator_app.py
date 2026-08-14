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
    probability_rr_surface,
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
    z = pnl.to_numpy(dtype=float, copy=True)
    z[counts.to_numpy(dtype=int) == 0] = np.nan
    finite = np.abs(z[np.isfinite(z)])
    scale = float(finite.max()) if finite.size else 1.0
    figure = go.Figure(go.Surface(
        x=pnl.columns.to_numpy(dtype=float) * 100,
        y=pnl.index.to_numpy(dtype=float),
        z=z,
        customdata=counts.to_numpy(dtype=int),
        colorscale="RdYlGn",
        cmin=-scale,
        cmax=scale,
        colorbar={"title": "Net P&L"},
        hovertemplate=(
            "Probability > %{x:.0f}%<br>Min RR: %{y:.1f}<br>"
            "Net P&L: ₹%{z:,.0f}<br>Trades: %{customdata}<extra></extra>"
        ),
    ))
    figure.update_layout(
        title=f"{direction} surface",
        height=520,
        margin={"l": 0, "r": 0, "t": 45, "b": 0},
        scene={
            "xaxis_title": "Probability cutoff (%)",
            "yaxis_title": "Minimum predicted RR (0 = Any)",
            "zaxis_title": "Net P&L (₹)",
            "camera": {"eye": {"x": 1.45, "y": -1.55, "z": 1.15}},
        },
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
    rr_labels = ["Any"] + [f"{value:g}" for value in RR_CUTOFFS if value is not None]
    selected_rr_label = st.selectbox("Minimum predicted reward/risk", rr_labels, index=0)
    selected_rr = None if selected_rr_label == "Any" else float(selected_rr_label)

    st.header("Option approximation")
    option_premium = st.number_input("Fixed entry premium", 25.0, 500.0, 150.0, 5.0)
    option_delta = st.slider("ATM delta assumption", 0.20, 0.80, 0.50, 0.05)
    capital = st.number_input("Capital deployed per signal", 10_000.0, 1_000_000.0, 100_000.0, 10_000.0)
    round_trip_cost = st.number_input("Costs per completed trade", 0.0, 5_000.0, 0.0, 50.0)

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
    round_trip_cost=round_trip_cost,
)
trades = simulate_trades(forecasts, assumptions, rule="rr")
metrics = summarize(trades)

st.info(
    f"Training: **{window['train_start'].date()} to {window['train_end'].date()}** "
    f"({window['training_sessions']} sessions) · Holdout: **{window['test_start'].date()} to "
    f"{window['test_end'].date()}** ({window['test_sessions']} sessions)"
)

top = st.columns(6)
top[0].metric("Orders", f"{metrics['trades']}", f"{metrics['calls']} CALL · {metrics['puts']} PUT")
top[1].metric("Profitable trades", percent(metrics["win_probability"]))
top[2].metric("Target / stop", f"{metrics['targets']} / {metrics['stops']}")
top[3].metric("Average target", f"{metrics['average_target_points']:.1f} option pts")
top[4].metric("Average stop", f"{metrics['average_stop_points']:.1f} option pts")
top[5].metric("Net P&L", money(metrics["net_pnl"]), f"{money(metrics['expectancy'])} / trade")

secondary = st.columns(4)
secondary[0].metric("Target hit rate", percent(metrics["target_hit_rate"]))
secondary[1].metric("Profit factor", "∞" if np.isinf(metrics["profit_factor"]) else f"{metrics['profit_factor']:.2f}")
secondary[2].metric("Maximum drawdown", money(metrics["max_drawdown"]))
secondary[3].metric("Capital convention", money(capital), "reused independently per signal")

comparison_tab, trades_tab, evidence_tab, assumptions_tab = st.tabs(
    ["RR comparison", "Selected trades", "Probability evidence", "Assumptions"]
)

with comparison_tab:
    comparison = compare_rr_cutoffs(forecasts, replace(assumptions, rr_cutoff=None))
    surface = probability_rr_surface(forecasts, replace(assumptions, rr_cutoff=None))
    current_v2_trades = simulate_trades(forecasts, replace(assumptions, rr_cutoff=None), rule="ev", minimum_ev_r=0.10)
    current_v2 = summarize(current_v2_trades)
    st.subheader("Probability × reward/risk × net P&L")
    call_chart, put_chart = st.columns(2)
    call_chart.plotly_chart(surface_figure(surface, "CALL"), use_container_width=True)
    put_chart.plotly_chart(surface_figure(surface, "PUT"), use_container_width=True)
    st.caption(
        "Each surface uses the untouched six-month holdout. RR 0 means no RR cutoff; combinations with "
        "zero trades are left blank. Hover to see the trade count behind each point."
    )
    st.subheader("Raw reward/risk cutoffs")
    display = comparison[[
        "rr_cutoff", "trades", "calls", "puts", "targets", "stops",
        "win_probability", "average_target_points", "average_stop_points",
        "net_pnl", "expectancy", "profit_factor", "max_drawdown",
    ]].copy()
    display.columns = [
        "Min RR", "Orders", "CALL", "PUT", "Targets", "Stops", "Profitable %",
        "Avg target pts", "Avg stop pts", "Net P&L", "P&L / trade", "Profit factor", "Max drawdown",
    ]
    display["Profitable %"] = display["Profitable %"].map(lambda value: round(value * 100, 1))
    st.dataframe(display, use_container_width=True, hide_index=True)
    chart = comparison.set_index("rr_cutoff")[["net_pnl", "max_drawdown"]].rename(
        columns={"net_pnl": "Net P&L", "max_drawdown": "Max drawdown"}
    )
    st.bar_chart(chart)
    st.subheader("Current live V2 policy benchmark")
    st.caption("Probability > selected cutoff and expected-value proxy ≥ +0.10R.")
    bench = st.columns(5)
    bench[0].metric("Orders", current_v2["trades"])
    bench[1].metric("Profitable", percent(current_v2["win_probability"]))
    bench[2].metric("Target / stop", f"{current_v2['targets']} / {current_v2['stops']}")
    bench[3].metric("Net P&L", money(current_v2["net_pnl"]))
    bench[4].metric("P&L / trade", money(current_v2["expectancy"]))

with trades_tab:
    if trades.empty:
        st.info("No CALL or PUT forecast passed the selected filters.")
    else:
        trade_display = trades.copy()
        trade_display["timestamp"] = pd.to_datetime(trade_display["timestamp"]).dt.date
        trade_display["probability"] = (trade_display["probability"] * 100).round(1)
        trade_display = trade_display.rename(columns={
            "timestamp": "Date", "direction": "Side", "probability": "Probability %",
            "predicted_reward_risk": "Predicted RR", "option_target_points": "Target option pts",
            "option_stop_points": "Stop option pts", "outcome": "Outcome", "net_pnl": "Net P&L",
        })
        st.dataframe(
            trade_display[["Date", "Side", "Probability %", "Predicted RR", "Target option pts", "Stop option pts", "Outcome", "Net P&L"]],
            use_container_width=True,
            hide_index=True,
        )
        daily = trades.assign(Date=pd.to_datetime(trades["timestamp"]).dt.date).groupby("Date")["net_pnl"].sum().cumsum()
        st.line_chart(daily.rename("Cumulative net P&L"))

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
        - Each qualifying CALL and PUT gets a separate capital deployment; capital is not compounded.
        - Predicted target and stop are NIFTY percentages converted to option points through the selected delta.
        - If target and stop both occur inside one 4-hour candle, the simulator conservatively records the stop first.
        - A trade that reaches neither level exits at the first candle's close.
        - Results are out-of-sample for the selected six months, but repeated cutoff testing on the same holdout can still overfit it.
        """
    )
