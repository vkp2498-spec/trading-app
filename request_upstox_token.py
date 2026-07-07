import os
import requests
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"


def load_env():
    if not ENV_FILE.exists():
        return

    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main():
    load_env()

    client_id = os.getenv("UPSTOX_API_KEY")
    client_secret = os.getenv("UPSTOX_API_SECRET")

    if not client_id or not client_secret:
        raise RuntimeError("Set UPSTOX_API_KEY and UPSTOX_API_SECRET in .env")

    url = f"https://api.upstox.com/v3/login/auth/token/request/{client_id}"

    headers = {
        "accept": "application/json",
        "Content-Type": "application/json",
    }

    payload = {
        "client_secret": client_secret,
    }

    response = requests.post(url, headers=headers, json=payload, timeout=30)

    print("Status:", response.status_code)
    print(response.text[:1000])


if __name__ == "__main__":
    main()