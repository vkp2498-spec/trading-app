import argparse
import json
import os
import sys
import traceback
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from backtest_data import UpstoxBacktestData
from backtest_report import build_reports
from strategy_core import load_env_file
from strategy_replay import StrategyReplay


IST = ZoneInfo("Asia/Kolkata")
BASE_DIR = Path(__file__).resolve().parent
BACKTEST_DIR = BASE_DIR / "data" / "backtests"
STATUS_FILE = BACKTEST_DIR / "status.json"
LATEST_FILE = BACKTEST_DIR / "latest.json"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, default=str))
    temporary.replace(path)


def in_market_hours():
    now = datetime.now(IST)
    return now.weekday() < 5 and time(9, 15) <= now.time() <= time(15, 35)


def default_dates(days):
    end = datetime.now(IST).date() - timedelta(days=1)
    # Twenty-two trading sessions are approximately one calendar month in
    # India. Keep the default bounded to that window rather than stretching it
    # into a six-week replay.
    start = end - timedelta(days=max(int(days * 1.45), days + 7))
    return start, end


def parse_args():
    parser = argparse.ArgumentParser(description="Point-in-time strategy replay using Upstox Plus history")
    parser.add_argument("--days", type=int, default=22, help="Approximate trading days when dates are omitted")
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--use-llm", action="store_true")
    parser.add_argument("--no-stock-futures", action="store_true")
    parser.add_argument(
        "--portfolio-mode",
        choices=("live", "independent"),
        default=os.getenv("BACKTEST_PORTFOLIO_MODE", "live"),
        help="live models one account; independent reports each strategy track separately",
    )
    parser.add_argument("--slippage-bps", type=float, default=float(os.getenv("BACKTEST_SLIPPAGE_BPS", "8")))
    parser.add_argument("--cost-per-order", type=float, default=float(os.getenv("BACKTEST_COST_PER_ORDER", "25")))
    parser.add_argument("--allow-market-hours", action="store_true")
    parser.add_argument("--validate-access", action="store_true")
    return parser.parse_args()


def main():
    load_env_file()
    args = parse_args()
    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
    state = {
        "status": "STARTING",
        "started_at": datetime.now(IST).isoformat(),
        "message": "Preparing Upstox Plus replay",
        "pid": os.getpid(),
    }

    def progress(message, **extra):
        state.update({"status": "RUNNING", "message": message, "updated_at": datetime.now(IST).isoformat(), **extra})
        write_json(STATUS_FILE, state)
        print(message, flush=True)

    write_json(STATUS_FILE, state)
    try:
        data = UpstoxBacktestData(BACKTEST_DIR / "cache", progress=progress)
        if args.validate_access:
            result = data.validate_access()
            print(json.dumps({"upstox_access": result}))
            state.update({"status": "COMPLETE", "message": "Upstox access is valid", "completed_at": datetime.now(IST).isoformat()})
            write_json(STATUS_FILE, state)
            return 0
        if in_market_hours() and not args.allow_market_hours:
            raise RuntimeError("Historical replay is disabled during market hours to avoid competing with the live bot")
        default_start, default_end = default_dates(args.days)
        from_date = args.from_date or default_start
        to_date = args.to_date or default_end
        run_id = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
        output_dir = BACKTEST_DIR / "runs" / run_id
        engine = StrategyReplay(
            data=data,
            from_date=from_date,
            to_date=to_date,
            progress=progress,
            use_llm=args.use_llm,
            include_stock_futures=not args.no_stock_futures,
            portfolio_mode=args.portfolio_mode,
            slippage_bps=args.slippage_bps,
            cost_per_order=args.cost_per_order,
        )
        trades, decisions, coverage = engine.run()
        assumptions = [
            "All decisions use completed candles only; entries use the next completed five-minute candle.",
            (
                "Live portfolio mode allows one global position at a time and every trade uses one exchange lot."
                if args.portfolio_mode == "live"
                else "Independent mode evaluates each strategy category separately; category totals are diagnostic and cannot be added together as one account result."
            ),
            "When target and stop occur in one candle, the stop is assumed first.",
            "A newly trailed stop becomes active from the following candle.",
            f"Entry and exit slippage are estimated at {args.slippage_bps:.1f} bps per side.",
            f"Estimated charges are Rs {args.cost_per_order:.2f} per order, two orders per trade.",
            "Historical option-chain OI is reconstructed from contract candles; it is not a saved Upstox option-chain snapshot.",
            "Historical order-book depth and daily FII snapshots are unavailable; the institutional component is marked RECONSTRUCTED_HISTORY.",
            "The portfolio tries index option candidates first and scans stock futures only when neither index has a qualified setup.",
            "LLM decisions are replayed only when --use-llm is supplied; deterministic safety rules always remain active.",
            f"Portfolio mode: {args.portfolio_mode}.",
        ]
        summary = build_reports(trades, decisions, output_dir, assumptions, coverage)
        latest = {
            "run_id": run_id,
            "path": str(output_dir),
            "from_date": str(from_date),
            "to_date": str(to_date),
            "completed_at": datetime.now(IST).isoformat(),
            "summary": summary,
        }
        write_json(LATEST_FILE, latest)
        state.update(
            {
                "status": "COMPLETE",
                "message": f"Replay complete: {len(trades)} trades",
                "completed_at": datetime.now(IST).isoformat(),
                "run_id": run_id,
                "output_dir": str(output_dir),
            }
        )
        write_json(STATUS_FILE, state)
        print(json.dumps(summary, indent=2, default=str))
        return 0
    except Exception as error:
        state.update(
            {
                "status": "FAILED",
                "message": str(error),
                "failed_at": datetime.now(IST).isoformat(),
                "traceback": traceback.format_exc(),
            }
        )
        write_json(STATUS_FILE, state)
        print(traceback.format_exc(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
