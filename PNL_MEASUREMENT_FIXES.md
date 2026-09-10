# P&L and measurement reliability — 10 September 2026

This release does not alter entry scores, expiry selection, capital allocation,
stop/target distances, trailing thresholds, or the 15-minute scan schedule.

- An empty broker P&L response is unavailable evidence, never a zero result.
  Reconciliation preserves the journal and publishes `PENDING` in
  `data/upstox_pnl_sync_status.json`.
- Missing trade groups, insufficient closed quantities, missing/nonfinite P&L,
  potentially paginated reports and concurrent journal changes cannot produce
  compensating adjustments. Paper results are not reconciled against real money.
- Today's positions cannot reconcile a historical date; open positions cannot
  supply closed-trade results. Closed-position fallback uses realized P&L only.
- NIFTY observed high/low quotes persist when no stream/trailing-stage update
  occurs. Exit fills always contribute to journal excursion bounds.
- Profitable stop exits with an activated profit-protection stage are labelled
  `TRAILING_STOP`; losing stop exits remain `STOP_LOSS`.
- A neutral signal no longer generates a false missing-close/ATR diagnostic.

`scripts/repair_nifty_measurements.py` previews historical changes by default.
With `--confirm`, it refuses active bot state, backs up the full journal and
archives an audit before modifying it. It removes only exact cancellation
adjustments for dates whose retained reconciliation log confirms an empty
broker response. Unproven historical adjustments remain untouched. It repairs
NIFTY excursion bounds from known entry/exit fills and profitable trailing labels
without changing executed quantities, prices or per-trade gross P&L.

Historical missing ticks cannot be recovered from this repair. Observed MFE/MAE
are lower bounds, not complete tick histories, and should not be treated as
validated inputs for adaptive live stops.

Broker schema reference: [Upstox trade-wise realized P&L](https://upstox.com/developer/api-documentation/get-profit-and-loss-report/).
