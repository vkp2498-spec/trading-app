# NIFTY option-buyer schedule

Effective 10 September 2026 for `VAMSI_NIFTY_OPTION_BUY_V1` on Vamsi AWS.

- Scan one minute after each completed 15-minute candle: 09:31, 09:46,
  10:01, ... 14:31, 14:46 IST (22 weekday scan opportunities).
- Deduplicate by the completed 15-minute boundary; reject off-schedule scan
  invocations before collecting market data or submitting orders.
- The inclusive env window is `NIFTY_OPTION_BUY_FIRST_ENTRY_TIME=09:31` and
  `NIFTY_OPTION_BUY_LAST_ENTRY_TIME=14:46`. The whole final minute is valid.
- Keep the existing 15M trend / 5M structure scoring, contract selection,
  allocation and daily trade controls unchanged. Scan cadence is not a change
  to the strategy's indicator timeframes.
- Continue two-second position monitoring, with monitor recovery cron through
  15:29 IST. Request square-off of any remaining bot position at 15:25 IST
  through the existing centralized square-off path; broker execution timing
  remains subject to connectivity and exchange conditions.
- Reconcile broker trade history at 15:30 IST, after the square-off request.

Install the schedule on the UTC-configured AWS host with:

```sh
venv/bin/python scripts/sync_trading_cron.py --mode nifty-option-buy
```

Update only the two entry-window env values during this deployment. Do not
reset positions, journals, counters, credentials or unrelated account settings.
Back up `.env` and the crontab first. Dashboard/mobile summary timing labels
are served by `dashboard_data.py`; restart those services after deployment.
