import argparse
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from dashboard_score_buckets import DASHBOARD_SCORE_BUCKETS, dashboard_score_bucket
from market_technicals import INDEX_KEYS, _parse_candles, fetch_v3_intraday_minutes, upstox_headers
from strategy_core import now_ist


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_FILE = BASE_DIR / "logs" / "trade_bot.log"
SCAN_FILE = DATA_DIR / "scan_decisions.csv"
ANALYSIS_FILE = DATA_DIR / "analysis_history.csv"
AUDIT_FILE = DATA_DIR / "score_followthrough_audit.csv"
STATUS_FILE = DATA_DIR / "score_followthrough_status.json"
IST = ZoneInfo("Asia/Kolkata")
SYMBOLS = ("NIFTY", "BANKNIFTY")
SCORE_BUCKETS = DASHBOARD_SCORE_BUCKETS
KNOWLEDGE_SYMBOLS = ("NIFTY", "BANKNIFTY", "SENSEX")
KNOWLEDGE_SCAN_FILE = DATA_DIR / "vamsi_kb_intraday" / "scans.csv"
KNOWLEDGE_AUDIT_FILE = DATA_DIR / "vamsi_kb_intraday" / "post_market_followthrough.csv"
KNOWLEDGE_SUMMARY_FILE = DATA_DIR / "vamsi_kb_intraday" / "post_market_summary.json"
KNOWLEDGE_STATUS_FILE = DATA_DIR / "vamsi_kb_intraday" / "post_market_status.json"
KNOWLEDGE_EXIT_DEFAULTS = {
    "NIFTY": {"target": 30.0, "stop": 30.0},
    "BANKNIFTY": {"target": 60.0, "stop": 60.0},
    "SENSEX": {"target": 40.0, "stop": 40.0},
}
KNOWLEDGE_AUDIT_COLUMNS = [
    "observation_id",
    "trading_date",
    "scan_time",
    "scan_slot",
    "symbol",
    "action",
    "direction",
    "knowledge_score",
    "score_bucket",
    "selection_score",
    "categories_json",
    "blockers",
    "reference_price",
    "target_points",
    "stop_points",
    "favorable_points_before_stop",
    "adverse_points_before_stop",
    "target_hit_before_stop",
    "stop_hit",
    "bars_evaluated",
    "window_end",
]
KNOWLEDGE_GATE_LABELS = {
    "setup": "SETUP / REGIME",
    "completed_candles": "5M + 15M ALIGNMENT",
    "breadth": "CONSTITUENT BREADTH",
    "option_chain": "OPTION CHAIN",
    "option_flow": "OPTION VWAP / VOLUME",
    "contract_quality": "SPREAD / DELTA / QUOTE",
    "entry_freshness": "ENTRY FRESHNESS",
}

LOG_PREFIX = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| (?P<message>.*)$"
)
SIGNAL_RE = re.compile(
    r"^(NIFTY|BANKNIFTY) signal: (BULLISH|BEARISH|NEUTRAL), "
    r"confidence=([A-Z]+), score=([-+]?\d+(?:\.\d+)?)"
)
OVERRIDE_RE = re.compile(
    r"^(NIFTY|BANKNIFTY) neutral-chain override candidate: direction=(BULLISH|BEARISH)"
)
CANDIDATE_RE = re.compile(
    r"^(NIFTY|BANKNIFTY) (?:BUY|BOLLINGER_REVERSAL) candidate: .*?"
    r"score=([-+]?\d+(?:\.\d+)?) "
    r"(?:version=([A-Z0-9_-]+) )?reason=(.*?) contract="
)
DECISION_RE = re.compile(
    r"^(NIFTY|BANKNIFTY) score ([-+]?\d+(?:\.\d+)?) (buy|reject)"
    r"(?: version=([A-Z0-9_-]+))?$"
)
NO_TRADE_RE = re.compile(r"^(NIFTY|BANKNIFTY) no trade: (.+)$")

AUDIT_COLUMNS = [
    "observation_id",
    "trading_date",
    "signal_time",
    "symbol",
    "score",
    "score_version",
    "score_bucket",
    "action",
    "direction",
    "chain_direction",
    "chain_confidence",
    "reason_category",
    "reason",
    "reference_price",
    "next_15m_high",
    "next_15m_low",
    "next_15m_close",
    "up_points",
    "down_points",
    "close_change_points",
    "favorable_points",
    "adverse_points",
    "direction_correct",
    "nifty_10_point_hit",
    "nifty_20_point_hit",
    "nifty_30_point_hit",
    "banknifty_20_point_hit",
    "banknifty_40_point_hit",
    "banknifty_60_point_hit",
    "banknifty_90_point_hit",
    "window_start",
    "window_end",
    "path_horizon_minutes",
    "minute_path_json",
    "source",
]


def score_bucket(score):
    return dashboard_score_bucket(score)


def normalize_score_buckets(frame):
    """Rebucket stored rows so older audit files use the current definitions."""
    if frame.empty or "score" not in frame.columns:
        return frame
    normalized = frame.copy()
    scores = pd.to_numeric(normalized["score"], errors="coerce")
    valid = scores.notna()
    normalized.loc[valid, "score_bucket"] = scores.loc[valid].map(score_bucket)
    return normalized


def reason_category(reason):
    text = str(reason or "").lower()
    rules = [
        ("REWARD_RISK", ("reward/risk", "reachable reward", "technical target")),
        (
            "UNIFIED_SCORE",
            ("unified entry score", "weighted score", "does not qualify", "score is below"),
        ),
        ("REGIME_STRUCTURE", ("regime", "structure", "retest", "extension")),
        ("OPTION_QUALITY", ("spread", "delta", "depth", "greeks", "tradeability")),
        ("OPTION_FLOW", ("vwap", "volume", "option premium", "atm flow")),
        ("TIMEFRAME_CONFLICT", ("5m", "15m", "two_hour", "2h", "opposite")),
        ("BREADTH", ("breadth", "major-bank")),
        ("PORTFOLIO_RISK", ("portfolio", "daily", "risk", "cooldown", "re-entry")),
        ("NEUTRAL_SIGNAL", ("neutral", "direction is unavailable")),
    ]
    for category, terms in rules:
        if any(term in text for term in terms):
            return category
    return "OTHER"


def _timestamp(value):
    result = pd.Timestamp(value)
    if result.tzinfo is None:
        return result.tz_localize(IST)
    return result.tz_convert(IST)


def parse_log_scans(log_file, date_text):
    path = Path(log_file)
    if not path.exists():
        return pd.DataFrame()

    state = {symbol: {} for symbol in SYMBOLS}
    events = []
    with path.open(errors="ignore") as handle:
        lines = list(handle)
    for line in lines:
        matched = LOG_PREFIX.match(line.strip())
        if not matched or not matched.group("timestamp").startswith(date_text):
            continue
        timestamp = _timestamp(matched.group("timestamp"))
        message = matched.group("message")

        signal = SIGNAL_RE.match(message)
        if signal:
            symbol, direction, confidence, chain_score = signal.groups()
            state[symbol] = {
                "timestamp": timestamp,
                "direction": direction if direction != "NEUTRAL" else "",
                "chain_direction": direction,
                "chain_confidence": confidence,
                "chain_score": float(chain_score),
                "candidate_score": None,
                "score_version": "",
                "reason": "",
            }
            continue

        override = OVERRIDE_RE.match(message)
        if override:
            symbol, direction = override.groups()
            state[symbol]["direction"] = direction
            continue

        candidate = CANDIDATE_RE.match(message)
        if candidate:
            symbol, candidate_score, score_version, candidate_reason = candidate.groups()
            numeric = float(candidate_score)
            current = state[symbol].get("candidate_score")
            if current is None or numeric >= current:
                state[symbol]["candidate_score"] = numeric
                state[symbol]["score_version"] = score_version or ""
                state[symbol]["reason"] = candidate_reason.strip()
            continue

        decision = DECISION_RE.match(message)
        if decision:
            symbol, final_score, action, score_version = decision.groups()
            current = state[symbol]
            events.append(
                {
                    "timestamp": timestamp,
                    "symbol": symbol,
                    "score": float(final_score),
                    "score_version": score_version or current.get("score_version", ""),
                    "action": action,
                    "direction": current.get("direction", ""),
                    "chain_direction": current.get("chain_direction", ""),
                    "chain_confidence": current.get("chain_confidence", ""),
                    "reason": current.get("reason", ""),
                    "source": "long_log",
                }
            )
            continue

        no_trade = NO_TRADE_RE.match(message)
        if no_trade:
            symbol, explanation = no_trade.groups()
            for event in reversed(events):
                if event["symbol"] != symbol:
                    continue
                if timestamp - event["timestamp"] <= pd.Timedelta(seconds=15):
                    if not event.get("reason"):
                        event["reason"] = explanation.strip()
                break

    return pd.DataFrame(events)


def read_structured_scans(scan_file, date_text):
    path = Path(scan_file)
    if not path.exists():
        return pd.DataFrame()
    frame = pd.read_csv(path)
    if frame.empty:
        return frame
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce", utc=True)
    frame["timestamp"] = frame["timestamp"].dt.tz_convert(IST)
    frame = frame[frame["timestamp"].dt.date.astype(str) == date_text].copy()
    frame["score"] = pd.to_numeric(frame["score"], errors="coerce")
    if "score_version" not in frame.columns:
        frame["score_version"] = ""
    frame = frame.dropna(subset=["timestamp", "score"])
    frame["source"] = "scan_journal"
    return frame


def _analysis_rows(date_text):
    if not ANALYSIS_FILE.exists():
        return pd.DataFrame()
    frame = pd.read_csv(ANALYSIS_FILE)
    if frame.empty:
        return frame
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce", utc=True)
    frame["timestamp"] = frame["timestamp"].dt.tz_convert(IST)
    return frame[frame["timestamp"].dt.date.astype(str) == date_text].copy()


def _analysis_evidence(row):
    try:
        raw = json.loads(row.get("raw_json") or "{}")
    except Exception:
        raw = {}
    option = raw.get("option_summary") or {}
    decision = raw.get("llm_decision") or {}
    entry_score = option.get("unified_entry_score") or {}
    weighted = option.get("weighted_alignment") or {}
    direction = option.get("bias") or decision.get("decision") or ""
    if direction not in {"BULLISH", "BEARISH"}:
        direction = ""
    return {
        "score": entry_score.get("score", weighted.get("score")),
        "score_version": entry_score.get("score_version", weighted.get("score_version", "")),
        "direction": direction,
        "chain_direction": option.get("chain_bias") or option.get("bias") or "",
        "chain_confidence": option.get("chain_confidence") or option.get("confidence") or "",
        "reason": decision.get("reason") or row.get("llm_reason") or "",
    }


def combine_scan_sources(log_scans, structured_scans, analysis_rows):
    frames = [frame for frame in (log_scans, structured_scans) if frame is not None and not frame.empty]
    if not frames and (analysis_rows is None or analysis_rows.empty):
        return pd.DataFrame()
    scans = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()

    if scans.empty:
        scans = analysis_rows[["timestamp", "symbol"]].copy()
        scans["score"] = None
        scans["action"] = "unknown"
        scans["source"] = "analysis_journal"

    scans = scans.sort_values("timestamp")
    scans["minute_key"] = scans["timestamp"].dt.floor("min")
    source_rank = {"long_log": 0, "scan_journal": 1, "analysis_journal": 2}
    scans["source_rank"] = scans["source"].map(source_rank).fillna(9)
    scans = scans.sort_values(["timestamp", "source_rank"]).drop_duplicates(
        ["symbol", "minute_key"], keep="first"
    )

    if analysis_rows is not None and not analysis_rows.empty:
        grouped = {
            symbol: part.sort_values("timestamp")
            for symbol, part in analysis_rows.groupby("symbol")
        }
        for index, scan in scans.iterrows():
            candidates = grouped.get(scan["symbol"])
            if candidates is None or candidates.empty:
                continue
            distance = (candidates["timestamp"] - scan["timestamp"]).abs()
            nearest_index = distance.idxmin()
            if distance.loc[nearest_index] > pd.Timedelta(minutes=2):
                continue
            evidence = _analysis_evidence(candidates.loc[nearest_index])
            for field, value in evidence.items():
                current = scan.get(field)
                if current is None or current == "" or pd.isna(current):
                    scans.at[index, field] = value

    scans["score"] = pd.to_numeric(scans["score"], errors="coerce")
    return scans.dropna(subset=["timestamp", "symbol", "score"]).sort_values("timestamp")


def non_overlapping_scans(scans, horizon_minutes=15):
    if scans.empty:
        return scans
    selected = []
    horizon = pd.Timedelta(minutes=int(horizon_minutes))
    for _, group in scans.groupby("symbol"):
        remaining = group.sort_values("timestamp")
        while not remaining.empty:
            first_time = remaining.iloc[0]["timestamp"]
            window = remaining[remaining["timestamp"] < first_time + horizon]
            buys = window[
                window.get("action", pd.Series(index=window.index, dtype=object))
                .astype(str)
                .str.lower()
                .eq("buy")
            ]
            chosen = buys.iloc[0] if not buys.empty else window.iloc[0]
            selected.append(chosen.name)
            remaining = remaining[
                remaining["timestamp"] >= chosen["timestamp"] + horizon
            ]
    return scans.loc[selected].sort_values(["timestamp", "symbol"]).reset_index(drop=True)


def _fetch_historical_day(instrument_key, trading_date):
    date_text = trading_date.isoformat()
    url = (
        "https://api.upstox.com/v3/historical-candle/"
        f"{instrument_key}/minutes/1/{date_text}/{date_text}"
    )
    response = requests.get(url, headers=upstox_headers(), timeout=30)
    if response.status_code >= 300:
        raise RuntimeError(
            f"Upstox historical candle API failed {response.status_code}: {response.text[:500]}"
        )
    return _parse_candles(response.json())


def fetch_day_candles(symbol, trading_date):
    if trading_date == now_ist().date():
        frame = fetch_v3_intraday_minutes(INDEX_KEYS[symbol], minutes=1)
    else:
        frame = _fetch_historical_day(INDEX_KEYS[symbol], trading_date)
    if frame.empty:
        return frame
    frame = frame.copy()
    index = pd.DatetimeIndex(frame.index)
    frame.index = index.tz_localize(IST) if index.tz is None else index.tz_convert(IST)
    return frame[frame.index.date == trading_date]


def _serialize_minute_path(candles):
    rows = []
    for timestamp, candle in candles.iterrows():
        rows.append(
            {
                "t": _timestamp(timestamp).isoformat(),
                "o": round(float(candle["open"]), 2),
                "h": round(float(candle["high"]), 2),
                "l": round(float(candle["low"]), 2),
                "c": round(float(candle["close"]), 2),
            }
        )
    return json.dumps(rows, separators=(",", ":"))


def evaluate_followthrough(
    scan,
    candles,
    horizon_minutes=15,
    exit_path_minutes=60,
):
    signal_time = _timestamp(scan["timestamp"])
    window_start = signal_time.floor("min")
    window_end = window_start + pd.Timedelta(minutes=int(horizon_minutes))
    window = candles[(candles.index >= window_start) & (candles.index < window_end)]
    if window.empty:
        return None
    path_end = window_start + pd.Timedelta(minutes=max(int(exit_path_minutes), 1))
    exit_path = candles[(candles.index >= window_start) & (candles.index < path_end)]

    reference = float(window.iloc[0]["open"])
    high = float(window["high"].max())
    low = float(window["low"].min())
    close = float(window.iloc[-1]["close"])
    up_points = max(high - reference, 0.0)
    down_points = max(reference - low, 0.0)
    direction = str(scan.get("direction") or "").upper()
    if direction == "BULLISH":
        favorable, adverse = up_points, down_points
        direction_correct = close > reference
    elif direction == "BEARISH":
        favorable, adverse = down_points, up_points
        direction_correct = close < reference
    else:
        favorable = adverse = None
        direction_correct = None

    symbol = scan["symbol"]
    signal_iso = signal_time.isoformat()
    row = {
        "observation_id": f"{signal_time.date()}|{symbol}|{signal_iso}",
        "trading_date": str(signal_time.date()),
        "signal_time": signal_iso,
        "symbol": symbol,
        "score": round(float(scan["score"]), 2),
        "score_version": scan.get("score_version", ""),
        "score_bucket": score_bucket(scan["score"]),
        "action": scan.get("action", ""),
        "direction": direction,
        "chain_direction": scan.get("chain_direction", ""),
        "chain_confidence": scan.get("chain_confidence", ""),
        "reason_category": reason_category(scan.get("reason")),
        "reason": scan.get("reason", ""),
        "reference_price": round(reference, 2),
        "next_15m_high": round(high, 2),
        "next_15m_low": round(low, 2),
        "next_15m_close": round(close, 2),
        "up_points": round(up_points, 2),
        "down_points": round(down_points, 2),
        "close_change_points": round(close - reference, 2),
        "favorable_points": round(favorable, 2) if favorable is not None else None,
        "adverse_points": round(adverse, 2) if adverse is not None else None,
        "direction_correct": direction_correct,
        "nifty_10_point_hit": symbol == "NIFTY" and favorable is not None and favorable >= 10,
        "nifty_20_point_hit": symbol == "NIFTY" and favorable is not None and favorable >= 20,
        "nifty_30_point_hit": symbol == "NIFTY" and favorable is not None and favorable >= 30,
        "banknifty_20_point_hit": symbol == "BANKNIFTY" and favorable is not None and favorable >= 20,
        "banknifty_40_point_hit": symbol == "BANKNIFTY" and favorable is not None and favorable >= 40,
        "banknifty_60_point_hit": symbol == "BANKNIFTY" and favorable is not None and favorable >= 60,
        "banknifty_90_point_hit": symbol == "BANKNIFTY" and favorable is not None and favorable >= 90,
        "window_start": window.index.min().isoformat(),
        "window_end": window.index.max().isoformat(),
        "path_horizon_minutes": int(exit_path_minutes),
        "minute_path_json": _serialize_minute_path(exit_path),
        "source": scan.get("source", ""),
    }
    return row


def market_is_closed(trading_date):
    if trading_date.weekday() >= 5:
        return True, "Weekend"
    url = f"https://api.upstox.com/v2/market/holidays/{trading_date.isoformat()}"
    response = requests.get(url, headers=upstox_headers(), timeout=20)
    if response.status_code >= 300:
        return False, f"Holiday check unavailable ({response.status_code}); candle availability will decide"
    payload = response.json().get("data") or []
    entries = payload if isinstance(payload, list) else [payload]
    for entry in entries:
        if entry.get("holiday_type") != "TRADING_HOLIDAY":
            continue
        closed = {str(value).upper() for value in entry.get("closed_exchanges") or []}
        open_exchanges = {
            str(value.get("exchange") or "").upper()
            for value in entry.get("open_exchanges") or []
        }
        if {"NSE", "NFO"}.issubset(closed) and not ({"NSE", "NFO"} & open_exchanges):
            return True, entry.get("description") or "NSE/NFO trading holiday"
    return False, "Trading session"


def upsert_audit(rows, audit_file=AUDIT_FILE):
    path = Path(audit_file)
    incoming = pd.DataFrame(rows, columns=AUDIT_COLUMNS)
    if incoming.empty:
        return pd.DataFrame()
    if path.exists():
        existing = pd.read_csv(path)
        combined = pd.concat([existing, incoming], ignore_index=True, sort=False)
    else:
        combined = incoming
    combined = combined.drop_duplicates("observation_id", keep="last")
    combined = normalize_score_buckets(combined)
    combined = combined.reindex(columns=AUDIT_COLUMNS).sort_values(
        ["trading_date", "signal_time", "symbol"]
    )
    path.parent.mkdir(exist_ok=True)
    temp = path.with_suffix(".tmp")
    combined.to_csv(temp, index=False)
    temp.replace(path)
    return combined


def read_audit(audit_file=AUDIT_FILE):
    path = Path(audit_file)
    if not path.exists():
        return pd.DataFrame(columns=AUDIT_COLUMNS)
    return normalize_score_buckets(pd.read_csv(path))


def build_bucket_summary(frame, minimum_samples=20):
    if frame.empty:
        return pd.DataFrame()
    working = frame.copy()
    for column in ("up_points", "down_points", "favorable_points", "adverse_points"):
        working[column] = pd.to_numeric(working[column], errors="coerce")
    direction_correct = working["direction_correct"].astype(str).str.lower().map(
        {"true": 1.0, "false": 0.0}
    )
    working["direction_correct_numeric"] = direction_correct
    grouped = (
        working.groupby(["score_bucket", "symbol"], observed=False)
        .agg(
            samples=("observation_id", "count"),
            avg_up_points=("up_points", "mean"),
            avg_down_points=("down_points", "mean"),
            avg_favorable_points=("favorable_points", "mean"),
            avg_adverse_points=("adverse_points", "mean"),
            direction_accuracy=("direction_correct_numeric", "mean"),
        )
        .reset_index()
    )
    grouped["direction_accuracy"] = grouped["direction_accuracy"] * 100
    grouped["evidence"] = grouped["samples"].apply(
        lambda value: "USABLE" if int(value) >= int(minimum_samples) else "BUILDING"
    )
    return grouped


def build_reason_summary(frame, minimum_samples=20):
    if frame.empty:
        return pd.DataFrame()
    working = frame.copy()
    for column in ("favorable_points", "adverse_points"):
        working[column] = pd.to_numeric(working[column], errors="coerce")
    grouped = (
        working.groupby(["symbol", "reason_category"], observed=False)
        .agg(
            samples=("observation_id", "count"),
            avg_favorable_points=("favorable_points", "mean"),
            avg_adverse_points=("adverse_points", "mean"),
        )
        .reset_index()
    )
    grouped["evidence"] = grouped["samples"].apply(
        lambda value: "USABLE" if int(value) >= int(minimum_samples) else "BUILDING"
    )
    return grouped


def knowledge_exit_points(symbol):
    symbol = str(symbol or "").upper()
    defaults = KNOWLEDGE_EXIT_DEFAULTS[symbol]

    def configured(kind):
        names = (
            f"VAMSI_KB_{symbol}_{kind}_POINTS",
            f"{symbol}_{kind}_POINTS",
        )
        for name in names:
            value = os.getenv(name)
            if value not in (None, ""):
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                if number > 0:
                    return number
        return defaults[kind.lower()]

    return {"target": configured("TARGET"), "stop": configured("STOP")}


def read_knowledge_scans(trading_date, scan_file=KNOWLEDGE_SCAN_FILE):
    path = Path(scan_file)
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        frame = pd.read_csv(path)
    except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError):
        return pd.DataFrame()
    if frame.empty:
        return frame
    if "symbol" not in frame.columns:
        frame["symbol"] = "NIFTY"
    frame["symbol"] = frame["symbol"].fillna("NIFTY").astype(str).str.upper()
    frame.loc[~frame["symbol"].isin(KNOWLEDGE_SYMBOLS), "symbol"] = "NIFTY"
    slot_values = frame.get("scan_slot", frame.get("scan_time"))
    frame["scan_slot_timestamp"] = pd.to_datetime(slot_values, errors="coerce", utc=True)
    if "scan_time" not in frame.columns:
        frame["scan_time"] = slot_values
    fallback = pd.to_datetime(frame["scan_time"], errors="coerce", utc=True)
    frame["scan_slot_timestamp"] = frame["scan_slot_timestamp"].fillna(fallback)
    frame = frame.dropna(subset=["scan_slot_timestamp"])
    frame["scan_slot_timestamp"] = frame["scan_slot_timestamp"].dt.tz_convert(IST)
    date_value = trading_date.isoformat() if hasattr(trading_date, "isoformat") else str(trading_date)
    frame = frame[
        frame["scan_slot_timestamp"].dt.date.astype(str) == date_value
    ].copy()
    for column in ("knowledge_score", "selection_score"):
        if column not in frame.columns:
            frame[column] = None
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in ("action", "direction", "blockers", "evidence"):
        if column not in frame.columns:
            frame[column] = ""
        frame[column] = frame[column].fillna("").astype(str)
    return frame.sort_values(["scan_slot_timestamp", "symbol"])


def knowledge_categories(scan):
    categories = []
    score = scan.get("knowledge_score")
    if score is not None and not pd.isna(score):
        categories.append(f"SCORE {score_bucket(float(score))}")
    try:
        evidence = json.loads(scan.get("evidence") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        evidence = {}
    for key, item in evidence.items() if isinstance(evidence, dict) else ():
        if isinstance(item, dict) and not bool(item.get("passed")):
            label = KNOWLEDGE_GATE_LABELS.get(
                str(key), str(key).replace("_", " ").upper()
            )
            categories.append(f"REJECT · {label}")
    action = str(scan.get("action") or "").upper()
    if action == "NO_CANDIDATE":
        categories.append("REJECT · NO COMPLETE CANDIDATE")
    elif action == "ERROR":
        categories.append("REJECT · DATA UNAVAILABLE")
    elif action in {"CAPITAL_REJECT", "EXECUTION_REJECT"}:
        categories.append(f"REJECT · {action.replace('_', ' ')}")
    elif action == "REJECT" and not any(
        category.startswith("REJECT ·") for category in categories
    ):
        categories.append("REJECT · OTHER")
    return list(dict.fromkeys(categories))


def evaluate_knowledge_followthrough(scan, candles, market_close="15:30"):
    direction = str(scan.get("direction") or "").upper()
    symbol = str(scan.get("symbol") or "").upper()
    if direction not in {"BULLISH", "BEARISH"} or symbol not in KNOWLEDGE_SYMBOLS:
        return None
    if candles is None or candles.empty:
        return None
    slot = _timestamp(scan.get("scan_slot_timestamp") or scan.get("scan_slot"))
    closing_time = datetime.strptime(market_close, "%H:%M").time()
    end = pd.Timestamp.combine(slot.date(), closing_time).tz_localize(IST)
    window = candles[(candles.index >= slot) & (candles.index < end)]
    if window.empty:
        return None

    levels = knowledge_exit_points(symbol)
    target_points = float(levels["target"])
    stop_points = float(levels["stop"])
    reference = float(window.iloc[0]["open"])
    best_favorable = 0.0
    worst_adverse = 0.0
    target_hit = False
    stop_hit = False
    bars_evaluated = 0
    last_timestamp = window.index[0]

    for timestamp, candle in window.iterrows():
        high = float(candle["high"])
        low = float(candle["low"])
        if direction == "BULLISH":
            stopped_this_bar = low <= reference - stop_points
            favorable = max(high - reference, 0.0)
            adverse = max(reference - low, 0.0)
        else:
            stopped_this_bar = high >= reference + stop_points
            favorable = max(reference - low, 0.0)
            adverse = max(high - reference, 0.0)

        # Minute candles cannot reveal whether their high or low happened
        # first. A bar touching the stop is conservatively treated stop-first,
        # so only movement from earlier bars qualifies as available evidence.
        if stopped_this_bar:
            stop_hit = True
            last_timestamp = timestamp
            break
        best_favorable = max(best_favorable, favorable)
        worst_adverse = max(worst_adverse, adverse)
        target_hit = target_hit or favorable >= target_points
        bars_evaluated += 1
        last_timestamp = timestamp

    score = scan.get("knowledge_score")
    numeric_score = None if score is None or pd.isna(score) else float(score)
    categories = knowledge_categories(scan)
    if not categories:
        return None
    slot_iso = slot.isoformat()
    return {
        "observation_id": f"{slot.date()}|{symbol}|{slot_iso}",
        "trading_date": str(slot.date()),
        "scan_time": str(scan.get("scan_time") or ""),
        "scan_slot": slot_iso,
        "symbol": symbol,
        "action": str(scan.get("action") or ""),
        "direction": direction,
        "knowledge_score": round(numeric_score, 2) if numeric_score is not None else None,
        "score_bucket": score_bucket(numeric_score) if numeric_score is not None else "",
        "selection_score": (
            round(float(scan.get("selection_score")), 2)
            if scan.get("selection_score") is not None
            and not pd.isna(scan.get("selection_score"))
            else None
        ),
        "categories_json": json.dumps(categories, separators=(",", ":")),
        "blockers": str(scan.get("blockers") or ""),
        "reference_price": round(reference, 2),
        "target_points": target_points,
        "stop_points": stop_points,
        "favorable_points_before_stop": round(best_favorable, 2),
        "adverse_points_before_stop": round(worst_adverse, 2),
        "target_hit_before_stop": bool(target_hit),
        "stop_hit": bool(stop_hit),
        "bars_evaluated": bars_evaluated,
        "window_end": _timestamp(last_timestamp).isoformat(),
    }


def upsert_knowledge_audit(rows, audit_file=KNOWLEDGE_AUDIT_FILE):
    path = Path(audit_file)
    incoming = pd.DataFrame(rows, columns=KNOWLEDGE_AUDIT_COLUMNS)
    if path.exists():
        try:
            existing = pd.read_csv(path)
        except (OSError, pd.errors.ParserError, pd.errors.EmptyDataError):
            existing = pd.DataFrame(columns=KNOWLEDGE_AUDIT_COLUMNS)
        combined = pd.concat([existing, incoming], ignore_index=True, sort=False)
    else:
        combined = incoming
    if not combined.empty:
        combined = combined.drop_duplicates("observation_id", keep="last")
        combined = combined.reindex(columns=KNOWLEDGE_AUDIT_COLUMNS).sort_values(
            ["trading_date", "scan_slot", "symbol"]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    combined.to_csv(temporary, index=False)
    temporary.replace(path)
    return combined


def build_knowledge_summary(frame, trading_date=None, status="COMPLETE", message=""):
    score_order = [f"SCORE {bucket}" for bucket in DASHBOARD_SCORE_BUCKETS]
    gate_order = [f"REJECT · {label}" for label in KNOWLEDGE_GATE_LABELS.values()]
    observed_categories = []
    expanded = []
    if frame is not None and not frame.empty:
        for _, row in frame.iterrows():
            try:
                categories = json.loads(row.get("categories_json") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                categories = []
            for category in categories:
                observed_categories.append(category)
                expanded.append(
                    {
                        "symbol": str(row.get("symbol") or "").upper(),
                        "category": str(category),
                        "favorable": pd.to_numeric(
                            row.get("favorable_points_before_stop"), errors="coerce"
                        ),
                        "target_hit": str(row.get("target_hit_before_stop")).lower()
                        in {"true", "1"},
                    }
                )
    preferred = score_order + gate_order
    extras = sorted(set(observed_categories) - set(preferred))
    columns = [item for item in preferred if item in observed_categories] + extras
    expanded_frame = pd.DataFrame(expanded)
    rows = []
    for symbol in KNOWLEDGE_SYMBOLS:
        levels = knowledge_exit_points(symbol)
        cells = []
        for category in columns:
            if expanded_frame.empty:
                subset = expanded_frame
            else:
                subset = expanded_frame[
                    (expanded_frame["symbol"] == symbol)
                    & (expanded_frame["category"] == category)
                ]
            favorable = pd.to_numeric(
                subset.get("favorable", pd.Series(dtype=float)), errors="coerce"
            ).dropna()
            samples = int(len(favorable))
            cells.append(
                {
                    "column": category,
                    "averageFavorablePoints": (
                        round(float(favorable.mean()), 2) if samples else None
                    ),
                    "samples": samples,
                    "targetHitRate": (
                        round(float(subset.loc[favorable.index, "target_hit"].mean()) * 100, 1)
                        if samples
                        else None
                    ),
                }
            )
        rows.append(
            {
                "symbol": symbol,
                "targetPoints": levels["target"],
                "stopPoints": levels["stop"],
                "cells": cells,
            }
        )
    cumulative_observations = int(len(frame)) if frame is not None else 0
    return {
        "status": status,
        "message": message,
        "tradingDate": (
            trading_date.isoformat() if hasattr(trading_date, "isoformat") else str(trading_date or "")
        ),
        "updatedAt": now_ist().isoformat(),
        "cumulativeObservations": cumulative_observations,
        "columns": columns,
        "rows": rows,
        "method": (
            "Every overlapping five-minute scan; favourable movement is measured "
            "from the next tradable minute until the configured stop first touches "
            "or the session ends. Same-minute target/stop ambiguity is stop-first."
        ),
    }


def _atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True))
    temporary.replace(path)


def run_knowledge_engine_audit(
    trading_date=None,
    skip_holiday_check=False,
    scan_file=KNOWLEDGE_SCAN_FILE,
    audit_file=KNOWLEDGE_AUDIT_FILE,
    summary_file=KNOWLEDGE_SUMMARY_FILE,
    status_file=KNOWLEDGE_STATUS_FILE,
    candles_by_symbol=None,
):
    trading_date = trading_date or now_ist().date()
    if not skip_holiday_check:
        closed, explanation = market_is_closed(trading_date)
        if closed:
            status = "SKIPPED"
            scans = pd.DataFrame()
            message = explanation
        else:
            status = "COMPLETE"
            scans = read_knowledge_scans(trading_date, scan_file)
            message = ""
    else:
        status = "COMPLETE"
        scans = read_knowledge_scans(trading_date, scan_file)
        message = ""

    rows = []
    skipped_without_direction = 0
    if status == "COMPLETE" and not scans.empty:
        candles_by_symbol = candles_by_symbol or {
            symbol: fetch_day_candles(symbol, trading_date)
            for symbol in KNOWLEDGE_SYMBOLS
            if symbol in set(scans["symbol"])
        }
        for _, scan in scans.iterrows():
            evaluated = evaluate_knowledge_followthrough(
                scan, candles_by_symbol.get(scan["symbol"], pd.DataFrame())
            )
            if evaluated is None:
                skipped_without_direction += 1
            else:
                rows.append(evaluated)
        message = f"Saved {len(rows)} overlapping five-minute observations"
    elif status == "COMPLETE":
        status = "SKIPPED"
        message = "No knowledge-engine scan decisions were found"

    combined = upsert_knowledge_audit(rows, audit_file)
    summary = build_knowledge_summary(
        combined,
        trading_date=trading_date,
        status=status,
        message=message,
    )
    summary["todayObservations"] = len(rows)
    summary["todayScansWithoutDirectionalEvidence"] = skipped_without_direction
    _atomic_write_json(summary_file, summary)
    _atomic_write_json(status_file, summary)
    return summary


def _write_status(status, message, **extra):
    DATA_DIR.mkdir(exist_ok=True)
    payload = {
        "status": status,
        "message": message,
        "updated_at": now_ist().isoformat(),
        **extra,
    }
    STATUS_FILE.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return payload


def run_audit(
    trading_date=None,
    horizon_minutes=15,
    exit_path_minutes=60,
    skip_holiday_check=False,
):
    trading_date = trading_date or now_ist().date()
    date_text = trading_date.isoformat()
    if not skip_holiday_check:
        closed, explanation = market_is_closed(trading_date)
        if closed:
            return _write_status("SKIPPED", explanation, trading_date=date_text, rows=0)

    log_scans = parse_log_scans(LOG_FILE, date_text)
    structured = read_structured_scans(SCAN_FILE, date_text)
    analysis = _analysis_rows(date_text)
    scans = combine_scan_sources(log_scans, structured, analysis)
    scans = non_overlapping_scans(scans, horizon_minutes=horizon_minutes)
    if scans.empty:
        return _write_status(
            "SKIPPED",
            "No NIFTY or BANKNIFTY scan decisions were found",
            trading_date=date_text,
            rows=0,
        )

    candles = {
        symbol: fetch_day_candles(symbol, trading_date)
        for symbol in SYMBOLS
        if symbol in set(scans["symbol"])
    }
    rows = []
    for _, scan in scans.iterrows():
        frame = candles.get(scan["symbol"], pd.DataFrame())
        evaluated = evaluate_followthrough(
            scan,
            frame,
            horizon_minutes=horizon_minutes,
            exit_path_minutes=exit_path_minutes,
        )
        if evaluated:
            rows.append(evaluated)
    if not rows:
        return _write_status(
            "SKIPPED",
            "Scan decisions existed but complete following candles were unavailable",
            trading_date=date_text,
            rows=0,
        )

    combined = upsert_audit(rows)
    return _write_status(
        "COMPLETE",
        f"Saved {len(rows)} non-overlapping observations",
        trading_date=date_text,
        rows=len(rows),
        cumulative_rows=len(combined),
        audit_file=str(AUDIT_FILE),
    )


def main():
    parser = argparse.ArgumentParser(description="Audit scan scores against the following 15 minutes")
    parser.add_argument("--date", help="Trading date in YYYY-MM-DD format")
    parser.add_argument(
        "--knowledge-engine-all-scans",
        action="store_true",
        help="Audit every overlapping VAMSI knowledge-engine five-minute scan through session end",
    )
    parser.add_argument("--horizon-minutes", type=int, default=15)
    parser.add_argument(
        "--exit-path-minutes",
        type=int,
        default=60,
        help="Minute path stored for shadow target/stop replay (default: 60)",
    )
    parser.add_argument("--skip-holiday-check", action="store_true")
    args = parser.parse_args()
    trading_date = date.fromisoformat(args.date) if args.date else now_ist().date()
    try:
        if args.knowledge_engine_all_scans:
            result = run_knowledge_engine_audit(
                trading_date=trading_date,
                skip_holiday_check=args.skip_holiday_check,
            )
        else:
            result = run_audit(
                trading_date=trading_date,
                horizon_minutes=args.horizon_minutes,
                exit_path_minutes=args.exit_path_minutes,
                skip_holiday_check=args.skip_holiday_check,
            )
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception as error:
        if args.knowledge_engine_all_scans:
            failure = build_knowledge_summary(
                pd.DataFrame(columns=KNOWLEDGE_AUDIT_COLUMNS),
                trading_date=trading_date,
                status="FAILED",
                message=str(error),
            )
            _atomic_write_json(KNOWLEDGE_STATUS_FILE, failure)
        else:
            _write_status("FAILED", str(error), trading_date=trading_date.isoformat(), rows=0)
        raise


if __name__ == "__main__":
    main()
