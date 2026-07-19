"""Persistent Upstox Plus market and portfolio stream service.

The official SDK handles V3 protobuf decoding. This process writes compact
JSON caches so cron-launched trading code can consume the latest tick without
opening another websocket or polling every second.
"""

import json
import os
import threading
import time
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
INSTRUMENTS_FILE = DATA_DIR / "upstox_stream_instruments.json"
MARKET_CACHE_FILE = DATA_DIR / "upstox_market_stream.json"
PORTFOLIO_CACHE_FILE = DATA_DIR / "upstox_portfolio_stream.json"
LOG_FILE = DATA_DIR / "upstox_stream_status.json"


def load_env_file():
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def token():
    load_env_file()
    value = os.getenv("UPSTOX_ACCESS_TOKEN") or os.getenv("UPSTOX_ANALYTICS_TOKEN")
    if not value:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN or UPSTOX_ANALYTICS_TOKEN is not set")
    return value


def _atomic_write(path, payload):
    DATA_DIR.mkdir(exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str))
    temporary.replace(path)


def write_stream_instruments(instrument_keys):
    keys = sorted({str(key) for key in instrument_keys if key})[:50]
    _atomic_write(INSTRUMENTS_FILE, {"updated_at": time.time(), "instrument_keys": keys})


def read_stream_instruments():
    try:
        payload = json.loads(INSTRUMENTS_FILE.read_text())
        return list(payload.get("instrument_keys") or [])
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return [
            "NSE_INDEX|Nifty 50",
            "NSE_INDEX|Nifty Bank",
            "NSE_INDEX|India VIX",
        ]


def read_market_cache(instrument_key=None):
    try:
        payload = json.loads(MARKET_CACHE_FILE.read_text())
        feeds = payload.get("feeds") or {}
        return feeds.get(instrument_key, {}) if instrument_key else feeds
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def read_portfolio_cache():
    try:
        return json.loads(PORTFOLIO_CACHE_FILE.read_text())
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _unwrap(value):
    if not isinstance(value, dict):
        return {}
    for key in ("fullFeed", "full_feed", "marketFF", "market_ff", "indexFF", "index_ff", "oc"):
        nested = value.get(key)
        if isinstance(nested, dict):
            return nested
    return value


def _normalise_feed(feed):
    feed = _unwrap(feed)
    ltpc = feed.get("ltpc") or {}
    market_level = feed.get("marketLevel") or feed.get("market_level") or {}
    extended = feed.get("eFeedDetails") or feed.get("e_feed_details") or {}
    greeks = feed.get("optionGreeks") or feed.get("option_greeks") or {}
    quotes = market_level.get("bidAskQuote") or market_level.get("bid_ask_quote") or []
    if isinstance(quotes, dict):
        quotes = [quotes]
    first_quote = quotes[0] if quotes else {}
    return {
        "ltp": ltpc.get("ltp"),
        "close": ltpc.get("cp") or extended.get("cp") or extended.get("close"),
        "ltt": ltpc.get("ltt"),
        "ltq": ltpc.get("ltq"),
        "bid_price": first_quote.get("bp"),
        "ask_price": first_quote.get("ap"),
        "bid_qty": first_quote.get("bq"),
        "ask_qty": first_quote.get("aq"),
        "option_greeks": greeks,
        "oi": extended.get("oi"),
        "prev_oi": extended.get("poi") or extended.get("prev_oi"),
        "change_oi": extended.get("changeOi") or extended.get("change_oi"),
        "volume": extended.get("vtt") or extended.get("tv"),
        "total_buy_quantity": extended.get("tbq") or extended.get("mbpBuy"),
        "total_sell_quantity": extended.get("tsq") or extended.get("mbpSell"),
        "received_at": time.time(),
        "raw": feed,
    }


def _market_message(message):
    if isinstance(message, str):
        try:
            message = json.loads(message)
        except json.JSONDecodeError:
            return
    if not isinstance(message, dict):
        return
    incoming = message.get("feeds") or {}
    if not incoming:
        return
    try:
        existing = json.loads(MARKET_CACHE_FILE.read_text())
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        existing = {"updated_at": None, "feeds": {}}
    feeds = existing.setdefault("feeds", {})
    for key, feed in incoming.items():
        feeds[key] = _normalise_feed(feed)
    existing["updated_at"] = time.time()
    _atomic_write(MARKET_CACHE_FILE, existing)


def _portfolio_message(message):
    if isinstance(message, str):
        try:
            message = json.loads(message)
        except json.JSONDecodeError:
            message = {"raw": message}
    payload = {"received_at": time.time(), "message": message}
    _atomic_write(PORTFOLIO_CACHE_FILE, payload)


def _status(**values):
    try:
        previous = json.loads(LOG_FILE.read_text())
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        previous = {}
    previous.update(values, updated_at=time.time())
    _atomic_write(LOG_FILE, previous)


def run():
    try:
        import upstox_client
    except ImportError as error:
        raise RuntimeError("Install upstox-python-sdk before starting upstox_streams.py") from error

    configuration = upstox_client.Configuration()
    configuration.access_token = token()
    api_client = upstox_client.ApiClient(configuration)

    market_keys = read_stream_instruments()
    market = upstox_client.MarketDataStreamerV3(api_client, market_keys, "full_d30")
    portfolio = upstox_client.PortfolioDataStreamer(
        api_client,
        order_update=True,
        position_update=True,
        holding_update=True,
        gtt_update=True,
    )
    market.auto_reconnect(True, 5, -1)
    portfolio.auto_reconnect(True, 5, -1)

    market.on("open", lambda: _status(market="connected", market_keys=read_stream_instruments()))
    market.on("message", _market_message)
    market.on("error", lambda error: _status(market_error=str(error)))
    market.on("close", lambda: _status(market="closed"))
    portfolio.on("open", lambda: _status(portfolio="connected"))
    portfolio.on("message", _portfolio_message)
    portfolio.on("error", lambda error: _status(portfolio_error=str(error)))
    portfolio.on("close", lambda: _status(portfolio="closed"))

    def refresh_market_subscription():
        previous = set(market_keys)
        env_file = BASE_DIR / ".env"
        previous_env_mtime = env_file.stat().st_mtime if env_file.exists() else None
        while True:
            time.sleep(10)
            try:
                current = set(read_stream_instruments())
                if current != previous:
                    if current - previous:
                        market.subscribe(sorted(current - previous), "full_d30")
                    if previous - current:
                        market.unsubscribe(sorted(previous - current))
                    previous = current
                    _status(market_keys=sorted(current))

                current_env_mtime = env_file.stat().st_mtime if env_file.exists() else None
                if previous_env_mtime is not None and current_env_mtime != previous_env_mtime:
                    # The webhook rotates the daily token in .env. Let systemd
                    # restart this process with the new token cleanly.
                    _status(token_rotation_detected=True)
                    market.disconnect()
                    portfolio.disconnect()
                    return
                previous_env_mtime = current_env_mtime
            except Exception as error:
                _status(subscription_error=str(error))

    threading.Thread(target=refresh_market_subscription, daemon=True).start()

    threading.Thread(target=portfolio.connect, daemon=True).start()
    _status(service="starting", market_keys=market_keys)
    market.connect()


if __name__ == "__main__":
    run()
