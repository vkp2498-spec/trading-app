"""Small durable-file helpers shared by the live trading processes."""

from __future__ import annotations

import csv
import json
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows development fallback
    fcntl = None


@contextmanager
def file_lock(path):
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_write_json(path, payload, *, sort_keys=False):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_path = destination.with_suffix(destination.suffix + ".lock")
    with file_lock(lock_path):
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=destination.name + ".",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            json.dump(payload, temporary, indent=2, sort_keys=sort_keys, default=str)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.replace(destination)


def locked_append_csv(path, fieldnames, row):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_path = destination.with_suffix(destination.suffix + ".lock")
    with file_lock(lock_path):
        exists = destination.exists() and destination.stat().st_size > 0
        with destination.open("a", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            if not exists:
                writer.writeheader()
            writer.writerow({field: row.get(field, "") for field in fieldnames})
            handle.flush()
            os.fsync(handle.fileno())
