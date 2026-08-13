#!/usr/bin/env python3
"""Install the canonical ML_SHADOW_V1 paper cron without disturbing other jobs."""

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
)


def canonical_block(app_dir: Path) -> list[str]:
    root = str(app_dir)
    python = f"{root}/venv/bin/python"
    log_dir = f"{root}/logs"
    return [
        BLOCK_START,
        "# AWS cron uses UTC. Train at 08:45 IST using data only through the prior day.",
        f"15 3 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/ml_shadow_train.lock {python} {root}/ml_shadow_v1.py --train >> {log_dir}/ml_shadow_v1.log 2>&1",
        "# Score each completed 15-minute NIFTY candle from 09:31 through 14:16 IST.",
        f"1,16,31,46 4-8 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/ml_shadow_scan.lock {python} {root}/ml_shadow_v1.py --scan >> {log_dir}/ml_shadow_v1.log 2>&1",
        "# A single flock-protected paper monitor runs for the session.",
        f"44-59 3 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/ml_shadow_monitor.lock {python} {root}/ml_shadow_v1.py --monitor >> {log_dir}/ml_shadow_v1.log 2>&1",
        f"* 4-9 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/ml_shadow_monitor.lock {python} {root}/ml_shadow_v1.py --monitor >> {log_dir}/ml_shadow_v1.log 2>&1",
        f"59 9 * * 1-5 cd {root} && /usr/bin/flock -n /tmp/ml_shadow_squareoff.lock {python} {root}/ml_shadow_v1.py --squareoff >> {log_dir}/ml_shadow_v1.log 2>&1",
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
        else "Canonical ML_SHADOW_V1 paper cron installed"
    )


if __name__ == "__main__":
    main()
