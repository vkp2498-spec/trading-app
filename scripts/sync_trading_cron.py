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
    "ml_shadow_v1.py",
    "ml_shadow_4h_v2_live.py",
    "vamsi_kb_intraday.py",
)


def canonical_block(app_dir: Path) -> list[str]:
    root = str(app_dir)
    python = f"{root}/venv/bin/python"
    log_dir = f"{root}/logs"
    return [
        BLOCK_START,
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


def normalized_crontab(existing: str, app_dir: Path, enabled: bool = True) -> str:
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
        retained.extend(canonical_block(app_dir))
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
    args = parser.parse_args()
    app_dir = Path(args.app_dir).expanduser().resolve()
    updated = normalized_crontab(read_crontab(), app_dir, enabled=not args.disable)
    if args.dry_run:
        print(updated, end="")
        return
    (app_dir / "logs").mkdir(parents=True, exist_ok=True)
    subprocess.run(["crontab", "-"], input=updated, text=True, check=True)
    print(
        "Managed NIFTY trading cron disabled"
        if args.disable
        else "Canonical VAMSI_KB_INTRADAY_V1 cron installed"
    )


if __name__ == "__main__":
    main()
