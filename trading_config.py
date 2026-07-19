"""Server-side capital profile selection for the mobile controls.

The mobile app is only a client.  Trading limits are stored here so the
selection window and the values used by the bot cannot be bypassed by hiding
or changing a UI control.
"""

from datetime import datetime, time
from pathlib import Path
from tempfile import NamedTemporaryFile
from zoneinfo import ZoneInfo

import json


IST = ZoneInfo("Asia/Kolkata")
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "data" / "trading_config.json"
DEFAULT_PROFILE_ID = "1_LOT"

# Daily profit/loss fields remain in the API for mobile-client compatibility,
# but zero means no daily P&L gate. Capital is allocated independently to each
# eligible NIFTY and BANKNIFTY position.
CAPITAL_PROFILES = {
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


def now_ist():
    return datetime.now(IST)


def selection_window_open(now=None):
    current = now or now_ist()
    return time(9, 0) <= current.time() < time(9, 15)


def _default_config():
    today = now_ist().date().isoformat()
    return {
        "profileId": DEFAULT_PROFILE_ID,
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
        config["profileId"] = DEFAULT_PROFILE_ID
    return config


def _write(config):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=CONFIG_FILE.parent,
        prefix="trading_config_",
        delete=False,
    ) as temporary:
        json.dump(config, temporary, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.replace(CONFIG_FILE)


def _ensure_automatic_reset(config, current=None):
    current = current or now_ist()
    today = current.date().isoformat()
    if current.time() >= time(15, 30) and config.get("resetDoneDate") != today:
        config["profileId"] = DEFAULT_PROFILE_ID
        config["selectedDate"] = today
        config["selectedAt"] = current.isoformat()
        config["resetDoneDate"] = today
        _write(config)
    return config


def get_config():
    current = now_ist()
    config = _ensure_automatic_reset(_read(), current)
    profile_id = config["profileId"]
    profile = CAPITAL_PROFILES[profile_id]
    return {
        "profileId": profile_id,
        "profile": profile,
        "options": [
            {"id": key, **value}
            for key, value in CAPITAL_PROFILES.items()
        ],
        "serverTime": current.isoformat(),
        "selectionWindowOpen": selection_window_open(current),
        "selectionWindow": "09:00-09:15 IST",
        "automaticReset": "15:30 IST -> 1 Lot",
        "selectedDate": config.get("selectedDate"),
        "selectedAt": config.get("selectedAt"),
    }


def select_profile(profile_id):
    current = now_ist()
    config = _ensure_automatic_reset(_read(), current)
    if not selection_window_open(current):
        raise PermissionError("Capital selection is available only from 09:00 to 09:15 IST")
    if profile_id not in CAPITAL_PROFILES:
        raise ValueError("Unknown capital profile")
    config.update({
        "profileId": profile_id,
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
