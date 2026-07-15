import json
import os
import socket
import stat
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

app = FastAPI()
TOKEN_KEY = "UPSTOX_ACCESS_TOKEN"
SENSITIVE_PAYLOAD_KEYS = {"access_token", "token"}


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


def redact_payload(obj):
    if isinstance(obj, dict):
        return {
            key: "***hidden***"
            if str(key).lower() in SENSITIVE_PAYLOAD_KEYS
            else redact_payload(value)
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [redact_payload(item) for item in obj]
    return obj


def update_env_token(token):
    token = str(token or "").strip()
    if not token:
        raise ValueError("Access token is empty")

    lines = []
    original_stat = None
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text().splitlines()
        original_stat = ENV_FILE.stat()

    updated = []
    inserted = False
    removed_duplicates = 0
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else None
        if key != TOKEN_KEY:
            updated.append(line)
            continue

        if not inserted:
            updated.append(f"{TOKEN_KEY}={token}")
            inserted = True
        else:
            removed_duplicates += 1

    if not inserted:
        updated.append(f"{TOKEN_KEY}={token}")

    temporary_file = ENV_FILE.with_name(f".{ENV_FILE.name}.tmp")
    temporary_file.write_text("\n".join(updated) + "\n")

    if original_stat is not None:
        os.chmod(temporary_file, stat.S_IMODE(original_stat.st_mode))
        try:
            os.chown(temporary_file, original_stat.st_uid, original_stat.st_gid)
        except PermissionError:
            pass
    else:
        os.chmod(temporary_file, 0o600)

    temporary_file.replace(ENV_FILE)
    os.environ[TOKEN_KEY] = token
    return {
        "env_file": str(ENV_FILE),
        "removed_duplicate_token_lines": removed_duplicates,
    }


@app.get("/health")
def health():
    load_env_file()
    return {
        "status": "ok",
        "instance": os.getenv("INSTANCE_NAME") or socket.gethostname(),
    }


@app.post("/upstox-token-webhook")
async def upstox_token_webhook(request: Request):
    load_env_file()

    expected_secret = os.getenv("WEBHOOK_SECRET")
    provided_secret = request.query_params.get("secret") or request.headers.get("X-Webhook-Secret")
    if expected_secret and provided_secret and provided_secret != expected_secret:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    payload = await request.json()
    safe_payload = redact_payload(payload)
    (LOG_DIR / "upstox_webhook_last.json").write_text(json.dumps(safe_payload, indent=2))

    token = find_access_token(payload)
    if not token:
        raise HTTPException(status_code=400, detail="No access token found in webhook payload")

    update_result = update_env_token(token)
    status = {
        "status": "updated",
        "received_at": datetime.now(timezone.utc).isoformat(),
        "instance": os.getenv("INSTANCE_NAME") or socket.gethostname(),
        "token_length": len(str(token)),
        **update_result,
    }
    (LOG_DIR / "upstox_webhook_status.json").write_text(
        json.dumps(status, indent=2, sort_keys=True)
    )

    return {
        "status": "ok",
        "message": "UPSTOX_ACCESS_TOKEN updated",
        "instance": status["instance"],
    }
