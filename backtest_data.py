import gzip
import hashlib
import json
import os
import socket
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests
import urllib3.util.connection as urllib3_cn


urllib3_cn.allowed_gai_family = lambda: socket.AF_INET

IST = "Asia/Kolkata"
UPSTOX_BASE = "https://api.upstox.com"
UPSTOX_INSTRUMENTS_URL = (
    "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
)


def _date(value):
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000).date()
    return pd.Timestamp(value).date()


def _as_ist_index(frame):
    if frame.empty:
        return frame
    frame = frame.copy()
    index = pd.DatetimeIndex(frame.index)
    if index.tz is None:
        index = index.tz_localize(IST)
    else:
        index = index.tz_convert(IST)
    frame.index = index
    return frame.sort_index()[~frame.index.duplicated(keep="last")]


def parse_candles(payload):
    rows = []
    for candle in (payload or {}).get("data", {}).get("candles", []) or []:
        if len(candle) < 5:
            continue
        rows.append(
            {
                "timestamp": pd.to_datetime(candle[0]),
                "open": float(candle[1]),
                "high": float(candle[2]),
                "low": float(candle[3]),
                "close": float(candle[4]),
                "volume": float(candle[5] or 0) if len(candle) > 5 else 0.0,
                "oi": float(candle[6] or 0) if len(candle) > 6 else 0.0,
            }
        )
    if not rows:
        return pd.DataFrame()
    return _as_ist_index(pd.DataFrame(rows).set_index("timestamp"))


class UpstoxBacktestData:
    """Cached Upstox Plus history used by the point-in-time replay."""

    def __init__(self, root, token=None, progress=None, pause_seconds=0.12):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache_only = str(os.getenv("BACKTEST_CACHE_ONLY", "false")).lower() == "true"
        self.cache_from_date = os.getenv("BACKTEST_CACHE_FROM_DATE", "").strip()
        self.cache_to_date = os.getenv("BACKTEST_CACHE_TO_DATE", "").strip()
        self.token = token or os.getenv("UPSTOX_ACCESS_TOKEN") or os.getenv(
            "UPSTOX_ANALYTICS_TOKEN"
        )
        if not self.token and not self.cache_only:
            raise RuntimeError("UPSTOX_ACCESS_TOKEN or UPSTOX_ANALYTICS_TOKEN not set")
        if not self.token:
            self.token = "CACHE_ONLY"
        self.progress = progress or (lambda message: None)
        self.pause_seconds = max(float(pause_seconds), 0)
        self.session = requests.Session()
        self.session.headers.update(
            {"Accept": "application/json", "Authorization": f"Bearer {self.token}"}
        )
        self._instrument_rows = None

    def _key(self, *parts):
        raw = "|".join(str(part) for part in parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    def _json_path(self, namespace, *parts):
        folder = self.root / namespace
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{self._key(*parts)}.json"

    def _csv_path(self, namespace, *parts):
        folder = self.root / namespace
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{self._key(*parts)}.csv.gz"

    def _get(self, url, params=None, attempts=5):
        if self.cache_only:
            raise FileNotFoundError(
                "Backtest cache miss while BACKTEST_CACHE_ONLY=true. "
                "Select a cached date range or explicitly allow downloads."
            )
        error = None
        for attempt in range(attempts):
            response = self.session.get(url, params=params, timeout=45)
            if response.status_code < 300:
                if self.pause_seconds:
                    time.sleep(self.pause_seconds)
                return response.json()
            error = RuntimeError(
                f"Upstox API failed {response.status_code}: {response.text[:500]}"
            )
            if response.status_code not in {429, 500, 502, 503, 504}:
                break
            time.sleep(min(2 ** attempt, 12))
        raise error

    def _cached_json(self, namespace, parts, loader):
        path = self._json_path(namespace, *parts)
        if path.exists():
            return json.loads(path.read_text())
        if self.cache_only:
            raise FileNotFoundError(f"Missing cached {namespace} data: {parts}")
        value = loader()
        path.write_text(json.dumps(value, indent=2, default=str))
        return value

    def _cached_frame(self, namespace, parts, loader):
        path = self._csv_path(namespace, *parts)
        if path.exists():
            frame = pd.read_csv(path, index_col="timestamp", parse_dates=["timestamp"])
            return _as_ist_index(frame)
        if (
            self.cache_only
            and namespace == "candles"
            and len(parts) == 5
            and self.cache_from_date
            and self.cache_to_date
        ):
            instrument_key, interval, requested_start, requested_end, expired = parts
            cache_start = _date(self.cache_from_date)
            cache_end = _date(self.cache_to_date)
            requested_start = _date(requested_start)
            requested_end = _date(requested_end)
            candidate_starts = (cache_start, cache_start - timedelta(days=50))
            if requested_end <= cache_end:
                for candidate_start in candidate_starts:
                    if candidate_start > requested_start:
                        continue
                    candidate = self._csv_path(
                        namespace,
                        instrument_key,
                        interval,
                        candidate_start,
                        cache_end,
                        bool(expired),
                    )
                    if not candidate.exists():
                        continue
                    frame = pd.read_csv(
                        candidate,
                        index_col="timestamp",
                        parse_dates=["timestamp"],
                    )
                    frame = _as_ist_index(frame)
                    dates = frame.index.date
                    return frame[(dates >= requested_start) & (dates <= requested_end)]
        if self.cache_only:
            raise FileNotFoundError(
                f"Missing cached {namespace} data for requested range: {parts}"
            )
        frame = _as_ist_index(loader())
        if not frame.empty:
            frame.to_csv(path, index_label="timestamp", compression="gzip")
        return frame

    def validate_access(self):
        payload = self._get(f"{UPSTOX_BASE}/v2/user/profile")
        return bool((payload or {}).get("data"))

    def _payload_rows(self, payload):
        data = (payload or {}).get("data", [])
        if isinstance(data, dict):
            for key in ("contracts", "expiries", "data"):
                if key in data:
                    data = data[key]
                    break
        return data or []

    def get_expiries(self, underlying_key):
        def load_expired():
            payload = self._get(
                f"{UPSTOX_BASE}/v2/expired-instruments/expiries",
                {"instrument_key": underlying_key},
            )
            return [str(item) for item in self._payload_rows(payload)]

        expired = self._cached_json("metadata", ("expiries", underlying_key), load_expired)
        active = []
        try:
            payload = self._get(
                f"{UPSTOX_BASE}/v2/option/contract",
                {"instrument_key": underlying_key},
            )
            active = [row.get("expiry") for row in self._payload_rows(payload)]
        except Exception:
            active = []
        return sorted({_date(value).isoformat() for value in [*expired, *active] if value})

    def get_option_contracts(self, underlying_key, expiry):
        expiry = _date(expiry)

        def loader():
            if expiry < datetime.now().date():
                url = f"{UPSTOX_BASE}/v2/expired-instruments/option/contract"
            else:
                url = f"{UPSTOX_BASE}/v2/option/contract"
            payload = self._get(
                url,
                {"instrument_key": underlying_key, "expiry_date": expiry.isoformat()},
            )
            rows = self._payload_rows(payload)
            return [dict(row) for row in rows if row]

        return self._cached_json(
            "metadata", ("option-contracts", underlying_key, expiry), loader
        )

    def get_future_contracts(self, underlying_key, expiry):
        expiry = _date(expiry)

        def loader():
            payload = self._get(
                f"{UPSTOX_BASE}/v2/expired-instruments/future/contract",
                {"instrument_key": underlying_key, "expiry_date": expiry.isoformat()},
            )
            return [dict(row) for row in self._payload_rows(payload) if row]

        if expiry < datetime.now().date():
            return self._cached_json(
                "metadata", ("future-contracts", underlying_key, expiry), loader
            )
        return [
            row
            for row in self.active_instruments()
            if row.get("underlying_key") == underlying_key
            and str(row.get("instrument_type", "")).upper() in {"FUT", "FUTSTK", "FUTIDX"}
            and row.get("expiry")
            and _date(row["expiry"]) == expiry
        ]

    def active_instruments(self):
        if self._instrument_rows is not None:
            return self._instrument_rows
        path = self.root / "complete.json.gz"
        if not path.exists() or (
            not self.cache_only and (time.time() - path.stat().st_mtime) > 86400
        ):
            if self.cache_only:
                raise FileNotFoundError("Cached Upstox instrument master is missing")
            self.progress("Downloading the current Upstox instrument master")
            response = self.session.get(UPSTOX_INSTRUMENTS_URL, timeout=90)
            response.raise_for_status()
            path.write_bytes(response.content)
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            self._instrument_rows = json.load(handle)
        return self._instrument_rows

    def _contract_key(self, contract):
        return (
            contract.get("expired_instrument_key")
            or contract.get("instrument_key")
            or contract.get("instrument_token")
        )

    def candles(self, contract_or_key, interval, from_date, to_date, expired=None):
        contract = contract_or_key if isinstance(contract_or_key, dict) else {}
        instrument_key = self._contract_key(contract) if contract else str(contract_or_key)
        if not instrument_key:
            return pd.DataFrame()
        contract_expiry = contract.get("expiry")
        if expired is None:
            expired = bool(contract.get("expired_instrument_key")) or (
                bool(contract_expiry) and _date(contract_expiry) < datetime.now().date()
            )
        start = _date(from_date)
        end = _date(to_date)
        parts = (instrument_key, interval, start, end, bool(expired))

        def loader():
            frames = []
            cursor = start
            while cursor <= end:
                chunk_end = min(cursor + timedelta(days=27), end)
                self.progress(
                    f"Fetching {interval} candles for {contract.get('trading_symbol', instrument_key)} "
                    f"({cursor} to {chunk_end})"
                )
                encoded = quote(instrument_key, safe="")
                if expired:
                    url = (
                        f"{UPSTOX_BASE}/v2/expired-instruments/historical-candle/"
                        f"{encoded}/{interval}/{chunk_end}/{cursor}"
                    )
                else:
                    unit, value = self._normal_interval(interval)
                    url = (
                        f"{UPSTOX_BASE}/v3/historical-candle/{encoded}/"
                        f"{unit}/{value}/{chunk_end}/{cursor}"
                    )
                frames.append(parse_candles(self._get(url)))
                cursor = chunk_end + timedelta(days=1)
            valid = [frame for frame in frames if not frame.empty]
            return pd.concat(valid).sort_index() if valid else pd.DataFrame()

        return self._cached_frame("candles", parts, loader)

    @staticmethod
    def _normal_interval(interval):
        text = str(interval).lower()
        if text.endswith("minute"):
            return "minutes", int(text.removesuffix("minute"))
        if text.endswith("hour"):
            return "hours", int(text.removesuffix("hour"))
        if text in {"day", "1day"}:
            return "days", 1
        raise ValueError(f"Unsupported candle interval: {interval}")
