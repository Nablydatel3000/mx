"""Artificial technical fixtures, NOT GLDRUBF market data."""
import sqlite3
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from unittest.mock import patch
from src.gldrubf_bot.models import Bar, Quote, UTC
from src.gldrubf_bot.engine import Costs


class SQLiteResourceTestCase(unittest.TestCase):
    """Check explicit closure even on systems that can unlink open DB files."""

    def setUp(self):
        super().setUp()
        self._sqlite_connections = []
        connect = sqlite3.connect

        def tracked_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            # Keep strong references: garbage collection must not hide a leak.
            self._sqlite_connections.append((connection, str(args[0])))
            return connection

        self.addCleanup(self._close_sqlite_connections)
        self.enterContext(patch('sqlite3.connect', side_effect=tracked_connect))

    def _open_sqlite_connections(self):
        opened = []
        for connection, path in self._sqlite_connections:
            try:
                connection.in_transaction
            except sqlite3.ProgrammingError:  # A closed connection rejects access.
                continue
            opened.append((connection, path))
        return opened

    def _close_sqlite_connections(self):
        for connection, _ in self._open_sqlite_connections():
            connection.close()

    def tearDown(self):
        try:
            leaked = [path for _, path in self._open_sqlite_connections()]
            # Report the resource failure without cascading temp-dir cleanup errors.
            self._close_sqlite_connections()
            self.assertEqual(leaked, [], 'SQLite connections left open: ' + repr(leaked))
        finally:
            super().tearDown()


def bars(prices):
    result = []
    for i, p in enumerate(prices):
        t = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i)
        result.append(Bar(t.date(), t, t+timedelta(hours=18), D(p), D(p), D(p), D(p), 10))
    return result


def costs(**kw):
    values = dict(point_value_rub=D(2), tick=D(1), commission_rub=D('.5'), slippage_ticks=1,
                  initial_cash_rub=D(1000), funding_daily_rub=D(2))
    values.update(kw)
    return Costs(**values)


UP = [100]*30+[90]*20+[400]
DOWN = [100]*30+[101]*20+[1]
