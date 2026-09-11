#!/usr/bin/env python3
"""Install the canonical VAMSI knowledge-engine cron without disturbing other jobs."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


BLOCK_START = "# BEGIN CODEX NIFTY TRADING CRON"
BLOCK_END = "# END CODEX NIFTY TRADING CRON"
MANAGED_COMMANDS = (
    "trade_bot.py",
    "adaptive_score_calibration.py",
    "post_market_score_audit.py",
    "vamsi_kb_daily_plan.py",
    "ml_shadow_v1.py",
    "ml_shadow_4h_v2_live.py",
    "vamsi_kb_intraday.py",
    "vamsi_opening_pulse.py",
    "vamsi_nifty_option_buy.py",
    "sync_upstox_today_trades.py",
)


def canonical_block(app_dir: Path) -> list[str]:
    root = str(app_dir)
    python = f"{root}/venv/bin/python"
    log_dir = f"{root}/logs"
    return [
        BLOCK_START,
        "# Freeze the evidence-based plan at 08:30 IST using prior completed days.",
        f"0 3 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/vamsi_kb_daily_plan.lock {python} {root}/vamsi_kb_daily_plan.py --generate >> {log_dir}/daily_plan.log 2>&1",
        "# AWS cron uses UTC. Scan one minute after each completed 5M candle.",
        f"51,56 3 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/vamsi_kb_scan.lock {python} {root}/vamsi_kb_intraday.py --scan >> {log_dir}/trade_bot.log 2>&1",
        f"1-56/5 4-8 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/vamsi_kb_scan.lock {python} {root}/vamsi_kb_intraday.py --scan >> {log_dir}/trade_bot.log 2>&1",
        f"1,6,11,16,21,26,31,36,41,46,51,56 9 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/vamsi_kb_scan.lock {python} {root}/vamsi_kb_intraday.py --scan >> {log_dir}/trade_bot.log 2>&1",
        "# One flock-protected monitor owns protection, staged trailing and exits.",
        f"45-59 3 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/trade_bot_monitor.lock {python} {root}/trade_bot.py --monitor >> {log_dir}/trade_bot.log 2>&1",
        f"* 4-9 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/trade_bot_monitor.lock {python} {root}/trade_bot.py --monitor >> {log_dir}/trade_bot.log 2>&1",
        f"0 10 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/trade_bot_monitor.lock {python} {root}/trade_bot.py --monitor >> {log_dir}/trade_bot.log 2>&1",
        "# Final bot square-off is 15:29 IST.",
        f"59 9 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/trade_bot_squareoff.lock {python} {root}/trade_bot.py --squareoff >> {log_dir}/trade_bot.log 2>&1",
        "# Every five-minute verdict is audited at 16:00 IST, including overlaps.",
        f"30 10 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/post_market_score_audit.lock {python} {root}/post_market_score_audit.py --knowledge-engine-all-scans >> {log_dir}/post_market_audit.log 2>&1",
        BLOCK_END,
    ]


def opening_pulse_block(app_dir: Path) -> list[str]:
    root = str(app_dir)
    python = f"{root}/venv/bin/python"
    log_dir = f"{root}/logs"
    return [
        BLOCK_START,
        "# One mandatory SENSEX opening-pulse decision at 09:20 IST.",
        f"50 3 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/vamsi_opening_pulse.lock {python} {root}/vamsi_opening_pulse.py --scan >> {log_dir}/trade_bot.log 2>&1",
        "# Cancel the GTT and market-square-off any remaining position at 15:00 IST.",
        f"30 9 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/vamsi_opening_pulse_squareoff.lock {python} {root}/vamsi_opening_pulse.py --squareoff >> {log_dir}/trade_bot.log 2>&1",
        "# Reconcile the completed broker result into dashboard history at 15:05 IST.",
        f"35 9 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/upstox_trade_sync.lock {python} {root}/sync_upstox_today_trades.py >> {log_dir}/upstox_trade_sync.log 2>&1",
        BLOCK_END,
    ]


def nifty_option_buy_block(app_dir: Path) -> list[str]:
    root = str(app_dir)
    python = f"{root}/venv/bin/python"
    log_dir = f"{root}/logs"
    return [
        BLOCK_START,
        "# UTC: scans at 09:15, 09:30, ... 14:45 IST; 09:15 waits for the first candle.",
        f"45 3 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/vamsi_nifty_option_buy.lock {python} {root}/vamsi_nifty_option_buy.py --scan >> {log_dir}/trade_bot.log 2>&1",
        f"0,15,30,45 4-8 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/vamsi_nifty_option_buy.lock {python} {root}/vamsi_nifty_option_buy.py --scan >> {log_dir}/trade_bot.log 2>&1",
        f"0,15 9 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/vamsi_nifty_option_buy.lock {python} {root}/vamsi_nifty_option_buy.py --scan >> {log_dir}/trade_bot.log 2>&1",
        "# One monitor owns the broker stop, target, time stop, reversal exit and trailing.",
        f"45-59 3 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/trade_bot_monitor.lock {python} {root}/trade_bot.py --monitor >> {log_dir}/trade_bot.log 2>&1",
        f"* 4-8 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/trade_bot_monitor.lock {python} {root}/trade_bot.py --monitor >> {log_dir}/trade_bot.log 2>&1",
        f"* 9 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/trade_bot_monitor.lock {python} {root}/trade_bot.py --monitor >> {log_dir}/trade_bot.log 2>&1",
        "# Close any remaining bot position at 15:25 IST; reconcile at 15:30 IST.",
        f"55 9 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/trade_bot_squareoff.lock {python} {root}/trade_bot.py --squareoff >> {log_dir}/trade_bot.log 2>&1",
        f"0 10 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/upstox_trade_sync.lock {python} {root}/sync_upstox_today_trades.py >> {log_dir}/upstox_trade_sync.log 2>&1",
        BLOCK_END,
    ]


def normalized_crontab(
    existing: str,
    app_dir: Path,
    enabled: bool = True,
    mode: str = "knowledge",
) -> str:
    retained = []
    inside_managed_block = False
    for line in existing.splitlines():
        stripped = line.strip()
        if stripped == BLOCK_START:
            inside_managed_block = True
            continue
        if stripped == BLOCK_END:
            inside_managed_block = False
            continue
        if inside_managed_block:
            continue
        if any(command in line for command in MANAGED_COMMANDS):
            continue
        retained.append(line.rstrip())
    while retained and not retained[-1]:
        retained.pop()
    if retained and enabled:
        retained.append("")
    if enabled:
        retained.extend(
            opening_pulse_block(app_dir)
            if mode == "opening-pulse"
            else nifty_option_buy_block(app_dir)
            if mode == "nifty-option-buy"
            else canonical_block(app_dir)
        )
    return "\n".join(retained) + ("\n" if retained else "")


def read_crontab() -> str:
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout
    if "no crontab" in result.stderr.lower():
        return ""
    raise RuntimeError(result.stderr.strip() or "could not read crontab")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", default=str(Path.cwd()))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--disable",
        action="store_true",
        help="Remove all managed trading jobs while preserving unrelated cron entries.",
    )
    parser.add_argument(
        "--mode",
        choices=("knowledge", "opening-pulse", "nifty-option-buy"),
        default="knowledge",
        help="Install the selected production schedule.",
    )
    args = parser.parse_args()
    app_dir = Path(args.app_dir).expanduser().resolve()
    updated = normalized_crontab(
        read_crontab(),
        app_dir,
        enabled=not args.disable,
        mode=args.mode,
    )
    if args.dry_run:
        print(updated, end="")
        return
    (app_dir / "logs").mkdir(parents=True, exist_ok=True)
    subprocess.run(["crontab", "-"], input=updated, text=True, check=True)
    print(
        "Managed NIFTY trading cron disabled"
        if args.disable
        else (
            "VAMSI_OPENING_PULSE_V1 cron installed"
            if args.mode == "opening-pulse"
            else "VAMSI_NIFTY_OPTION_BUY_V1 cron installed"
            if args.mode == "nifty-option-buy"
            else "Canonical VAMSI_KB_INTRADAY_V1 cron installed"
        )
    )


if __name__ == "__main__":
    main()
