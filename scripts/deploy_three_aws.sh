#!/usr/bin/env bash
set -euo pipefail

reset_tracking_data=false
if [[ "${1:-}" == "--reset-tracking-data" ]]; then
  reset_tracking_data=true
  shift
fi

if [[ "$#" -ne 3 ]]; then
  echo "Usage: $0 [--reset-tracking-data] <vamsi-ssh-host> <ganesh-ssh-host> <sastry-ssh-host>" >&2
  echo "Each value may be an SSH alias or ubuntu@IP-address." >&2
  exit 2
fi

hosts=("$1" "$2" "$3")
remote_app_dir="${REMOTE_APP_DIR:-/home/ubuntu/trading-app}"
ssh_options=(-o BatchMode=yes -o ConnectTimeout=15)
if [[ -n "${SSH_IDENTITY_FILE:-}" ]]; then
  ssh_options+=(-i "$SSH_IDENTITY_FILE")
fi

remote_dir_quoted=$(printf '%q' "$remote_app_dir")

echo "Phase 1/3: pulling origin/main on all three instances"
for host in "${hosts[@]}"; do
  echo "[$host] pulling code"
  ssh "${ssh_options[@]}" "$host" \
    "cd $remote_dir_quoted && git pull --ff-only origin main"
done

echo "Phase 2/3: confirming no instance has an active bot position"
for host in "${hosts[@]}"; do
  echo "[$host] safety preflight"
  ssh "${ssh_options[@]}" "$host" \
    "cd $remote_dir_quoted && venv/bin/python scripts/sync_core_env.py --env-file .env --dry-run --refuse-active-state"
done

echo "Phase 3/3: resetting requested metrics, normalizing env, generating calibration, and restarting long-running code"
for host in "${hosts[@]}"; do
  echo "[$host] applying canonical NIFTY strategy"
  reset_command=""
  if [[ "$reset_tracking_data" == true ]]; then
    reset_command="venv/bin/python reset_tracking_data.py --confirm && "
  fi
  ssh "${ssh_options[@]}" "$host" \
    "set -e; cd $remote_dir_quoted && (pkill -f '[t]rade_bot.py --monitor' || true) && ${reset_command}venv/bin/python scripts/sync_core_env.py --env-file .env --refuse-active-state && venv/bin/python adaptive_score_calibration.py && if [ -f .trading_cron_disabled ]; then venv/bin/python scripts/sync_trading_cron.py --app-dir $remote_dir_quoted --disable; else venv/bin/python scripts/sync_trading_cron.py --app-dir $remote_dir_quoted; fi && for service in nifty-app hk-mobile-api upstox-streams upstox-token-webhook; do if systemctl list-unit-files \"\${service}.service\" --no-legend 2>/dev/null | grep -q \"\${service}.service\"; then sudo systemctl restart \"\${service}\"; fi; done"
done

echo "Deployment complete on all three instances. Cron will relaunch the monitor when scheduled."
