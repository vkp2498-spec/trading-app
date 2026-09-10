"""Set/rotate the OpenAI credential through hidden terminal input, never argv."""
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from safe_storage import file_lock
from scripts.sync_core_env import write_atomic


def main():
    if not sys.stdin.isatty():
        raise SystemExit("Use an interactive terminal: credential input must be hidden")
    key = getpass.getpass("OpenAI API key (hidden): ").strip()
    if not key.startswith("sk-") or len(key) < 30 or any(c.isspace() for c in key):
        raise SystemExit("Invalid key format; no changes made")
    path = Path(__file__).resolve().parents[1] / ".env"
    with file_lock(path.with_name(".env.lock")):
        lines = path.read_text().splitlines()
        lines = [line for line in lines if line.strip().split("=", 1)[0].strip() != "OPENAI_API_KEY"]
        lines.append("OPENAI_API_KEY=" + key)
        write_atomic(path, "\n".join(lines) + "\n")
        os.chmod(path, 0o600)
    print("OpenAI credential saved privately. Value not displayed.")


if __name__ == "__main__":
    main()
