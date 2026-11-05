import contextlib
import csv
import io
import json
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
from decimal import Decimal as D

from src.gldrubf_bot.cli import main, paper_step, runner_lock
from src.gldrubf_bot.api import APIError, Instrument, TradingDay
from src.gldrubf_bot.engine import PaperEngine
from src.gldrubf_bot.io import save_calendar
from src.gldrubf_bot.models import Quote, DataError, instant, iso, number
from src.gldrubf_bot.paper import execute_paper
from tests.helpers import bars, costs, UP, SQLiteResourceTestCase


class WorkflowTests(SQLiteResourceTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bars = bars(UP+[1,1])
        self.inst = Instrument('test-uid','fixture','GLDRUBF','TEST_EXCHANGE',D(1))
        self.days = {b.day:TradingDay(b.day,True,((b.time,b.closed_at),),b.closed_at) for b in self.bars}

    def fixture_files(self):
        with (self.root/'daily.csv').open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=list(self.bars[0].payload()));w.writeheader();w.writerows(b.payload() for b in self.bars)
        # Independent next-session observations; NEVER use the signal candle close as a fill.
        with (self.root/'quotes.csv').open('w',newline='') as f:
            w=csv.writer(f);w.writerow(['time','price'])
            for i in [50,51]:w.writerow([iso(self.bars[i+1].time+timedelta(hours=1)),100+i])
        save_calendar(self.root/'calendar.json',self.inst,self.days)

    def replay_args(self):
        return ['replay','--bars',str(self.root/'daily.csv'),'--quotes',str(self.root/'quotes.csv'),
                '--calendar',str(self.root/'calendar.json'),'--uid','test-uid','--db',str(self.root/'db.sqlite3'),
                '--output',str(self.root/'out'),'--point-value-rub','2','--tick-size','1',
                '--commission-rub','.5','--slippage-ticks','1','--funding-per-contract-day-rub','2']

    def test_cli_replay_restart_idempotence(self):
        self.fixture_files()
        with contextlib.redirect_stdout(io.StringIO()):main(self.replay_args())
        first=(self.root/'out/summary.json').read_text()
        with contextlib.redirect_stdout(io.StringIO()):main(self.replay_args())
        self.assertEqual(first,(self.root/'out/summary.json').read_text())
        self.assertEqual(json.loads(first)['fills'],1)

    def test_missing_calendar_bar_stops_before_replay(self):
        self.fixture_files()
        data=json.loads((self.root/'calendar.json').read_text());data['days'].pop(10)
        (self.root/'calendar.json').write_text(json.dumps(data))
        with self.assertRaises(SystemExit):main(self.replay_args())
        self.assertFalse((self.root/'db.sqlite3').exists())

    def test_api_failure_does_not_create_fill(self):
        e=PaperEngine(self.root/'api.db','test-uid',costs())
        client=Mock();client.point_value.return_value=D(2);client.completed_daily.side_effect=APIError('unavailable')
        with self.assertRaises(APIError):paper_step(e,client,self.inst,clock=lambda:self.bars[-1].closed_at)
        self.assertEqual(e.summary()['fills'],0)
        self.assertIsNone(e.latest_day())

    def test_paper_next_observation_after_knowledge_and_stale_rejected(self):
        e=PaperEngine(self.root/'api.db','test-uid',costs())
        b=self.bars[:51];known=b[-1].closed_at+timedelta(minutes=10)
        calendar={b[-1].day:TradingDay(b[-1].day,True,((b[-1].time,known+timedelta(hours=2)),),known+timedelta(hours=2))}
        client=Mock();client.point_value.return_value=D(2);client.completed_daily.return_value=(b,calendar)
        client.quote.return_value=Quote(known,D(100))
        self.assertEqual(paper_step(e,client,self.inst,clock=lambda:known),'NO_FILL')
        later=known+timedelta(seconds=10);client.quote.return_value=Quote(later,D(100))
        self.assertEqual(paper_step(e,client,self.inst,clock=lambda:later),'PAPER_FILL')
        self.assertEqual(paper_step(e,client,self.inst,clock=lambda:later),'NO_FILL')
        old=known+timedelta(seconds=11);client.quote.return_value=Quote(old,D(100))
        self.assertEqual(paper_step(e,client,self.inst,clock=lambda:old+timedelta(hours=1)),'WAIT_FRESH_PRICE')

    def test_changed_point_value_stops_before_any_signal(self):
        e=PaperEngine(self.root/'api.db','test-uid',costs());client=Mock();client.point_value.return_value=D(3)
        with self.assertRaises(DataError):paper_step(e,client,self.inst)
        client.completed_daily.assert_not_called()
        self.assertEqual(e.summary()['fills'],0)

    def test_legacy_database_preserved_and_price_conflict_detected(self):
        path=self.root/'legacy.db'
        execute_paper(path,'2026-01-01',1,100)
        with self.assertRaises(ValueError):execute_paper(path,'2026-01-01',1,101)
        before=path.read_bytes()
        with self.assertRaises(DataError):PaperEngine(path,'test-uid',costs())
        self.assertEqual(before,path.read_bytes())

    def test_lock_blocks_second_runner(self):
        with runner_lock(self.root/'lock.db'):
            with self.assertRaises(DataError):
                with runner_lock(self.root/'lock.db'):pass

    def test_utc_offsets_and_invalid_numbers(self):
        self.assertEqual(instant('2026-01-01T03:00:00+03:00'),instant('2026-01-01T00:00:00Z'))
        for x in ['NaN','Infinity','-Infinity']:
            with self.assertRaises(DataError):number(x)
        with self.assertRaises(DataError):instant('2026-01-01T10:00:00')

    def test_quotes_cannot_run_past_a_missing_completed_daily_bar(self):
        self.fixture_files()
        # Remove the last daily candle, then quote in the following verified session.
        lines=(self.root/'daily.csv').read_text().splitlines()
        (self.root/'daily.csv').write_text('\n'.join(lines[:-1])+'\n')
        later=bars(UP+[1,1,1])[-1]
        self.days[later.day]=TradingDay(later.day,True,((later.time,later.closed_at),),later.closed_at)
        save_calendar(self.root/'calendar.json',self.inst,self.days)
        (self.root/'quotes.csv').write_text('time,price\n'+iso(later.time+timedelta(hours=1))+',100\n')
        with self.assertRaises(SystemExit):main(self.replay_args())
        self.assertFalse((self.root/'db.sqlite3').exists())
