"""Synthetic integration of autonomous provider-clock PAPER, never live orders."""
import contextlib
import io
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from src.gldrubf_bot import autonomous
from src.gldrubf_bot.api import APIError
from src.gldrubf_bot.cli import main
from src.gldrubf_bot.engine import PaperEngine
from src.gldrubf_bot.history import load_snapshot
from src.gldrubf_bot.models import DataError, MOSCOW, UTC, instant, iso
from src.gldrubf_bot.research import replay_history
from tests.helpers import SQLiteResourceTestCase, costs, UP, DOWN
from tests.history_helpers import INST, START, NOW, candles, client_for, snapshot, evidence_file
from tests.test_api import FakeResponse
from tempfile import TemporaryDirectory


class AutonomousTests(SQLiteResourceTestCase):
    def setUp(self):
        super().setUp()
        self.tmp=TemporaryDirectory(prefix='mx autonomous пробел ')
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def engine(self,name='auto.db',**scenario):
        return PaperEngine(self.root/name,INST.uid,costs(**scenario))

    def step(self,engine,path,now=NOW,*,quote=None,trading=True,audit=None):
        client,requests,_=client_for(candles(),quote_time=quote if quote is not None else now,trading=trading)
        event=autonomous.step(engine,client,INST,path,clock=lambda:now,audit=audit)
        return event,requests

    def test_sma_both_directions_only_after_actual_knowledge_and_restart(self):
        for label,prices,target in [('long',UP,1),('short',DOWN,-1)]:
            with self.subTest(direction=label):
                path,_,_,_=snapshot(self.root,candles(prices),label)
                engine=self.engine(label+'.db')
                self.assertEqual(self.step(engine,path)[0],'NO_FILL')
                self.assertEqual(engine.summary()['pending'],target)
                later=NOW+timedelta(seconds=1)
                self.assertEqual(self.step(engine,path,later)[0],'PAPER_FILL')
                before=engine.summary()
                self.assertEqual((before['position'],before['fills']),(target,1))
                restored=self.engine(label+'.db')
                self.assertEqual(self.step(restored,path,later)[0],'NO_FILL')
                self.assertEqual(restored.summary(),before)

    def test_saturday_sunday_and_moscow_date_shift_are_kept_without_calendar_claim(self):
        raw=candles(UP)
        for row in raw:
            row['time']=iso(instant(row['time'])+timedelta(hours=21))
        path,_,_,_=snapshot(self.root,raw)
        engine=self.engine()
        checks=[]
        _,requests=self.step(engine,path,audit=checks.append)
        with engine.db() as db:
            rows=list(db.execute('SELECT day,payload,known_at FROM bars ORDER BY day'))
        self.assertEqual(len(rows),51)
        self.assertTrue(any(instant(json.loads(r['payload'])['candle']['time']).weekday()==5 for r in rows))
        self.assertTrue(any(instant(json.loads(r['payload'])['candle']['time']).weekday()==6 for r in rows))
        first=json.loads(rows[0]['payload'])
        self.assertEqual(first['provider_time_msk'],(START+timedelta(hours=21)).astimezone(MOSCOW).isoformat())
        self.assertNotEqual(first['provider_time_msk'][:10],rows[0]['day'])
        self.assertNotIn('closed_at',first)
        self.assertEqual(first['first_confirmed_at'],iso(NOW))
        self.assertFalse(checks[0]['trading_dates_verified'])
        schedules=[p for m,p in requests if m.endswith('/TradingSchedules')]
        self.assertTrue(all(instant(p['from']).date()==NOW.date() for p in schedules))

    def test_incomplete_becomes_complete_without_manual_evidence_or_repeated_fill(self):
        raw=candles();raw[-1]['isComplete']=False
        first,_,_,_=snapshot(self.root,raw)
        engine=self.engine()
        self.step(engine,first)
        self.step(engine,first,NOW+timedelta(seconds=1))
        self.assertEqual(engine.summary()['position'],1)
        later=NOW+timedelta(seconds=60)
        second,_,_,_=snapshot(self.root,candles(),'second',previous=first,clock=later)
        self.assertEqual(self.step(engine,second,later)[0],'NO_FILL')
        self.assertEqual(engine.summary()['pending'],-1)
        self.assertEqual(self.step(engine,second,later+timedelta(seconds=1))[0],'PAPER_FILL')
        before=engine.summary()
        self.assertEqual((before['position'],before['fills']),(-1,3))
        self.step(self.engine(),second,later+timedelta(seconds=2))
        self.assertEqual(self.engine().summary()['fills'],3)

    def test_api_gap_catchup_uses_current_price_and_confirmation_cost_clock(self):
        first,_,_,_=snapshot(self.root,candles(UP))
        engine=self.engine()
        self.step(engine,first)
        self.step(engine,first,NOW+timedelta(seconds=1))
        later=NOW+timedelta(days=3)
        full=candles(UP+[1,1,1])
        second,_,_,_=snapshot(self.root,full,'after_gap',previous=first,clock=later)
        self.step(engine,second,later)
        self.assertEqual(engine.summary()['fills'],1)
        self.assertEqual(engine.summary()['pending'],-1)
        with engine.db() as db:
            confirmed=[r[0] for r in db.execute('SELECT effective_at FROM funding ORDER BY day DESC LIMIT 3')]
        self.assertEqual(confirmed,[iso(later)]*3)
        self.assertEqual(self.step(engine,second,later+timedelta(seconds=1))[0],'PAPER_FILL')
        self.assertEqual(self.engine().summary()['position'],-1)

    def test_unknown_initial_gaps_warn_but_late_insertions_stop_prefix_rewrite(self):
        raw=candles();raw.pop(20)
        first,_,_,_=snapshot(self.root,raw)
        engine=self.engine()
        self.step(engine,first)
        report=json.loads((first/'validation.json').read_text())
        self.assertTrue(report['gaps'])
        self.assertFalse(report['coverage_confirmed'])
        before=engine.summary()
        second,_,_,_=snapshot(self.root,candles(),'filled_gap',clock=NOW+timedelta(seconds=1))
        with self.assertRaisesRegex(DataError,'Late insertion'):
            self.step(engine,second,NOW+timedelta(seconds=1))
        self.assertEqual(engine.summary(),before)

    def test_cached_revision_and_disappearance_block_even_without_previous_snapshot_pointer(self):
        first,_,_,_=snapshot(self.root)
        engine=self.engine();self.step(engine,first);self.step(engine,first,NOW+timedelta(seconds=1))
        before=engine.summary()
        for kind in ('changed','missing'):
            raw=candles()
            if kind=='changed':raw[10]['volume']='99'
            else:raw.pop(10)
            path,_,_,_=snapshot(self.root,raw,kind,clock=NOW+timedelta(seconds=2))
            with self.subTest(kind=kind),self.assertRaises(DataError):
                self.step(self.engine(),path,NOW+timedelta(seconds=2))
            self.assertEqual(self.engine().summary(),before)

    def test_stale_future_and_closed_market_do_not_fill(self):
        path,_,_,_=snapshot(self.root)
        engine=self.engine();self.step(engine,path)
        current=NOW+timedelta(seconds=200)
        self.assertEqual(self.step(engine,path,current,quote=NOW)[0],'WAIT_FRESH_PRICE')
        self.assertEqual(self.step(engine,path,current,trading=False)[0],'WAIT_MARKET')
        with self.assertRaises(DataError):self.step(engine,path,current,quote=current+timedelta(seconds=1))
        self.assertEqual(engine.summary()['fills'],0)
        closed=NOW.replace(hour=19)
        fresh,_,_,_=snapshot(self.root,candles(),'closed',clock=closed)
        self.assertEqual(self.step(engine,fresh,closed)[0],'WAIT_MARKET')
        self.assertEqual(engine.summary()['fills'],0)

    def test_network_or_audit_failure_preserves_position(self):
        path,_,_,_=snapshot(self.root)
        engine=self.engine();self.step(engine,path);self.step(engine,path,NOW+timedelta(seconds=1))
        before=engine.summary()
        client,_,_=client_for(candles())
        client.current_sessions=lambda *a: (_ for _ in ()).throw(APIError('safe network failure',retryable=True))
        with self.assertRaises(APIError):autonomous.step(engine,client,INST,path,clock=lambda:NOW+timedelta(seconds=2))
        self.assertEqual(self.engine().summary(),before)
        checks=[]
        def failed_audit(payload):checks.append(payload);raise OSError('storage fault')
        with self.assertRaises(OSError):self.step(engine,path,NOW+timedelta(seconds=2),audit=failed_audit)
        self.assertEqual(self.engine().summary(),before)

    def test_missing_financing_rate_rolls_back_complete_batch(self):
        first,_,_,_=snapshot(self.root,candles(UP))
        engine=self.engine(funding_daily_rub=None)
        self.step(engine,first);self.step(engine,first,NOW+timedelta(seconds=1))
        before=engine.summary()
        later=NOW+timedelta(seconds=60)
        full,_,_,_=snapshot(self.root,candles(),'new_bars',clock=later)
        engine.rates[(START+timedelta(days=51)).date()]=Decimal(3)
        with self.assertRaisesRegex(DataError,'financing'):
            self.step(engine,full,later)
        self.assertEqual(engine.summary(),before)
        self.assertEqual(self.engine(funding_daily_rub=None).summary(),before)

    def test_current_overnight_window_is_verified_independently_of_date_label(self):
        now=NOW.replace(day=22,hour=22)  # Sunday UTC, Monday Moscow.
        path,_,_,_=snapshot(self.root,candles(),'overnight',clock=now)
        engine=self.engine()
        requests=[]
        def client_at(when):
            client,_,_=client_for(candles(),quote_time=when)
            original=client.transport
            def transport(request,timeout):
                if request.full_url.endswith('/TradingSchedules'):
                    payload=json.loads(request.data);requests.append(payload)
                    return FakeResponse(json.dumps({'exchanges':[dict(exchange=INST.exchange,days=[dict(
                        date='2026-02-23T00:00:00Z',isTradingDay=True,
                        startTime='2026-02-22T21:00:00Z',endTime='2026-02-23T01:00:00Z')])]}).encode())
                return original(request,timeout)
            client.transport=transport
            return client
        checks=[]
        self.assertEqual(autonomous.step(engine,client_at(now),INST,path,clock=lambda:now,audit=checks.append),'NO_FILL')
        later=now+timedelta(seconds=1)
        self.assertEqual(autonomous.step(engine,client_at(later),INST,path,clock=lambda:later),'PAPER_FILL')
        self.assertEqual(checks[0]['checked_at_msk'][:10],'2026-02-23')
        self.assertEqual(checks[0]['calendar'][0]['day'],'2026-02-23')
        self.assertEqual(instant(requests[0]['from']).date(),now.date())
        self.assertEqual(instant(requests[0]['to'])-instant(requests[0]['from']),timedelta(days=2))

    def test_absent_current_calendar_waits_without_inventing_permission(self):
        path,_,_,_=snapshot(self.root)
        engine=self.engine()
        client,_,_=client_for(candles())
        client.current_sessions=lambda *a:{}
        self.assertEqual(autonomous.step(engine,client,INST,path,clock=lambda:NOW),'WAIT_CALENDAR')
        self.assertEqual(engine.summary()['fills'],0)

    def test_cli_retries_transient_metadata_failure_before_initializing_ledger(self):
        path,_,_,_=snapshot(self.root)
        client,_,_=client_for(candles())
        attempts=[]
        def instrument(uid):
            attempts.append(uid)
            if len(attempts)==1:raise APIError('safe transient startup',retryable=True)
            return INST
        client.instrument=instrument
        pauses=[]
        def sleep(seconds):
            pauses.append(seconds)
            if len(pauses)==1:self.assertFalse((self.root/'startup.db').exists())
            else:raise KeyboardInterrupt()
        args=['paper','--history',str(path),'--db',str(self.root/'startup.db'),'--output',str(self.root/'logs'),
              '--commission-rub','.5','--slippage-ticks','1','--funding-per-contract-day-rub','0']
        with patch('src.gldrubf_bot.cli.ReadOnlyClient',return_value=client), \
             patch('src.gldrubf_bot.cli.datetime',wraps=datetime) as app_clock, \
             patch('src.gldrubf_bot.autonomous.datetime',wraps=datetime) as forward_clock, \
             patch('src.gldrubf_bot.history.datetime',wraps=datetime) as history_clock, \
             patch('src.gldrubf_bot.cli.time.sleep',side_effect=sleep),contextlib.redirect_stdout(io.StringIO()):
            for clock in (app_clock,forward_clock,history_clock):clock.now.return_value=NOW
            main(args)
        self.assertEqual(len(attempts),2)
        self.assertEqual(pauses,[60,60])
        self.assertEqual(json.loads((self.root/'logs/summary.json').read_text())['fills'],0)

    def test_empty_or_insufficient_history_waits_without_synthetic_warmup(self):
        for index,prices in enumerate(([],UP[:50])):
            path,_,_,_=snapshot(self.root,candles(prices),str(index))
            engine=self.engine(str(index)+'.db')
            event,requests=self.step(engine,path)
            self.assertEqual(event,'WAIT_HISTORY')
            self.assertEqual(engine.summary()['fills'],0)
            self.assertFalse(any(m.endswith('/GetLastPrices') for m,_ in requests))

    def test_source_boundary_is_pinned_and_mode_mixing_is_rejected(self):
        engine=self.engine()
        start=autonomous.source_start(engine,NOW)
        self.assertEqual(autonomous.source_start(self.engine(),NOW+timedelta(days=5)),start)
        with self.assertRaises(DataError):autonomous.source_start(engine,NOW,requested=start+timedelta(days=1))
        path,_,_,_=snapshot(self.root)
        evidence,quotes=evidence_file(self.root,path)
        with self.assertRaises(DataError):replay_history(engine,path,evidence,quotes)

    def test_restart_rejects_semantically_corrupt_pending_or_sma_target(self):
        path,_,_,_=snapshot(self.root,candles(UP))
        for name,statement in [('pending',"UPDATE state SET pending=-1 WHERE id=1"),
                               ('cleared',"UPDATE state SET pending=0,pending_day=NULL,pending_after=NULL WHERE id=1"),
                               ('target',"UPDATE bars SET target=-1 WHERE target=1")]:
            engine=self.engine(name+'.db');self.step(engine,path)
            with engine.db() as db:db.execute(statement)
            with self.subTest(kind=name),self.assertRaises(DataError):self.engine(name+'.db')

    def test_delayed_snapshot_waits_for_refresh_instead_of_trading_archived_prices(self):
        path,_,_,_=snapshot(self.root)
        engine=self.engine()
        self.assertEqual(self.step(engine,path,NOW+timedelta(minutes=10))[0],'WAIT_HISTORY_REFRESH')
        self.assertEqual(engine.summary()['fills'],0)

    def test_continuous_cli_bootstraps_without_snapshot_or_evidence_and_recovers(self):
        current={'now':NOW}
        raw=candles()
        client,requests,_=client_for(raw)
        original=client.transport
        def transport(request,timeout):
            if request.full_url.endswith('/GetCandles'):
                payload=json.loads(request.data);requests.append(('MarketDataService/GetCandles',payload))
                subset=[r for r in raw if instant(payload['from']).date()<=instant(r['time']).date()<=instant(payload['to']).date()]
                return FakeResponse(json.dumps({'candles':subset}).encode())
            if request.full_url.endswith('/GetLastPrices'):
                return FakeResponse(json.dumps({'lastPrices':[dict(instrumentUid=INST.uid,time=iso(current['now']),
                    price={'units':'100','nano':0})]}).encode())
            return original(request,timeout)
        client.transport=transport;client.instrument=lambda uid:INST
        args=['paper','--db',str(self.root/'cli.db'),'--output',str(self.root/'logs'),
              '--commission-rub','.5','--slippage-ticks','1','--funding-per-contract-day-rub','2']
        sleeps=[]
        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps)==2:raise KeyboardInterrupt()
            current['now']+=timedelta(seconds=60)
        with patch('src.gldrubf_bot.cli.ReadOnlyClient',return_value=client), \
             patch('src.gldrubf_bot.cli.datetime',wraps=datetime) as app_clock, \
             patch('src.gldrubf_bot.autonomous.datetime',wraps=datetime) as forward_clock, \
             patch('src.gldrubf_bot.history.datetime',wraps=datetime) as history_clock, \
             patch('src.gldrubf_bot.cli.time.sleep',side_effect=sleep),contextlib.redirect_stdout(io.StringIO()):
            for clock in (app_clock,forward_clock,history_clock):clock.now.side_effect=lambda tz:current['now']
            main(args)
            first=json.loads((self.root/'logs/summary.json').read_text())
            current['now']+=timedelta(seconds=1)
            main(args+['--once'])
        self.assertEqual(first['fills'],1)
        self.assertEqual(json.loads((self.root/'logs/summary.json').read_text())['fills'],1)
        self.assertEqual(sleeps,[60,60])
        self.assertFalse(list((self.root/'logs/history').rglob('evidence_template.json')))
        schedules=[p for m,p in requests if m.endswith('/TradingSchedules')]
        self.assertTrue(all(instant(p['from']).date()==NOW.date() for p in schedules))
