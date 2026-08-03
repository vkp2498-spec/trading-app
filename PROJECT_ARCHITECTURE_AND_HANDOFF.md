# Hare Krishna Trading Bot: Architecture and Project Handoff

Last updated: 2026-08-03 IST
Repository: `git@github.com:vkp2498-spec/trading-app.git`  
Primary branch: `main`

## 1. Purpose of this document

This is the durable handoff for the trading project developed through the original Codex conversation on the Mac mini. It records the current architecture, strategy decisions, deployment model, operating procedures, important incidents, retired experiments, and the reasoning that shaped the system.

It is intentionally not a raw chat transcript. The source conversation contains many intermediate ideas, temporary fixes, obsolete settings, copied logs, and credentials-adjacent operational details. This document condenses that history into an engineering source of context that can be read on a new computer or in a new Codex task.

Never add API keys, access tokens, webhook secrets, APNs private keys, Android keystore passwords, or production `.env` contents to this document or Git.

## 2. Current project state

The project is an experimental automated trading platform for Indian markets using Upstox. It has:

- A shared Python codebase deployed to two AWS Lightsail Ubuntu instances.
- Separate Vamsi and Ganesh strategy engines selected through `.env`.
- Intraday option entry checks, live position monitoring, broker-side protective stops, risk controls, and forced square-off.
- A Streamlit web dashboard.
- A FastAPI mobile backend used by iOS and Android clients.
- Daily Upstox notifier-token automation through a webhook.
- Upstox Plus streaming and market-information integration.
- Post-market reviews, forensic analysis, score follow-through audits, and historical replay tools.
- A safe utility for resetting dashboard/mobile tracking while preserving configuration and credentials.

The system uses real capital, but historical research has not established a stable profitable edge. It must be treated as experimental and safety-critical. A clean run, a high score, or a profitable day is not evidence of future profitability.

## 3. Source-of-truth order

When this document and the implementation differ, use this order:

1. Current code on `main`.
2. Effective account-specific `.env` on the relevant AWS instance.
3. Broker positions/orders and local runtime state.
4. Automated tests and generated data.
5. This handoff document.
6. Historical conversation notes.

The AWS `.env` files are deliberately not in Git, so account behavior can differ even when both instances run the same commit.

## 4. High-level architecture

```mermaid
flowchart TD
    CRON["AWS cron schedules"] --> BOT["trade_bot.py"]
    BOT --> SELECT{"TRADING_ENGINE"}
    SELECT -->|VAMSI| VE["Vamsi signal engine"]
    SELECT -->|GANESH| GE["Ganesh gap-reversal engine"]

    VE --> CORE["Option chain, technicals, breadth, institutional context"]
    GE --> GAP["Opening gap, active 2H candle, pivots, Bollinger middle"]

    CORE --> RISK["Shared execution and portfolio-risk layer"]
    GAP --> RISK
    RISK --> UPSTOX["Upstox orders and broker protective stops"]

    MON["Long-running position monitor"] --> STATE["Bot state slots"]
    STATE --> UPSTOX
    UPSTOX --> JOURNAL["Trade and analysis journals"]

    JOURNAL --> DASH["Streamlit dashboard"]
    JOURNAL --> API["FastAPI mobile API"]
    API --> IOS["iOS app"]
    API --> ANDROID["Android app"]

    TOKEN["Daily token request"] --> APPROVAL["Upstox approval"]
    APPROVAL --> NGINX["Nginx HTTPS webhook"]
    NGINX --> WEBHOOK["token_webhook.py on port 9000"]
    WEBHOOK --> ENV["Atomic .env token update"]

    STREAM["upstox_streams.py"] --> CORE
    STREAM --> MON
```

## 5. Repository map

### Live trading and strategy

- `trade_bot.py`: Main orchestration, entry dispatch, broker execution, persistent state, live monitor, exits, square-off, and shared safety controls.
- `strategy_core.py`: Upstox option-chain access, expiry selection, option recommendations, and directional signal construction.
- `market_technicals.py`: 5-minute, 15-minute, and 2-hour technical analysis, pivots, Bollinger Bands, moving averages, momentum, and option-premium level conversion.
- `signal_score.py`: Weighted signal alignment.
- `live_trade_filters.py`: Market regime, entry structure, reward/risk, extension, and invalidation filters.
- `portfolio_risk.py`: Aggregate risk, day-level circuit breakers, and correlation checks.
- `institutional_flow.py`: Futures/OI, nearby option flow, VIX, FII/DII, PCR, max pain, and persistence context.
- `banknifty_breadth.py`: Major-bank breadth for BANKNIFTY.
- `nifty_breadth.py`: NIFTY constituent and heavyweight breadth.
- `market_information.py`: Upstox Plus market-information data and caching.
- `ganesh_gap_reversal.py`: Pure functions for Ganesh's opening-gap reversal strategy.
- `option_chain_trend.py`: Persistent option-chain trend snapshots.

### Streaming, broker, and persistence

- `upstox_streams.py`: Market and portfolio stream service and cache files.
- `upstox-streams.service.example`: Example systemd unit.
- `safe_storage.py`: Locked and atomic state/file operations.
- `trade_journal.py`: Executed trade journal.
- `analysis_journal.py`: Signal analysis records.
- `scan_journal.py`: Concise scan decisions and score history.
- `trade_history_schema.py`: Trade-history schema normalization.
- `merge_trade_history.py`: Safe merging and deduplication of trade history.
- `sync_upstox_today_trades.py`: Broker-to-local reconciliation aid.
- `manual_index_trade.py`: Controlled manual index-option entry integrated with bot monitoring.

### Dashboard and mobile

- `performance_dashboard.py`: Streamlit performance and research dashboard.
- `dashboard_data.py`: Shared dashboard/mobile payload construction and state aggregation.
- `mobile_api.py`: FastAPI mobile dashboard, trading configuration, screener, and order endpoints.
- `mobile_orders.py`: Mobile delivery-order operations.
- `trading_config.py`: Mobile-selected capital profile and dynamically scaled risk settings.
- `apns_push.py`: Apple push notifications.
- `stock_screener.py`: Weekly delivery-stock research screener.
- `aws_control.py`: Restricted AWS-side control helpers.

### Authentication

- `request_upstox_token.py`: Sends the daily Upstox token approval request.
- `token_webhook.py`: Validates the notifier webhook and atomically updates `.env`.

### Research and diagnostics

- `post_market_review.py`: Post-market review and loss analysis.
- `post_market_score_audit.py`: Score-bucket follow-through audit.
- `adaptive_score_calibration.py`: Pre-market Vamsi score-rule calibration from stored follow-through evidence.
- `banknifty_post_market.py`: BANKNIFTY veto/technical follow-through study.
- `trade_forensics.py`: Executed and rejected signal excursion analysis.
- `session_insights.py`: Session-level observations.
- `counterfactual_replay.py`: Counterfactual decision replay.
- `strategy_replay.py`, `run_strategy_replay.py`: Historical strategy replay.
- `backtest_data.py`, `backtest_report.py`: Historical data and reporting helpers.
- `check_pnl.py`: Quick P&L reporting.

### Operations

- `.env.example`: Canonical documented configuration. It contains no secrets.
- `reset_tracking_data.py`: Archives and resets dashboard/mobile trading statistics.
- `reset_trading_config.py`: Resets the mobile capital profile to its configured default.
- `requirements.txt`: Python dependencies.
- `test_*.py`: Unit and regression tests.

Some legacy modules remain for historical compatibility or research. Their presence does not mean that their strategy is active.

## 6. Strategy engine selection

Each AWS instance selects one entry engine:

```dotenv
TRADING_ENGINE=VAMSI
```

or:

```dotenv
TRADING_ENGINE=GANESH
```

Only the selected engine may create new entries. The monitor and square-off layers always inspect every known bot state slot. This is important: changing the selected engine must not orphan a position created by the other engine.

Current state slots include the Vamsi NIFTY/BANKNIFTY lanes, legacy compatibility slots, and the Ganesh `GANESH_GAP_NIFTY` / `GANESH_GAP_BANKNIFTY` lanes.

## 7. Vamsi engine

### 7.1 Intent

The Vamsi engine is the evolved multi-signal intraday long-option engine. Its primary instruments are NIFTY and BANKNIFTY call/put options. It combines market structure, option-chain evidence, option-premium flow, breadth, and institutional context before constructing an entry.

### 7.2 Main evidence families

- Option-chain direction and confidence.
- 5-minute and 15-minute completed-candle trend and momentum.
- 2-hour higher-timeframe context.
- Classic pivots.
- Bollinger upper/lower bands and middle-band moving average.
- ATM or selected-option VWAP, slope, volume ratio, spread, and Greeks.
- NIFTY constituent breadth or BANKNIFTY major-bank breadth.
- Futures price/OI behavior, basis, VIX, FII/DII context, PCR, and max pain.
- Market regime and entry structure.
- Reachable technical reward/risk and entry extension.
- Portfolio risk, daily risk, position correlation, broker state, and monitor health.

The chain is evidence, not infallible truth. A strongly opposite chain can veto a trade. A neutral/low-confidence chain contributes little or no evidence and may be overridden only by an unusually strong aligned technical setup using the configured neutral-chain threshold.

Vamsi uses a daily adaptive weighted-score rule when `VAMSI_ADAPTIVE_SCORE_ENABLED=true`. At 09:00 IST, `adaptive_score_calibration.py` analyzes prior (never same-day) rows in `data/score_followthrough_audit.csv` separately for NIFTY and BANKNIFTY. It compares minimum-and-above rules with contiguous bounded ranges, requires minimum sample/day evidence, and saves the effective rule in `data/vamsi_adaptive_score_config.json`. A range is selected only when it materially outperforms the best minimum-only rule. If today's file is missing, stale, invalid, or lacks enough evidence, the engine safely returns to the strict static rule `score > VAMSI_MIN_WEIGHTED_SCORE` (currently 20). Adaptive boundaries are inclusive. A qualifying score still proceeds only if regime direction, completed-candle structure, breadth conflicts, option quality, 15-minute reward/risk, entry extension, broker reconciliation, monitor health, account caps, and portfolio/day-risk controls all pass. Five-minute data remains an entry-timing and confirmation input; it does not limit technical reward headroom.

### 7.3 Contract selection

- NIFTY deliberately separates evidence from execution: nearest-expiry ATM
  option-chain, OI/PCR, VWAP, volume, and trend data drive the analysis, while
  Vamsi selective/Test orders buy the next-expiry ATM contract.
- Ganesh uses the same NIFTY expiry split. In FAITHFUL mode the nearest-expiry
  evidence is recorded as context without silently adding a new entry veto.
- BANKNIFTY continues to analyze and execute the configured nearest contract.
- Non-NIFTY contract comparison can include suitable ATM and nearby one-strike-ITM contracts when enabled.
- Both candidates must pass spread, Greek, structure, and feasibility checks.
- Capital allocation is converted into whole lots and rounded down.
- `ACCOUNT_MAX_OPTION_CAPITAL` and `ACCOUNT_MAX_LOTS_PER_ENTRY` remain hard account ceilings.
- NIFTY expiry selection has evolved toward next-week contracts to avoid expiry-day distortion. Confirm the exact current selection in `strategy_core.py` before changing it.

### 7.4 Default risk/exit shape in `.env.example`

The current documented defaults are:

```dotenv
NIFTY_TARGET_POINTS=30
NIFTY_STOP_POINTS=30
BANKNIFTY_TARGET_POINTS=90
BANKNIFTY_STOP_POINTS=90
MIN_TECHNICAL_REWARD_RISK=0.8
OPTION_DELTA_APPROXIMATION=0.50
```

These are underlying-index points, converted into option-premium levels after the actual fill using the configured delta approximation. They are not guaranteed outcomes and should not be interpreted as exact option Greeks.

Profit protection currently supports staged locks and runner behavior:

```dotenv
PROFIT_BOOKING_TARGET_PERCENT=80
PROFIT_BOOKING_MODE=runner
PROFIT_RUNNER_LOCK_PERCENT=55
PROFIT_PROTECTION_STAGE_ONE_TRIGGER_PERCENT=60
PROFIT_PROTECTION_STAGE_ONE_LOCK_PERCENT=20
PROFIT_PROTECTION_STAGE_TWO_TRIGGER_PERCENT=70
PROFIT_PROTECTION_STAGE_TWO_LOCK_PERCENT=35
```

The trigger/lock values must satisfy the validation ordering enforced by the code. Invalid sequences intentionally stop the monitor rather than run with incoherent protection.

### 7.5 Capital and mobile profile

`OPTION_CAPITAL_PER_ENTRY` supports:

- `1`: exactly one lot.
- A positive rupee amount: buy the maximum whole lots within that premium allocation.
- `MAX`: use broker-available capital subject to account caps and safety rules.

The mobile app can select a profile during its configured morning window. The selected amount applies per index opportunity; it is not divided between NIFTY and BANKNIFTY. A Rs 300,000 selection means NIFTY may use up to Rs 300,000, and BANKNIFTY may separately use up to Rs 300,000 if another valid trade is permitted.

Dynamic risk values scale from the active capital profile, but absolute caps always win. Capital allocation and risk are different:

```text
planned risk = abs(entry premium - stop premium) * actual quantity
```

Never raise an account cap merely because a larger mobile profile was selected.

### 7.6 Retired T20 experiment

The experimental T20 fallback lane has been removed from runtime code, configuration, state monitoring, dashboards, and tests. Historical journal rows remain readable as ordinary index-option history. Do not add its old environment variables back to AWS `.env` files.

## 8. Ganesh engine

### 8.1 Intent

The Ganesh engine is a focused NIFTY/BANKNIFTY opening-gap reversal strategy. It is deliberately simpler than the Vamsi engine. It evaluates both indices independently but permits at most one combined Ganesh trade per day.

Current Ganesh AWS selection:

```dotenv
TRADING_ENGINE=GANESH
GANESH_GAP_MODE=FAITHFUL
GANESH_GAP_LIVE_TRADING=true
GANESH_LOTS_PER_ENTRY=1
```

Actual orders also require:

```dotenv
ENABLE_LIVE_TRADING=true
```

### 8.2 Entry model

- Start evaluating after 09:30 IST.
- Compute the official opening gap from the previous close and today's open.
- Require the configured minimum absolute gap, default 0.20%.
- Use NSE-aligned active 2-hour candles starting at 09:15, 11:15, and 13:15.
- For a gap down, first observe a red active candle, then a confirmed red-to-green reversal, and buy an ATM CE.
- For a gap up, first observe a green active candle, then a confirmed green-to-red reversal, and buy an ATM PE.
- Require two confirmation scans by default, unless a configured reversal-distance buffer confirms earlier.
- Use a next-week NIFTY option or the nearest supported BANKNIFTY expiry, and one lot by default.
- Allow at most one combined Ganesh trade per day across NIFTY and BANKNIFTY.
- Do not re-enter after the daily trade is completed.

### 8.3 Targets and exits

- Compute classic pivots from the previous official daily OHLC.
- Compute the 20-period, 2-standard-deviation Bollinger middle band.
- Lock the nearest valid technical target beyond the configured minimum distance, default 15 NIFTY points.
- Convert the spot target to an option-premium target with the configured delta approximation.
- Use a default 20% option-premium stop and a broker-side protective order.
- Exit on target, confirmed opposite active-candle color, option stop, daily risk circuit, stale data, or universal 15:29 square-off.

### 8.4 Faithful versus enhanced mode

`FAITHFUL` implements the requested gap/color/target system without adding optional confirmation gates. `ENHANCED` can require volume, Bollinger direction, and minimum reward/risk. Do not switch modes during an open position.

NIFTY scans are written to `data/ganesh_gap_scans.csv`; BANKNIFTY scans use `data/ganesh_gap_banknifty_scans.csv`. Live state uses `trade_state_GANESH_GAP_NIFTY.json` or `trade_state_GANESH_GAP_BANKNIFTY.json` for the selected opportunity.

## 9. Shared execution and safety layer

Both engines use the same safety-critical infrastructure.

### 9.1 Before entry

- Validate live-trading switches.
- Reconcile local state with broker positions and orders.
- Require a healthy position monitor when configured.
- Validate market-data freshness.
- Apply account capital and lot ceilings.
- Compute actual planned stop risk using final quantity.
- Apply open-portfolio, daily-loss, daily-profit, consecutive-loss, and correlation controls.
- Check spread/Greeks/depth when enabled.
- Place a market-protected entry order.
- Confirm broker fill and calculate levels from the fill.
- Arm a broker-side protective stop.
- If protection cannot be established safely, flatten rather than leave an unprotected position.

### 9.2 While open

- Read broker position/order state with caching and retry controls.
- Re-arm a protective stop if a stale canceled stop is detected.
- Move protection only through validated staged rules.
- Apply target, stop, structural invalidation, and time-stop rules for the relevant strategy.
- Use locks and fresh-state checks to prevent duplicate exits and duplicate journal rows.

### 9.3 At exit

- Cancel the protective stop before an intentional market exit where applicable.
- Place the closing order once.
- Confirm broker status.
- Journal the trade once.
- Clear local state only after finalization.
- Keep broker reconciliation as the ultimate truth when local state is ambiguous.

### 9.4 Universal square-off

The square-off command inspects all bot-managed state slots, not only the currently selected entry engine. It does not intentionally manage unrelated manual positions.

## 10. Runtime files and data

Typical persistent files include:

- `data/trade_history.csv`: Closed-trade journal used by dashboard and mobile analytics.
- `data/analysis_history.csv`: Detailed signal snapshots.
- `data/scan_decisions.csv`: Compact scan results.
- `data/vamsi_adaptive_score_config.json`: Today's pre-market Vamsi minimum/range decision and its evidence summary.
- `data/ganesh_gap_scans.csv`: Ganesh gap-state evidence.
- `data/ganesh_gap_banknifty_scans.csv`: Ganesh BANKNIFTY gap-state evidence.
- `data/day_risk_state.json`: Day-level risk/circuit state.
- `data/monitor_health.json`: Monitor heartbeat/failure state.
- `data/trading_config.json`: Active mobile capital profile.
- `data/apns_devices.json`: Registered Apple devices.
- `trade_state_*.json`: Bot-owned open/pending state.
- `daily_trade_count.json`: Daily lane counts.
- `reentry_guard_*.json`: Re-entry state.
- `logs/trade_bot.log`: Main operational log.
- `logs/token_request.log`: Daily token request log.
- `logs/upstox_webhook_last.json`: Last webhook payload/status aid where configured.

Never edit an open-position state file casually. Verify the broker first.

## 11. AWS deployment model

### 11.1 Instances

- One Lightsail Ubuntu instance for Ganesh.
- One Lightsail Ubuntu instance for Vamsi.
- Both pull the same `main` branch.
- Each has its own static public IP, domain/subdomain, `.env`, access token, mobile token, state, logs, and services.

The domains used during the project are:

- `harekrishnatradingbot.com` and/or `www.harekrishnatradingbot.com` for the primary/Ganesh site.
- `vamsi.harekrishnatradingbot.com` for Vamsi.

DNS is hosted through GoDaddy/Route 53-style records and points to Lightsail static IPs. Nginx terminates/reverse-proxies HTTPS traffic. Confirm the live Nginx and certificate configuration on each instance because it is not stored in this repository.

### 11.2 Services

Common services are:

- `nifty-app.service`: Streamlit dashboard, normally on `127.0.0.1:8501`.
- `hk-mobile-api.service`: FastAPI mobile backend.
- `upstox-token-webhook.service`: Uvicorn token webhook on `127.0.0.1:9000`.
- `upstox-streams.service`: Upstox Plus market and portfolio streams.
- `nginx.service`: Public reverse proxy and TLS endpoint.
- `cron.service`: Schedules token, entry, monitor, reset, review, and square-off jobs.

Basic checks:

```bash
sudo systemctl status nifty-app hk-mobile-api upstox-token-webhook upstox-streams nginx cron --no-pager
curl -s http://127.0.0.1:9000/health
curl -I http://127.0.0.1:8501
```

If `nifty-app` is disabled after an instance reboot:

```bash
sudo systemctl enable --now nifty-app
```

### 11.3 Canonical trading schedule

Lightsail servers use UTC. IST is UTC+05:30.

The established schedule is:

- Daily Upstox token request: 07:30 IST on weekdays, `0 2 * * 1-5` in UTC cron.
- Vamsi adaptive score calibration: 09:00 IST on weekdays, `30 3 * * 1-5` in UTC cron.
- Entry checks: every 5 minutes beginning at 09:20 IST. The bot's internal market window prevents late entries.
- Position monitor: launch at/around 09:20 IST and keep its internal loop alive. `flock` prevents overlapping monitor processes.
- Last Vamsi and Ganesh entry scan: 15:25 IST.
- Forced square-off: 15:29 IST, `59 9 * * 1-5` in UTC cron.
- Mobile profile default reset: around 15:30 IST when configured.
- Post-market audits: around 18:00 IST on weekdays when configured.

An example cron layout is:

```cron
# Entry checks: 09:20 IST onward, every five minutes.
50,55 3 * * 1-5 cd /home/ubuntu/trading-app && /usr/bin/flock -n /tmp/index_entry_bot.lock /home/ubuntu/trading-app/venv/bin/python /home/ubuntu/trading-app/trade_bot.py >> /home/ubuntu/trading-app/logs/trade_bot.log 2>&1
*/5 4-9 * * 1-5 cd /home/ubuntu/trading-app && /usr/bin/flock -n /tmp/index_entry_bot.lock /home/ubuntu/trading-app/venv/bin/python /home/ubuntu/trading-app/trade_bot.py >> /home/ubuntu/trading-app/logs/trade_bot.log 2>&1

# Start/preserve the long-running position monitor. flock prevents duplicates.
50-59 3 * * 1-5 cd /home/ubuntu/trading-app && /usr/bin/flock -n /tmp/index_monitor_bot.lock /home/ubuntu/trading-app/venv/bin/python /home/ubuntu/trading-app/trade_bot.py --monitor >> /home/ubuntu/trading-app/logs/trade_bot.log 2>&1
* 4-9 * * 1-5 cd /home/ubuntu/trading-app && /usr/bin/flock -n /tmp/index_monitor_bot.lock /home/ubuntu/trading-app/venv/bin/python /home/ubuntu/trading-app/trade_bot.py --monitor >> /home/ubuntu/trading-app/logs/trade_bot.log 2>&1

# Square off at 15:29 IST.
59 9 * * 1-5 cd /home/ubuntu/trading-app && /usr/bin/flock -n /tmp/index_squareoff.lock /home/ubuntu/trading-app/venv/bin/python /home/ubuntu/trading-app/trade_bot.py --squareoff >> /home/ubuntu/trading-app/logs/trade_bot.log 2>&1

# Request Upstox token approval at 07:30 IST.
0 2 * * 1-5 cd /home/ubuntu/trading-app && /home/ubuntu/trading-app/venv/bin/python /home/ubuntu/trading-app/request_upstox_token.py >> /home/ubuntu/trading-app/logs/token_request.log 2>&1

# Calibrate today's Vamsi score rule at 09:00 IST (03:30 UTC).
30 3 * * 1-5 cd /home/ubuntu/trading-app && /usr/bin/flock -n /tmp/vamsi_score_calibration.lock /home/ubuntu/trading-app/venv/bin/python /home/ubuntu/trading-app/adaptive_score_calibration.py >> /home/ubuntu/trading-app/logs/adaptive_score_calibration.log 2>&1
```

The repeated cron launch of `--monitor` is a recovery mechanism: while the long-running process holds the lock, later launches exit. If it dies, a later cron invocation restarts it. Never write `- 4-9`; the minute field must be `*`.

Cron itself is not exchange-holiday aware. On holidays, market-dependent jobs should no-op or fail safely. Add a maintained exchange calendar before claiming holiday-native scheduling.

## 12. Upstox daily token flow

1. Cron runs `request_upstox_token.py` at 07:30 IST.
2. The account holder receives and approves the Upstox notifier request.
3. Upstox posts the access token to the public notifier webhook.
4. Nginx proxies `/upstox-token-webhook` to `127.0.0.1:9000`.
5. `token_webhook.py` validates `WEBHOOK_SECRET`.
6. The webhook updates `UPSTOX_ACCESS_TOKEN` in `.env` atomically.
7. Long-running services detect token rotation or are restarted if necessary.

The correct local webhook port is 9000. A public GET to the webhook path returns 405 because the endpoint accepts POST; that is expected. A 400 POST means the route was reached but the body/secret was invalid.

Useful checks:

```bash
sudo systemctl status upstox-token-webhook --no-pager
sudo journalctl -u upstox-token-webhook -n 100 --no-pager
curl -s http://127.0.0.1:9000/health
stat -c "%y %n" /home/ubuntu/trading-app/.env
```

Verify the token without printing it:

```bash
cd /home/ubuntu/trading-app
venv/bin/python - <<'PY'
import os
import requests
from trade_bot import load_env

load_env()
token = os.getenv("UPSTOX_ACCESS_TOKEN", "")
response = requests.get(
    "https://api.upstox.com/v2/user/profile",
    headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
    timeout=20,
)
print("Token set:", bool(token))
print("Status:", response.status_code)
print(response.text[:500])
PY
```

Do not use `source .env` as the normal application loader. Unquoted values containing spaces produce shell `command not found` errors. Python's `load_env()` is the intended path. Also remember that an already-exported shell variable may override `.env` depending on loader semantics.

## 13. Mobile and dashboard behavior

### 13.1 Dashboard

The Streamlit dashboard reads journals, live state files, Upstox positions, and research outputs. It includes performance, live cockpit, post-market analysis, forensics, and research views accumulated during the project.

Restart after code changes that affect the dashboard:

```bash
sudo systemctl restart nifty-app
```

### 13.2 Mobile API

The FastAPI backend exposes health, dashboard, trading-config, screener, holdings, and controlled order endpoints. It requires its account-specific `MOBILE_API_TOKEN` for protected routes.

Restart after backend/config schema changes:

```bash
sudo systemctl restart hk-mobile-api
```

### 13.3 Capital selection

The mobile allocation window is intended for approximately 09:00-09:15 IST. Multiple edits can be made during the open window; the final accepted selection becomes the active profile. Outside the window it is locked. A scheduled reset returns the account to `DEFAULT_TRADING_PROFILE` after market hours.

The account-level caps in `.env` cannot be bypassed by mobile selection. For Ganesh, `GANESH_LOTS_PER_ENTRY=1` controls the gap strategy independently.

### 13.4 App build notes

The iOS release workflow used was Clean Build Folder, select Any iOS Device, Product > Archive, then distribute to TestFlight.

The Android application ID is:

```text
com.harekrishnatradingbot.mobile
```

The release bundle is normally:

```text
app/build/outputs/bundle/release/app-release.aab
```

Google Play expects the established upload certificate fingerprint. Keep the keystore and passwords outside Git and transfer them securely to the new Mac. Do not generate a replacement key for an existing Play listing unless using Google's supported key-reset process.

## 14. Research findings and strategy history

The project went through many experiments. The important conclusion is not that the newest strategy is proven; it is that the system now has better evidence collection and safer operational controls.

### 14.1 Major evolution

1. Began as a NIFTY/BANKNIFTY option bot with simple percent targets and stops.
2. Added Streamlit, cron operation, automatic token approval, Lightsail hosting, domains, and a second account.
3. Added 4-hour/15-minute technicals and LLM decisions.
4. Replaced 4-hour with 2-hour context, added 5-minute momentum, option VWAP/volume, pivots, Bollinger Bands, and option-chain trend.
5. Added weighted alignment, live one-minute/second-level monitoring, staged protection, and post-market analysis.
6. Added institutional footprints, Upstox Plus data, NIFTY breadth, and BANKNIFTY bank breadth.
7. Experimented with option selling, stock futures, stock options, overnight gap positions, and a T20 lane.
8. Removed or disabled strategies that added margin risk, costs, operational complexity, or poor observed results.
9. Built mobile capital controls, aggregate risk, daily circuits, broker reconciliation, and duplicate-exit protection.
10. Built local historical replay and post-trade forensics.
11. Removed the LLM from the primary live decision path after replay showed little benefit relative to deterministic rules.
12. Added the explicit VAMSI/GANESH engine split and Ganesh's focused opening-gap reversal strategy.

### 14.2 Historical replay lessons

- Early multi-month replays of the then-current strategy were negative after estimated costs.
- Turning LLM decisions on or off did not materially improve the result.
- RR 1.25 variants remained negative; the selective variant was merely less negative.
- An oracle swing study produced very large theoretical profits but used future candles to identify turning points and was explicitly non-deployable.
- A strict 1:1 variant produced a small positive result in one run, but not enough evidence for robustness.
- Simulated results are sensitive to fill assumptions, candle ordering, spread, slippage, expiry selection, and missing historical option data.

No simulation should be selected only because it is the best among several losing variants. Avoid optimizing on the same months used to judge the model.

### 14.3 Retired or disabled paths

- Automated option selling: removed from the production objective because of margin and tail risk.
- Stock futures trading: retired after high transaction costs, tight stops, duplicated protective-order incidents, and poor live results.
- Intraday stock-option scanner: later removed/disabled from the primary engine because of product restrictions, liquidity/depth filters, and weak selection evidence.
- Overnight gap-up/gap-down holding: removed from the Vamsi production path. Ganesh's strategy is an intraday opening-gap reversal, not an overnight position.
- WhatsApp/Twilio alerts: removed after template-window complexity; Apple push remains.
- LLM trade approval: removed from the primary live engine after replay did not justify its role.
- T20 mode: removed from runtime code and configuration.

## 15. Important incidents and fixes

### Duplicate protective stops and exits

The bot once armed two protective stops and later produced duplicate exit records during failure handling. The current design uses state freshness checks, locks, idempotent finalization, and broker reconciliation. Any new exit path must use the same centralized finalization logic.

### Upstox rate limiting

A one-second monitor generated HTTP 429 errors. Broker-read caching, stream data, retries, and a configurable monitor interval were introduced. The current example is two seconds, but faster is not automatically safer.

### Protective stop margin rejection

Upstox rejected a protective option stop from the retired T20 experiment as if it required large additional margin. The bot flattened for safety, and duplicate failure paths worsened the recorded loss. The experiment was later removed.

### Post-fill guardrail

A filled order was immediately flattened because the post-fill technical reward/risk no longer passed. Post-fill checks are useful, but must not unexpectedly reinterpret a score-qualified trade without clear logging and tests.

### Monitor failures

Invalid profit-protection trigger ordering stopped the monitor intentionally. The solution is to correct configuration, run validation, and restart the monitor. Do not bypass validation.

### Cron syntax

The malformed line `- 4-9 * * 1-5` did not mean every minute. It was invalid. Use `* 4-9 * * 1-5`.

### Token webhook

The service and Nginx must agree on port 9000. A missing status file does not itself prove the webhook is down; inspect systemd logs and `.env` modification time.

### Intraday stock-option product

Upstox rejected intraday option buys and required delivery product `D` for the attempted stock-option flow. That experiment was later retired from the main system.

### Dashboard inconsistencies

Manual broker trades, duplicate journal rows, and untracked exits can make dashboard P&L differ from Upstox. Reconcile against broker truth and deduplicate the journal; never edit totals alone.

## 16. Logging and post-market workflow

The production log can be concise, while structured CSV journals preserve analysis. Long diagnostic logging can be enabled temporarily when investigating entry decisions.

Watch live operation:

```bash
tail -F /home/ubuntu/trading-app/logs/trade_bot.log
```

If the log was reset and did not exist, the reset utility now recreates it. `tail -F` is preferred because it follows file recreation.

Useful review tools:

```bash
venv/bin/python check_pnl.py
venv/bin/python trade_forensics.py --date YYYY-MM-DD --forward-candles 6 --post-exit-candles 6
venv/bin/python post_market_review.py --date YYYY-MM-DD
```

The forensic tools use completed OHLC candles. They cannot know tick order inside one candle. Maximum favorable/adverse excursion is hindsight diagnostic evidence, not proof that a live exit was wrong.

## 17. Resetting dashboard and app statistics

Use the tracked reset utility only when broker positions are closed.

Preview:

```bash
cd /home/ubuntu/trading-app
venv/bin/python reset_tracking_data.py
```

Perform reset:

```bash
venv/bin/python reset_tracking_data.py --confirm
sudo systemctl restart nifty-app hk-mobile-api
```

The utility archives affected files under:

```text
archive/tracking_reset_YYYYMMDD_HHMMSS/
```

It preserves `.env`, credentials, APNs devices, mobile configuration, market stream caches, and backtest data. It refuses to proceed when active local bot state exists unless `--force` is supplied. Use `--force` only after independently verifying the broker has no bot position.

## 18. Safe deployment workflow

### Local development

```bash
cd /path/to/trading-app
git status
git pull --ff-only origin main
source venv/bin/activate
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -p 'test_*.py'
git diff --check
git add <specific-files>
git commit -m "Describe the change"
git push origin main
```

Prefer specific files over `git add .` when secrets, generated CSVs, downloaded instruments, or local state may be present.

### AWS deployment

```bash
cd /home/ubuntu/trading-app
git status
git pull --ff-only origin main
```

Then:

- Review `.env` rather than replacing it.
- Run focused tests or syntax checks.
- Restart only services affected by the change.
- Do not restart/kill the monitor during an open position unless the risk of leaving the old code is greater and broker protection is verified.
- Cron-launched entry checks use fresh Python processes, so most entry-code changes apply on the next run after pull.
- Long-running monitor, stream, dashboard, webhook, and mobile services require restart to load new code.

Typical restart commands:

```bash
sudo systemctl restart nifty-app hk-mobile-api upstox-streams
sudo systemctl restart upstox-token-webhook
```

Check for an active monitor:

```bash
pgrep -af "trade_bot.py --monitor"
```

## 19. Verify effective engine and configuration

On each AWS instance:

```bash
cd /home/ubuntu/trading-app
venv/bin/python - <<'PY'
import trade_bot

trade_bot.load_env()
print("engine:", trade_bot.trading_engine())
print("ganesh live:", trade_bot.ganesh_gap_live_enabled())
print("monitor interval:", trade_bot.position_monitor_interval_seconds())
PY
```

Expected account selections at this handoff:

- Vamsi: `TRADING_ENGINE=VAMSI`.
- Ganesh: `TRADING_ENGINE=GANESH`, `GANESH_GAP_MODE=FAITHFUL`, `GANESH_GAP_LIVE_TRADING=true`, `GANESH_LOTS_PER_ENTRY=1`.

The Ganesh instance also needs `ENABLE_LIVE_TRADING=true` to place actual orders.

## 20. New MacBook Air setup

### 20.1 Clone and create Python environment

```bash
git clone git@github.com:vkp2498-spec/trading-app.git
cd trading-app
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

Use the example for local paper/research defaults. Do not copy production credentials through Git. Transfer required secrets through a secure password manager or recreate them directly on the target machine.

### 20.2 Run tests

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -p 'test_*.py'
```

At the time this handoff was last updated, the latest full suite had 162 passing tests. The number will change as tests are added.

### 20.3 Run dashboard locally

```bash
streamlit run performance_dashboard.py
```

Live dashboard features require a valid token and expected data files. Offline/post-market sections can work from preserved journals and downloaded data.

### 20.4 Files that must stay out of Git

- `.env`
- Upstox access/API tokens and webhook secrets
- APNs `.p8` private key
- Android keystore and password properties
- Production state files and account journals unless deliberately anonymized
- Downloaded broker instrument/cache files if ignored
- Any file containing account IDs, phone numbers, or private URLs/tokens

## 21. Known gaps and open work

- Ganesh's NIFTY/BANKNIFTY strategy has unit-tested rules but does not yet have a fully validated multi-month option replay with realistic spreads, fills, and costs.
- Neither engine has a demonstrated out-of-sample profitable edge.
- Account-specific AWS `.env`, systemd units, Nginx files, DNS, and certificates are not versioned in this repository.
- Exchange-holiday scheduling needs a maintained NSE holiday calendar if true holiday skipping is required.
- Broker APIs impose date-range and rate limits; research code must cache and chunk requests.
- Historical option data can be incomplete or use contracts unavailable at the simulated timestamp.
- The monitor still depends on API/stream health and cannot eliminate network or broker risk.
- Manual trades can complicate broker reconciliation and dashboard attribution.
- Legacy stock-futures and older LLM code may remain import-compatible in the repository even though disabled.
- The separate Mac research dashboard/folder may not be part of this repository. Locate and version it separately if it is still needed.

## 22. Rules for future changes

1. Read this document and the relevant code before editing.
2. Treat broker position and order state as authoritative.
3. Add a regression test for every live incident fixed.
4. Keep entry logic separate from execution, state finalization, and broker protection.
5. Never create a new exit path that bypasses idempotent finalization.
6. Never leave a live position without verified broker protection unless immediately flattening.
7. Compute risk from final fill, stop, and quantity, not from capital allocation alone.
8. Keep account caps independent of mobile selection.
9. Do not optimize on one day or one month's outcomes.
10. Compare changes using out-of-sample periods, realistic costs, and independent validation.
11. Paper trade or use one lot before increasing exposure.
12. Keep production secrets and mutable state out of Git.

## 23. Recommended first prompt on the new Mac

Use this in a new Codex task:

> Read `PROJECT_ARCHITECTURE_AND_HANDOFF.md`, `.env.example`, `trade_bot.py`, `ganesh_gap_reversal.py`, and the relevant tests before making changes. This is a live-capital Upstox trading system deployed to separate Vamsi and Ganesh AWS instances. Treat broker execution, protective stops, state finalization, risk controls, and backward compatibility as safety-critical. Do not expose or commit secrets. Inspect the working tree first, preserve account-specific behavior, implement changes end to end, run focused tests plus the full suite, and clearly distinguish production code from research-only analysis.

## 24. Final perspective

The strongest part of this project is no longer a single indicator or score. It is the combination of explicit engine selection, structured evidence, broker reconciliation, state safety, account caps, monitoring, and post-market diagnostics. The weakest part remains the unproven trading edge.

The correct next stage is disciplined evidence collection: small exposure, stable rules, clean journals, realistic replay, and no mid-session strategy improvisation. Operational sophistication can prevent avoidable failures, but it cannot manufacture profitability.
