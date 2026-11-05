"""Critical boundary regressions; synthetic events, never real MOEX data or orders."""
import json
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from src.gldrubf_bot.api import parse_day, Instrument
from src.gldrubf_bot.cli import paper_step
from src.gldrubf_bot.engine import PaperEngine
from src.gldrubf_bot.models import DataError, Quote, UTC, iso
from tests.helpers import bars, costs, UP, DOWN, SQLiteResourceTestCase
from tests import test_api as api_fixtures
from tests.test_api import day, candle, INST, NOW


class FinancingChronologyAudit(SQLiteResourceTestCase):
    def setUp(self):
        super().setUp()
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'audit.db'
        self.engine = PaperEngine(self.path, 'audit-uid', costs())

    def warm(self, prices=UP):
        b = bars(prices)
        self.engine.process_bars(b, b[-1].closed_at)
        return b

    def fund_row(self, day):
        with closing(sqlite3.connect(self.path)) as db, db:
            return db.execute('SELECT position,cash FROM funding WHERE day=?', (day.isoformat(),)).fetchone()

    def test_late_quote_cannot_rewrite_already_settled_position(self):
        b = self.warm()
        # Day 52 has no new crossover, so an old long pending target remains.
        newer = bars(UP+[1])[-1]
        observed = newer.closed_at+timedelta(seconds=30)
        self.engine.process_bars([newer], observed)
        self.assertEqual(self.fund_row(newer.day)[0], 0)
        before = self.engine.summary()
        late = Quote(newer.closed_at-timedelta(seconds=1), D(100))
        self.assertFalse(self.engine.observe(late))
        self.assertEqual(self.engine.summary(), before)
        # A fresh observation can fill, but must not retroactively charge the flat day.
        self.assertTrue(self.engine.observe(Quote(observed+timedelta(seconds=1), D(100))))
        self.assertEqual(self.fund_row(newer.day)[0], 0)
        PaperEngine(self.path, 'audit-uid', costs())

    def test_watermark_survives_restart_without_new_crossover(self):
        self.warm()
        newer = bars(UP+[1])[-1]
        observed = newer.closed_at+timedelta(minutes=1)
        self.engine.process_bars([newer], observed)
        restored = PaperEngine(self.path, 'audit-uid', costs())
        self.assertFalse(restored.observe(Quote(observed-timedelta(seconds=1), D(100))))
        self.assertEqual(restored.summary()['fills'], 0)

    def test_fill_exactly_at_cutoff_is_after_funding_snapshot(self):
        self.warm()
        newer = bars(UP+[1])[-1]
        self.engine.process_bars([newer], newer.closed_at)
        self.assertTrue(self.engine.observe(Quote(newer.closed_at, D(100))))
        self.assertEqual(self.fund_row(newer.day)[0], 0)  # convention: position immediately BEFORE end
        following = bars(UP+[1,1])[-1]
        self.engine.process_bars([following], following.closed_at)
        self.assertEqual(self.fund_row(following.day), (1, '-2'))

    def test_long_short_flip_then_funding_uses_short(self):
        b = self.warm()
        self.engine.observe(Quote(b[-1].closed_at+timedelta(seconds=1), D(100)))
        b = bars(UP+[1,1])
        self.engine.process_bars(b[-2:], b[-1].closed_at)
        self.engine.observe(Quote(b[-1].closed_at+timedelta(seconds=1), D(110)))
        later = bars(UP+[1,1,1])[-1]
        self.engine.process_bars([later], later.closed_at)
        self.assertEqual(self.fund_row(later.day), (-1, '2'))
        self.assertEqual(self.engine.summary()['fills'], 3)

    def test_short_long_flip_then_funding_uses_long(self):
        b = self.warm(DOWN)
        self.engine.observe(Quote(b[-1].closed_at+timedelta(seconds=1), D(100)))
        b = bars(DOWN+[400])
        self.engine.process_bars([b[-1]], b[-1].closed_at)
        self.engine.observe(Quote(b[-1].closed_at+timedelta(seconds=1), D(90)))
        later = bars(DOWN+[400,400])[-1]
        self.engine.process_bars([later], later.closed_at)
        self.assertEqual(self.fund_row(later.day), (1, '-2'))

    def test_multiday_recovery_charges_held_position_not_new_pending(self):
        b = self.warm()
        self.engine.observe(Quote(b[-1].closed_at+timedelta(seconds=1), D(100)))
        restored = PaperEngine(self.path, 'audit-uid', costs())
        extra = bars(UP+[1,1,1])[-3:]
        known = extra[-1].closed_at+timedelta(hours=1)
        restored.process_bars(extra, known)
        self.assertEqual(D(restored.summary()['funding']), D(-6))
        self.assertEqual(restored.summary()['position'], 1)
        self.assertEqual(restored.summary()['pending'], -1)
        restored.process_bars(extra, known+timedelta(seconds=1))
        self.assertEqual(D(restored.summary()['funding']), D(-6))
        self.assertTrue(restored.observe(Quote(known+timedelta(seconds=2), D(110))))
        self.assertEqual(restored.summary()['fills'], 3)

    def test_recovery_rejects_self_consistent_wrong_funding_position(self):
        b = self.warm()
        self.engine.observe(Quote(b[-1].closed_at+timedelta(seconds=1), D(100)))
        newer = bars(UP+[1])[-1]
        self.engine.process_bars([newer], newer.closed_at)
        # Arithmetic balances agree, but the historical position was LONG, not SHORT.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE funding SET position=-1,cash='2' WHERE day=?", (newer.day.isoformat(),))
            db.execute("UPDATE state SET funding='2' WHERE id=1")
        with self.assertRaises(DataError):PaperEngine(self.path, 'audit-uid', costs())

    def test_recovery_rejects_missing_zero_cash_funding_day(self):
        b = self.warm()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM funding WHERE day=?', (b[0].day.isoformat(),))
        with self.assertRaises(DataError):PaperEngine(self.path, 'audit-uid', costs())

    def test_recovery_rejects_shifted_settlement_time(self):
        b = self.warm()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE funding SET effective_at=? WHERE day=?',
                       (iso(b[0].closed_at+timedelta(hours=1)), b[0].day.isoformat()))
        with self.assertRaises(DataError):PaperEngine(self.path, 'audit-uid', costs())

    def test_disappeared_cached_day_cannot_silently_remain_in_sma(self):
        b = self.warm()
        now = b[-1].closed_at+timedelta(days=1)
        newer = bars(UP+[1])[-1]
        client = Mock()
        client.point_value.return_value = D(2)
        # Simulates revised calendar marking a formerly known trading day as closed.
        client.completed_daily.return_value = (b[-7:-1]+[newer], {})
        client.quote.return_value = None
        before = self.engine.latest_day()
        with self.assertRaises(DataError):
            paper_step(self.engine, client, Instrument('audit-uid','fixture','GLDRUBF','MOEX',D(1)), clock=lambda:now)
        self.assertEqual(self.engine.latest_day(), before)
        client.quote.assert_not_called()


    def test_flat_day_rate_still_matches_fixed_scenario_on_recovery(self):
        b=self.warm()
        self.assertEqual(D(self.engine.summary()['funding']),D(0))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE funding SET long_cost='999' WHERE day=?",(b[0].day.isoformat(),))
        with self.assertRaises(DataError):PaperEngine(self.path,'audit-uid',costs())

    def test_calendar_completion_revision_rejected_atomically(self):
        b=self.warm()
        old=b[-1]
        from dataclasses import replace
        revised=replace(old,closed_at=old.closed_at+timedelta(hours=1))
        before=self.engine.summary()
        with self.assertRaises(DataError):self.engine.process_bars([revised],revised.closed_at)
        self.assertEqual(self.engine.summary(),before)


class CalendarBoundaryAudit(unittest.TestCase):
    client = api_fixtures.APITests.client

    def test_trailing_clearing_does_not_shorten_official_day(self):
        raw = day()
        raw.update(clearingStartTime='2026-01-02T17:00:00Z', clearingEndTime='2026-01-02T18:00:00Z')
        result = parse_day(raw)
        self.assertEqual(result.close, datetime(2026,1,2,18,tzinfo=UTC))
        self.assertFalse(result.contains(datetime(2026,1,2,17,30,tzinfo=UTC)))

    def test_auction_not_a_continuous_market_fill_window(self):
        raw = day()
        raw.update(closingAuctionStartTime='2026-01-02T17:50:00Z', closingAuctionEndTime='2026-01-02T18:00:00Z')
        result = parse_day(raw)
        self.assertEqual(result.close.hour, 18)
        self.assertFalse(result.contains(datetime(2026,1,2,17,55,tzinfo=UTC)))
        self.assertTrue(result.contains(datetime(2026,1,2,17,49,tzinfo=UTC)))

    def test_missing_auction_boundary_fails_closed(self):
        raw = day()
        raw['openingAuctionStartTime'] = '2026-01-02T07:00:00Z'
        with self.assertRaises(DataError):parse_day(raw)

    def test_day_not_complete_before_original_end_even_if_api_flag_true(self):
        raw = day()
        raw.update(clearingStartTime='2026-01-02T17:00:00Z', clearingEndTime='2026-01-02T18:00:00Z')
        c = self.client(lambda method,p: {'exchanges':[{'exchange':'MOEX_FUT','days':[raw]}]}
                        if method.endswith('TradingSchedules') else {'candles':[candle()]})
        got,_ = c.completed_daily(INST,datetime(2026,1,2,tzinfo=UTC),datetime(2026,1,2,17,30,tzinfo=UTC))
        self.assertEqual(got, [])

    def test_special_saturday_and_holiday_are_calendar_not_weekday_rules(self):
        c = self.client(lambda method,p: {'exchanges':[{'exchange':'MOEX_FUT','days':[day('2026-01-03'),day('2026-01-04',False)]}]}
                        if method.endswith('TradingSchedules') else {'candles':[candle('2026-01-03')]})
        got,_ = c.completed_daily(INST,datetime(2026,1,3,tzinfo=UTC),datetime(2026,1,4,19,tzinfo=UTC))
        self.assertEqual([b.day.isoformat() for b in got], ['2026-01-03'])

    def test_main_evening_clearing_and_timezone_offsets(self):
        raw=day()
        raw.update(startTime='2026-01-02T10:00:00+03:00',endTime='2026-01-02T18:00:00+03:00',
                   eveningStartTime='2026-01-02T19:00:00+03:00',eveningEndTime='2026-01-02T23:00:00+03:00',
                   clearingStartTime='2026-01-02T14:00:00+03:00',clearingEndTime='2026-01-02T14:10:00+03:00')
        s=parse_day(raw)
        self.assertEqual(s.close.hour,20)
        self.assertFalse(s.contains(datetime(2026,1,2,11,5,tzinfo=UTC)))
        self.assertFalse(s.contains(datetime(2026,1,2,15,30,tzinfo=UTC)))
        self.assertTrue(s.contains(datetime(2026,1,2,17,tzinfo=UTC)))
        self.assertFalse(s.contains(s.close))

    def test_daily_utc_label_is_not_replaced_by_moscow_date(self):
        raw = day()
        raw.update(startTime='2026-01-02T21:00:00Z', endTime='2026-01-02T23:00:00Z')
        bar = candle();bar['time']='2026-01-02T22:00:00Z'  # Jan 3 in Moscow, Jan 2 provider label
        c = self.client(lambda method,p: {'exchanges':[{'exchange':'MOEX_FUT','days':[raw,day('2026-01-03',False)]}]}
                        if method.endswith('TradingSchedules') else {'candles':[bar]})
        got,_=c.completed_daily(INST,datetime(2026,1,2,tzinfo=UTC),datetime(2026,1,3,1,tzinfo=UTC))
        self.assertEqual([b.day.isoformat() for b in got], ['2026-01-02'])

    def test_ambiguous_overnight_daily_mapping_is_not_guessed(self):
        earlier=day('2026-01-02')
        earlier.update(startTime='2026-01-02T23:00:00Z',endTime='2026-01-03T02:00:00Z')
        later=day('2026-01-03')
        later.update(startTime='2026-01-03T03:00:00Z',endTime='2026-01-03T08:00:00Z')
        bar=candle('2026-01-03');bar['time']='2026-01-03T00:30:00Z'
        previous=candle('2026-01-02');previous['time']='2026-01-02T23:00:00Z'
        c=self.client(lambda method,p: {'exchanges':[{'exchange':'MOEX_FUT','days':[earlier,later]}]}
                       if method.endswith('TradingSchedules') else {'candles':[previous,bar]})
        with self.assertRaisesRegex(DataError,'overnight mapping'):
            c.completed_daily(INST,datetime(2026,1,2,tzinfo=UTC),datetime(2026,1,3,9,tzinfo=UTC))
