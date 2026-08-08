"""Archive dashboard/mobile trading statistics and reset their runtime counters."""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
ARCHIVE_DIR = BASE_DIR / "archive"

DATA_NAMES = {
    "trade_history.csv",
    "analysis_history.csv",
    "scan_decisions.csv",
    "ganesh_gap_scans.csv",
    "ganesh_gap_banknifty_scans.csv",
    "day_risk_state.json",
    "monitor_health.json",
    "stock_scanner_status.json",
    "score_followthrough_audit.csv",
    "score_followthrough_status.json",
    "vamsi_adaptive_score_config.json",
}

DATA_PATTERNS = (
    "counterfactual_*",
    "post_market_review_*",
    "post_market_summary_*",
    "post_market_llm_insights_*",
    "executed_trade_forensics_*",
    "rejected_signal_forensics_*",
    "trade_forensics_summary_*",
    "session_insights_*",
    "banknifty_veto_observations_*",
    "banknifty_veto_episodes_*",
    "banknifty_veto_summary_*",
)

NEW_ACCOUNT_DATA_NAMES = {
    "trading_config.json",
    "apns_devices.json",
    "upstox_market_stream.json",
    "upstox_portfolio_stream.json",
    "upstox_stream_status.json",
}


def active_local_states() -> list[Path]:
    active = []
    for path in sorted(BASE_DIR.glob("trade_state_*.json")):
        try:
            state = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            state = {}
        if state.get("instrument_key") and state.get("status") in {
            "POSITION_OPEN",
            "EXIT_PENDING",
            "BUY_PLACED_NOT_COMPLETE",
            "SELL_PLACED_NOT_COMPLETE",
        }:
            active.append(path)
    return active


def reset_candidates(new_account: bool = False) -> list[Path]:
    candidates = []
    candidates.extend(BASE_DIR.glob("trade_state_*.json"))
    candidates.extend(BASE_DIR.glob("reentry_guard_*.json"))
    candidates.append(BASE_DIR / "daily_trade_count.json")
    candidates.extend(DATA_DIR / name for name in DATA_NAMES)
    for pattern in DATA_PATTERNS:
        candidates.extend(DATA_DIR.glob(pattern))
    if new_account:
        candidates.extend(DATA_DIR / name for name in NEW_ACCOUNT_DATA_NAMES)
    candidates.append(DATA_DIR / "watch_states")
    candidates.append(LOG_DIR / "trade_bot.log")
    return sorted({path for path in candidates if path.exists()}, key=lambda path: str(path))


def archive_destination(root: Path, source: Path) -> Path:
    try:
        relative = source.relative_to(BASE_DIR)
    except ValueError:
        relative = Path(source.name)
    return root / relative


def run_reset(
    confirm: bool = False,
    force: bool = False,
    new_account: bool = False,
) -> tuple[Path, list[Path]]:
    active = active_local_states()
    if active and not force:
        names = ", ".join(path.name for path in active)
        raise RuntimeError(
            "Active local bot state found: "
            + names
            + ". Confirm broker positions are closed before using --force."
        )

    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    archive_root = ARCHIVE_DIR / f"tracking_reset_{stamp}"
    candidates = reset_candidates(new_account=new_account)
    if not confirm:
        return archive_root, candidates

    for source in candidates:
        destination = archive_destination(archive_root, source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / "trade_bot.log").touch()
    return archive_root, candidates


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Archive mobile/dashboard trading statistics without deleting account "
            "configuration, tokens, device registrations, or market-data caches."
        )
    )
    parser.add_argument("--confirm", action="store_true", help="Perform the reset.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Proceed despite an active local state only after broker positions are verified closed.",
    )
    parser.add_argument(
        "--new-account",
        action="store_true",
        help=(
            "Also archive copied mobile profile, APNs devices, and account stream "
            "caches so configuration regenerates for a newly cloned account."
        ),
    )
    args = parser.parse_args()

    archive_root, candidates = run_reset(
        confirm=args.confirm,
        force=args.force,
        new_account=args.new_account,
    )
    action = "Archived" if args.confirm else "Would archive"
    print(f"{action} {len(candidates)} item(s):")
    for path in candidates:
        print(f"  {path.relative_to(BASE_DIR)}")
    print(f"Archive: {archive_root}")
    if not args.confirm:
        print("Dry run only. Run again with --confirm after checking the list.")


if __name__ == "__main__":
    main()
