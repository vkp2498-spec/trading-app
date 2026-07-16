import json
from pathlib import Path

import pandas as pd


CATEGORY_ORDER = [
    "NIFTY_OPTION_BUY",
    "NIFTY_OPTION_SELL",
    "BANKNIFTY_OPTION_BUY",
    "BANKNIFTY_OPTION_SELL",
    "STOCK_FUTURE",
]


def _metrics(frame):
    if frame.empty:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_percent": 0.0,
            "gross_pnl": 0.0,
            "estimated_costs": 0.0,
            "net_pnl": 0.0,
            "profit_factor": 0.0,
            "expectancy": 0.0,
        }
    pnl = pd.to_numeric(frame["net_pnl"], errors="coerce").fillna(0)
    winners = pnl[pnl > 0]
    losers = pnl[pnl < 0]
    loss_total = abs(float(losers.sum()))
    return {
        "trades": int(len(frame)),
        "wins": int((pnl > 0).sum()),
        "losses": int((pnl < 0).sum()),
        "win_percent": round(float((pnl > 0).mean() * 100), 2),
        "gross_pnl": round(float(frame["gross_pnl"].sum()), 2),
        "estimated_costs": round(float(frame["estimated_costs"].sum()), 2),
        "net_pnl": round(float(pnl.sum()), 2),
        "profit_factor": round(float(winners.sum()) / loss_total, 3) if loss_total else 0.0,
        "expectancy": round(float(pnl.mean()), 2),
    }


def build_reports(trades, decisions, output_dir, assumptions=None, coverage=None):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    trades_df = pd.DataFrame(trades)
    decisions_df = pd.DataFrame(decisions)

    if trades_df.empty:
        trades_df = pd.DataFrame(
            columns=[
                "trade_date", "category", "symbol", "trading_symbol", "entry_time",
                "exit_time", "entry_price", "exit_price", "quantity", "gross_pnl",
                "estimated_costs", "net_pnl", "exit_reason",
            ]
        )
    else:
        trades_df["gross_pnl"] = pd.to_numeric(trades_df["gross_pnl"], errors="coerce").fillna(0)
        trades_df["estimated_costs"] = pd.to_numeric(
            trades_df["estimated_costs"], errors="coerce"
        ).fillna(0)
        trades_df["net_pnl"] = trades_df["gross_pnl"] - trades_df["estimated_costs"]
        sort_column = "entry_time" if "entry_time" in trades_df.columns else "trade_date"
        trades_df = trades_df.sort_values(sort_column).reset_index(drop=True)

    category_rows = []
    for category in CATEGORY_ORDER:
        row = {"category": category, **_metrics(trades_df[trades_df["category"] == category])}
        category_rows.append(row)
    category_df = pd.DataFrame(category_rows)

    daily_rows = []
    for trade_date, part in trades_df.groupby("trade_date", sort=True):
        row = {"trade_date": trade_date, **_metrics(part)}
        for category in CATEGORY_ORDER:
            row[f"{category}_pnl"] = round(
                float(part.loc[part["category"] == category, "net_pnl"].sum()), 2
            )
            row[f"{category}_trades"] = int((part["category"] == category).sum())
        daily_rows.append(row)
    daily_df = pd.DataFrame(daily_rows)
    if not daily_df.empty:
        daily_df["cumulative_net_pnl"] = daily_df["net_pnl"].cumsum().round(2)
        daily_df["equity_peak"] = daily_df["cumulative_net_pnl"].cummax()
        daily_df["drawdown"] = (
            daily_df["cumulative_net_pnl"] - daily_df["equity_peak"]
        ).round(2)

    # Keep one row per day and category for the dashboard and for spreadsheet
    # review. These columns make category-level cumulative performance explicit.
    category_daily_rows = []
    if not trades_df.empty:
        for trade_date, part in trades_df.groupby("trade_date", sort=True):
            for category in CATEGORY_ORDER:
                category_part = part[part["category"] == category]
                metrics = _metrics(category_part)
                category_daily_rows.append(
                    {"trade_date": trade_date, "category": category, **metrics}
                )
    category_daily_df = pd.DataFrame(category_daily_rows)
    if not category_daily_df.empty:
        category_daily_df["cumulative_net_pnl"] = (
            category_daily_df.sort_values(["category", "trade_date"])
            .groupby("category")["net_pnl"]
            .cumsum()
            .round(2)
        )
        category_daily_df["equity_peak"] = (
            category_daily_df.sort_values(["category", "trade_date"])
            .groupby("category")["cumulative_net_pnl"]
            .cummax()
        )
        category_daily_df["drawdown"] = (
            category_daily_df["cumulative_net_pnl"] - category_daily_df["equity_peak"]
        ).round(2)

    overall = _metrics(trades_df)
    overall["max_drawdown"] = (
        round(float(daily_df["drawdown"].min()), 2) if not daily_df.empty else 0.0
    )
    overall["from_date"] = str(trades_df["trade_date"].min()) if not trades_df.empty else None
    overall["to_date"] = str(trades_df["trade_date"].max()) if not trades_df.empty else None
    summary = {
        "overall": overall,
        "categories": category_rows,
        "assumptions": assumptions or [],
        "coverage": coverage or {},
    }

    trades_df.to_csv(output_dir / "trades.csv", index=False)
    decisions_df.to_csv(output_dir / "decisions.csv", index=False)
    category_df.to_csv(output_dir / "category_summary.csv", index=False)
    daily_df.to_csv(output_dir / "daily_summary.csv", index=False)
    category_daily_df.to_csv(output_dir / "category_daily_summary.csv", index=False)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    return summary
