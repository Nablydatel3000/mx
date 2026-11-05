"""Offline CSV replay for technical testing; no broker token required."""
from __future__ import annotations

import argparse
import csv
from datetime import date
from pathlib import Path

from .paper import execute_paper
from .strategy import DailyClose, crossover


def read_daily_closes(path: Path) -> list[DailyClose]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        sample = stream.read(2048)
        stream.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=";,")
        reader = csv.DictReader(stream, dialect=dialect)
        if not {"day", "close"}.issubset(reader.fieldnames or []):
            raise ValueError("CSV requires columns: day,close")
        rows = [
            DailyClose(date.fromisoformat(r["day"]), float(r["close"].replace(",", ".")))
            for r in reader
        ]
    if not rows:
        raise ValueError("Empty CSV")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="GLDRUBF offline PAPER replay")
    parser.add_argument("--csv", required=True, type=Path, help="Daily closes CSV: day,close")
    parser.add_argument("--db", default="paper.sqlite3", type=Path)
    args = parser.parse_args()
    bars = read_daily_closes(args.csv)
    if len(bars) < 51:
        raise ValueError("Need at least 51 completed daily closes")
    for i in range(51, len(bars) + 1):
        signal = crossover(bars[:i])
        result = execute_paper(args.db, signal.day.isoformat(), signal.target, bars[i - 1].close)
        if result.executed:
            print(f"{result.day}: position {result.position_before} -> {result.position_after}")
    print("Offline replay finished. No broker orders sent.")


if __name__ == "__main__":
    main()
