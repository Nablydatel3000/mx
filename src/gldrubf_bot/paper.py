"""Deprecated target-only prototype. v1 CLI uses engine.py, not this function."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PaperResult:
    day: str
    signal: int
    position_before: int
    position_after: int
    executed: bool


def execute_paper(db_path: str | Path, day: str, target: int, price: float) -> PaperResult:
    """Idempotent daily signal application, one contract maximum.

    target=0 means no crossing, so maintain the previous position.
    A flip is recorded as one paper event, not as a real broker order.
    """
    if date.fromisoformat(day).isoformat() != day:
        raise ValueError("Day must be canonical YYYY-MM-DD")
    if type(target) is not int or target not in (-1, 0, 1):
        raise ValueError("Invalid target")
    if not (0 < price < float("inf")):
        raise ValueError("Invalid execution price")
    # Connection.__exit__ commits/rolls back but does not close the file handle.
    # Exit the transaction first, then close on success, early return or error.
    with closing(sqlite3.connect(str(db_path), timeout=15)) as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS paper_events (
            day TEXT PRIMARY KEY,
            signal INTEGER NOT NULL,
            price REAL NOT NULL,
            before_position INTEGER NOT NULL,
            after_position INTEGER NOT NULL
        )""")
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT signal, before_position, after_position, price FROM paper_events WHERE day = ?",
            (day,),
        ).fetchone()
        if existing is not None:
            if existing[0] != target or existing[3] != price:
                raise ValueError("Conflicting signal/price for an already processed day")
            return PaperResult(day, target, existing[1], existing[2], False)
        row = db.execute(
            "SELECT after_position FROM paper_events ORDER BY day DESC LIMIT 1"
        ).fetchone()
        last_day = db.execute(
            "SELECT day FROM paper_events ORDER BY day DESC LIMIT 1"
        ).fetchone()
        if last_day is not None and day <= last_day[0]:
            raise ValueError("Cannot process a day earlier than the latest event")
        before = row[0] if row else 0
        after = before if target == 0 else target
        db.execute(
            "INSERT INTO paper_events VALUES (?, ?, ?, ?, ?)",
            (day, target, price, before, after),
        )
        return PaperResult(day, target, before, after, before != after)
