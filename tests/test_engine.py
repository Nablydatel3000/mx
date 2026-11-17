import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import timedelta
from decimal import Decimal as D
from src.gldrubf_bot.engine import PaperEngine
from src.gldrubf_bot.models import Bar, Quote, DataError, iso
from tests.helpers import bars, costs, UP, DOWN, SQLiteResourceTestCase


class EngineTests(SQLiteResourceTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'paper.db'
        self.e = PaperEngine(self.path, 'verified-fixture-uid', costs())
        self.b = bars(UP)

    def warm(self):
        self.e.process_bars(self.b, self.b[-1].closed_at)

    def test_next_price_strictly_after_signal_and_restart(self):
        self.warm()
        t = self.b[-1].closed_at
        self.assertFalse(self.e.observe(Quote(t, D(100))))
        e = PaperEngine(self.path, 'verified-fixture-uid', costs())
        self.assertEqual(e.summary()['pending'], 1)
        self.assertTrue(e.observe(Quote(t+timedelta(seconds=1), D(100))))
        self.assertFalse(e.observe(Quote(t+timedelta(seconds=1), D(100))))
        self.assertEqual(e.summary()['entry'], '101')
        self.assertEqual(e.summary()['fills'], 1)

    def test_flip_long_close_short_open_pnl_fees_funding(self):
        self.warm()
        self.e.observe(Quote(self.b[-1].closed_at+timedelta(seconds=1), D(100)))
        more = bars(UP+[1, 1])
        self.e.process_bars(more, more[-1].closed_at)
        self.assertEqual(self.e.summary()['pending'], -1)
        self.e.observe(Quote(more[-1].closed_at+timedelta(seconds=1), D(110)))
        s = self.e.summary()
        self.assertEqual((s['position'], s['fills']), (-1, 3))
        self.assertEqual(D(s['realized_gross']), D(16))
        self.assertEqual(D(s['commission']), D('1.5'))
        self.assertEqual(D(s['funding']), D(-4))
        self.assertEqual(D(s['equity']), D('1008.5'))
        self.assertEqual(PaperEngine(self.path, 'verified-fixture-uid', costs()).summary(), s)

    def test_short_pnl_and_signed_funding(self):
        b = bars(DOWN)
        self.e.process_bars(b, b[-1].closed_at)
        self.e.observe(Quote(b[-1].closed_at+timedelta(seconds=1), D(100)))
        self.assertEqual(self.e.summary()['position'], -1)
        b = bars(DOWN+[400])
        self.e.process_bars(b, b[-1].closed_at)
        self.e.observe(Quote(b[-1].closed_at+timedelta(seconds=1), D(90)))
        s = self.e.summary()
        self.assertEqual(s['position'], 1)
        self.assertEqual(D(s['realized_gross']), D(16))
        self.assertEqual(D(s['funding']), D(2))

    def test_prefix_no_lookahead(self):
        self.e.process_bars(self.b[:50], self.b[49].closed_at)
        self.assertEqual(self.e.summary()['pending'], 0)
        self.e.process_bars(self.b, self.b[-1].closed_at)
        self.assertEqual(self.e.summary()['pending'], 1)
        # Future bar cannot be accepted before its official completion.
        future = bars(UP+[1])[-1]
        before = self.e.summary()
        with self.assertRaises(DataError):
            self.e.process_bars([future], future.closed_at-timedelta(seconds=1))
        self.assertEqual(self.e.summary(), before)

    def test_repeated_bars_prices_and_revisions(self):
        self.warm()
        s = self.e.summary()
        self.e.process_bars(self.b, self.b[-1].closed_at+timedelta(hours=1))
        self.assertEqual(s, self.e.summary())
        old = self.b[-1]
        revised = Bar(old.day, old.time, old.closed_at, D(401), D(401), D(401), D(401), 10)
        with self.assertRaises(DataError):
            self.e.process_bars([revised], old.closed_at)
        self.e.observe(Quote(old.closed_at+timedelta(seconds=1), D(100)))
        with self.assertRaises(DataError):
            self.e.observe(Quote(old.closed_at+timedelta(seconds=1), D(101)))

    def test_config_identity_and_corrupt_state_fail_closed(self):
        with self.assertRaises(DataError):
            PaperEngine(self.path, 'different-uid', costs())
        with self.assertRaises(DataError):
            PaperEngine(self.path, 'verified-fixture-uid', costs(commission_rub=D(9)))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE state SET gross='999' WHERE id=1")
        with self.assertRaises(DataError):
            PaperEngine(self.path, 'verified-fixture-uid', costs())

    def test_atomic_funding_failure_no_partial_batch(self):
        path = Path(self.tmp.name)/'external.db'
        e = PaperEngine(path, 'uid', costs(funding_daily_rub=None))
        e.process_bars(self.b, self.b[-1].closed_at)
        e.observe(Quote(self.b[-1].closed_at+timedelta(seconds=1), D(100)))
        more = bars(UP+[1, 1])
        before = e.summary()
        e.rates[more[-2].day] = D(3)  # next rate missing: first insertion must also roll back
        with self.assertRaises(DataError):
            e.process_bars(more[-2:], more[-1].closed_at)
        self.assertEqual(e.latest_day(), self.b[-1].day)
        self.assertEqual(e.summary(), before)

    def test_off_grid_and_bad_slippage_roll_back(self):
        self.warm()
        with self.assertRaises(DataError):
            self.e.observe(Quote(self.b[-1].closed_at+timedelta(seconds=1), D('100.5')))
        self.assertEqual(self.e.summary()['fills'], 0)
        # Short sale slippage cannot generate a negative virtual fill.
        path = Path(self.tmp.name)/'negative.db'
        e = PaperEngine(path, 'uid', costs(slippage_ticks=200))
        b = bars(DOWN)
        e.process_bars(b, b[-1].closed_at)
        with self.assertRaises(DataError):
            e.observe(Quote(b[-1].closed_at+timedelta(seconds=1), D(100)))
        self.assertEqual(e.summary()['fills'], 0)

    def test_subsecond_time_order_and_export(self):
        self.warm()
        t = self.b[-1].closed_at+timedelta(seconds=1)
        self.e.observe(Quote(t, D(100)))
        self.e.observe(Quote(t+timedelta(microseconds=1), D(101)))
        with self.assertRaises(DataError):
            self.e.observe(Quote(t-timedelta(microseconds=1), D(100)))
        out = Path(self.tmp.name)/'out'
        self.e.export(out)
        self.assertEqual(json.loads((out/'summary.json').read_text())['mark_time'], iso(t+timedelta(microseconds=1)))
        self.assertTrue((out/'fills.csv').exists())

    def test_reversal_transaction_rolls_back_both_legs_on_storage_error(self):
        self.warm()
        self.e.observe(Quote(self.b[-1].closed_at+timedelta(seconds=1),D(100)))
        more=bars(UP+[1,1]);self.e.process_bars(more,more[-1].closed_at)
        before=self.e.summary()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TRIGGER test_failure BEFORE INSERT ON fills
                WHEN NEW.kind='OPEN' BEGIN SELECT RAISE(ABORT,'injected storage fault'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.e.observe(Quote(more[-1].closed_at+timedelta(seconds=1),D(110)))
        self.assertEqual(self.e.summary(),before)
        self.assertEqual(PaperEngine(self.path,'verified-fixture-uid',costs()).summary(),before)

    def test_external_funding_revision_detected_after_restart(self):
        path=Path(self.tmp.name)/'rates.db';e=PaperEngine(path,'uid',costs(funding_daily_rub=None))
        e.process_bars(self.b,self.b[-1].closed_at)
        e.observe(Quote(self.b[-1].closed_at+timedelta(seconds=1),D(100)))
        following=bars(UP+[1])[-1];e.rates[following.day]=D(3)
        e.process_bars([following],following.closed_at)
        with self.assertRaises(DataError):
            PaperEngine(path,'uid',costs(funding_daily_rub=None),funding_rates={following.day.isoformat():'4'})
