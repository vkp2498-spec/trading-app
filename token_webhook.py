import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

app = FastAPI()


def load_env_file():
    if not ENV_FILE.exists():
        return

    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def find_access_token(obj):
    if isinstance(obj, dict):
        for key in ["access_token", "token"]:
            if obj.get(key):
                return obj[key]

        for value in obj.values():
            token = find_access_token(value)
            if token:
                return token

    if isinstance(obj, list):
        for item in obj:
            token = find_access_token(item)
            if token:
                return token

    return None


def update_env_token(token):
    lines = []
    found = False

    if ENV_FILE.exists():
        lines = ENV_FILE.read_text().splitlines()

    updated = []
    for line in lines:
        if line.startswith("UPSTOX_ACCESS_TOKEN="):
            updated.append(f"UPSTOX_ACCESS_TOKEN={token}")
            found = True
        else:
            updated.append(line)

    if not found:
        updated.append(f"UPSTOX_ACCESS_TOKEN={token}")

    ENV_FILE.write_text("\n".join(updated) + "\n")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/upstox-token-webhook")
async def upstox_token_webhook(request: Request):
    load_env_file()

    expected_secret = os.getenv("WEBHOOK_SECRET")
    provided_secret = request.query_params.get("secret") or request.headers.get("X-Webhook-Secret")

    if expected_secret and provided_secret != expected_secret:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    payload = await request.json()

    safe_payload = payload
    if isinstance(payload, dict):
        safe_payload = dict(payload)
        if "access_token" in safe_payload:
            safe_payload["access_token"] = "***hidden***"
        if "token" in safe_payload:
            safe_payload["token"] = "***hidden***"

    (LOG_DIR / "upstox_webhook_last.json").write_text(json.dumps(safe_payload, indent=2))

    token = find_access_token(payload)
    if not token:
        raise HTTPException(status_code=400, detail="No access token found in webhook payload")

    update_env_token(token)

    return {"status": "ok", "message": "UPSTOX_ACCESS_TOKEN updated"}