from __future__ import annotations

from collections import deque
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import ast
import csv
import json
import os
import re

import requests

from adaptive_exit_shadow import SHADOW_CONFIG_FILE
from adaptive_live_policy import LIVE_POLICY_FILE
from unified_entry_score import UNIFIED_SCORE_VERSION


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"

ENV_FILE = BASE_DIR / ".env"
TRADE_HISTORY_FILE = DATA_DIR / "trade_history.csv"
ANALYSIS_HISTORY_FILE = DATA_DIR / "analysis_history.csv"
LOG_FILE = LOG_DIR / "trade_bot.log"
STOCK_SCANNER_STATUS_FILE = DATA_DIR / "stock_scanner_status.json"

SYMBOLS = ["NIFTY", "BANKNIFTY"]
STATE_SLOTS = SYMBOLS + ["STOCK_FUTURE", "GANESH_GAP_NIFTY", "GANESH_GAP_BANKNIFTY"]
UPSTOX_SYNC_EXIT_REASON = "UPSTOX_SYNC_ADJUSTMENT"
EDGE_SCORE_BANDS = (
    ("00-49", 0.0, 50.0),
    ("50-59", 50.0, 60.0),
    ("60-64", 60.0, 65.0),
    ("65-69", 65.0, 70.0),
    ("70-74", 70.0, 75.0),
    ("75-79", 75.0, 80.0),
    ("80-84", 80.0, 85.0),
    ("85-89", 85.0, 90.0),
    ("90-100", 90.0, 101.0),
)
EDGE_UNSCORED_BAND = "Unscored"
EDGE_TIME_BUCKETS = (
    ("opening", "09:15–10:00", 9 * 60 + 15, 10 * 60),
    ("morning", "10:00–11:00", 10 * 60, 11 * 60),
    ("late_morning", "11:00–13:00", 11 * 60, 13 * 60),
    ("early_afternoon", "13:00–14:00", 13 * 60, 14 * 60),
    ("late_afternoon", "14:00–15:30", 14 * 60, 15 * 60 + 30),
    ("unknown", "Unknown time", None, None),
)

UPSTOX_POSITIONS_URL = (
    "https://api.upstox.com/v2/"
    "portfolio/short-term-positions"
)
UPSTOX_TRADE_PNL_URL = (
    "https://api.upstox.com/v2/trade/profit-loss/data"
)


def load_env():
    """
    Load values from .env without printing secrets.
    """
    if not ENV_FILE.exists():
        return

    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)

        os.environ.setdefault(
            key.strip(),
            value.strip().strip('"').strip("'"),
        )


def file_status(path: Path) -> dict:
    return {
        "name": path.name,
        "exists": path.exists(),
        "sizeBytes": path.stat().st_size if path.exists() else 0,
    }


def read_last_lines(
    path: Path,
    max_lines: int = 2500,
) -> list[str]:
    """
    Read only the end of a potentially large log file.
    """
    if not path.exists():
        return []

    with path.open("r", errors="ignore") as file:
        return list(deque(file, maxlen=max_lines))


def safe_literal_dict(text: str) -> dict:
    """
    Safely parse dictionary text found in bot logs.

    ast.literal_eval does not execute arbitrary Python code.
    """
    try:
        value = ast.literal_eval(text)

        if isinstance(value, dict):
            return value

        return {}
    except (ValueError, SyntaxError):
        return {}


def extract_between(
    line: str,
    start: str,
    end: str,
) -> str:
    if start not in line:
        return ""

    part = line.split(start, 1)[1]

    if end and end in part:
        part = part.split(end, 1)[0]

    return part.strip()


def empty_symbol_status(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "lastUpdate": None,
        "status": "NO DATA",
        "signal": None,
        "confidence": None,
        "optionScore": None,
        "entryScore": None,
        "scoreVersion": None,
        "entryGrade": None,
        "baseAlignmentScore": None,
        "weightedScore": None,
        "weightedGrade": None,
        "atmOptionFlow": None,
        "reason": None,
        "position": None,
    }


def parse_latest_bot_status() -> dict:
    """
    Parse the latest status of NIFTY and BANKNIFTY from the bot log.

    Returns only dashboard-safe information.
    """
    statuses = {
        symbol: empty_symbol_status(symbol)
        for symbol in SYMBOLS
    }

    last_bot_log = None

    for raw_line in read_last_lines(LOG_FILE):
        line = raw_line.strip()

        timestamp_match = re.match(
            r"^(\d{4}-\d{2}-\d{2} "
            r"\d{2}:\d{2}:\d{2}) \|",
            line,
        )

        if timestamp_match:
            last_bot_log = timestamp_match.group(1)

        for symbol in SYMBOLS:
            if f"| {symbol} " not in line:
                continue

            item = statuses[symbol]

            if timestamp_match:
                item["lastUpdate"] = timestamp_match.group(1)

            signal_match = re.search(
                rf"{symbol} signal: "
                rf"([A-Z]+), "
                rf"confidence=([A-Z]+), "
                rf"score=([-0-9.]+)",
                line,
            )

            if signal_match:
                item["status"] = "SIGNAL CHECKED"
                item["signal"] = signal_match.group(1)
                item["confidence"] = signal_match.group(2)

                try:
                    item["optionScore"] = float(
                        signal_match.group(3)
                    )
                except ValueError:
                    item["optionScore"] = None

            if f"{symbol} no trade:" in line:
                item["status"] = "REJECTED"
                item["reason"] = line.split(
                    f"{symbol} no trade:",
                    1,
                )[1].strip()

            if f"{symbol} ERROR:" in line:
                item["status"] = "ERROR"
                item["reason"] = line.split(
                    f"{symbol} ERROR:",
                    1,
                )[1].strip()

            if f"{symbol} MARKET BUY placed" in line:
                item["status"] = "BOUGHT"

            if f"{symbol} POSITION OPEN:" in line:
                item["status"] = "OPEN"
                item["position"] = line.split(
                    f"{symbol} POSITION OPEN:",
                    1,
                )[1].strip()

            if f"{symbol} open position active:" in line:
                item["status"] = "OPEN"
                item["position"] = line.split(
                    f"{symbol} open position active:",
                    1,
                )[1].strip()

            if f"{symbol} TARGET exit" in line:
                item["status"] = "TARGET HIT"

            if f"{symbol} STOP_LOSS exit" in line:
                item["status"] = "STOP LOSS HIT"

            if f"{symbol} bot squareoff" in line:
                item["status"] = "SQUAREOFF"

            if f"{symbol} analysis:" in line:
                weighted_text = extract_between(
                    line,
                    "weighted=",
                    " llm=",
                )

                weighted = safe_literal_dict(
                    weighted_text
                )

                if weighted:
                    item["weightedScore"] = weighted.get(
                        "score"
                    )
                    item["weightedGrade"] = weighted.get(
                        "grade"
                    )

                flow_text = extract_between(
                    line,
                    "atm_option_flow=",
                    "",
                )

                flow = safe_literal_dict(flow_text)

                if flow:
                    item["atmOptionFlow"] = {
                        "bias": flow.get("bias"),
                        "close": flow.get("close"),
                        "vwap": flow.get("vwap"),
                        "volumeRatio": flow.get(
                            "volume_ratio"
                        ),
                    }

                llm_text = extract_between(
                    line,
                    "llm=",
                    " atm_option_flow=",
                )

                llm = safe_literal_dict(llm_text)

                if llm:
                    item["status"] = (
                        "APPROVED"
                        if llm.get("execute_trade")
                        else "REJECTED"
                    )

                    item["reason"] = llm.get(
                        "reason",
                        item["reason"],
                    )

    return {
        "lastBotLog": last_bot_log,
        "symbols": [
            statuses[symbol]
            for symbol in SYMBOLS
        ],
    }

IST = ZoneInfo("Asia/Kolkata")


def safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def analysis_indicator(label: str, maximum: float, contribution: float, detail: str) -> dict:
    return {
        "label": label,
        "weight": maximum,
        "contribution": round(contribution, 1),
        "detail": detail,
    }


def weighted_components(option_summary: dict, technicals: dict) -> list[dict]:
    """Return unified score components, with legacy weighted-score fallback."""
    unified = option_summary.get("unified_entry_score", {}) or {}
    if unified:
        components = unified.get("components", {}) or {}
        weights = unified.get("weights", {}) or {}
        details = unified.get("details", {}) or {}
        direction = details.get("direction_and_structure", {}) or {}
        regime = details.get("market_regime", {}) or {}
        feasibility = details.get("trade_feasibility", {}) or {}
        breadth = (
            technicals.get("banknifty_breadth", {})
            or technicals.get("nifty_breadth", {})
            or {}
        )
        institutional = technicals.get("institutional_flow", {}) or {}
        return [
            analysis_indicator(
                "Core alignment",
                safe_float(weights.get("core_alignment"), 25),
                safe_float(components.get("core_alignment")),
                f"Base score {safe_float(details.get('base_alignment_score')):.1f}/100",
            ),
            analysis_indicator(
                "Direction & structure",
                safe_float(weights.get("direction_and_structure"), 25),
                safe_float(components.get("direction_and_structure")),
                (
                    f"15M {direction.get('fifteen_minute', 0)}/8 • "
                    f"5M {direction.get('five_minute', 0)}/5 • "
                    f"{direction.get('structure_type') or 'no structure'}"
                ),
            ),
            analysis_indicator(
                "Breadth",
                safe_float(weights.get("breadth"), 20),
                safe_float(components.get("breadth")),
                f"{breadth.get('bias') or 'NEUTRAL'} • raw {breadth.get('score', '—')}",
            ),
            analysis_indicator(
                "Institutional context",
                safe_float(weights.get("institutional"), 10),
                safe_float(components.get("institutional")),
                (
                    f"{institutional.get('bias') or 'NEUTRAL'} • "
                    f"{institutional.get('confidence') or 'LOW'}"
                ),
            ),
            analysis_indicator(
                "Market regime",
                safe_float(weights.get("market_regime"), 10),
                safe_float(components.get("market_regime")),
                (
                    f"{regime.get('regime') or 'UNKNOWN'} • "
                    f"{regime.get('regime_direction') or 'NEUTRAL'}"
                ),
            ),
            analysis_indicator(
                "Trade feasibility",
                safe_float(weights.get("trade_feasibility"), 10),
                safe_float(components.get("trade_feasibility")),
                (
                    f"R:R {safe_float(feasibility.get('observed_reward_risk')):.2f} • "
                    f"target {feasibility.get('technical_target', 0)}/2"
                ),
            ),
        ]

    weighted = option_summary.get("weighted_alignment", {}) or {}
    component_values = {}
    labels = {
        "Option-chain": "option_chain",
        "15M": "fifteen_min",
        "2H": "two_hour",
        "5M momentum/volume": "five_min",
        "ATM option VWAP/volume": "atm_option_flow",
    }
    for reason in weighted.get("reasons", []) or []:
        match = re.search(r"^(.+?) component=([-0-9.]+)/([0-9.]+)", str(reason))
        if match:
            key = labels.get(match.group(1))
            if key:
                component_values[key] = safe_float(match.group(2))

    fifteen = technicals.get("fifteen_min", {}) or {}
    two_hour = technicals.get("two_hour", {}) or {}
    five_min = technicals.get("five_min", {}) or {}
    flow = technicals.get("atm_option_flow", {}) or {}
    return [
        analysis_indicator(
            "Option chain", 35, component_values.get("option_chain", 0),
            f"{option_summary.get('bias') or '—'} • {option_summary.get('confidence') or '—'}",
        ),
        analysis_indicator(
            "15-minute trend", 25, component_values.get("fifteen_min", 0),
            f"{fifteen.get('bias') or '—'} • {fifteen.get('confidence') or '—'}",
        ),
        analysis_indicator(
            "2-hour trend", 10, component_values.get("two_hour", 0),
            f"{two_hour.get('bias') or '—'} • {two_hour.get('confidence') or '—'}",
        ),
        analysis_indicator(
            "5-minute momentum", 15, component_values.get("five_min", 0),
            f"Momentum {five_min.get('momentum_score', '—')} • volume {'confirmed' if five_min.get('volume_confirmed') else 'not confirmed'}",
        ),
        analysis_indicator(
            "ATM option flow", 15, component_values.get("atm_option_flow", 0),
            f"{flow.get('bias') or '—'} • volume {safe_float(flow.get('volume_ratio')):.2f}x",
        ),
    ]


def normalize_analysis(row: dict) -> dict:
    try:
        raw = json.loads(row.get("raw_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        raw = {}
    option_summary = raw.get("option_summary", {}) or {}
    technicals = raw.get("technicals", {}) or {}
    llm = raw.get("llm_decision", {}) or {}
    entry_score = (
        option_summary.get("unified_entry_score")
        or option_summary.get("weighted_alignment")
        or {}
    )
    entry = safe_float(option_summary.get("entry_price"), None)
    target = safe_float(option_summary.get("target_price") or llm.get("target_price"), None)
    stop = safe_float(option_summary.get("stop_loss_price") or llm.get("stop_loss_price"), None)
    reward = abs(target - entry) if target is not None and entry is not None else None
    risk = abs(entry - stop) if stop is not None and entry is not None else None
    traded = bool(llm.get("execute_trade")) and str(llm.get("decision") or "").upper() != "NO_TRADE"
    return {
        "timestamp": row.get("timestamp", ""),
        "symbol": row.get("symbol", ""),
        "decision": "TRADED" if traded else "REJECTED",
        "signal": option_summary.get("bias"),
        "llmDecision": llm.get("decision") or ("TRADE" if traded else "NO_TRADE"),
        "llmConfidence": llm.get("confidence"),
        "reason": llm.get("reason") or row.get("llm_reason") or "",
        "overallScore": safe_float(entry_score.get("score"), None),
        "scoreVersion": entry_score.get("score_version") or "LEGACY_WEIGHTED_SCORE",
        "baseAlignmentScore": safe_float(
            (entry_score.get("details") or {}).get("base_alignment_score"),
            safe_float((option_summary.get("weighted_alignment") or {}).get("score"), None),
        ),
        "grade": entry_score.get("grade"),
        "indicators": weighted_components(option_summary, technicals),
        "strike": safe_float(option_summary.get("strike"), None),
        "instrument": option_summary.get("trading_symbol") or "",
        "optionType": option_summary.get("option_type") or row.get("option_type") or "",
        "entryPrice": entry,
        "targetPrice": target,
        "stopLossPrice": stop,
        "risk": round(risk, 2) if risk is not None else None,
        "reward": round(reward, 2) if reward is not None else None,
        "rewardRiskRatio": round(reward / risk, 2) if reward is not None and risk else None,
    }


def latest_symbol_analyses() -> list[dict]:
    latest = {}
    if ANALYSIS_HISTORY_FILE.exists():
        try:
            with ANALYSIS_HISTORY_FILE.open("r", newline="", errors="ignore") as file:
                for row in csv.DictReader(file):
                    symbol = str(row.get("symbol") or "").upper()
                    if symbol in SYMBOLS:
                        latest[symbol] = normalize_analysis(row)
        except (OSError, csv.Error):
            pass
    return [latest.get(symbol, {"symbol": symbol, "decision": "NO DATA", "indicators": []}) for symbol in SYMBOLS]


def apply_latest_entry_scores(bot_status: dict, analyses: list[dict]) -> dict:
    """Project the authoritative unified entry score onto the mobile bot cards."""
    by_symbol = {
        str(analysis.get("symbol") or "").upper(): analysis
        for analysis in analyses
    }
    for item in bot_status.get("symbols", []):
        analysis = by_symbol.get(str(item.get("symbol") or "").upper())
        if not analysis:
            continue
        item["entryScore"] = analysis.get("overallScore")
        item["scoreVersion"] = analysis.get("scoreVersion")
        item["entryGrade"] = analysis.get("grade")
        item["baseAlignmentScore"] = analysis.get("baseAlignmentScore")
    return bot_status


def parse_history_timestamp(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def scan_bucket(timestamp: datetime) -> datetime:
    minute = (timestamp.minute // 5) * 5
    return timestamp.replace(minute=minute, second=0, microsecond=0)


def short_scan_reason(reason: str, fallback: str) -> str:
    text = " ".join(str(reason or "").split()).strip(" .")
    lower = text.lower()
    replacements = (
        ("configured option capital/risk budget", "Risk budget insufficient"),
        ("daily first-outcome", "Daily trade limit reached"),
        ("active index position", "Another index position is active"),
        ("unified entry score", "Unified score below today's threshold"),
        ("weighted score", "Score below entry threshold"),
        ("volume confirmation", "Volume confirmation below threshold"),
        ("did not pass deterministic", "Entry rules not met"),
    )
    for phrase, summary in replacements:
        if phrase in lower:
            return summary
    if not text:
        return fallback
    if len(text) <= 72:
        return text
    shortened = text[:69].rsplit(" ", 1)[0]
    return f"{shortened}..."


def build_today_scans(now: datetime | None = None) -> list[dict]:
    current = (now or datetime.now(IST)).astimezone(IST)
    today = current.date()
    grouped: dict[datetime, dict] = {}

    def result_for(bucket: datetime, symbol: str) -> dict:
        row = grouped.setdefault(
            bucket,
            {
                "id": bucket.isoformat(),
                "timestamp": bucket.isoformat(),
                "nifty": None,
                "bankNifty": None,
            },
        )
        key = "bankNifty" if symbol == "BANKNIFTY" else "nifty"
        if row[key] is None:
            row[key] = {
                "decision": "REJECTED",
                "reason": "Entry rules not met",
                "score": None,
                "scoreVersion": None,
            }
        return row[key]

    if ANALYSIS_HISTORY_FILE.exists():
        try:
            with ANALYSIS_HISTORY_FILE.open("r", newline="", errors="ignore") as file:
                for raw_row in csv.DictReader(file):
                    symbol = str(raw_row.get("symbol") or "").upper()
                    timestamp = parse_history_timestamp(raw_row.get("timestamp"))
                    if symbol not in SYMBOLS or timestamp is None or timestamp.date() != today:
                        continue
                    analysis = normalize_analysis(raw_row)
                    result = result_for(scan_bucket(timestamp), symbol)
                    entered = analysis.get("decision") == "TRADED"
                    result.update(
                        {
                            "decision": "ENTERED" if entered else "REJECTED",
                            "reason": short_scan_reason(
                                analysis.get("reason"),
                                "Entry signal accepted" if entered else "Entry rules not met",
                            ),
                            "score": analysis.get("overallScore"),
                            "scoreVersion": analysis.get("scoreVersion"),
                        }
                    )
        except (OSError, csv.Error):
            pass

    latest_events: dict[str, tuple[datetime, dict]] = {}
    decision_pattern = re.compile(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| "
        r"(NIFTY|BANKNIFTY) score ([^ ]+) (reject|buy)"
        r"(?: version=([A-Z0-9_-]+))?$",
        re.IGNORECASE,
    )
    reason_patterns = (
        re.compile(r"^(NIFTY|BANKNIFTY) no trade: (.+)$", re.IGNORECASE),
        re.compile(r"^(NIFTY|BANKNIFTY) portfolio gate rejected entry: (.+)$", re.IGNORECASE),
        re.compile(r"^(NIFTY|BANKNIFTY) MARKET BUY rejected: (.+)$", re.IGNORECASE),
        re.compile(r"^(NIFTY|BANKNIFTY) order execution ERROR: (.+)$", re.IGNORECASE),
    )
    for raw_line in read_last_lines(LOG_FILE, max_lines=10000):
        line = raw_line.strip()
        match = decision_pattern.match(line)
        if match:
            timestamp = parse_history_timestamp(match.group(1))
            if timestamp is None or timestamp.date() != today:
                continue
            symbol = match.group(2).upper()
            score_text = match.group(3)
            action = match.group(4).lower()
            score_version = match.group(5)
            result = result_for(scan_bucket(timestamp), symbol)
            score = safe_float(score_text, None)
            result.update(
                {
                    "decision": "ENTERED" if action == "buy" else "REJECTED",
                    "reason": short_scan_reason(
                        result.get("reason"),
                        "Entry signal accepted" if action == "buy" else "Entry rules not met",
                    ),
                    "score": score if score is not None else result.get("score"),
                    "scoreVersion": score_version or result.get("scoreVersion"),
                }
            )
            if score_text.lower() == "used":
                result["reason"] = "Daily trade limit reached"
            elif action == "buy":
                result["reason"] = "Entered trade"
            latest_events[symbol] = (timestamp, result)
            continue

        timestamp_match = re.match(
            r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| (.+)$",
            line,
        )
        if not timestamp_match:
            continue
        timestamp = parse_history_timestamp(timestamp_match.group(1))
        message = timestamp_match.group(2)
        if timestamp is None or timestamp.date() != today:
            continue
        for pattern in reason_patterns:
            reason_match = pattern.match(message)
            if not reason_match:
                continue
            symbol = reason_match.group(1).upper()
            event = latest_events.get(symbol)
            if event and 0 <= (timestamp - event[0]).total_seconds() <= 180:
                event[1]["decision"] = "REJECTED"
                event[1]["reason"] = short_scan_reason(
                    reason_match.group(2),
                    "Entry rules not met",
                )
            break

    return [grouped[key] for key in sorted(grouped, reverse=True)]


def empty_trade_performance() -> dict:
    return {
        "today": {
            "closedTrades": 0,
            "closedPnL": 0.0,
            "winRate": 0.0,
            "symbolPnL": {
                symbol: 0.0
                for symbol in SYMBOLS
            },
            "categoryPnL": {
                "optionSell": 0.0,
                "optionBuy": 0.0,
                "stockFutures": 0.0,
                "overall": 0.0,
            },
        },
        "cumulative": {
            "totalTrades": 0,
            "totalPnL": 0.0,
            "winRate": 0.0,
            "averageProfitPerWinningTrade": 0.0,
            "averageLossPerLosingTrade": 0.0,
            "symbolPnL": {
                symbol: 0.0
                for symbol in SYMBOLS
            },
            "categoryPerformance": category_performance(
                []
            ),
            "dayOfWeekPerformance": day_of_week_performance([]),
            "optionTypePerformance": option_type_performance([]),
            "indexTradeSequencePerformance": index_trade_sequence_performance([]),
            "secondTradeContextPerformance": second_trade_context_performance([]),
        },
        "equityCurve": [],
        "pnlCalendar": [],
        "edgeAnalytics": edge_analytics([]),
        "recentTrades": [],
    }


def calculate_win_rate(trades: list[dict]) -> float:
    if not trades:
        return 0.0

    winning_trades = sum(
        1
        for trade in trades
        if trade["grossPnL"] > 0
    )

    return round(
        winning_trades / len(trades) * 100,
        1,
    )


def average_trade_results(trades: list[dict]) -> tuple[float, float]:
    profits = [trade["grossPnL"] for trade in trades if trade["grossPnL"] > 0]
    losses = [abs(trade["grossPnL"]) for trade in trades if trade["grossPnL"] < 0]
    return (
        round(sum(profits) / len(profits), 2) if profits else 0.0,
        round(sum(losses) / len(losses), 2) if losses else 0.0,
    )


def approximate_other_charges(trade: dict) -> float:
    quantity = abs(safe_int(trade.get("quantity")))
    entry_price = abs(safe_float(trade.get("entryPrice")))
    exit_price = abs(safe_float(trade.get("exitPrice")))
    if quantity <= 0:
        return 0.0

    transaction_type = str(
        trade.get("transactionType") or "BUY"
    ).upper()
    if transaction_type == "SELL":
        sell_premium = quantity * entry_price
        buy_premium = quantity * exit_price
    else:
        buy_premium = quantity * entry_price
        sell_premium = quantity * exit_price

    premium_turnover = buy_premium + sell_premium
    brokerage = 40.0
    stt = sell_premium * 0.0015
    transaction_charges = premium_turnover * 0.0003553
    sebi = premium_turnover * 0.000001
    stamp_duty = buy_premium * 0.00003
    ipft = premium_turnover * 0.000000001
    gst = (brokerage + transaction_charges + ipft) * 0.18
    square_off = (
        75.0 * 1.18
        if "SQUARE" in str(trade.get("exitReason") or "").upper()
        else 0.0
    )
    raw_charges = (
        brokerage + stt + transaction_charges + sebi + stamp_duty + ipft + gst + square_off
    )
    return round(
        raw_charges * safe_float(trade.get("normalizationFactor"), 1.0),
        2,
    )


def total_other_charges(trades: list[dict]) -> float:
    return round(
        sum(approximate_other_charges(trade) for trade in trades),
        2,
    )


PER_LAKH = 100_000.0


def trade_value_at_entry(trade: dict) -> float:
    return round(
        abs(safe_float(trade.get("entryPrice")))
        * abs(safe_int(trade.get("quantity"))),
        2,
    )


def normalize_trade_per_lakh(trade: dict) -> dict:
    trade_value = trade_value_at_entry(trade)
    factor = PER_LAKH / trade_value if trade_value > 0 else 1.0
    normalized = dict(trade)
    normalized["tradeValueAtEntry"] = trade_value
    normalized["normalizationFactor"] = factor
    for field in (
        "grossPnL",
        "priorTradePnL",
        "riskPerTradeLimit",
        "remainingIndexRiskBudget",
        "plannedRisk",
        "maxFavorablePnL",
        "maxAdversePnL",
    ):
        if field in normalized:
            normalized[field] = round(safe_float(normalized.get(field)) * factor, 2)
    return normalized


def normalize_trades_per_lakh(trades: list[dict]) -> list[dict]:
    return [normalize_trade_per_lakh(trade) for trade in trades]


def symbol_pnl(trades: list[dict]) -> dict:
    totals = {
        symbol: 0.0
        for symbol in SYMBOLS
    }

    for trade in trades:
        symbol = str(
            trade.get("underlyingSymbol")
            or trade.get("symbol")
            or ""
        ).upper()
        if "BANKNIFTY" in symbol:
            symbol = "BANKNIFTY"
        elif "NIFTY" in symbol:
            symbol = "NIFTY"

        if symbol not in totals:
            totals[symbol] = 0.0

        totals[symbol] += trade["grossPnL"]

    return {
        symbol: round(value, 2)
        for symbol, value in totals.items()
    }


def performance_stats(trades: list[dict]) -> dict:
    gross_pnl = round(sum(trade["grossPnL"] for trade in trades), 2)
    average_profit, average_loss = average_trade_results(trades)
    return {
        "trades": len(trades),
        "grossPnL": gross_pnl,
        "netPnL": gross_pnl,
        "winRate": calculate_win_rate(trades),
        "averageProfit": average_profit,
        "averageLoss": average_loss,
    }


def symbol_performance_stats(trades: list[dict]) -> dict:
    stats = {
        "OVERALL": performance_stats(trades),
    }
    for symbol in SYMBOLS:
        stats[symbol] = performance_stats(
            [
                trade
                for trade in trades
                if normalized_underlying(trade) == symbol
            ]
        )
    return stats


def trade_category(trade: dict) -> str:
    instrument_class = str(
        trade.get("instrumentClass") or ""
    ).upper()
    position_side = str(
        trade.get("positionSide") or ""
    ).upper()
    transaction_type = str(
        trade.get("transactionType") or "BUY"
    ).upper()

    if "FUT" in instrument_class or "FUT" in position_side:
        return "STOCK_FUTURES"

    if transaction_type == "SELL":
        return "OPTION_SELL"

    return "OPTION_BUY"


def category_performance(
    trades: list[dict],
) -> list[dict]:
    groups = [
        ("NIFTY", "OPTION_SELL"),
        ("NIFTY", "OPTION_BUY"),
        ("BANKNIFTY", "OPTION_SELL"),
        ("BANKNIFTY", "OPTION_BUY"),
        ("ALL", "STOCK_FUTURES"),
    ]

    summaries = []

    for symbol, category in groups:
        matching = [
            trade
            for trade in trades
            if str(
                trade.get("underlyingSymbol")
                or trade.get("symbol")
                or ""
            ).upper()
            == symbol
            and trade_category(trade) == category
        ]

        if category == "STOCK_FUTURES":
            matching = [
                trade for trade in trades
                if trade_category(trade) == category
            ]

        summaries.append(
            {
                "symbol": symbol,
                "category": category,
                "tradeCount": len(matching),
                "winRate": calculate_win_rate(
                    matching
                ),
                "cumulativePnL": round(
                    sum(
                        trade["grossPnL"]
                        for trade in matching
                    ),
                    2,
                ),
            }
        )

    return summaries


def normalized_underlying(trade: dict) -> str:
    symbol = str(
        trade.get("underlyingSymbol")
        or trade.get("underlying_symbol")
        or trade.get("symbol")
        or ""
    ).upper()
    if "BANKNIFTY" in symbol:
        return "BANKNIFTY"
    if "NIFTY" in symbol:
        return "NIFTY"
    return symbol


def inferred_instrument_class(trade: dict) -> str:
    """Classify legacy journal rows that predate the instrument_class column."""
    explicit = str(
        trade.get("instrumentClass")
        or trade.get("instrument_class")
        or ""
    ).upper()
    if explicit and explicit != "INDEX_OPTION":
        return explicit

    trading_symbol = str(
        trade.get("tradingSymbol")
        or trade.get("trading_symbol")
        or ""
    ).upper()
    underlying = normalized_underlying(trade)
    if re.search(r"(?:^|[ _-])FUT(?:$|[ _-])", trading_symbol):
        return "STOCK_FUTURE"
    return explicit or "INDEX_OPTION"


def option_type(trade: dict) -> str:
    explicit = str(
        trade.get("optionType")
        or trade.get("option_type")
        or ""
    ).upper()
    if explicit in {"PE", "PUT"}:
        return "PUT"
    if explicit in {"CE", "CALL"}:
        return "CALL"

    trading_symbol = str(
        trade.get("tradingSymbol")
        or trade.get("trading_symbol")
        or ""
    ).upper()
    if re.search(r"(?:^|[ _-]|\d)(?:PE|PUT)(?:$|[ _-]|\d)", trading_symbol):
        return "PUT"
    if re.search(r"(?:^|[ _-]|\d)(?:CE|CALL)(?:$|[ _-]|\d)", trading_symbol):
        return "CALL"
    return "UNKNOWN"


def day_of_week_performance(trades: list[dict]) -> list[dict]:
    totals = {
        (day, symbol): 0.0
        for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
        for symbol in SYMBOLS
    }
    for trade in trades:
        try:
            day = datetime.strptime(str(trade.get("tradeDate") or ""), "%Y-%m-%d").strftime("%A")
        except (TypeError, ValueError):
            continue
        symbol = normalized_underlying(trade)
        if (day, symbol) in totals:
            totals[(day, symbol)] += safe_float(trade.get("grossPnL"))
    return [
        {"day": day, "symbol": symbol, "netPnL": round(totals[(day, symbol)], 2)}
        for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
        for symbol in SYMBOLS
    ]


def option_type_performance(trades: list[dict]) -> list[dict]:
    totals = {
        (option, symbol): 0.0
        for option in ("CALL", "PUT")
        for symbol in SYMBOLS
    }
    for trade in trades:
        key = (option_type(trade), normalized_underlying(trade))
        if key in totals:
            totals[key] += safe_float(trade.get("grossPnL"))
    return [
        {"optionType": option, "symbol": symbol, "netPnL": round(totals[(option, symbol)], 2)}
        for option in ("CALL", "PUT")
        for symbol in SYMBOLS
    ]


def index_trade_sequence_performance(trades: list[dict]) -> list[dict]:
    index_trades = [
        trade
        for trade in trades
        if normalized_underlying(trade) in SYMBOLS
        and str(trade.get("instrumentClass") or "INDEX_OPTION").upper() == "INDEX_OPTION"
    ]
    groups = {}
    for trade in index_trades:
        sequence = safe_int(trade.get("tradeSequence"))
        if sequence <= 0:
            sequence = 1
        label = "Trade 1" if sequence == 1 else "Trade 2+"
        groups.setdefault(label, []).append(trade)

    return [
        {
            "sequence": label,
            **performance_stats(groups.get(label, [])),
        }
        for label in ("Trade 1", "Trade 2+")
    ]


def second_trade_context_performance(trades: list[dict]) -> list[dict]:
    rows = [
        trade
        for trade in trades
        if safe_int(trade.get("tradeSequence")) >= 2
        and normalized_underlying(trade) in SYMBOLS
    ]
    groups = {}
    for trade in rows:
        key = trade.get("priorTradeOutcome") or "UNKNOWN"
        groups.setdefault(key, []).append(trade)

    return [
        {
            "priorOutcome": key,
            **performance_stats(value),
        }
        for key, value in sorted(groups.items())
    ]


def is_upstox_sync_trade(trade: dict) -> bool:
    exit_reason = str(trade.get("exitReason") or "").upper()
    trading_symbol = str(trade.get("tradingSymbol") or "").upper()
    return (
        exit_reason == UPSTOX_SYNC_EXIT_REASON
        or trading_symbol.startswith("UPSTOX SYNC")
    )


def bot_only_trades(trades: list[dict]) -> list[dict]:
    return [
        trade
        for trade in trades
        if not is_upstox_sync_trade(trade)
    ]


def is_dashboard_index_trade(trade: dict) -> bool:
    return (
        not is_upstox_sync_trade(trade)
        and normalized_underlying(trade) in SYMBOLS
        and str(trade.get("instrumentClass") or "INDEX_OPTION").upper() == "INDEX_OPTION"
    )


def dashboard_index_trades(trades: list[dict]) -> list[dict]:
    return [trade for trade in trades if is_dashboard_index_trade(trade)]


def selective_index_trades(trades: list[dict]) -> list[dict]:
    return dashboard_index_trades(trades)


def is_paper_trade(trade: dict) -> bool:
    strategy = str(trade.get("strategy") or "").strip().upper()
    return strategy == "SELECTIVE_PAPER" or strategy.endswith("_PAPER")


def is_stock_option_trade(trade: dict) -> bool:
    instrument_class = str(
        trade.get("instrumentClass") or ""
    ).upper()
    return instrument_class == "STOCK_OPTION"


def entry_minutes(trade: dict) -> int | None:
    value = str(trade.get("entryTime") or "").strip()
    if not value:
        return None
    match = re.search(r"(?:T|\s)(\d{1,2}):(\d{2})", value)
    if not match:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", value)
    if not match:
        return None
    hour = safe_int(match.group(1), -1)
    minute = safe_int(match.group(2), -1)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour * 60 + minute


def edge_score_band(score: float | None, score_version: str | None = None) -> str:
    # Historical journal rows predate the score_version column but still carry
    # the numeric entry score that was shown in the dashboard at trade time.
    # Keep those rows usable; otherwise schema migration turns the entire
    # historical heat map into the hidden Unscored category.
    if score is None:
        return EDGE_UNSCORED_BAND
    for label, lower, upper in EDGE_SCORE_BANDS:
        if score >= lower and score < upper:
            return label
    return EDGE_UNSCORED_BAND


def edge_time_bucket(minutes: int | None) -> str:
    if minutes is None:
        return "unknown"
    for bucket_id, _label, start, end in EDGE_TIME_BUCKETS:
        if start is None or end is None:
            continue
        if start <= minutes < end or (
            bucket_id == "late_afternoon" and minutes == end
        ):
            return bucket_id
    return "unknown"


def edge_analytics(trades: list[dict]) -> dict:
    eligible = []
    for trade in trades:
        if normalized_underlying(trade) not in SYMBOLS:
            continue
        if str(trade.get("instrumentClass") or "INDEX_OPTION").upper() != "INDEX_OPTION":
            continue
        score = safe_float(trade.get("score"), None)
        score_version = trade.get("scoreVersion")
        score_band = edge_score_band(score, score_version)
        time_bucket = edge_time_bucket(entry_minutes(trade))
        enriched = dict(trade)
        enriched["edgeScoreBand"] = score_band
        enriched["edgeTimeBucket"] = time_bucket
        eligible.append(enriched)

    wins = [trade for trade in eligible if safe_float(trade.get("grossPnL")) > 0]
    losses = [trade for trade in eligible if safe_float(trade.get("grossPnL")) < 0]
    gross_profit = sum(safe_float(trade.get("grossPnL")) for trade in wins)
    gross_loss = abs(sum(safe_float(trade.get("grossPnL")) for trade in losses))
    total_pnl = sum(safe_float(trade.get("grossPnL")) for trade in eligible)

    matrix = []
    for bucket_id, _label, _start, _end in EDGE_TIME_BUCKETS:
        for score_label in [
            *[label for label, _lower, _upper in EDGE_SCORE_BANDS],
            EDGE_UNSCORED_BAND,
        ]:
            matching = [
                trade
                for trade in eligible
                if trade["edgeTimeBucket"] == bucket_id
                and trade["edgeScoreBand"] == score_label
            ]
            trades_count = len(matching)
            pnl = sum(safe_float(trade.get("grossPnL")) for trade in matching)
            matrix.append(
                {
                    "timeBucket": bucket_id,
                    "scoreBand": score_label,
                    "expectancy": round(pnl / trades_count, 2) if trades_count else 0.0,
                    "trades": trades_count,
                    "winRate": calculate_win_rate(matching),
                }
            )

    time_performance = []
    for bucket_id, label, _start, _end in EDGE_TIME_BUCKETS:
        matching = [
            trade
            for trade in eligible
            if trade["edgeTimeBucket"] == bucket_id
        ]
        count = len(matching)
        pnl = sum(safe_float(trade.get("grossPnL")) for trade in matching)
        time_performance.append(
            {
                "id": bucket_id,
                "label": label,
                "expectancy": round(pnl / count, 2) if count else 0.0,
                "trades": count,
            }
        )

    populated_cells = [cell for cell in matrix if cell["trades"] > 0]
    best_cell = max(populated_cells, key=lambda cell: cell["expectancy"], default=None)
    labels_by_id = {
        bucket_id: label
        for bucket_id, label, _start, _end in EDGE_TIME_BUCKETS
    }
    best_zone = None
    if best_cell:
        best_zone = {
            **best_cell,
            "timeLabel": labels_by_id[best_cell["timeBucket"]],
        }
        key_insight = (
            f"{best_zone['scoreBand']} trades during {best_zone['timeLabel']} "
            f"have the strongest expectancy at ₹{best_zone['expectancy']:,.0f} per trade."
        )
    else:
        key_insight = "More scored index-option trades are needed to identify a reliable edge zone."

    return {
        "scoreVersion": UNIFIED_SCORE_VERSION,
        "overallExpectancy": round(total_pnl / len(eligible), 2) if eligible else 0.0,
        "winRate": calculate_win_rate(eligible),
        "wins": len(wins),
        "profitFactor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "totalTrades": len(eligible),
        "symbolTrades": {
            symbol: sum(
                1
                for trade in eligible
                if normalized_underlying(trade) == symbol
            )
            for symbol in SYMBOLS
        },
        "scoreBands": [
            *[label for label, _lower, _upper in EDGE_SCORE_BANDS],
            EDGE_UNSCORED_BAND,
        ],
        "timeBuckets": [
            {"id": bucket_id, "label": label}
            for bucket_id, label, _start, _end in EDGE_TIME_BUCKETS
        ],
        "matrix": matrix,
        "timePerformance": time_performance,
        "bestZone": best_zone,
        "keyInsight": key_insight,
    }


def normalized_edge_analytics_per_lakh(trades: list[dict]) -> dict:
    """Return size-independent expectancy using ₹1 lakh of entry premium per trade."""
    scalable = []
    excluded_unscalable = 0
    excluded_hidden = 0
    for trade in trades:
        if trade_value_at_entry(trade) <= 0:
            excluded_unscalable += 1
            continue
        score = safe_float(trade.get("score"), None)
        time_bucket = edge_time_bucket(entry_minutes(trade))
        if (
            score is None
            or score < 50
            or time_bucket == "unknown"
        ):
            excluded_hidden += 1
            continue
        scalable.append(normalize_trade_per_lakh(trade))

    analytics = edge_analytics(scalable)
    hidden_score_bands = {"<50", "00-49", EDGE_UNSCORED_BAND}
    analytics["scoreBands"] = [
        band
        for band in analytics.get("scoreBands", [])
        if band not in hidden_score_bands
    ]
    analytics["timeBuckets"] = [
        bucket
        for bucket in analytics.get("timeBuckets", [])
        if bucket.get("id") != "unknown"
    ]
    visible_times = {bucket["id"] for bucket in analytics["timeBuckets"]}
    visible_scores = set(analytics["scoreBands"])
    analytics["matrix"] = [
        cell
        for cell in analytics.get("matrix", [])
        if cell.get("timeBucket") in visible_times
        and cell.get("scoreBand") in visible_scores
    ]
    analytics["timePerformance"] = [
        item
        for item in analytics.get("timePerformance", [])
        if item.get("id") in visible_times
    ]
    analytics["normalizationBasis"] = "PER_LAKH_ENTRY_PREMIUM"
    analytics["normalizationLabel"] = "Per ₹1L deployed"
    analytics["excludedUnscalableTrades"] = excluded_unscalable
    analytics["excludedHiddenCategoryTrades"] = excluded_hidden
    best_zone = analytics.get("bestZone")
    if best_zone:
        analytics["keyInsight"] = (
            f"{best_zone['scoreBand']} trades during {best_zone['timeLabel']} "
            f"have the strongest normalized expectancy at "
            f"₹{best_zone['expectancy']:,.0f} per trade per ₹1L deployed."
        )
    return analytics


def today_category_pnl(
    trades: list[dict],
) -> dict:
    option_sell = sum(
        trade["grossPnL"]
        for trade in trades
        if trade_category(trade) == "OPTION_SELL"
    )
    option_buy = sum(
        trade["grossPnL"]
        for trade in trades
        if trade_category(trade) == "OPTION_BUY"
    )
    stock_futures = sum(
        trade["grossPnL"]
        for trade in trades
        if trade_category(trade) == "STOCK_FUTURES"
    )

    return {
        "optionSell": round(option_sell, 2),
        "optionBuy": round(option_buy, 2),
        "stockFutures": round(stock_futures, 2),
        "overall": round(
            option_sell
            + option_buy
            + stock_futures,
            2,
        ),
    }


def normalize_trade(row: dict) -> dict:
    trade = {
        "tradeDate": row.get("trade_date", ""),
        "symbol": row.get("symbol", ""),
        "underlyingSymbol": row.get("underlying_symbol", row.get("symbol", "")),
        "instrumentClass": row.get("instrument_class", "INDEX_OPTION"),
        "strategy": str(row.get("strategy") or "SELECTIVE").strip().upper(),
        "tradingSymbol": row.get(
            "trading_symbol",
            "",
        ),
        "optionType": row.get("option_type", ""),
        "direction": row.get("direction", ""),
        "transactionType": str(
            row.get("transaction_type") or "BUY"
        ).upper(),
        "positionSide": row.get(
            "position_side",
            "",
        ),
        "quantity": safe_int(row.get("quantity")),
        "entryTime": row.get("entry_time", ""),
        "entryPrice": safe_float(
            row.get("entry_price")
        ),
        "exitTime": row.get("exit_time", ""),
        "exitPrice": safe_float(
            row.get("exit_price")
        ),
        "targetPrice": safe_float(
            row.get("target_price")
        ),
        "stopLossPrice": safe_float(
            row.get("stop_loss_price")
        ),
        "exitReason": row.get("exit_reason", ""),
        "grossPnL": safe_float(
            row.get("gross_pnl")
        ),
        "score": safe_float(row.get("score"), None),
        "scoreVersion": row.get("score_version", ""),
        "tradeSequence": safe_int(row.get("trade_sequence")),
        "priorTradeSymbol": row.get("prior_trade_symbol", ""),
        "priorTradeOutcome": row.get("prior_trade_outcome", ""),
        "priorTradePnL": safe_float(row.get("prior_trade_pnl")),
        "riskPerTradeLimit": safe_float(row.get("risk_per_trade_limit")),
        "remainingIndexRiskBudget": safe_float(row.get("remaining_index_risk_budget")),
        "plannedRisk": safe_float(row.get("planned_risk")),
        "status": row.get("status", "CLOSED"),
    }
    trade["instrumentClass"] = inferred_instrument_class(trade)
    trade["optionType"] = option_type(trade)
    return trade


def read_trade_history() -> list[dict]:
    if not TRADE_HISTORY_FILE.exists():
        return []

    try:
        with TRADE_HISTORY_FILE.open(
            "r",
            newline="",
            errors="ignore",
        ) as file:
            reader = csv.DictReader(file)

            return [
                normalize_trade(row)
                for row in reader
                if row
            ]
    except (OSError, csv.Error):
        return []


def build_equity_curve(
    trades: list[dict],
    daily_overrides: dict[str, float] | None = None,
) -> list[dict]:
    daily_totals = {}

    for trade in trades:
        trade_date = trade["tradeDate"]

        if not trade_date:
            continue

        daily_totals.setdefault(
            trade_date,
            0.0,
        )

        daily_totals[trade_date] += (
            trade["grossPnL"]
        )

    if daily_overrides:
        for trade_date, pnl in daily_overrides.items():
            daily_totals[trade_date] = pnl

    cumulative_pnl = 0.0
    points = []

    for trade_date in sorted(daily_totals):
        daily_pnl = round(
            daily_totals[trade_date],
            2,
        )

        cumulative_pnl += daily_pnl

        points.append(
            {
                "date": trade_date,
                "dailyPnL": daily_pnl,
                "cumulativePnL": round(
                    cumulative_pnl,
                    2,
                ),
            }
        )

    return points


def build_pnl_calendar(trades: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for trade in trades:
        trade_date = str(trade.get("tradeDate") or "").strip()
        if trade_date:
            grouped.setdefault(trade_date, []).append(trade)

    calendar = []
    for trade_date in sorted(grouped):
        daily_trades = grouped[trade_date]
        gross_pnl = round(
            sum(safe_float(trade.get("grossPnL")) for trade in daily_trades),
            2,
        )
        calendar.append(
            {
                "date": trade_date,
                "trades": len(daily_trades),
                "grossPnL": gross_pnl,
                "netPnL": gross_pnl,
            }
        )
    return calendar


def fetch_upstox_today_pnl() -> tuple[float | None, str | None, int, dict[str, float], dict[str, int]]:
    """Fetch today's realized gross P&L directly from Upstox."""
    headers = upstox_headers()
    if not headers:
        return None, "UPSTOX_ACCESS_TOKEN is not configured", 0, {}, {}

    today = datetime.now(IST)
    date_text = today.strftime("%d-%m-%Y")
    financial_year = (
        f"{today.year % 100:02d}{(today.year + 1) % 100:02d}"
        if today.month >= 4
        else f"{(today.year - 1) % 100:02d}{today.year % 100:02d}"
    )

    try:
        response = requests.get(
            UPSTOX_TRADE_PNL_URL,
            headers=headers,
            params={
                "from_date": date_text,
                "to_date": date_text,
                "segment": "FO",
                "financial_year": financial_year,
                "page_number": 1,
                "page_size": 5000,
            },
            timeout=12,
        )
        if response.status_code >= 300:
            return None, f"Upstox P&L request failed with status {response.status_code}", 0, {}, {}

        payload = response.json()
        rows = payload.get("data", [])
        if not isinstance(rows, list):
            return None, "Upstox P&L response contained invalid data", 0, {}, {}

        gross_pnl = sum(
            safe_float(row.get("sell_amount"))
            - safe_float(row.get("buy_amount"))
            for row in rows
            if isinstance(row, dict)
        )
        symbol_totals = {symbol: 0.0 for symbol in SYMBOLS}
        symbol_counts = {symbol: 0 for symbol in SYMBOLS}
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("scrip_name") or "").upper()
            symbol = "BANKNIFTY" if "BANKNIFTY" in name else "NIFTY"
            symbol_counts[symbol] += 1
            symbol_totals[symbol] += (
                safe_float(row.get("sell_amount"))
                - safe_float(row.get("buy_amount"))
            )
        return round(gross_pnl, 2), None, len(rows), {
            symbol: round(value, 2)
            for symbol, value in symbol_totals.items()
        }, symbol_counts
    except (requests.RequestException, ValueError, TypeError) as error:
        return None, f"Upstox P&L request failed: {type(error).__name__}", 0, {}, {}


def _build_trade_performance_payload(
    trades: list[dict],
    today_text: str,
) -> dict:
    today_trades = [
        trade
        for trade in trades
        if trade["tradeDate"] == today_text
    ]

    recent_trades = sorted(
        trades,
        key=lambda trade: trade["exitTime"],
        reverse=True,
    )[:20]
    local_today_pnl = round(sum(trade["grossPnL"] for trade in today_trades), 2)
    if today_trades:
        today_closed_pnl = local_today_pnl
        today_closed_trades = len(today_trades)
        today_pnl_source = "TRADE_LOG"
        today_symbol_pnl = symbol_pnl(today_trades)
        today_symbol_trades = {
            symbol: sum(
                1
                for trade in today_trades
                if normalized_underlying(trade) == symbol
            )
            for symbol in SYMBOLS
        }
    else:
        today_closed_pnl = 0.0
        today_closed_trades = 0
        today_pnl_source = "UNAVAILABLE"
        today_symbol_pnl = symbol_pnl([])
        today_symbol_trades = {symbol: 0 for symbol in SYMBOLS}
    today_categories = today_category_pnl(today_trades)
    today_categories["overall"] = today_closed_pnl
    average_profit, average_loss = average_trade_results(trades)
    bot_pnl = today_closed_pnl
    total_upstox_pnl = None
    manual_other_pnl = None
    cumulative_total_pnl = round(
        sum(
            trade["grossPnL"]
            for trade in trades
        ),
        2,
    )
    cumulative_symbol_pnl = symbol_pnl(trades)
    day_of_week = day_of_week_performance(trades)
    today_stats = symbol_performance_stats(today_trades)
    cumulative_stats = symbol_performance_stats(trades)

    return {
        "today": {
            "closedTrades": today_closed_trades,
            "closedPnL": today_closed_pnl,
            "netPnL": today_stats["OVERALL"]["netPnL"],
            "botPnL": bot_pnl,
            "closedPnLSource": today_pnl_source,
            "closedPnLError": None,
            "brokerClosedTrades": 0,
            "brokerClosedPnL": None,
            "manualOtherPnL": manual_other_pnl,
            "totalUpstoxPnL": total_upstox_pnl,
            "winRate": calculate_win_rate(
                today_trades
            ),
            "symbolPnL": today_symbol_pnl,
            "symbolTrades": today_symbol_trades,
            "symbolStats": {
                symbol: today_stats[symbol]
                for symbol in SYMBOLS
            },
            "overallStats": today_stats["OVERALL"],
            "categoryPnL": today_categories,
            "optionTypePerformance": option_type_performance(today_trades),
            "indexTradeSequencePerformance": index_trade_sequence_performance(today_trades),
            "secondTradeContextPerformance": second_trade_context_performance(today_trades),
        },
        "cumulative": {
            "totalTrades": len(trades),
            "totalPnL": cumulative_total_pnl,
            "netPnL": cumulative_stats["OVERALL"]["netPnL"],
            "winRate": calculate_win_rate(
                trades
            ),
            "averageProfitPerWinningTrade": average_profit,
            "averageLossPerLosingTrade": average_loss,
            "symbolPnL": cumulative_symbol_pnl,
            "symbolStats": {
                symbol: cumulative_stats[symbol]
                for symbol in SYMBOLS
            },
            "overallStats": cumulative_stats["OVERALL"],
            "categoryPerformance": category_performance(
                trades
            ),
            "dayOfWeekPerformance": day_of_week,
            "optionTypePerformance": option_type_performance(trades),
            "indexTradeSequencePerformance": index_trade_sequence_performance(trades),
            "secondTradeContextPerformance": second_trade_context_performance(trades),
        },
        "equityCurve": build_equity_curve(
            trades,
        ),
        "pnlCalendar": build_pnl_calendar(trades),
        "edgeAnalytics": edge_analytics(trades),
        "recentTrades": recent_trades,
    }


def build_trade_performance(analytics_mode: str = "real") -> dict:
    analytics_mode = str(analytics_mode or "real").strip().lower()
    if analytics_mode not in {"real", "mixed"}:
        raise ValueError("analytics_mode must be real or mixed")
    today_text = datetime.now(IST).strftime("%Y-%m-%d")
    history = read_trade_history()
    trades = [
        trade
        for trade in selective_index_trades(history)
        if normalized_underlying(trade) == "NIFTY"
        and (analytics_mode == "mixed" or not is_paper_trade(trade))
    ]
    raw = _build_trade_performance_payload(trades, today_text)
    normalized = _build_trade_performance_payload(
        normalize_trades_per_lakh(trades),
        today_text,
    )
    normalized_edge = normalized_edge_analytics_per_lakh(trades)
    raw["edgeAnalytics"] = normalized_edge
    normalized["edgeAnalytics"] = normalized_edge
    raw["normalizedPerLakh"] = normalized
    raw["analyticsMode"] = analytics_mode.upper()
    normalized["analyticsMode"] = analytics_mode.upper()
    return raw

def state_file(symbol: str) -> Path:
    return BASE_DIR / f"trade_state_{symbol}.json"


def read_json_file(
    path: Path,
    default=None,
):
    if default is None:
        default = {}

    if not path.exists():
        return default

    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def upstox_headers():
    token = os.getenv("UPSTOX_ACCESS_TOKEN")

    if not token:
        return None

    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }


def fetch_upstox_positions() -> tuple[list, str | None]:
    """
    Read current positions from Upstox.

    No order-placement endpoint is used.
    Error responses are deliberately sanitized.
    """
    headers = upstox_headers()

    if not headers:
        return [], "UPSTOX_ACCESS_TOKEN is not configured"

    try:
        response = requests.get(
            UPSTOX_POSITIONS_URL,
            headers=headers,
            timeout=12,
        )

        if response.status_code >= 300:
            return (
                [],
                "Upstox positions request failed "
                f"with status {response.status_code}",
            )

        payload = response.json()

        positions = payload.get("data", [])

        if not isinstance(positions, list):
            return [], "Unexpected Upstox response format"

        return positions, None

    except requests.Timeout:
        return [], "Upstox request timed out"

    except requests.RequestException:
        return [], "Unable to contact Upstox"

    except ValueError:
        return [], "Upstox returned invalid JSON"


def broker_position_quantity(position: dict) -> int:
    for key in ["quantity", "net_quantity"]:
        value = position.get(key)

        if value is not None:
            return safe_int(value)

    buy_quantity = safe_float(
        position.get("day_buy_quantity")
    )

    sell_quantity = safe_float(
        position.get("day_sell_quantity")
    )

    return int(buy_quantity - sell_quantity)


def broker_position_ltp(
    position: dict,
) -> float | None:
    for key in [
        "last_price",
        "ltp",
        "close_price",
    ]:
        value = position.get(key)

        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue

    return None


def broker_position_pnl(position: dict) -> float:
    for key in [
        "pnl",
        "day_pnl",
        "unrealised",
        "unrealized_pnl",
        "profit_and_loss",
    ]:
        value = position.get(key)

        if value is not None:
            return safe_float(value)

    return 0.0


def broker_average_price(
    position: dict,
    entry_transaction_type: str = "BUY",
) -> float | None:
    side_specific_keys = (
        ["sell_price", "day_sell_price"]
        if str(entry_transaction_type).upper() == "SELL"
        else ["buy_price", "day_buy_price"]
    )

    for key in ["average_price", *side_specific_keys, "avg_price"]:
        value = position.get(key)

        if value is not None:
            price = safe_float(value)

            if price > 0:
                return price

    return None


def find_broker_position(
    positions: list[dict],
    instrument_key: str,
) -> dict | None:
    for position in positions:
        position_key = (
            position.get("instrument_token")
            or position.get("instrument_key")
        )

        if position_key == instrument_key:
            return position

    return None


def calculate_target_progress(
    entry_price: float,
    last_price: float | None,
    target_price: float,
    entry_transaction_type: str = "BUY",
) -> float | None:
    is_short = str(entry_transaction_type).upper() == "SELL"
    if (
        last_price is None
        or entry_price <= 0
        or target_price <= 0
        or (is_short and target_price >= entry_price)
        or (not is_short and target_price <= entry_price)
    ):
        return None

    achieved = entry_price - last_price if is_short else last_price - entry_price
    planned = entry_price - target_price if is_short else target_price - entry_price
    progress = achieved / planned * 100

    return round(progress, 1)


def build_live_positions() -> dict:
    broker_positions, upstox_error = (
        fetch_upstox_positions()
    )

    dashboard_positions = []

    for symbol in STATE_SLOTS:
        state = read_json_file(
            state_file(symbol)
        )

        instrument_key = state.get(
            "instrument_key"
        )

        if not state or not instrument_key:
            continue

        broker_position = find_broker_position(
            broker_positions,
            instrument_key,
        )

        quantity = safe_int(
            state.get("quantity")
        )
        entry_transaction_type = str(
            state.get("entry_transaction_type")
            or "BUY"
        ).upper()
        is_short = entry_transaction_type == "SELL"

        broker_quantity = 0
        entry_price = safe_float(
            state.get("entry_price")
        )

        last_price = None

        if broker_position:
            broker_quantity = (
                broker_position_quantity(
                    broker_position
                )
            )

            last_price = broker_position_ltp(
                broker_position
            )

            broker_entry = broker_average_price(
                broker_position,
                entry_transaction_type,
            )

            if broker_entry is not None:
                entry_price = broker_entry

        planned_target_price = safe_float(
            state.get("target_price")
        )
        target_price = safe_float(
            state.get("profit_booking_price"),
            planned_target_price,
        )

        stop_loss_price = safe_float(
            state.get("stop_loss_price")
        )

        highest_price = safe_float(
            state.get("lowest_ltp")
            if is_short
            else state.get("highest_ltp"),
            entry_price,
        )

        live_pnl = None
        risk_to_stop = None
        reward_left = None

        if (
            last_price is not None
            and entry_price > 0
        ):
            live_pnl = round(
                (
                    entry_price - last_price
                    if is_short
                    else last_price - entry_price
                )
                * quantity,
                2,
            )

        if (
            last_price is not None
            and stop_loss_price > 0
        ):
            risk_to_stop = round(
                max(
                    stop_loss_price - last_price
                    if is_short
                    else last_price - stop_loss_price,
                    0,
                ) * quantity,
                2,
            )

        if (
            last_price is not None
            and target_price > 0
        ):
            reward_left = round(
                max(
                    last_price - target_price
                    if is_short
                    else target_price - last_price,
                    0,
                ) * quantity,
                2,
            )

        dashboard_positions.append(
            {
                "symbol": symbol,
                "underlyingSymbol": state.get("underlying_symbol", symbol),
                "instrumentClass": state.get("instrument_class", "INDEX_OPTION"),
                "tradingSymbol": state.get(
                    "trading_symbol",
                    "",
                ),
                "direction": state.get(
                    "direction",
                    "BUY",
                ),
                "transactionType": entry_transaction_type,
                "positionSide": state.get(
                    "position_side",
                    "SHORT_OPTION" if is_short else "LONG_OPTION",
                ),
                "status": state.get(
                    "status",
                    "OPEN",
                ),
                "quantity": quantity,
                "brokerQuantity": (
                    broker_quantity
                ),
                "entryPrice": entry_price,
                "lastPrice": last_price,
                "targetPrice": target_price,
                "plannedTargetPrice": planned_target_price,
                "profitBookingPercent": safe_float(
                    state.get("profit_booking_percent"),
                    80,
                ),
                "stopLossPrice": (
                    stop_loss_price
                ),
                "highestPrice": highest_price,
                "livePnL": live_pnl,
                "targetProgress": (
                    calculate_target_progress(
                        entry_price,
                        last_price,
                        target_price,
                        entry_transaction_type,
                    )
                ),
                "riskToStop": risk_to_stop,
                "rewardLeft": reward_left,
                "trailingStopActive": bool(
                    state.get(
                        "trailing_stop_active",
                        False,
                    )
                ),
                "trailingStopReason": (
                    state.get(
                        "trailing_stop_reason",
                        "",
                    )
                ),
                "createdAt": state.get(
                    "created_at",
                    "",
                ),
            }
        )

    total_live_pnl = round(
        sum(
            position["livePnL"] or 0
            for position in dashboard_positions
        ),
        2,
    )
    open_upstox_positions = [
        position
        for position in broker_positions
        if broker_position_quantity(position) != 0
    ]
    total_upstox_live_pnl = round(
        sum(
            broker_position_pnl(position)
            for position in open_upstox_positions
        ),
        2,
    )

    return {
        "upstoxStatus": (
            "healthy"
            if upstox_error is None
            else "check"
        ),
        "error": upstox_error,
        "openTradeCount": len(
            dashboard_positions
        ),
        "totalLivePnL": total_live_pnl,
        "upstoxOpenPositionCount": len(
            open_upstox_positions
        ),
        "totalUpstoxLivePnL": total_upstox_live_pnl,
        "positions": dashboard_positions,
    }


def normalize_live_positions_per_lakh(live: dict) -> dict:
    normalized = dict(live)
    positions = []
    for position in live.get("positions", []):
        scaled = dict(position)
        trade_value = abs(safe_float(position.get("entryPrice"))) * abs(
            safe_int(position.get("quantity"))
        )
        factor = PER_LAKH / trade_value if trade_value > 0 else 1.0
        scaled["tradeValueAtEntry"] = round(trade_value, 2)
        scaled["normalizationFactor"] = factor
        for field in ("livePnL", "riskToStop", "rewardLeft"):
            value = position.get(field)
            if value is not None:
                scaled[field] = round(safe_float(value) * factor, 2)
        positions.append(scaled)
    normalized["positions"] = positions
    normalized["totalLivePnL"] = round(
        sum(safe_float(position.get("livePnL")) for position in positions),
        2,
    )
    return normalized

def build_health_snapshot() -> dict:
    load_env()

    latest_analyses = latest_symbol_analyses()
    bot_status = apply_latest_entry_scores(
        parse_latest_bot_status(),
        latest_analyses,
    )

    trade_performance = build_trade_performance()
    live_positions = build_live_positions()
    today = trade_performance.get("today", {})
    bot_pnl = round(
        safe_float(today.get("botPnL"))
        + safe_float(live_positions.get("totalLivePnL")),
        2,
    )
    upstox_closed_pnl = today.get(
        "totalUpstoxPnL",
        today.get("closedPnL"),
    )
    total_upstox_live_pnl = live_positions.get(
        "totalUpstoxLivePnL",
        live_positions.get("totalLivePnL"),
    )
    total_upstox_pnl = round(
        safe_float(upstox_closed_pnl)
        + safe_float(total_upstox_live_pnl),
        2,
    )
    manual_other_pnl = (
        round(
            total_upstox_pnl - bot_pnl,
            2,
        )
    )
    stock_scanner = read_json_file(
        STOCK_SCANNER_STATUS_FILE,
        {
            "enabled": os.getenv("ENABLE_STOCK_FUTURES_SCANNER", "false").lower() == "true",
            "status": "NO DATA",
            "message": "The stock-futures scanner has not run yet",
        },
    )

    return {
        "status": "ok",
        "service": "HK Trading Dashboard Data",
        "baseDirectory": str(BASE_DIR),
        "files": {
            "tradeHistory": file_status(
                TRADE_HISTORY_FILE
            ),
            "analysisHistory": file_status(
                ANALYSIS_HISTORY_FILE
            ),
            "botLog": file_status(LOG_FILE),
            "stockScannerStatus": file_status(STOCK_SCANNER_STATUS_FILE),
            "environmentFilePresent": ENV_FILE.exists(),
        },
        "configuration": {
            "upstoxTokenPresent": bool(
                os.getenv("UPSTOX_ACCESS_TOKEN")
            )
        },
        "bot": bot_status,
        "lastRuns": latest_analyses,
        "todayScans": build_today_scans(),
        "performance": trade_performance,
        "live": live_positions,
        "upstoxAccount": {
            "botPnL": bot_pnl,
            "manualOtherPnL": manual_other_pnl,
            "totalUpstoxPnL": total_upstox_pnl,
            "openPositionCount": live_positions.get(
                "upstoxOpenPositionCount",
                0,
            ),
            "closedPnLError": today.get("closedPnLError"),
        },
        "stockFuturesScanner": stock_scanner,
        "shadowExitCalibration": read_json_file(
            SHADOW_CONFIG_FILE,
            {
                "mode": "SHADOW_ONLY",
                "execution_applied": False,
                "status": "NO DATA",
            },
        ),
        "adaptiveLivePolicy": read_json_file(
            LIVE_POLICY_FILE,
            {
                "mode": "AUTO_ADAPTIVE_LIVE",
                "global_status": "NO DATA",
                "execution_applied": False,
            },
        ),
    }


if __name__ == "__main__":
    snapshot = build_health_snapshot()

    print(
        json.dumps(
            snapshot,
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )
