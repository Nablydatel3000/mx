"""MOCK broker only. No credentials/network/orders or real market data required."""
import contextlib
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal as D
import io
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
import urllib.error
from unittest.mock import Mock,patch

from src.gldrubf_bot import live
from src.gldrubf_bot.live_api import UID,Broker,quoted
from src.gldrubf_bot.live_store import Store
from src.gldrubf_bot.api import APIError,ReadOnlyClient,Instrument,TradingDay
from src.gldrubf_bot.models import DataError,Quote,iso
from src.gldrubf_bot.operations import atomic_json
from src.gldrubf_bot.locking import runner_lock
from tests.helpers import SQLiteResourceTestCase,UP
from tests.history_helpers import NOW,START
from tests.test_api import FakeResponse


def config():
    return dict(account_id='12345678',capital_rub='15000',contracts=2,instrument_uid=UID,
        max_complete_age_hours=168,quote_max_age_seconds=120,snapshot_max_age_seconds=30,
        margin_stress_multiplier='2',cash_reserve_rub='1000',max_order_fee_rub='20',
        max_slippage_ticks=2,order_ttl_seconds=120,poll_seconds=60,min_session_remaining_seconds=30)


def rows(prices,now):
    return [dict(uid=UID,time=iso(START+timedelta(days=i)),provider_day=(START+timedelta(days=i)).date().isoformat(),
        open=str(p),high=str(p),low=str(p),close=str(p),volume=10,is_complete=True,observed_at=iso(now)) for i,p in enumerate(prices)]


class FakeBroker:
    def __init__(self,clock):
        self.clock=clock;self.positions=0;self.blocked=0;self.cash=D(15000);self.registry={};self.posts=[];self.cancels=[]
        self.reject=False;self.timeout=False;self.unknown_orders=[];self.maxlots=2;self.fee_value=D(10)
        self.read=Mock();self.read.instrument.return_value=Instrument(UID,'fixture','GLDRUBF','FIXTURE',D('.1'))
        self.read.current_sessions.side_effect=lambda *args:{NOW.date():TradingDay(NOW.date(),True,((NOW-timedelta(hours=1),NOW+timedelta(hours=1)),),NOW+timedelta(hours=1))}
        self.read.quote.side_effect=lambda *args:Quote(self.clock(),D(100))
        self.guard=None;self.position_reads=0
    def accounts(self):pass
    def account(self):
        self.position_reads+=1
        return dict(position=self.positions,blocked=self.blocked,balance_rub=str(self.cash),liquid_portfolio_rub=str(self.cash),
            portfolio_value_rub=str(self.cash),available_conservative_rub=str(self.cash),margin_deficit=False)
    def orders(self):
        return [deepcopy(r) for r in self.registry.values() if r['executionReportStatus'] in ('EXECUTION_REPORT_STATUS_NEW','EXECUTION_REPORT_STATUS_PARTIALLYFILL')]+self.unknown_orders
    def order(self,request_id):
        if request_id not in self.registry:raise APIError('synthetic request not found')
        return deepcopy(self.registry[request_id])
    def post(self,payload):
        if self.guard:self.guard('OrdersService/PostOrder',payload)
        self.posts.append(deepcopy(payload));key=payload['orderId']
        r=dict(orderId='broker-'+key,orderRequestId=key,instrumentUid=UID,direction=payload['direction'],orderType='ORDER_TYPE_LIMIT',
            executionReportStatus='EXECUTION_REPORT_STATUS_REJECTED' if self.reject else 'EXECUTION_REPORT_STATUS_NEW',
            lotsRequested=payload['quantity'],lotsExecuted='0',stages=[],executedCommission=dict(currency='rub',units='0',nano=0))
        self.registry[key]=r
        if self.timeout:raise APIError('synthetic timeout after acceptance')
        return deepcopy(r)
    def fill(self,n,status=None):
        key=self.posts[-1]['orderId'];r=self.registry[key]
        direction=1 if r['direction']=='ORDER_DIRECTION_BUY' else -1
        self.positions+=direction*(n-int(r['lotsExecuted']))
        r['lotsExecuted']=str(n)
        r['executionReportStatus']=status or ('EXECUTION_REPORT_STATUS_FILL' if n==int(r['lotsRequested']) else 'EXECUTION_REPORT_STATUS_PARTIALLYFILL')
        r['stages']+= [dict(tradeId=key+'-trade-'+str(i),quantity='1',price=dict(currency='rub',units='100',nano=0),executionTime=iso(self.clock())) for i in range(len(r['stages']),n)]
        r['executedCommission']=dict(currency='rub',units=str(n),nano=0)
    def cancel(self,key):
        if self.guard:self.guard('OrdersService/CancelOrder',dict(accountId='12345678',orderId=key,orderIdType='ORDER_ID_TYPE_REQUEST'))
        self.cancels.append(key);self.registry[key]['executionReportStatus']='EXECUTION_REPORT_STATUS_CANCELLED'
        return dict(time=iso(self.clock()))
    def capacity(self,side):return self.maxlots
    def fee(self,*args):return self.fee_value
    def margin(self,instrument):return dict(rub_per_point='1',long_rub='1400',short_rub='1400')
    def call(self,method,payload):
        if method.endswith('GetTradingStatus'):
            return dict(instrumentUid=UID,tradingStatus='SECURITY_TRADING_STATUS_NORMAL_TRADING',limitOrderAvailableFlag=True,apiTradeAvailableFlag=True)
        if method.endswith('GetOrderBook'):
            return dict(instrumentUid=UID,orderbookTs=iso(self.clock()),asks=[dict(price=quoted(D('100.1')),quantity='2')],
                bids=[dict(price=quoted(D('99.9')),quantity='2')],limitDown=quoted(50),limitUp=quoted(150))
        raise AssertionError(method)


class LiveEngineTests(SQLiteResourceTestCase):
    def setUp(self):
        super().setUp();self.tmp=TemporaryDirectory(prefix='mx mock live ');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.now=NOW;self.c=config();self.clock=lambda:self.now
        self.store=Store(self.root/'live.sqlite3',live.fingerprint(self.c),START,self.now)
        self.controls=live.Controls(self.root,self.store,self.c,True,self.clock)
        atomic_json(self.root/'LIVE_PERMISSION.json',dict(ack=live.ACK,config_hash=live.fingerprint(self.c),ledger_id=self.store.meta()['ledger_id'],issued_at=iso(NOW-timedelta(seconds=1)),expires_at=iso(NOW+timedelta(hours=1))))
        self.broker=FakeBroker(self.clock);self.broker.guard=self.controls.guard
        self.engine=live.Engine(self.store,self.broker,self.controls,self.clock)
        self.warm=UP[:50]+[UP[49]]
        self.store.ingest(rows(self.warm,self.now),self.now)
    def signal(self,prices=None):
        self.now+=timedelta(seconds=1);self.prices=prices or self.warm+[UP[-1]]
        self.store.ingest(rows(self.prices,self.now),self.now);self.now+=timedelta(seconds=1)
    def open_two(self):
        self.signal();self.assertEqual(self.engine.step(),'ORDER_SENT_OPEN')
        self.now+=timedelta(seconds=1);self.broker.fill(2)
        self.assertEqual(self.engine.step(),'TARGET_CONFIRMED')
    def restore(self):
        self.store=Store(self.root/'live.sqlite3',live.fingerprint(self.c),START,self.now)
        self.controls.store=self.store;self.engine=live.Engine(self.store,self.broker,self.controls,self.clock)

    def test_bootstrap_does_not_send_historical_order(self):
        self.assertEqual(self.engine.step(),'NO_SIGNAL');self.assertFalse(self.broker.posts)
    def test_two_contract_order_has_limit_bound_and_independent_gates(self):
        self.signal();self.assertEqual(self.engine.step(),'ORDER_SENT_OPEN')
        payload=self.broker.posts[0]
        self.assertEqual(payload['quantity'],'2');self.assertEqual(payload['instrumentId'],UID)
        self.assertEqual(payload['orderType'],'ORDER_TYPE_LIMIT');self.assertIs(payload['confirmMarginTrade'],False)
        self.assertEqual(payload['timeInForce'],'TIME_IN_FORCE_FILL_AND_KILL')
        self.assertGreaterEqual(self.broker.position_reads,2)
    def test_partial_fill_restart_finishes_same_order_without_duplicate(self):
        self.signal();self.engine.step();key=self.broker.posts[0]['orderId']
        self.now+=timedelta(seconds=1);self.broker.fill(1)
        self.assertEqual(self.engine.step(),'WAIT_ORDER');self.assertEqual(self.store.position(),1)
        self.restore();self.assertEqual(self.engine.step(),'WAIT_ORDER')
        self.now+=timedelta(seconds=1);self.broker.fill(2)
        self.assertEqual(self.engine.step(),'TARGET_CONFIRMED')
        self.assertEqual(len(self.broker.posts),1);self.assertEqual(self.store.orders()[0]['request_id'],key)
    def test_reversal_waits_for_full_close_and_broker_flat(self):
        self.open_two();self.signal(self.prices+[1,1])
        self.assertEqual(self.engine.step(),'ORDER_SENT_CLOSE');self.assertEqual(self.broker.posts[-1]['quantity'],'2')
        self.now+=timedelta(seconds=1);self.broker.fill(1)
        self.assertEqual(self.engine.step(),'WAIT_ORDER');self.assertEqual(len(self.broker.posts),2)
        self.now+=timedelta(seconds=1);self.broker.fill(2)
        self.assertEqual(self.engine.step(),'ORDER_SENT_OPEN')
        self.assertEqual(self.broker.posts[-1]['direction'],'ORDER_DIRECTION_SELL')
        self.assertEqual(self.store.orders()[-1]['before_pos'],0)
    def test_repeated_signal_and_filled_restart_no_new_order(self):
        self.open_two();self.restore();self.store.ingest(rows(self.prices,self.now),self.now)
        self.assertEqual(self.engine.step(),'NO_SIGNAL');self.assertEqual(len(self.broker.posts),1)
    def test_timeout_after_acceptance_is_never_automatically_reposted(self):
        self.signal();self.broker.timeout=True
        with self.assertRaises(APIError):self.engine.step()
        self.assertEqual(self.store.orders()[0]['status'],'UNKNOWN');self.restore()
        self.assertEqual(self.engine.step(),'HALTED');self.assertEqual(len(self.broker.posts),1)
        self.assertEqual(self.engine.reconcile()['position'],0)
        self.assertEqual(self.store.orders()[0]['status'],'NEW')
    def test_crash_after_persist_before_http_does_not_resend_unknown_uuid(self):
        self.signal();self.broker.post=Mock(side_effect=SystemExit(7))
        with self.assertRaises(SystemExit):self.engine.step()
        self.restore();self.assertEqual(self.store.orders()[0]['status'],'SENDING')
        with self.assertRaises(APIError):self.engine.reconcile()
        self.assertFalse(self.broker.posts)
    def test_rejection_halts_without_retry(self):
        self.signal();self.broker.reject=True;self.engine.step()
        self.assertEqual(self.engine.step(),'HALTED');self.restore()
        self.assertEqual(self.engine.step(),'HALTED');self.assertEqual(len(self.broker.posts),1)
    def test_partial_cancel_records_real_position_and_halts(self):
        self.signal();self.engine.step();self.now+=timedelta(seconds=1);self.broker.fill(1)
        self.engine.cancel();self.assertEqual(self.store.position(),1)
        self.assertEqual(self.engine.step(),'HALTED');self.assertEqual(len(self.broker.posts),1)
    def test_cancel_ack_does_not_assume_zero_position(self):
        self.signal();self.engine.step();self.now+=timedelta(seconds=1);self.broker.fill(2)
        self.engine.cancel();self.assertEqual(self.store.position(),2)
    def test_foreign_order_or_position_never_adopted(self):
        self.signal();self.broker.positions=1
        self.assertEqual(self.engine.step(),'HALTED');self.assertFalse(self.broker.posts)
        self.broker.positions=0;self.broker.unknown_orders=[dict(instrumentUid=UID,orderRequestId='foreign',orderId='other')]
        with self.assertRaises(DataError):self.engine.reconcile()
    def test_insufficient_capital_or_capacity_never_downsizes(self):
        self.signal();self.broker.cash=D(5000)
        self.assertEqual(self.engine.step(),'WAIT_CAPITAL_TWO_CONTRACTS');self.assertFalse(self.broker.posts)
        self.broker.cash=D(15000);self.broker.maxlots=1
        self.assertEqual(self.engine.step(),'WAIT_BROKER_CAPACITY');self.assertFalse(self.broker.posts)
    def test_kill_revocation_and_ttl_cancel_only_owned_order(self):
        self.signal();self.engine.step();atomic_json(self.root/'STOP',dict(stop=True))
        self.assertEqual(self.engine.step(),'HALTED');self.assertEqual(len(self.broker.cancels),1)
        self.assertEqual(self.broker.cancels[0],self.broker.posts[0]['orderId'])
    def test_expired_permission_or_absent_flag_cannot_send(self):
        self.signal();self.controls.allow_live=False
        self.assertEqual(self.engine.step(),'HALTED');self.assertFalse(self.broker.posts)
    def test_stale_price_cross_window_and_fee_ceiling_block(self):
        self.signal();self.broker.read.quote.side_effect=lambda *unused:Quote(self.now-timedelta(seconds=121),D(100))
        self.assertEqual(self.engine.step(),'WAIT_PRICE')
        self.broker.read.quote.side_effect=lambda *unused:Quote(self.now,D(100));self.broker.fee_value=D(21)
        self.assertEqual(self.engine.step(),'WAIT_FEE_LIMIT');self.assertFalse(self.broker.posts)
    def test_position_changed_during_preflight_is_not_traded(self):
        self.signal();account=self.broker.account;calls=[]
        def racing():
            calls.append(1)
            if len(calls)==2:self.broker.positions=1
            return account()
        self.broker.account=racing
        with self.assertRaises(DataError):self.engine.step()
        self.assertFalse(self.broker.posts)
    def test_quote_expires_during_capacity_check_no_dispatch(self):
        self.signal()
        def delayed(side):self.now+=timedelta(seconds=121);return 2
        self.broker.capacity=delayed
        self.assertEqual(self.engine.step(),'WAIT_REFRESH');self.assertFalse(self.broker.posts)
    def test_session_end_buffer_prevents_new_order(self):
        self.signal();self.now=NOW+timedelta(hours=1,seconds=-10)
        self.assertEqual(self.engine.step(),'WAIT_SESSION_END_BUFFER');self.assertFalse(self.broker.posts)
    def test_outside_window_broker_fill_is_preserved_and_halts(self):
        self.signal();self.engine.step();self.now=NOW+timedelta(hours=1,seconds=1)
        atomic_json(self.root/'LIVE_PERMISSION.json',dict(ack=live.ACK,config_hash=live.fingerprint(self.c),ledger_id=self.store.meta()['ledger_id'],issued_at=iso(NOW),expires_at=iso(NOW+timedelta(hours=2))))
        self.broker.fill(2)
        self.assertEqual(self.engine.step(),'HALTED')
        self.assertEqual(self.store.position(),2)
        self.assertEqual(self.store.meta()['halt'],'EXECUTION_OUTSIDE_ALLOWED_WINDOW')
        self.assertEqual(len(self.broker.posts),1)
    def test_price_limit_does_not_chase_book(self):
        self.signal();call=self.broker.call
        def moved(method,payload):
            result=call(method,payload)
            if method.endswith('GetOrderBook'):result['asks'][0]['price']=quoted(101)
            return result
        self.broker.call=moved
        self.assertEqual(self.engine.step(),'WAIT_PRICE_BOUND');self.assertFalse(self.broker.posts)
    def test_ledger_tamper_negative_trade_or_missing_meta_refused(self):
        self.open_two()
        with self.store.db() as db:db.execute("UPDATE live_trades SET price='-1'")
        with self.assertRaises(DataError):self.restore()
    def test_old_sqlite_copy_is_refused_by_external_intent_highwater(self):
        before=self.store.path.read_bytes();self.signal();self.engine.step()
        self.store.path.write_bytes(before)
        with self.assertRaises(DataError):self.restore()
    def test_ttl_cancels_once_and_halts(self):
        self.signal();self.engine.step();self.now+=timedelta(seconds=121)
        self.assertEqual(self.engine.step(),'HALTED')
        self.assertEqual(len(self.broker.cancels),1)
        self.assertEqual(self.store.orders()[0]['status'],'CANCELLED')
    def test_blocked_futures_position_only_monitored_never_opens_another_order(self):
        self.open_two();self.signal(self.prices+[1,1]);self.engine.step()
        self.broker.blocked=2
        self.assertEqual(self.engine.step(),'WAIT_ORDER');self.assertEqual(len(self.broker.posts),2)
    def test_confirmed_fill_but_lagging_position_blocks_opposite_open(self):
        self.open_two();self.signal(self.prices+[1,1]);self.engine.step()
        self.now+=timedelta(seconds=1);self.broker.fill(2);self.broker.positions=2
        self.assertEqual(self.engine.step(),'HALTED');self.assertEqual(len(self.broker.posts),2)
    def test_missing_stage_details_block_instead_of_inventing_fill(self):
        self.signal();self.engine.step();self.broker.registry[self.broker.posts[0]['orderId']]['lotsExecuted']='1'
        with self.assertRaises(DataError):self.engine.reconcile()
        self.assertEqual(self.store.position(),0)
        report=live.statistics(self.store)['latest_broker_order_report']
        self.assertEqual(report['lots_executed'],1);self.assertEqual(report['returned_stage_count'],0)
    def test_trade_identity_is_scoped_to_order_not_assumed_globally_unique(self):
        self.open_two();old=self.broker.registry[self.broker.posts[0]['orderId']]['stages'][0]['tradeId']
        self.signal(self.prices+[1,1]);self.engine.step();self.now+=timedelta(seconds=1);self.broker.fill(2)
        self.broker.registry[self.broker.posts[-1]['orderId']]['stages'][0]['tradeId']=old
        self.engine.reconcile();self.assertEqual(self.store.position(),0)
    def test_same_account_second_state_directory_is_locked(self):
        locks=self.root/'common-locks';locks.mkdir();other=self.root/'different-state'
        cfg=self.root/'config.json';cfg.write_text(json.dumps(self.c))
        import hashlib
        key=hashlib.sha256(self.c['account_id'].encode()).hexdigest()+'.lock'
        with runner_lock(locks/key),patch.object(live,'ACCOUNT_LOCK_ROOT',locks),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(live.main(['init','--state',str(other),'--config',str(cfg)]),78)
        self.assertFalse((other/'live.sqlite3').exists())
    def test_initialized_ledger_identity_is_never_silently_rebound(self):
        cfg=self.root/'config.json';cfg.write_text(json.dumps(self.c));locks=self.root/'identity-locks';locks.mkdir()
        marker=self.root/'LEDGER_INITIALIZED.json';atomic_json(marker,dict(ledger_id='different-ledger'))
        before=marker.read_bytes()
        with patch.object(live,'ACCOUNT_LOCK_ROOT',locks),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(live.main(['init','--state',str(self.root),'--config',str(cfg)]),78)
        self.assertEqual(marker.read_bytes(),before)
    def test_statistics_available_while_runner_is_locked_no_api(self):
        self.open_two();cfg=self.root/'config.json';cfg.write_text(json.dumps(self.c))
        atomic_json(self.root/'LEDGER_INITIALIZED.json',dict(initialized=True))
        with runner_lock(self.store.path),patch.object(live,'Broker') as broker,contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(live.main(['status','--state',str(self.root),'--config',str(cfg)]),0)
            broker.assert_not_called()
        result=json.loads(output.getvalue());self.assertEqual(result['ledger_position'],2)
        self.assertNotIn('payload',result['orders'][0]);self.assertIsNone(result['closed_trade_pnl_rub'])
    def cli_environment(self):
        cfg=self.root/'config.json';cfg.write_text(json.dumps(self.c))
        credential=self.root/'credential';credential.write_text('synthetic-cli-trading-token');credential.chmod(0o600)
        atomic_json(self.root/'LEDGER_INITIALIZED.json',dict(ledger_id=self.store.meta()['ledger_id']))
        locks=self.root/'cli-locks';locks.mkdir()
        self.broker.operations=Mock(return_value=[])
        from datetime import datetime
        case=self
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return case.now
        def factory(*args,**kwargs):
            self.broker.guard=kwargs.get('write_guard');return self.broker
        return cfg,credential,locks,Clock,factory
    def test_actual_cli_once_uses_mock_broker_and_restores_environment(self):
        self.signal();cfg,credential,locks,clock,factory=self.cli_environment()
        with patch.object(live,'ACCOUNT_LOCK_ROOT',locks),patch.object(live,'datetime',clock), \
             patch.object(live,'Broker',side_effect=factory),patch.object(live,'download_daily',return_value=self.root/'snapshot'), \
             patch.object(live.Engine,'ingest',return_value=True), \
             patch.dict(os.environ,{'T_INVEST_TOKEN':'synthetic-prior'}),contextlib.redirect_stdout(io.StringIO()) as output:
            result=live.main(['run','--allow-live','--once','--config',str(cfg),'--state',str(self.root),'--credential-file',str(credential)])
            self.assertEqual(result,0);self.assertEqual(os.environ['T_INVEST_TOKEN'],'synthetic-prior')
        self.assertEqual(len(self.broker.posts),1);self.assertNotIn('synthetic-cli-trading-token',output.getvalue())
    def test_capital_preflight_is_read_only_and_works_while_runner_locked(self):
        cfg,credential,locks,clock,factory=self.cli_environment()
        with runner_lock(self.store.path),patch.object(live,'Broker',side_effect=factory), \
             patch.object(live,'datetime',clock),contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(live.main(['preflight','--config',str(cfg),'--state',str(self.root),'--credential-file',str(credential)]),0)
        report=json.loads(output.getvalue());self.assertTrue(report['no_orders_or_reservation'])
        self.assertEqual(report['long']['required_rub'],'10814.4');self.assertFalse(self.broker.posts)
    def test_cli_interrupt_cancels_before_runner_lock_is_released(self):
        self.signal();self.engine.step();cfg,credential,locks,clock,factory=self.cli_environment()
        self.broker.operations=Mock(side_effect=[[],KeyboardInterrupt()])
        cancel=self.broker.cancel;locked=[]
        def checked(key):
            with self.assertRaises(DataError):
                with runner_lock(self.store.path):pass
            locked.append(True);return cancel(key)
        self.broker.cancel=checked
        with patch.object(live,'ACCOUNT_LOCK_ROOT',locks),patch.object(live,'datetime',clock), \
             patch.object(live,'Broker',side_effect=factory),patch.object(live,'download_daily',return_value=self.root/'snapshot'), \
             patch.object(live.Engine,'ingest',return_value=True),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(live.main(['run','--allow-live','--config',str(cfg),'--state',str(self.root),'--credential-file',str(credential)]),0)
        self.assertTrue(locked);self.assertEqual(len(self.broker.cancels),1)


class LiveTransportTests(unittest.TestCase):
    def test_mutation_single_attempt_and_read_only_client_stays_sealed(self):
        with patch.dict(os.environ,{'T_INVEST_TOKEN':'synthetic-live-token'}):
            transport=Mock(side_effect=urllib.error.URLError('fixture'))
            broker=Broker('12345678',transport=transport,write_guard=Mock())
            with self.assertRaises(APIError):broker.post(dict(accountId='12345678'))
            self.assertEqual(transport.call_count,1)
            client=ReadOnlyClient(transport=transport)
            with self.assertRaises(APIError):client.call('OrdersService/PostOrder',{})
            self.assertEqual(transport.call_count,1)
    def test_status_transport_cannot_write_even_with_trading_token(self):
        with patch.dict(os.environ,{'T_INVEST_TOKEN':'synthetic-live-token'}):
            transport=Mock();broker=Broker('12345678',transport=transport)
            with self.assertRaises(DataError):broker.post({})
            with self.assertRaises(DataError):broker.call('UsersService/CurrencyTransfer',{})
            transport.assert_not_called()
    def test_failure_diagnostics_do_not_contain_credentials_or_body(self):
        with patch.dict(os.environ,{'T_INVEST_TOKEN':'synthetic-secret'}):
            transport=Mock(return_value=FakeResponse(b'synthetic-secret'))
            broker=Broker('12345678',transport=transport,write_guard=Mock())
            with self.assertRaises(APIError) as error:broker.post({})
            self.assertNotIn('synthetic-secret',str(error.exception))


class LiveCapitalTests(unittest.TestCase):
    def test_two_contract_capital_threshold_uses_live_go_and_point_value(self):
        c=config();account=dict(available_conservative_rub='15000',liquid_portfolio_rub='15000',margin_deficit=False)
        r=live.risk_check(c,account,dict(long_rub='1400',short_rub='1500',rub_per_point='1'),1,D(10))
        self.assertEqual(r['required_rub'],'10804.4');self.assertTrue(r['allowed'])
        r=live.risk_check(c,account,dict(long_rub='6000',short_rub='7000',rub_per_point='1'),1,D(10))
        self.assertFalse(r['allowed'])


def cash(value):return dict(currency='rub',**quoted(D(value)))


class BrokerReadTests(unittest.TestCase):
    def broker(self,position=0):
        c=Broker.__new__(Broker);c.account_id='12345678'
        self.responses={
            'OperationsService/GetPositions':dict(accountId=c.account_id,money=[cash(15000)],blocked=[],
                futures=[dict(instrumentUid=UID,balance=str(position),blocked='0')] if position else []),
            'OperationsService/GetPortfolio':dict(accountId=c.account_id,totalAmountPortfolio=cash(15000),
                positions=[dict(instrumentUid=UID,instrumentType='futures',quantity=quoted(position),varMargin=cash(12))] if position else []),
            'OperationsService/GetWithdrawLimits':dict(money=[cash(15000)],blocked=[],blockedGuarantee=[cash(2800)] if position else []),
            'UsersService/GetMarginAttributes':dict(liquidPortfolio=cash(15000),startingMargin=cash(2800 if position else 0),
                correctedMargin=cash(2800 if position else 0),minimalMargin=cash(2000 if position else 0),amountOfMissingFunds=cash(0))}
        c.call=lambda method,payload:deepcopy(self.responses[method]);return c
    def test_balance_margin_and_portfolio_sources_agree(self):
        c=self.broker(2);a=c.account()
        self.assertEqual(a['position'],2);self.assertEqual(a['available_conservative_rub'],'12200')
        self.assertEqual(a['broker_var_margin_rub'],'12')
    def test_position_over_two_other_asset_or_mismatched_echo_refused(self):
        with self.assertRaises(DataError):self.broker(3).account()
        c=self.broker();self.responses['OperationsService/GetPositions']['securities']=[dict(balance='1',blocked='0')]
        with self.assertRaises(DataError):c.account()
        c=self.broker();self.responses['OperationsService/GetPositions']['accountId']='different'
        with self.assertRaises(DataError):c.account()
    def test_margin_deficit_is_reported_but_does_not_hide_risk_reducing_position(self):
        c=self.broker(2);self.responses['UsersService/GetMarginAttributes']['amountOfMissingFunds']=cash(100)
        self.assertTrue(c.account()['margin_deficit'])
    def test_operations_authoritative_snapshot_deduplicates_page_boundary(self):
        c=Broker.__new__(Broker);c.account_id='12345678'
        row=dict(id='operation1',brokerAccountId=c.account_id,date=iso(NOW),type='OPERATION_TYPE_ACCRUING_VARMARGIN',
            state='OPERATION_STATE_EXECUTED',instrumentUid=UID,payment=cash(12))
        c.call=Mock(side_effect=[dict(items=[row],hasNext=True,nextCursor='second'),dict(items=[row],hasNext=False)])
        result=c.operations(iso(NOW-timedelta(days=1)),NOW)
        self.assertEqual(len(result),1);self.assertEqual(result[0]['payment_rub'],'12')
    def test_request_uuid_get_state_and_cancel_use_official_id_type(self):
        c=Broker.__new__(Broker);c.account_id='12345678';c.call=Mock(return_value={})
        c.order('fixture-request');self.assertEqual(c.call.call_args.args[1]['orderIdType'],'ORDER_ID_TYPE_REQUEST')
        c.cancel('fixture-request');self.assertEqual(c.call.call_args.args[1]['orderIdType'],'ORDER_ID_TYPE_REQUEST')
