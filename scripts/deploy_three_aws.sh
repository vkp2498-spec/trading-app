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
roles=("ml-shadow" "disabled" "disabled")
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

echo "Phase 3/3: installing ML dependencies, applying roles, and restarting services"
for index in "${!hosts[@]}"; do
  host="${hosts[$index]}"
  role="${roles[$index]}"
  echo "[$host] applying role: $role"
  reset_command=""
  if [[ "$reset_tracking_data" == true && "$role" == "ml-shadow" ]]; then
    reset_command="venv/bin/python reset_tracking_data.py --confirm && "
  fi
  if [[ "$role" == "ml-shadow" ]]; then
    role_command="rm -f .trading_cron_disabled && venv/bin/python ml_shadow_v1.py --train && venv/bin/python scripts/sync_trading_cron.py --app-dir $remote_dir_quoted"
  else
    role_command="touch .trading_cron_disabled && venv/bin/python scripts/sync_trading_cron.py --app-dir $remote_dir_quoted --disable"
  fi
  ssh "${ssh_options[@]}" "$host" \
    "set -e; cd $remote_dir_quoted && (pkill -f '[t]rade_bot.py --monitor' || true) && (pkill -f '[m]l_shadow_v1.py --monitor' || true) && venv/bin/pip install -r requirements.txt && ${reset_command}venv/bin/python scripts/sync_core_env.py --env-file .env --role $role --refuse-active-state && $role_command && for service in nifty-app hk-mobile-api upstox-streams upstox-token-webhook; do if systemctl list-unit-files \"\${service}.service\" --no-legend 2>/dev/null | grep -q \"\${service}.service\"; then sudo systemctl restart \"\${service}\"; fi; done"
done

echo "Deployment complete: Vamsi=ML paper; Ganesh/Sastry=all trading schedules disabled."
