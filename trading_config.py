"""Server-side capital profile selection for the mobile controls.

The mobile app is only a client.  Trading limits are stored here so the
selection window and the values used by the bot cannot be bypassed by hiding
or changing a UI control.
"""

from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import json
import os

from safe_storage import atomic_write_json


IST = ZoneInfo("Asia/Kolkata")
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "data" / "trading_config.json"
DEFAULT_PROFILE_ID = "MAX"
RISK_LIMIT_OPTIONS = tuple(float(value) for value in range(5_000, 30_001, 5_000))

# Capital is allocated independently to each eligible NIFTY and BANKNIFTY
# position. Rupee risk and daily P&L fields are derived from the active capital
# profile before they are returned to the bot and mobile clients.
CAPITAL_PROFILES = {
    "MAX": {
        "label": "MAX",
        # Negative one is a JSON-safe sentinel. The trading bot resolves it
        # against Upstox available funds immediately before order sizing.
        "optionCapitalPerEntry": -1.0,
        "dailyMaxLoss": 0.0,
        "dailyProfitTarget": 0.0,
    },
    "1_LOT": {
        "label": "1 Lot",
        "optionCapitalPerEntry": 1.0,
        "dailyMaxLoss": 0.0,
        "dailyProfitTarget": 0.0,
    },
    "50000": {
        "label": "50K",
        "optionCapitalPerEntry": 50000.0,
        "dailyMaxLoss": 0.0,
        "dailyProfitTarget": 0.0,
    },
    "100000": {
        "label": "1L",
        "optionCapitalPerEntry": 100000.0,
        "dailyMaxLoss": 0.0,
        "dailyProfitTarget": 0.0,
    },
    "150000": {
        "label": "1.5L",
        "optionCapitalPerEntry": 150000.0,
        "dailyMaxLoss": 0.0,
        "dailyProfitTarget": 0.0,
    },
    "200000": {
        "label": "2L",
        "optionCapitalPerEntry": 200000.0,
        "dailyMaxLoss": 0.0,
        "dailyProfitTarget": 0.0,
    },
    "250000": {
        "label": "2.5L",
        "optionCapitalPerEntry": 250000.0,
        "dailyMaxLoss": 0.0,
        "dailyProfitTarget": 0.0,
    },
    "300000": {
        "label": "3L",
        "optionCapitalPerEntry": 300000.0,
        "dailyMaxLoss": 0.0,
        "dailyProfitTarget": 0.0,
    },
    "350000": {
        "label": "3.5L",
        "optionCapitalPerEntry": 350000.0,
        "dailyMaxLoss": 0.0,
        "dailyProfitTarget": 0.0,
    },
}


def configured_default_profile_id():
    profile_id = str(os.getenv("DEFAULT_TRADING_PROFILE", DEFAULT_PROFILE_ID)).strip().upper()
    if profile_id not in CAPITAL_PROFILES:
        raise RuntimeError(
            "DEFAULT_TRADING_PROFILE must be one of " + ", ".join(CAPITAL_PROFILES)
        )
    return profile_id


def now_ist():
    return datetime.now(IST)


def selection_window_open(now=None):
    current = now or now_ist()
    return time(9, 0) <= current.time() < time(9, 15)


def _default_config():
    today = now_ist().date().isoformat()
    return {
        "profileId": configured_default_profile_id(),
        "dailyProfitTarget": _default_risk_limit("DAILY_PROFIT_TARGET", 5_000),
        "dailyMaxLoss": _default_risk_limit("DAILY_MAX_LOSS", 5_000),
        "selectedDate": today,
        "selectedAt": None,
        "resetDoneDate": None,
    }


def _read():
    if not CONFIG_FILE.exists():
        return _default_config()
    try:
        value = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError, TypeError):
        return _default_config()
    if not isinstance(value, dict):
        return _default_config()
    config = _default_config()
    config.update(value)
    if config.get("profileId") not in CAPITAL_PROFILES:
        config["profileId"] = configured_default_profile_id()
    for field, env_name in (
        ("dailyProfitTarget", "DAILY_PROFIT_TARGET"),
        ("dailyMaxLoss", "DAILY_MAX_LOSS"),
    ):
        try:
            value = float(config.get(field))
        except (TypeError, ValueError):
            value = _default_risk_limit(env_name, 5_000)
        config[field] = (
            value
            if value in RISK_LIMIT_OPTIONS
            else _default_risk_limit(env_name, 5_000)
        )
    return config


def _write(config):
    atomic_write_json(CONFIG_FILE, config)


def _configured_float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return float(default)


def _configured_bool(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _default_risk_limit(name, default):
    value = _configured_float(name, default)
    return value if value in RISK_LIMIT_OPTIONS else float(default)


ABSOLUTE_LIMIT_CAPS = {
    "dailyMaxLoss": "DAILY_MAX_LOSS_ABSOLUTE_CAP",
    "dailyProfitTarget": "DAILY_PROFIT_TARGET_ABSOLUTE_CAP",
    "dailySoftLoss": "DAILY_SOFT_LOSS_ABSOLUTE_CAP",
    "peakProfitGivebackTrigger": "PEAK_PROFIT_GIVEBACK_ABSOLUTE_CAP",
    "indexRiskPerTrade": "INDEX_RISK_PER_TRADE_ABSOLUTE_CAP",
    "maxDailyIndexRisk": "MAX_DAILY_INDEX_RISK_ABSOLUTE_CAP",
    "maxOpenPortfolioRisk": "MAX_OPEN_PORTFOLIO_RISK_ABSOLUTE_CAP",
}


def _apply_absolute_limit_caps(values):
    for field, env_name in ABSOLUTE_LIMIT_CAPS.items():
        cap = max(_configured_float(env_name, 0.0), 0.0)
        if cap > 0 and float(values.get(field) or 0) > 0:
            values[field] = min(float(values[field]), cap)
    return values


def _profile_with_dynamic_limits(profile):
    """Scale trading-day guardrails from the selected rupee allocation."""
    values = dict(profile)
    capital = float(values.get("optionCapitalPerEntry") or 0)
    if capital <= 1:
        values.update(
            dailyMaxLoss=max(
                _configured_float("DAILY_MAX_LOSS", 10000.0),
                0.0,
            ),
            dailyProfitTarget=max(
                _configured_float("DAILY_PROFIT_TARGET", 10000.0),
                0.0,
            ),
            dailySoftLoss=max(
                _configured_float("DAILY_SOFT_LOSS", 0.0),
                0.0,
            ),
            peakProfitGivebackTrigger=max(
                _configured_float("PEAK_PROFIT_GIVEBACK_TRIGGER", 0.0),
                0.0,
            ),
            indexRiskPerTrade=max(
                _configured_float("INDEX_RISK_PER_TRADE", 0.0),
                0.0,
            ),
            maxDailyIndexRisk=max(
                _configured_float("MAX_DAILY_INDEX_RISK", 0.0),
                0.0,
            ),
            maxOpenPortfolioRisk=max(
                _configured_float("MAX_OPEN_PORTFOLIO_RISK", 0.0),
                0.0,
            ),
        )
        return _apply_absolute_limit_caps(values)
    if not _configured_bool("DYNAMIC_CAPITAL_RISK_ENABLED", True):
        return values

    percentages = {
        "dailyMaxLoss": ("DAILY_MAX_LOSS_CAPITAL_PERCENT", 15.0),
        "dailyProfitTarget": ("DAILY_PROFIT_TARGET_CAPITAL_PERCENT", 10.0),
        "dailySoftLoss": ("DAILY_SOFT_LOSS_CAPITAL_PERCENT", 7.5),
        "peakProfitGivebackTrigger": (
            "PEAK_PROFIT_GIVEBACK_TRIGGER_CAPITAL_PERCENT",
            7.5,
        ),
        "indexRiskPerTrade": ("INDEX_RISK_PER_TRADE_CAPITAL_PERCENT", 15.0),
        "maxDailyIndexRisk": ("MAX_DAILY_INDEX_RISK_CAPITAL_PERCENT", 15.0),
        # Includes the default 15% portfolio buffer around a 15% stop-risk cap.
        "maxOpenPortfolioRisk": ("MAX_OPEN_PORTFOLIO_RISK_CAPITAL_PERCENT", 17.25),
    }
    for field, (env_name, default_percent) in percentages.items():
        percent = max(_configured_float(env_name, default_percent), 0.0)
        values[field] = round(capital * percent / 100.0, 2)

    return _apply_absolute_limit_caps(values)


def _ensure_automatic_reset(config, current=None):
    current = current or now_ist()
    today = current.date().isoformat()
    selected_date = str(config.get("selectedDate") or "")
    stale_from_prior_day = selected_date < today
    after_daily_reset = (
        current.time() >= time(15, 30)
        and config.get("resetDoneDate") != today
    )
    if stale_from_prior_day or after_daily_reset:
        config["profileId"] = configured_default_profile_id()
        config["dailyProfitTarget"] = _default_risk_limit(
            "DAILY_PROFIT_TARGET", 5_000
        )
        config["dailyMaxLoss"] = _default_risk_limit("DAILY_MAX_LOSS", 5_000)
        config["selectedDate"] = today
        config["selectedAt"] = current.isoformat()
        config["resetDoneDate"] = today
        _write(config)
    return config


def get_config():
    current = now_ist()
    config = _ensure_automatic_reset(_read(), current)
    profile_id = config["profileId"]
    profile = _profile_with_dynamic_limits(CAPITAL_PROFILES[profile_id])
    profile["dailyProfitTarget"] = float(config["dailyProfitTarget"])
    profile["dailyMaxLoss"] = float(config["dailyMaxLoss"])
    profile = _apply_absolute_limit_caps(profile)
    return {
        "profileId": profile_id,
        "profile": profile,
        "options": [
            {"id": key, **_profile_with_dynamic_limits(value)}
            for key, value in CAPITAL_PROFILES.items()
        ],
        "dailyProfitTarget": profile["dailyProfitTarget"],
        "dailyMaxLoss": profile["dailyMaxLoss"],
        "riskLimitOptions": list(RISK_LIMIT_OPTIONS),
        "serverTime": current.isoformat(),
        "selectionWindowOpen": selection_window_open(current),
        "selectionWindow": "09:00-09:15 IST",
        "automaticReset": (
            "15:30 IST / next trading day -> " + configured_default_profile_id()
        ),
        "selectedDate": config.get("selectedDate"),
        "selectedAt": config.get("selectedAt"),
    }


def select_profile(profile_id, daily_profit_target=None, daily_max_loss=None):
    current = now_ist()
    config = _ensure_automatic_reset(_read(), current)
    if not selection_window_open(current):
        raise PermissionError("Capital selection is available only from 09:00 to 09:15 IST")
    if profile_id not in CAPITAL_PROFILES:
        raise ValueError("Unknown capital profile")
    daily_profit_target = float(
        config.get("dailyProfitTarget")
        if daily_profit_target is None
        else daily_profit_target
    )
    daily_max_loss = float(
        config.get("dailyMaxLoss") if daily_max_loss is None else daily_max_loss
    )
    if daily_profit_target not in RISK_LIMIT_OPTIONS:
        raise ValueError("Daily profit target must be 5000 to 30000 in steps of 5000")
    if daily_max_loss not in RISK_LIMIT_OPTIONS:
        raise ValueError("Daily maximum loss must be 5000 to 30000 in steps of 5000")
    config.update({
        "profileId": profile_id,
        "dailyProfitTarget": daily_profit_target,
        "dailyMaxLoss": daily_max_loss,
        "selectedDate": current.date().isoformat(),
        "selectedAt": current.isoformat(),
        "resetDoneDate": None,
    })
    _write(config)
    return get_config()


def active_value(name, fallback):
    """Return a selected profile value, falling back to .env before setup."""
    try:
        value = get_config()["profile"].get(name)
        return fallback if value is None else value
    except Exception:
        return fallback
