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

from dashboard_data import build_health_snapshot
from dashboard_data import load_env


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