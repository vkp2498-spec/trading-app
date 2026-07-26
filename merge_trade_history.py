from __future__ import annotations

import argparse
import csv
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from trade_history_schema import COLUMNS


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TARGET = BASE_DIR / "data" / "trade_history.csv"
SYNC_REASON = "UPSTOX_SYNC_ADJUSTMENT"


@dataclass
class MergeResult:
    rows: list[dict]
    source_counts: dict[str, int]
    duplicate_count: int
    enriched_field_count: int
    ignored_row_count: int


def clean(value) -> str:
    return str(value or "").strip()


def normalized_number(value) -> str:
    text = clean(value)
    if not text:
        return ""
    try:
        return f"{float(text):.6f}".rstrip("0").rstrip(".")
    except ValueError:
        return text


def normalized_symbol(row: dict) -> str:
    text = " ".join(
        clean(row.get(field)).upper()
        for field in ("underlying_symbol", "symbol", "trading_symbol")
    )
    if "BANKNIFTY" in text or "BANK NIFTY" in text:
        return "BANKNIFTY"
    if "NIFTY" in text:
        return "NIFTY"
    return clean(row.get("underlying_symbol") or row.get("symbol")).upper()


def trade_identity(row: dict) -> tuple[str, ...]:
    """Identify the same trade across overlapping full-file snapshots."""
    exit_time = clean(row.get("exit_time"))
    entry_time = clean(row.get("entry_time"))
    return (
        clean(row.get("trade_date")),
        normalized_symbol(row),
        clean(row.get("instrument_class")).upper(),
        clean(row.get("trading_symbol")).upper(),
        entry_time,
        exit_time,
        normalized_number(row.get("quantity")),
    )


def canonical_row(row: dict) -> dict:
    return {column: clean(row.get(column)) for column in COLUMNS}


def is_relevant_index_trade(row: dict) -> bool:
    return (
        normalized_symbol(row) in {"NIFTY", "BANKNIFTY"}
        and clean(row.get("instrument_class") or "INDEX_OPTION").upper() == "INDEX_OPTION"
        and clean(row.get("exit_reason")).upper() != SYNC_REASON
        and not clean(row.get("trading_symbol")).upper().startswith("UPSTOX SYNC")
    )


def read_rows(path: Path) -> list[dict]:
    with path.open("r", newline="", errors="ignore") as handle:
        return [canonical_row(row) for row in csv.DictReader(handle) if row]


def discover_sources(target: Path) -> list[Path]:
    backups = sorted(
        path
        for path in target.parent.glob("trade_history.*.bak")
        if path.resolve() != target.resolve()
    )
    return [*backups, target] if target.exists() else backups


def merge_sources(sources: list[Path]) -> MergeResult:
    merged: dict[tuple[str, ...], dict] = {}
    source_counts = {}
    duplicates = 0
    enriched_fields = 0
    ignored_rows = 0

    # Sources are oldest-to-newest, with the current CSV last. Newer values win,
    # while blank fields are enriched from any older copy of the same trade.
    for source in sources:
        rows = read_rows(source)
        source_counts[source.name] = len(rows)
        for row in rows:
            if not is_relevant_index_trade(row):
                ignored_rows += 1
                continue
            identity = trade_identity(row)
            previous = merged.get(identity)
            if previous is None:
                merged[identity] = row
                continue

            duplicates += 1
            combined = dict(row)
            for column in COLUMNS:
                if not combined.get(column) and previous.get(column):
                    combined[column] = previous[column]
                    enriched_fields += 1
            merged[identity] = combined

    rows = sorted(merged.values(), key=trade_sort_key)
    return MergeResult(rows, source_counts, duplicates, enriched_fields, ignored_rows)


def trade_sort_key(row: dict) -> tuple[str, ...]:
    return (
        clean(row.get("trade_date")),
        clean(row.get("exit_time")) or clean(row.get("entry_time")),
        normalized_symbol(row),
        clean(row.get("trading_symbol")),
    )


def safe_float(value) -> float:
    try:
        return float(clean(value))
    except ValueError:
        return 0.0


def index_audit(rows: list[dict]) -> list[dict]:
    audit = []
    for symbol in ("NIFTY", "BANKNIFTY"):
        matching = [
            row
            for row in rows
            if normalized_symbol(row) == symbol
            and clean(row.get("instrument_class") or "INDEX_OPTION").upper() == "INDEX_OPTION"
            and clean(row.get("exit_reason")).upper() != SYNC_REASON
        ]
        scored = sum(1 for row in matching if clean(row.get("score")))
        audit.append(
            {
                "symbol": symbol,
                "trades": len(matching),
                "pnl": round(sum(safe_float(row.get("gross_pnl")) for row in matching), 2),
                "scored": scored,
                "missing_scores": len(matching) - scored,
            }
        )
    return audit


def print_report(result: MergeResult, sources: list[Path]) -> None:
    print("Sources (oldest to newest):")
    for source in sources:
        print(f"  {source.name}: {result.source_counts.get(source.name, 0)} rows")
    print()
    print(f"Unique merged rows: {len(result.rows)}")
    print(f"Duplicate snapshot rows collapsed: {result.duplicate_count}")
    print(f"Blank fields enriched from older copies: {result.enriched_field_count}")
    print(f"Stock/manual rows ignored across snapshots: {result.ignored_row_count}")

    dates = sorted({clean(row.get("trade_date")) for row in result.rows if clean(row.get("trade_date"))})
    if dates:
        print(f"Date range: {dates[0]} to {dates[-1]}")

    print("\nIndex-option audit (Upstox sync adjustments excluded):")
    for item in index_audit(result.rows):
        print(
            f"  {item['symbol']:10s} trades={item['trades']:4d} "
            f"P&L={item['pnl']:12.2f} scored={item['scored']:4d} "
            f"missing_score={item['missing_scores']:4d}"
        )


def apply_merge(target: Path, result: MergeResult, original_mtime_ns: int | None) -> Path:
    if not result.rows:
        raise RuntimeError("Refusing to replace trade history with zero rows")
    if target.exists() and target.stat().st_mtime_ns != original_mtime_ns:
        raise RuntimeError("trade_history.csv changed during the merge; run again when trading is idle")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safety_backup = target.with_name(f"trade_history.pre_merge.{timestamp}.bak")
    if target.exists():
        shutil.copy2(target, safety_backup)

    temporary = target.with_name(f".{target.name}.merge.tmp")
    with temporary.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(result.rows)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    return safety_backup


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Safely merge overlapping trade_history backups into the current CSV."
    )
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument(
        "--source",
        action="append",
        type=Path,
        help="Explicit source file. Repeat for multiple files; current target is applied last.",
    )
    parser.add_argument("--apply", action="store_true", help="Write the validated merge to the target CSV.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    target = args.target.resolve()
    explicit_sources = [path.resolve() for path in (args.source or [])]
    if explicit_sources:
        sources = [path for path in explicit_sources if path != target]
        if target.exists():
            sources.append(target)
    else:
        sources = discover_sources(target)

    missing = [path for path in sources if not path.exists()]
    if missing:
        raise RuntimeError("Missing source files: " + ", ".join(str(path) for path in missing))
    if not sources:
        raise RuntimeError("No trade-history CSV or backup files were found")

    original_mtime_ns = target.stat().st_mtime_ns if target.exists() else None
    result = merge_sources(sources)
    print_report(result, sources)

    if not args.apply:
        print("\nDry run only. Review the audit, then rerun with --apply.")
        return

    safety_backup = apply_merge(target, result, original_mtime_ns)
    print(f"\nMerged history written to: {target}")
    print(f"Safety backup created at: {safety_backup}")


if __name__ == "__main__":
    main()
