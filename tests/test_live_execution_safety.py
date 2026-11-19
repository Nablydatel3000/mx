"""Offline regression of execution scope, final gates and durable no-retry state."""
from copy import deepcopy
from datetime import timedelta
import io
import contextlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from src.gldrubf_bot import live
from src.gldrubf_bot.api import ReadOnlyClient
from src.gldrubf_bot.live_api import Broker,UID
from src.gldrubf_bot.live_errors import DefinitelyNotSent
from src.gldrubf_bot.live_market import LiveReadClient,select_sessions,execution_request,limit_status
from src.gldrubf_bot.live_store import Store
from src.gldrubf_bot.models import DataError,instant,iso
from src.gldrubf_bot.operations import atomic_json
from tests.helpers import SQLiteResourceTestCase,UP
from tests.history_helpers import NOW,START
from tests.test_api import FakeResponse,day
from tests.test_holiday_phase_evidence import reported_fragment_day,CODE
from tests.test_live import FakeBroker,config,rows
from tests import test_live as fixtures


class ScopeTests(unittest.TestCase):
    def response(self,days):return dict(exchanges=[dict(exchange=CODE.upper(),days=days)])
    def select(self,days,now='2026-11-19T10:00:00Z'):
        return select_sessions(self.response(days),CODE,instant(now))

    def test_future_holiday_is_excluded_but_today_holiday_stays_blocked(self):
        result,evidence=self.select([day('2026-11-19'),reported_fragment_day()])
        self.assertTrue(any(d.contains(instant('2026-11-19T10:00:00Z')) for d in result.values()))
        self.assertEqual(evidence['excluded_record_indexes'],[1])
        with self.assertRaises(DataError):self.select([reported_fragment_day()],'2026-11-21T10:00:00Z')

    def test_future_label_with_current_bounds_is_not_excluded(self):
        unknown=reported_fragment_day()
        for item in unknown['intervals']:
            for key,value in item['interval'].items():item['interval'][key]=value.replace('2026-11-21','2026-11-19')
        unknown['startTime']='2026-11-19T07:00:00Z';unknown['endTime']='2026-11-19T16:00:00Z'
        with self.assertRaises(DataError):self.select([day('2026-11-19'),unknown])

    def test_future_missing_bound_or_contradiction_cannot_be_called_irrelevant(self):
        for mutate in (lambda d:d['intervals'][0]['interval'].pop('endTs'),
                       lambda d:d['intervals'][0]['interval'].update(endTs='2026-11-20T01:00:00Z'),
                       lambda d:d.pop('endTime'),lambda d:d.pop('intervals')):
            unknown=reported_fragment_day();mutate(unknown)
            with self.subTest(mutate=mutate),self.assertRaises((DataError,KeyError)):
                self.select([day('2026-11-19'),unknown])

    def test_no_coverage_does_not_create_permission(self):
        with self.assertRaises(DataError):self.select([reported_fragment_day()])
        with self.assertRaises(DataError):self.select([])

    def test_cross_midnight_window_uses_absolute_bounds_not_label(self):
        d=day('2026-11-18');a,b='2026-11-18T23:50:00Z','2026-11-19T00:30:00Z'
        d.update(startTime=a,endTime=b);d['intervals'][0]['interval'].update(startTs=a,endTs=b)
        result,_=self.select([d],'2026-11-19T00:05:00Z')
        self.assertTrue(next(iter(result.values())).contains(instant('2026-11-19T00:05:00Z')))

    def test_moscow_midnight_does_not_use_a_future_session_early(self):
        future=reported_fragment_day()
        result,_=self.select([day('2026-11-19'),day('2026-11-20'),future],'2026-11-19T21:01:00Z')
        self.assertFalse(any(d.contains(instant('2026-11-19T21:01:00Z')) for d in result.values()))
        request=execution_request(CODE,instant('2026-11-19T21:01:00Z'))
        self.assertEqual(request['from'],'2026-11-19T00:00:00.000000Z')
        self.assertEqual(request['to'],'2026-11-20T21:00:00.000000Z')

    def test_utc_day_request_never_requests_historical_date(self):
        request=execution_request(CODE,instant('2026-11-19T10:00:00Z'))
        self.assertEqual(request['to'],'2026-11-20T00:00:00.000000Z')
        self.assertEqual(request['exchange'],CODE)

    def test_selection_permission_cannot_cross_msk_midnight_into_excluded_phase(self):
        current=day('2026-11-19');a,b='2026-11-19T20:00:00Z','2026-11-19T22:00:00Z'
        current.update(startTime=a,endTime=b);current['intervals'][0]['interval'].update(startTs=a,endTs=b)
        future=day('2026-11-20');a='2026-11-19T21:00:00Z'
        future.update(startTime=a,endTime=b);future['intervals'][0].update(type='unknown_phase',interval=dict(startTs=a,endTs=b))
        calendar,evidence=self.select([current,future],'2026-11-19T20:59:59Z')
        self.assertEqual(evidence['excluded_record_indexes'],[1])
        self.assertFalse(any(d.contains(instant('2026-11-19T21:00:00Z')) for d in calendar.values()))
        with self.assertRaises(DataError):self.select([current,future],'2026-11-19T21:00:00Z')

    def test_paper_retains_original_global_parser_and_market_gate(self):
        self.assertIsNot(LiveReadClient.current_sessions,ReadOnlyClient.current_sessions)
        self.assertIsNot(LiveReadClient.quote,ReadOnlyClient.quote)

    def test_current_legacy_clearing_and_auction_are_excluded(self):
        for start,end in (('clearingStartTime','clearingEndTime'),('openingAuctionStartTime','openingAuctionEndTime')):
            d=day('2026-11-19');d.update({start:'2026-11-19T10:00:00Z',end:'2026-11-19T10:05:00Z'})
            calendar,_=self.select([d]);parsed=next(iter(calendar.values()))
            self.assertFalse(parsed.contains(instant('2026-11-19T10:00:00Z')))
            self.assertTrue(parsed.contains(instant('2026-11-19T10:05:00Z')))

    def test_distinct_labels_cannot_authorize_overlapping_windows(self):
        first=day('2026-11-19');second=deepcopy(first);second['date']='2026-11-20T00:00:00Z'
        with self.assertRaises(DataError):self.select([first,second])


class QuoteAndTransportTests(unittest.TestCase):
    def test_live_limit_quote_does_not_require_market_availability(self):
        status=dict(instrumentUid=UID,tradingStatus='SECURITY_TRADING_STATUS_NORMAL_TRADING',apiTradeAvailableFlag=True,limitOrderAvailableFlag=True,marketOrderAvailableFlag=False)
        client=LiveReadClient.__new__(LiveReadClient)
        client.call=Mock(side_effect=[status,dict(lastPrices=[dict(instrumentUid=UID,time=iso(NOW),price=dict(units='100'),lastPriceType='LAST_PRICE_EXCHANGE')])])
        self.assertEqual(client.quote(SimpleNamespace(uid=UID),NOW).price,100)

    def test_status_flags_and_instrument_are_not_truthy_strings(self):
        status=dict(instrumentUid=UID,tradingStatus='SECURITY_TRADING_STATUS_NORMAL_TRADING',apiTradeAvailableFlag=True,limitOrderAvailableFlag=True)
        for field,value in (('instrumentUid','foreign'),('limitOrderAvailableFlag','true'),('apiTradeAvailableFlag',1),('onlyBestPrice',True),('tradingStatus','SECURITY_TRADING_STATUS_OPENING_AUCTION')):
            bad=dict(status,**{field:value})
            with self.subTest(field=field):
                try:allowed=limit_status(bad,UID)
                except DataError:allowed=False
                self.assertFalse(allowed)

    def test_invalid_or_future_quotes_remain_rejected(self):
        status=dict(instrumentUid=UID,tradingStatus='SECURITY_TRADING_STATUS_NORMAL_TRADING',apiTradeAvailableFlag=True,limitOrderAvailableFlag=True)
        for change in (dict(time=iso(NOW+timedelta(seconds=1))),dict(lastPriceType='LAST_PRICE_DEALER'),dict(price=dict(units='0'))):
            quote=dict(instrumentUid=UID,time=iso(NOW),price=dict(units='100'),lastPriceType='LAST_PRICE_EXCHANGE');quote.update(change)
            client=LiveReadClient.__new__(LiveReadClient);client.call=Mock(side_effect=[status,dict(lastPrices=[quote])])
            with self.assertRaises(DataError):client.quote(SimpleNamespace(uid=UID),NOW)

    def test_guard_failure_is_proved_before_transport_entry(self):
        transport=Mock()
        with patch.dict(os.environ,{'T_INVEST_TOKEN':'synthetic'}):
            broker=Broker('12345678',transport=transport,write_guard=Mock(side_effect=DataError('refused')))
        with self.assertRaises(DefinitelyNotSent):broker.post(dict(accountId='12345678'))
        transport.assert_not_called()

    def test_same_exception_from_inside_transport_is_not_a_no_send_certificate(self):
        transport=Mock(side_effect=DefinitelyNotSent('transport entered'))
        with patch.dict(os.environ,{'T_INVEST_TOKEN':'synthetic'}):broker=Broker('12345678',transport=transport,write_guard=Mock())
        with self.assertRaises(live.APIError):broker.post(dict(accountId='12345678'))
        transport.assert_called_once()


class ExecutionSafetyTests(SQLiteResourceTestCase):
    signal=fixtures.LiveEngineTests.signal
    restore=fixtures.LiveEngineTests.restore
    open_two=fixtures.LiveEngineTests.open_two

    def setUp(self):
        super().setUp();self.tmp=TemporaryDirectory(prefix='execution safety ');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.now=NOW;self.c=config();self.clock=lambda:self.now
        self.store=Store(self.root/'live.sqlite3',live.fingerprint(self.c),START,self.now)
        self.controls=live.Controls(self.root,self.store,self.c,True,self.clock)
        atomic_json(self.root/'LIVE_PERMISSION.json',dict(ack=live.ACK,config_hash=live.fingerprint(self.c),ledger_id=self.store.meta()['ledger_id'],issued_at=iso(NOW-timedelta(seconds=1)),expires_at=iso(NOW+timedelta(hours=1))))
        self.broker=FakeBroker(self.clock);self.broker.guard=self.controls.guard
        self.engine=live.Engine(self.store,self.broker,self.controls,self.clock)
        self.warm=UP[:50]+[UP[49]];self.store.ingest(rows(self.warm,self.now),self.now)

    def test_status_changes_during_financial_reads_prevents_any_intent(self):
        self.signal();call=self.broker.call;stopped=[False]
        def capacity(side):stopped[0]=True;return 2
        self.broker.capacity=capacity
        def status(method,payload):
            result=call(method,payload)
            if method.endswith('GetTradingStatus') and stopped[0]:result['limitOrderAvailableFlag']=False
            return result
        self.broker.call=status
        self.assertEqual(self.engine.step(),'WAIT_MARKET');self.assertEqual(self.store.orders(),[])
        self.assertFalse(self.broker.posts);self.assertIsNotNone(self.store.signal())

    def test_final_status_api_failure_never_creates_intent(self):
        self.signal();call=self.broker.call;count=[0]
        def failed(method,payload):
            if method.endswith('GetTradingStatus'):
                count[0]+=1
                if count[0]==2:raise live.APIError('offline fixture outage')
            return call(method,payload)
        self.broker.call=failed
        with self.assertRaises(live.APIError):self.engine.step()
        self.assertEqual(self.store.orders(),[]);self.assertFalse(self.broker.posts)

    def test_final_status_uid_and_modes_are_checked_after_financial_reads(self):
        self.signal();call=self.broker.call;financial=[False]
        def capacity(side):financial[0]=True;return 2
        self.broker.capacity=capacity
        for change in (dict(instrumentUid='foreign'),dict(tradingStatus='SECURITY_TRADING_STATUS_OPENING_AUCTION'),
                       dict(apiTradeAvailableFlag=False),dict(onlyBestPrice=True),dict(limitOrderAvailableFlag='true')):
            financial[0]=False
            def changed(method,payload):
                r=call(method,payload)
                if financial[0] and method.endswith('GetTradingStatus'):r.update(change)
                return r
            self.broker.call=changed
            try:self.assertEqual(self.engine.step(),'WAIT_MARKET')
            except DataError:pass
            self.assertEqual(self.store.orders(),[]);self.assertFalse(self.broker.posts)

    def test_final_calendar_outage_preserves_pending_across_restart(self):
        self.signal();pending=self.store.signal();original=self.broker.read.current_sessions.side_effect;count=[0]
        def unavailable(*args):
            count[0]+=1
            if count[0]==2:raise live.APIError('calendar unavailable')
            return original(*args)
        self.broker.read.current_sessions.side_effect=unavailable
        with self.assertRaises(live.APIError):self.engine.step()
        self.restore();self.assertEqual(self.store.signal(),pending);self.assertEqual(self.store.orders(),[])

    def test_final_calendar_revision_removes_window_before_intent(self):
        self.signal();original=self.broker.read.current_sessions.side_effect;count=[0]
        def changed(*args):
            count[0]+=1
            return original(*args) if count[0]==1 else {}
        self.broker.read.current_sessions.side_effect=changed
        self.assertEqual(self.engine.step(),'WAIT_REFRESH');self.assertEqual(self.store.orders(),[])

    def test_not_sent_is_durable_and_cannot_be_repeated_after_restart(self):
        self.signal();transport=Mock()
        with patch.dict(os.environ,{'T_INVEST_TOKEN':'synthetic'}):adapter=Broker('12345678',transport=transport,write_guard=self.controls.guard)
        self.broker.post=adapter.post;intent=self.store.intent
        def kill_after_intent(*args):intent(*args);(self.root/'STOP').write_text('test stop')
        with patch.object(self.store,'intent',side_effect=kill_after_intent):self.assertEqual(self.engine.step(),'HALTED')
        transport.assert_not_called();order=self.store.orders()[0]
        self.assertEqual(order['status'],'NOT_SENT');self.assertEqual(self.store.position(),0)
        self.restore();self.assertEqual(self.engine.step(),'HALTED')
        self.assertTrue((self.root/'STOP').exists());self.assertEqual(len(self.store.orders()),1)
        self.assertEqual(self.store.orders(active=True),[]);transport.assert_not_called()

    def test_delayed_local_persistence_expires_gate_without_http_or_retry(self):
        self.signal();transport=Mock()
        with patch.dict(os.environ,{'T_INVEST_TOKEN':'synthetic'}):adapter=Broker('12345678',transport=transport,write_guard=self.controls.guard)
        self.broker.post=adapter.post;intent=self.store.intent
        def delayed(*args):intent(*args);self.now+=timedelta(seconds=121)
        with patch.object(self.store,'intent',side_effect=delayed):self.assertEqual(self.engine.step(),'HALTED')
        self.assertEqual(self.store.orders()[0]['status'],'NOT_SENT');transport.assert_not_called()
        self.restore();self.assertEqual(self.engine.step(),'HALTED');transport.assert_not_called()

    def test_total_cycle_budget_includes_preflight_and_persistence(self):
        self.signal();transport=Mock()
        with patch.dict(os.environ,{'T_INVEST_TOKEN':'synthetic'}):adapter=Broker('12345678',transport=transport,write_guard=self.controls.guard)
        self.broker.post=adapter.post;intent=self.store.intent
        def capacity(side):self.now+=timedelta(seconds=20);return 2
        def persisted(*args):intent(*args);self.now+=timedelta(seconds=20)
        self.broker.capacity=capacity
        with patch.object(self.store,'intent',side_effect=persisted):self.assertEqual(self.engine.step(),'HALTED')
        self.assertEqual(self.store.orders()[0]['status'],'NOT_SENT');transport.assert_not_called()

    def test_transport_timeout_is_unknown_and_never_repeated(self):
        self.signal();self.broker.timeout=True
        with self.assertRaises(live.APIError):self.engine.step()
        request=self.store.orders()[0]['request_id'];self.restore()
        self.assertEqual(self.engine.step(),'HALTED');self.assertEqual(self.store.orders()[0]['request_id'],request)
        self.assertEqual(len(self.broker.posts),1);self.assertEqual(self.store.orders()[0]['status'],'UNKNOWN')

    def test_not_sent_with_broker_service_charge_is_rejected_on_restore(self):
        self.signal();self.broker.post=Mock(side_effect=DefinitelyNotSent('fixture certificate'))
        self.assertEqual(self.engine.step(),'HALTED')
        with self.store.db() as db:db.execute("UPDATE live_orders SET service_commission='1' WHERE status='NOT_SENT'")
        with self.assertRaisesRegex(DataError,'NOT_SENT has broker activity'):self.restore()

    def test_partial_and_pending_survive_restart_without_new_uuid(self):
        self.signal();self.engine.step();self.broker.fill(1);self.engine.reconcile()
        request=self.store.orders()[0]['request_id'];self.restore()
        self.assertEqual(self.store.position(),1);self.assertEqual(self.engine.step(),'WAIT_ORDER')
        self.assertEqual(len(self.broker.posts),1);self.assertEqual(self.store.orders()[0]['request_id'],request)
        self.broker.fill(2);self.restore();self.assertEqual(self.engine.step(),'TARGET_CONFIRMED')
        self.assertEqual(len(self.broker.posts),1)

    def test_wrong_fingerprint_is_refused_without_rewriting_database_or_markers(self):
        before=self.store.path.read_bytes();marker=self.store.path.with_suffix('.highwater.json').read_bytes()
        with self.assertRaisesRegex(DataError,'version mismatch'):Store(self.store.path,'old-version',START,self.now)
        self.assertEqual(before,self.store.path.read_bytes());self.assertEqual(marker,self.store.path.with_suffix('.highwater.json').read_bytes())

    def test_status_refresh_with_different_fingerprint_never_reads_token_or_api(self):
        cfg=self.root/'config.json';cfg.write_text(json.dumps(self.c))
        atomic_json(self.root/'LEDGER_INITIALIZED.json',dict(ledger_id=self.store.meta()['ledger_id']))
        before=self.store.path.read_bytes()
        with patch.object(live,'fingerprint',return_value='different-version'),patch.object(live,'Broker') as broker,patch.object(live,'secret') as secret,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(live.main(['status','--refresh','--config',str(cfg),'--state',str(self.root)]),78)
            broker.assert_not_called();secret.assert_not_called()
        self.assertEqual(before,self.store.path.read_bytes())

    def test_unknown_existing_file_is_never_initialized(self):
        target=self.root/'unknown.db';target.write_bytes(b'unknown data');before=target.read_bytes()
        with self.assertRaises(DataError):Store(target,live.fingerprint(self.c),START,self.now)
        self.assertEqual(before,target.read_bytes())

    def test_new_offline_init_handles_lock_created_empty_file_without_api(self):
        state=self.root/'new';locks=self.root/'locks';locks.mkdir();cfg=self.root/'config.json';cfg.write_text(json.dumps(self.c))
        with patch.object(live,'ACCOUNT_LOCK_ROOT',locks),patch.object(live,'Broker') as broker,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(live.main(['init','--config',str(cfg),'--state',str(state)]),0)
            broker.assert_not_called()
        self.assertFalse((state/'LIVE_PERMISSION.json').exists())

    def test_empty_existing_state_is_not_automatically_adopted(self):
        target=self.root/'empty.db';target.touch()
        with self.assertRaises(DataError):Store(target,live.fingerprint(self.c),START,self.now)
        self.assertEqual(target.stat().st_size,0)
