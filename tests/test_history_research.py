"""Offline integration of API snapshots, timing evidence, replay and Forward recovery."""
import contextlib
import io
import json
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.gldrubf_bot.history import download_daily, load_snapshot
from src.gldrubf_bot.research import replay_history, forward_step, refresh_forward, timing_evidence
from src.gldrubf_bot.engine import PaperEngine
from src.gldrubf_bot.cli import main
from src.gldrubf_bot.models import DataError, iso, instant
from src.gldrubf_bot.api import APIError
from tests.helpers import SQLiteResourceTestCase, costs
from tests.history_helpers import INST, START, NOW, candles, client_for, snapshot, evidence_file
from tests.test_api import FakeResponse, make_client
import urllib.error


class HistoryResearchTests(SQLiteResourceTestCase):
    def setUp(self):
        super().setUp()
        self.temp = TemporaryDirectory(prefix='mx history пробел ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def engine(self, name='engine.db'):
        return PaperEngine(self.root/name, INST.uid, costs())

    def test_raw_bytes_hashes_normalization_and_no_calendar_calls(self):
        path, client, requests, bodies = snapshot(self.root)
        manifest, rows = load_snapshot(path)
        self.assertEqual((path/'raw/0000.json').read_bytes(), bodies[0])
        self.assertEqual(len(rows), 53)
        self.assertTrue(all(row['uid']==INST.uid for row in rows))
        self.assertEqual([m for m,_ in requests], ['MarketDataService/GetCandles'])
        self.assertIsNone(client.on_candles)
        report = json.loads((path/'validation.json').read_text())
        self.assertEqual(report['status'], 'valid_with_limits')
        self.assertFalse(report['coverage_confirmed'])
        self.assertFalse(report['trading_dates_verified'])
        self.assertEqual(json.loads((path/'evidence_template.json').read_text())['kind'], 'unresolved')
        self.assertFalse((path/'calendar.json').exists())
        with self.assertRaises(DataError):
            download_daily(client, INST, START, NOW, path, clock=lambda:NOW)
        self.assertEqual((path/'raw/0000.json').read_bytes(), bodies[0])

    def test_missing_dates_are_reported_without_synthetic_candles(self):
        raw = candles(); raw.pop(20)
        path, _, _, _ = snapshot(self.root, raw)
        _, rows = load_snapshot(path)
        report = json.loads((path/'validation.json').read_text())
        self.assertEqual(len(rows), 52)
        self.assertEqual(len(report['gaps']), 1)
        self.assertEqual(report['gaps'][0]['unobserved_calendar_dates'], 1)
        self.assertFalse(report['coverage_confirmed'])

    def test_identical_duplicates_are_counted_but_conflicts_block(self):
        raw = candles(); raw.insert(10, dict(raw[10]))
        path, _, _, _ = snapshot(self.root, raw)
        self.assertEqual(json.loads((path/'validation.json').read_text())['duplicate_records'], 1)
        self.assertEqual(len(load_snapshot(path)[1]), 53)
        raw[10] = dict(raw[10], volume='11')
        with self.assertRaises(DataError):
            snapshot(self.root, raw, 'conflict')
        self.assertTrue((self.root/'conflict/raw/0000.json').exists())
        self.assertEqual(json.loads((self.root/'conflict/validation.json').read_text())['status'], 'failed')

    def test_revisions_and_disappeared_completed_bars_preserve_both_sources(self):
        old, _, _, _ = snapshot(self.root)
        original = (old/'candles.json').read_bytes()
        for kind in ('revision', 'disappeared'):
            raw = candles()
            if kind == 'revision': raw[10]['volume'] = '20'
            else: raw.pop(10)
            with self.assertRaises(DataError):
                snapshot(self.root, raw, kind, previous=old)
            report = json.loads((self.root/kind/'validation.json').read_text())
            self.assertEqual(report['status'], 'blocked_revision')
            self.assertTrue(report['changes'][0]['kind'].startswith('completed_'))
            with self.assertRaises(DataError): load_snapshot(self.root/kind)
        self.assertEqual(original, (old/'candles.json').read_bytes())

    def test_incomplete_transition_is_recorded_and_never_silently_enters_signals(self):
        raw = candles(); raw[-1]['isComplete'] = False
        first, _, _, _ = snapshot(self.root, raw)
        second, _, _, _ = snapshot(self.root, candles(), 'second', previous=first)
        self.assertEqual(json.loads((second/'validation.json').read_text())['changes'][0]['kind'], 'became_complete')
        evidence, _ = evidence_file(self.root, first, kind='verified')
        client, requests, _ = client_for(candles())
        engine = self.engine()
        with self.assertRaises(DataError):
            forward_step(engine, client, INST, second, evidence, clock=lambda:NOW)
        self.assertFalse(requests)
        self.assertEqual(engine.summary()['fills'], 0)

    def test_bad_uid_prices_future_time_and_raw_order_are_rejected(self):
        for index, mutation in enumerate(('uid','ohlc','future','order','completion')):
            raw = candles()
            if mutation=='uid': raw[0]['instrumentUid']='wrong-uid'
            elif mutation=='ohlc': raw[0]['low']={'units':'999','nano':0}
            elif mutation=='future': raw[-1]['time']=iso(NOW+timedelta(days=1))
            elif mutation=='order': raw[0],raw[1]=raw[1],raw[0]
            else: raw[0]['isComplete']='true'
            with self.subTest(mutation=mutation), self.assertRaises(DataError):
                snapshot(self.root, raw, str(index))
            self.assertTrue((self.root/str(index)/'raw/0000.json').exists())
            self.assertEqual(json.loads((self.root/str(index)/'validation.json').read_text())['status'], 'failed')

    def test_snapshot_hash_tampering_is_detected(self):
        path, _, _, _ = snapshot(self.root)
        (path/'raw/0000.json').write_bytes(b'{}')
        with self.assertRaises(DataError): load_snapshot(path)

    def test_empty_protobuf_candles_can_be_archived_but_not_traded(self):
        path, _, _, _ = snapshot(self.root, [])
        evidence, _ = evidence_file(self.root, path)
        self.assertEqual(load_snapshot(path)[1], [])
        with self.assertRaises(DataError): timing_evidence(path, evidence)

    def test_http_errors_are_not_archived_and_capture_errors_are_not_retried(self):
        secret='t.fake-credential-must-not-be-archived'
        failure=urllib.error.HTTPError('https://example.invalid',401,secret,
            {'Content-Type':'application/json'},io.BytesIO(json.dumps({'code':16,'message':secret,'description':40003}).encode()))
        client=make_client(transport=lambda *a,**k: (_ for _ in ()).throw(failure))
        with self.assertRaises(APIError):
            download_daily(client,INST,START,NOW,self.root/'http',clock=lambda:NOW)
        for path in (self.root/'http').rglob('*'):
            if path.is_file(): self.assertNotIn(secret.encode(),path.read_bytes())
        client,requests,_=client_for(candles())
        original_open=Path.open
        def open_file(path,*args,**kwargs):
            if path.name=='0000.json': raise OSError('injected storage failure')
            return original_open(path,*args,**kwargs)
        with patch.object(Path,'open',new=open_file),self.assertRaises(DataError):
            download_daily(client,INST,START,NOW,self.root/'disk',clock=lambda:NOW)
        self.assertEqual(len(requests),1)
        self.assertIsNone(client.on_candles)

    def test_chunk_boundary_duplicate_is_audited_without_extra_candle(self):
        source=candles([100]*101)
        now=START+timedelta(days=102)
        calls=[]
        def transport(request,timeout):
            payload=json.loads(request.data);calls.append(payload)
            rows=[r for r in source if instant(payload['from']).date()<=instant(r['time']).date()<=instant(payload['to']).date()]
            return FakeResponse(json.dumps({'candles':rows}).encode())
        path=download_daily(make_client(transport=transport),INST,START,now,self.root/'chunks',clock=lambda:now)
        self.assertEqual(len(calls),2)
        self.assertEqual(len(load_snapshot(path)[1]),101)
        self.assertEqual(json.loads((path/'validation.json').read_text())['duplicate_records'],1)

    def test_explicit_trading_date_mapping_is_not_replaced_by_utc_label(self):
        path,_,_,_=snapshot(self.root)
        evidence,_=evidence_file(self.root,path)
        data=json.loads(evidence.read_text())
        for row in data['rows']:
            t=instant(row['time'])
            row.update(trade_day=(t+timedelta(days=1)).date().isoformat(),
                       completed_at=iso(t+timedelta(days=1,hours=2)),
                       available_at=iso(t+timedelta(days=1,hours=2,seconds=1)))
        evidence.write_text(json.dumps(data))
        _,_,events,_,_,_=timing_evidence(path,evidence)
        self.assertEqual(events[0][0].day,(START+timedelta(days=1)).date())
        self.assertNotEqual(events[0][0].day,events[0][0].time.date())

    def test_replay_explicit_timing_strict_next_quote_and_restart(self):
        path, _, _, _ = snapshot(self.root)
        evidence, quotes = evidence_file(self.root, path)
        engine = self.engine()
        report = replay_history(engine, path, evidence, quotes)
        before = engine.summary()
        self.assertEqual(before['fills'], 3)
        self.assertTrue(report['completion_and_availability_are_assumptions'])
        self.assertFalse(report['coverage_verified'])
        with engine.db() as db:
            first = db.execute('SELECT time FROM fills ORDER BY id LIMIT 1').fetchone()[0]
        self.assertEqual(first, iso(START+timedelta(days=50,hours=18,seconds=3)))
        restored = self.engine()
        replay_history(restored, path, evidence, quotes)
        self.assertEqual(restored.summary(), before)

    def test_replay_requires_known_timing_uid_quotes_and_windows(self):
        path, _, _, _ = snapshot(self.root)
        evidence, quotes = evidence_file(self.root, path)
        original = json.loads(evidence.read_text())
        for problem in ('unresolved','time','uid','windows','quote_hash','future_availability'):
            data=json.loads(json.dumps(original))
            if problem=='unresolved': data['kind']='unresolved'
            elif problem=='time': data['rows'][0]['completed_at']=data['rows'][0]['time']
            elif problem=='uid': data['quote_uid']='wrong'
            elif problem=='windows': data['execution_windows']=[]
            elif problem=='quote_hash': data['quotes_sha256']='wrong'
            else:
                for row in data['rows']:
                    row['available_at']=iso(instant(row['available_at'])+timedelta(days=1000))
            evidence.write_text(json.dumps(data))
            with self.subTest(problem=problem), self.assertRaises(DataError):
                replay_history(self.engine(problem+'.db'),path,evidence,quotes)
        evidence.write_text(json.dumps(original))

    def test_forward_only_current_schedules_status_and_observed_quote(self):
        path, _, _, _ = snapshot(self.root)
        evidence, _ = evidence_file(self.root,path,kind='verified')
        engine=self.engine()
        client, requests, _=client_for(candles(),quote_time=NOW)
        self.assertEqual(forward_step(engine,client,INST,path,evidence,clock=lambda:NOW),'NO_FILL')
        later=NOW+timedelta(seconds=1)
        refreshed=refresh_forward(client,INST,path,self.root/'refresh',clock=lambda:later)
        client2, requests2, _=client_for(candles(),quote_time=later)
        self.assertEqual(forward_step(engine,client2,INST,refreshed,evidence,clock=lambda:later),'PAPER_FILL')
        before=engine.summary()
        restored=self.engine()
        self.assertEqual(forward_step(restored,client2,INST,refreshed,evidence,clock=lambda:later),'NO_FILL')
        self.assertEqual(restored.summary(),before)
        schedules=[p for m,p in requests+requests2 if m.endswith('/TradingSchedules')]
        self.assertTrue(schedules)
        self.assertTrue(all(instant(p['from']).date()==NOW.date() for p in schedules))
        self.assertTrue(any(m.endswith('/GetTradingStatus') for m,_ in requests))

    def test_forward_refuses_assumptions_stale_or_incomplete_coverage(self):
        path, _, _, _=snapshot(self.root)
        evidence,_=evidence_file(self.root,path,kind='verified')
        original=json.loads(evidence.read_text())
        for problem in ('assumption','coverage','stale','future_coverage'):
            data=json.loads(json.dumps(original))
            if problem=='assumption': data['kind']='historical_assumption'
            elif problem=='coverage': data['expected_trade_days'].pop(10)
            elif problem=='stale': data['coverage_through']='2025-01-01'
            else: data['coverage_through']='2099-01-01'
            evidence.write_text(json.dumps(data))
            client,requests,_=client_for(candles())
            with self.subTest(problem=problem), self.assertRaises(DataError):
                forward_step(self.engine(problem+'.db'),client,INST,path,evidence,clock=lambda:NOW)
            self.assertFalse(any(m.endswith('/GetLastPrices') for m,_ in requests))

    def test_forward_persists_gate_before_virtual_fill_and_stops_on_audit_failure(self):
        path,_,_,_=snapshot(self.root)
        evidence,_=evidence_file(self.root,path,kind='verified')
        client,_,_=client_for(candles(),quote_time=NOW)
        engine=self.engine()
        checks=[]
        forward_step(engine,client,INST,path,evidence,clock=lambda:NOW,audit=checks.append)
        self.assertTrue(checks[0]['status_gate_passed'])
        self.assertEqual(checks[0]['calendar'][0]['day'],NOW.date().isoformat())
        later=NOW+timedelta(seconds=1)
        client,_,_=client_for(candles(),quote_time=later)
        def fail_audit(payload):
            raise OSError('injected audit storage failure')
        with self.assertRaises(OSError):
            forward_step(engine,client,INST,path,evidence,clock=lambda:later,audit=fail_audit)
        self.assertEqual(engine.summary()['fills'],0)

    def test_forward_blocks_closed_current_market_and_unavailable_status(self):
        path,_,_,_=snapshot(self.root)
        evidence,_=evidence_file(self.root,path,kind='verified')
        client,requests,_=client_for(candles(),trading=False)
        self.assertEqual(forward_step(self.engine(),client,INST,path,evidence,clock=lambda:NOW),'WAIT_MARKET')
        self.assertFalse(any(m.endswith('/GetLastPrices') for m,_ in requests))
        # Same current day after official close: missing complete day cannot be ignored.
        after_close=NOW.replace(hour=19)
        client,requests,_=client_for(candles(),quote_time=after_close)
        with self.assertRaises(DataError):
            forward_step(self.engine('closed.db'),client,INST,path,evidence,clock=lambda:after_close)
        self.assertFalse(any(m.endswith('/GetLastPrices') for m,_ in requests))

    def test_sqlite_rejects_mode_mixing_and_accepted_evidence_revision(self):
        path,_,_,_=snapshot(self.root)
        replay_evidence,quotes=evidence_file(self.root,path)
        replay_history(self.engine(),path,replay_evidence,quotes)
        forward_evidence,_=evidence_file(self.root,path,kind='verified',name='forward.json')
        client,_,_=client_for(candles())
        with self.assertRaises(DataError):
            forward_step(self.engine(),client,INST,path,forward_evidence,clock=lambda:NOW)
        engine=self.engine('forward.db')
        forward_step(engine,client,INST,path,forward_evidence,clock=lambda:NOW)
        before=engine.summary()
        data=json.loads(forward_evidence.read_text());data['source']='CHANGED PROVENANCE'
        forward_evidence.write_text(json.dumps(data))
        with self.assertRaises(DataError):
            forward_step(self.engine('forward.db'),client,INST,path,forward_evidence,clock=lambda:NOW)
        self.assertEqual(self.engine('forward.db').summary(),before)

    def test_historical_replay_cli_uses_snapshot_and_assumptions(self):
        path,_,_,_=snapshot(self.root)
        evidence,quotes=evidence_file(self.root,path)
        args=['historical-replay','--history',str(path),'--evidence',str(evidence),'--quotes',str(quotes),
              '--db',str(self.root/'cli.db'),'--output',str(self.root/'logs'),'--point-value-rub','2',
              '--tick-size','1','--commission-rub','.5','--slippage-ticks','1','--funding-per-contract-day-rub','2']
        with contextlib.redirect_stdout(io.StringIO()):
            main(args);main(args)
        result=json.loads((self.root/'logs/summary.json').read_text())
        self.assertEqual(result['fills'],3)
        self.assertTrue((self.root/'logs/research.json').is_file())
        original=(path/'candles.json').read_bytes()
        bad=list(args);bad[bad.index('--output')+1]=str(path)
        with self.assertRaises(SystemExit): main(bad)
        self.assertEqual((path/'candles.json').read_bytes(),original)

    def test_forward_cli_mock_api_and_sqlite_restart(self):
        path,_,_,_=snapshot(self.root)
        evidence,_=evidence_file(self.root,path,kind='verified')
        args=['paper','--history',str(path),'--evidence',str(evidence),'--db',str(self.root/'cli.db'),
              '--output',str(self.root/'logs'),'--once','--commission-rub','.5','--slippage-ticks','1',
              '--funding-per-contract-day-rub','2']
        client,requests,_=client_for(candles(),quote_time=NOW)
        client.instrument=lambda uid: INST
        with patch('src.gldrubf_bot.cli.ReadOnlyClient',return_value=client), \
             patch('src.gldrubf_bot.cli.datetime',wraps=datetime) as clock, \
             patch('src.gldrubf_bot.research.datetime',wraps=datetime) as research_clock, \
             patch('src.gldrubf_bot.history.datetime',wraps=datetime) as history_clock, \
             contextlib.redirect_stdout(io.StringIO()):
            clock.now.return_value=NOW;research_clock.now.return_value=NOW;history_clock.now.return_value=NOW
            main(args)
            first=json.loads((self.root/'logs/summary.json').read_text())
            later=NOW+timedelta(seconds=1)
            # New output name and observation time; transport returns the same already observed quote.
            clock.now.return_value=later;research_clock.now.return_value=later;history_clock.now.return_value=later
            main(args)
        self.assertEqual(json.loads((self.root/'logs/summary.json').read_text())['fills'],first['fills'])
        self.assertTrue(all(instant(p['from']).date()==NOW.date() for m,p in requests if m.endswith('/TradingSchedules')))
