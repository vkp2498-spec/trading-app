"""Apple Push Notification delivery for closed-trade alerts.

This module is deliberately independent from order placement. It is called only
after a closed trade has already been written to the trade journal.
"""

from __future__ import annotations

from datetime import datetime, timezone
import csv
import json
import os
from pathlib import Path
import re
import threading
from typing import Any

import httpx
import jwt


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DEVICE_FILE = DATA_DIR / "apns_devices.json"
TRADE_HISTORY_FILE = DATA_DIR / "trade_history.csv"
DEVICE_TOKEN_PATTERN = re.compile(r"^[0-9a-fA-F]{64,200}$")
_device_lock = threading.Lock()


class APNSConfigurationError(RuntimeError):
    """Raised when APNs credentials are incomplete."""


def _read_devices() -> list[dict[str, str]]:
    if not DEVICE_FILE.exists():
        return []

    try:
        value = json.loads(DEVICE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return []

    if not isinstance(value, list):
        return []

    return [item for item in value if isinstance(item, dict)]


def _write_devices(devices: list[dict[str, str]]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    temporary_file = DEVICE_FILE.with_suffix(".tmp")
    temporary_file.write_text(json.dumps(devices, indent=2))
    temporary_file.replace(DEVICE_FILE)


def validate_device_token(device_token: str) -> str:
    normalized = str(device_token).strip().replace(" ", "")

    if not DEVICE_TOKEN_PATTERN.fullmatch(normalized):
        raise ValueError("Invalid APNs device token")

    return normalized.lower()


def register_device(device_token: str) -> None:
    normalized = validate_device_token(device_token)

    with _device_lock:
        devices = _read_devices()
        devices = [
            item
            for item in devices
            if item.get("deviceToken") != normalized
        ]
        devices.append(
            {
                "deviceToken": normalized,
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            }
        )
        _write_devices(devices)


def unregister_device(device_token: str) -> None:
    normalized = validate_device_token(device_token)

    with _device_lock:
        devices = [
            item
            for item in _read_devices()
            if item.get("deviceToken") != normalized
        ]
        _write_devices(devices)


def registered_device_count() -> int:
    with _device_lock:
        return len(_read_devices())


def apns_is_configured() -> bool:
    required = [
        "APNS_KEY_ID",
        "APNS_TEAM_ID",
        "APNS_AUTH_KEY_PATH",
        "APNS_TOPIC",
    ]
    return all(os.getenv(name, "").strip() for name in required)


def _configuration() -> dict[str, str]:
    configuration = {
        "key_id": os.getenv("APNS_KEY_ID", "").strip(),
        "team_id": os.getenv("APNS_TEAM_ID", "").strip(),
        "key_path": os.getenv("APNS_AUTH_KEY_PATH", "").strip(),
        "topic": os.getenv("APNS_TOPIC", "").strip(),
    }

    missing = [key for key, value in configuration.items() if not value]
    if missing:
        raise APNSConfigurationError(
            "Missing APNs configuration: " + ", ".join(missing)
        )

    return configuration


def _provider_token(configuration: dict[str, str]) -> str:
    key_path = Path(configuration["key_path"])

    try:
        private_key = key_path.read_text()
    except OSError as error:
        raise APNSConfigurationError(
            "Unable to read the APNs authentication key"
        ) from error

    now = int(datetime.now(timezone.utc).timestamp())
    return jwt.encode(
        {"iss": configuration["team_id"], "iat": now},
        private_key,
        algorithm="ES256",
        headers={"kid": configuration["key_id"]},
    )


def _money(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0

    sign = "+" if number > 0 else "-" if number < 0 else ""
    return f"{sign}₹{abs(number):,.2f}"


def _today_pnl(trade_date: str) -> float:
    if not TRADE_HISTORY_FILE.exists():
        return 0.0

    total = 0.0
    try:
        with TRADE_HISTORY_FILE.open("r", newline="", errors="ignore") as file:
            for row in csv.DictReader(file):
                if str(row.get("trade_date", "")) != str(trade_date):
                    continue
                try:
                    total += float(row.get("gross_pnl") or 0)
                except (TypeError, ValueError):
                    continue
    except (OSError, csv.Error):
        return 0.0

    return round(total, 2)


def _send_payload(payload: dict[str, Any]) -> dict[str, int]:
    configuration = _configuration()
    provider_token = _provider_token(configuration)
    use_sandbox = os.getenv("APNS_USE_SANDBOX", "false").lower() == "true"
    host = (
        "https://api.sandbox.push.apple.com"
        if use_sandbox
        else "https://api.push.apple.com"
    )

    with _device_lock:
        devices = _read_devices()

    invalid_tokens: set[str] = set()
    sent = 0
    failed = 0

    headers = {
        "authorization": f"bearer {provider_token}",
        "apns-topic": configuration["topic"],
        "apns-push-type": "alert",
        "apns-priority": "10",
    }

    with httpx.Client(http2=True, timeout=15) as client:
        for item in devices:
            device_token = item.get("deviceToken", "")
            if not device_token:
                continue

            try:
                response = client.post(
                    f"{host}/3/device/{device_token}",
                    headers=headers,
                    json=payload,
                )
            except httpx.HTTPError:
                failed += 1
                continue

            if response.status_code == 200:
                sent += 1
                continue

            failed += 1
            reason = ""
            try:
                reason = response.json().get("reason", "")
            except (ValueError, AttributeError):
                pass

            if reason in {"BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered"}:
                invalid_tokens.add(device_token)

    if invalid_tokens:
        with _device_lock:
            remaining = [
                item
                for item in _read_devices()
                if item.get("deviceToken") not in invalid_tokens
            ]
            _write_devices(remaining)

    return {"sent": sent, "failed": failed}


def send_trade_closed_notification(journal_row: dict[str, Any]) -> dict[str, int]:
    if not apns_is_configured() or registered_device_count() == 0:
        return {"sent": 0, "failed": 0}

    profile = os.getenv("TRADING_PROFILE", "Trading").strip() or "Trading"
    symbol = str(journal_row.get("symbol") or "Trade")
    trade_pnl = float(journal_row.get("gross_pnl") or 0)
    day_pnl = _today_pnl(str(journal_row.get("trade_date") or ""))
    exit_time = str(journal_row.get("exit_time") or "")

    payload = {
        "aps": {
            "alert": {
                "title": f"{profile} • {symbol} closed",
                "body": f"Trade {_money(trade_pnl)}  |  Today {_money(day_pnl)}",
            },
            "sound": "default",
            "content-available": 1,
            "thread-id": f"hk-trading-{profile.lower()}",
        },
        "profile": profile,
        "symbol": symbol,
        "tradePnL": trade_pnl,
        "dayPnL": day_pnl,
        "exitTime": exit_time,
    }
    return _send_payload(payload)


def send_test_notification() -> dict[str, int]:
    profile = os.getenv("TRADING_PROFILE", "Trading").strip() or "Trading"
    payload = {
        "aps": {
            "alert": {
                "title": f"{profile} • Notifications ready",
                "body": "Trade +₹1,250.00  |  Today +₹4,860.00",
            },
            "sound": "default",
            "content-available": 1,
            "thread-id": f"hk-trading-{profile.lower()}",
        },
        "profile": profile,
        "isTest": True,
    }
    return _send_payload(payload)
