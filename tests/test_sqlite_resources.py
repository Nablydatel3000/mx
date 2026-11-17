"""File/transaction lifetime regressions, including Windows cleanup semantics."""
import csv
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from src.gldrubf_bot.cli import runner_lock
from src.gldrubf_bot.engine import PaperEngine
from src.gldrubf_bot.models import DataError
from src.gldrubf_bot.paper import execute_paper
from tests.helpers import SQLiteResourceTestCase, costs


class SQLiteLifecycleTests(SQLiteResourceTestCase):
    def test_legacy_returns_and_errors_close_before_directory_cleanup(self):
        with TemporaryDirectory(prefix='mx SQLite пробел ') as directory:
            path = Path(directory) / 'legacy.db'
            first = execute_paper(path, '2026-01-02', 1, 100)
            self.assertTrue(first.executed)
            self.assertEqual(self._open_sqlite_connections(), [])
            self.assertFalse(execute_paper(path, '2026-01-02', 1, 100).executed)
            self.assertEqual(self._open_sqlite_connections(), [])
            for day, price in [('2026-01-02', 101), ('2026-01-01', 100)]:
                with self.assertRaises(ValueError):
                    execute_paper(path, day, 1, price)
                self.assertEqual(self._open_sqlite_connections(), [])
            # Unlink while all connection objects are still strongly referenced.
            path.unlink()
        self.assertFalse(Path(directory).exists())

    def test_legacy_storage_failure_rolls_back_and_closes(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.db'
            execute_paper(path, '2026-01-02', 1, 100)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("""CREATE TRIGGER reject_insert BEFORE INSERT ON paper_events
                              BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END""")
            with self.assertRaises(sqlite3.IntegrityError):
                execute_paper(path, '2026-01-03', -1, 110)
            self.assertEqual(self._open_sqlite_connections(), [])
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute('SELECT day,after_position FROM paper_events').fetchall(),
                                 [('2026-01-02', 1)])
                db.execute('DROP TRIGGER reject_insert')
            # The transaction error must not leave a lock or a partial change.
            result = execute_paper(path, '2026-01-03', -1, 110)
            self.assertEqual((result.position_before, result.position_after), (1, -1))

    def test_engine_transactions_commit_rollback_and_close(self):
        with TemporaryDirectory() as directory:
            engine = PaperEngine(Path(directory) / 'paper.db', 'fixture-uid', costs())
            with engine.db() as db:
                db.execute("INSERT INTO quotes VALUES ('committed', '100')")
            self.assertEqual(self._open_sqlite_connections(), [])
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                with engine.db() as db:
                    db.execute("INSERT INTO quotes VALUES ('rolled-back', '101')")
                    raise RuntimeError('injected')
            self.assertEqual(self._open_sqlite_connections(), [])
            with engine.db() as db:
                self.assertEqual([row['time'] for row in db.execute('SELECT time FROM quotes')],
                                 ['committed'])

    def test_engine_constructor_failure_closes_without_changing_legacy(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'legacy.db'
            execute_paper(path, '2026-01-02', 1, 100)
            before = path.read_bytes()
            with self.assertRaisesRegex(DataError, 'Legacy target-only'):
                PaperEngine(path, 'fixture-uid', costs())
            self.assertEqual(self._open_sqlite_connections(), [])
            self.assertEqual(path.read_bytes(), before)
            path.unlink()

    def test_runner_lock_released_after_exception(self):
        with TemporaryDirectory(prefix='mx lock пробел ') as directory:
            path = Path(directory) / 'paper.db'
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                with runner_lock(path):
                    raise RuntimeError('injected')
            with runner_lock(path):
                with self.assertRaises(DataError):
                    with runner_lock(path):
                        self.fail('Second runner unexpectedly acquired the lock')
            Path(str(path) + '.runner.lock').unlink()

    def test_export_replaces_closed_files_in_unicode_directory(self):
        with TemporaryDirectory(prefix='mx export пробел ') as directory:
            root = Path(directory)
            engine = PaperEngine(root / 'paper.db', 'fixture-uid', costs())
            output = root / 'журнал с пробелами'
            engine.export(output)
            before = {p.name: p.read_bytes() for p in output.iterdir()}
            engine.export(output)
            self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})
            with (output / 'fills.csv').open(encoding='utf-8-sig', newline='') as stream:
                self.assertEqual(list(csv.reader(stream))[0][0], 'id')
            self.assertFalse(list(output.glob('*.tmp')))
            self.assertEqual(self._open_sqlite_connections(), [])
            for path in output.iterdir():
                path.unlink()
