from datetime import datetime
from zoneinfo import ZoneInfo
import hmac
import os

from fastapi import Depends
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import status
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.security import HTTPBearer
from pydantic import BaseModel

from apns_push import apns_is_configured
from apns_push import register_device
from apns_push import registered_device_count
from apns_push import send_test_notification
from apns_push import unregister_device
from dashboard_data import build_health_snapshot
from dashboard_data import build_trade_performance
from dashboard_data import load_env
from trading_config import get_config
from trading_config import select_profile
from stock_screener import get_screener
from stock_screener import run_screener
from stock_screener import save_invested
from stock_screener import start_screener
from mobile_orders import buy_delivery
from mobile_orders import exit_holding
from mobile_orders import holdings
from mobile_orders import live_orders_enabled


IST = ZoneInfo("Asia/Kolkata")

load_env()

MOBILE_API_TOKEN = os.getenv(
    "MOBILE_API_TOKEN",
    "",
).strip()

TRADING_PROFILE = os.getenv(
    "TRADING_PROFILE",
    "Vamsi",
).strip()

if len(MOBILE_API_TOKEN) < 32:
    raise RuntimeError(
        "MOBILE_API_TOKEN is missing or too short. "
        "Add a random token of at least 32 characters "
        "to the .env file."
    )


app = FastAPI(
    title="HK Trading Mobile API",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

bearer_scheme = HTTPBearer(
    auto_error=False
)


class NotificationDevice(BaseModel):
    deviceToken: str


class CapitalProfileSelection(BaseModel):
    profileId: str


class InvestedStock(BaseModel):
    symbol: str
    instrumentKey: str
    entryPrice: float
    quantity: int = 1


class LiveBuyRequest(BaseModel):
    symbol: str
    instrumentKey: str
    maximumAmount: float = 100_000
    confirmation: bool = False


class LiveExitRequest(BaseModel):
    symbol: str
    instrumentKey: str
    confirmation: bool = False


def require_mobile_token(
    credentials: (
        HTTPAuthorizationCredentials | None
    ) = Depends(bearer_scheme),
):
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={
                "WWW-Authenticate": "Bearer"
            },
        )

    if credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication scheme",
            headers={
                "WWW-Authenticate": "Bearer"
            },
        )

    token_matches = hmac.compare_digest(
        credentials.credentials,
        MOBILE_API_TOKEN,
    )

    if not token_matches:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={
                "WWW-Authenticate": "Bearer"
            },
        )


@app.get("/health")
def health():
    """
    Public health check.

    It does not return trading data or configuration.
    """
    return {
        "status": "ok",
        "service": "HK Trading Mobile API",
        "profile": TRADING_PROFILE,
        "serverTime": datetime.now(
            IST
        ).isoformat(),
    }


@app.get(
    "/api/v1/dashboard",
    dependencies=[
        Depends(require_mobile_token)
    ],
)
def dashboard():
    """
    Authenticated, read-only dashboard snapshot.
    """
    try:
        snapshot = build_health_snapshot()

        snapshot["profile"] = TRADING_PROFILE
        snapshot["serverTime"] = datetime.now(
            IST
        ).isoformat()

        # The local filesystem path is useful while
        # debugging but should not be returned to the app.
        snapshot.pop(
            "baseDirectory",
            None,
        )

        # The app doesn't need to know whether specific
        # secret environment variables exist.
        snapshot.pop(
            "configuration",
            None,
        )

        return snapshot

    except Exception:
        # Do not expose internal paths, tokens or broker
        # response details to a remote client.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Dashboard data is temporarily unavailable",
        )


@app.get(
    "/api/v1/watch-summary",
    dependencies=[Depends(require_mobile_token)],
)
def watch_summary():
    """Return only the two values needed by the Apple Watch app."""
    performance = build_trade_performance()
    recent_trades = performance.get("recentTrades", [])
    latest_trade = recent_trades[0] if recent_trades else None

    return {
        "profile": TRADING_PROFILE,
        "serverTime": datetime.now(IST).isoformat(),
        "latestTrade": latest_trade,
        "todayPnL": performance.get("today", {}).get("closedPnL", 0.0),
    }


@app.get(
    "/api/v1/trading-config",
    dependencies=[Depends(require_mobile_token)],
)
def trading_config():
    """Return the active capital profile and the current selection window."""
    return get_config()


@app.post(
    "/api/v1/trading-config",
    dependencies=[Depends(require_mobile_token)],
)
def update_trading_config(selection: CapitalProfileSelection):
    """Select the next trading profile during the 09:00-09:15 IST window."""
    try:
        return select_profile(selection.profileId)
    except PermissionError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(error),
        ) from error
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error


@app.get("/api/v1/screener", dependencies=[Depends(require_mobile_token)])
def stock_screener():
    """Return the latest cached equity/ETF research scan."""
    return get_screener()


@app.post("/api/v1/screener/run", dependencies=[Depends(require_mobile_token)])
def run_stock_screener():
    """Run a read-only NSE equity/ETF scan and cache the top five candidates."""
    try:
        return run_screener()
    except Exception:
        raise HTTPException(status_code=503, detail="Stock screener is temporarily unavailable")


@app.post("/api/v1/screener/run-background", dependencies=[Depends(require_mobile_token)])
def run_stock_screener_background():
    """Start the long scan and return immediately so mobile gateways do not time out."""
    try:
        return start_screener()
    except Exception:
        raise HTTPException(status_code=503, detail="Stock screener could not be started")


@app.post("/api/v1/screener/invested", dependencies=[Depends(require_mobile_token)])
def update_invested_stock(item: InvestedStock):
    current = get_screener().get("invested", [])
    current = [row for row in current if str(row.get("symbol", "")).upper() != item.symbol.upper()]
    current.append(item.model_dump())
    return save_invested(current)


@app.delete("/api/v1/screener/invested/{symbol}", dependencies=[Depends(require_mobile_token)])
def remove_invested_stock(symbol: str):
    current = [row for row in get_screener().get("invested", []) if str(row.get("symbol", "")).upper() != symbol.upper()]
    return save_invested(current)


@app.get("/api/v1/portfolio/holdings", dependencies=[Depends(require_mobile_token)])
def portfolio_holdings():
    try:
        return {"liveOrdersEnabled": live_orders_enabled(), "holdings": holdings()}
    except Exception:
        raise HTTPException(status_code=503, detail="Holdings are temporarily unavailable")


@app.post("/api/v1/orders/buy", dependencies=[Depends(require_mobile_token)])
def live_buy_order(request: LiveBuyRequest):
    if not request.confirmation:
        raise HTTPException(status_code=400, detail="Explicit order confirmation is required")
    try:
        return buy_delivery(request.instrumentKey, request.symbol, request.maximumAmount)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/v1/orders/exit", dependencies=[Depends(require_mobile_token)])
def live_exit_order(request: LiveExitRequest):
    if not request.confirmation:
        raise HTTPException(status_code=400, detail="Explicit exit confirmation is required")
    try:
        return exit_holding(request.instrumentKey, request.symbol)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post(
    "/api/v1/notifications/devices",
    dependencies=[Depends(require_mobile_token)],
    status_code=status.HTTP_204_NO_CONTENT,
)
def add_notification_device(device: NotificationDevice):
    try:
        register_device(device.deviceToken)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error


@app.delete(
    "/api/v1/notifications/devices",
    dependencies=[Depends(require_mobile_token)],
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_notification_device(device: NotificationDevice):
    try:
        unregister_device(device.deviceToken)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error


@app.get(
    "/api/v1/notifications/status",
    dependencies=[Depends(require_mobile_token)],
)
def notification_status():
    return {
        "profile": TRADING_PROFILE,
        "configured": apns_is_configured(),
        "registeredDevices": registered_device_count(),
    }


@app.post(
    "/api/v1/notifications/test",
    dependencies=[Depends(require_mobile_token)],
)
def test_notification():
    if not apns_is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="APNs is not configured",
        )

    return send_test_notification()
