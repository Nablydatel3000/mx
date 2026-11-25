"""Offline replay of outage incidents; synthetic transport, no broker orders."""
import contextlib
from datetime import timedelta
import io
import json
import os
import ssl
import unittest
import urllib.error
from unittest.mock import Mock,patch
from tempfile import TemporaryDirectory
from pathlib import Path

from src.gldrubf_bot import live
from src.gldrubf_bot.api import APIError,ReadOnlyClient
from src.gldrubf_bot.live_api import Broker
from src.gldrubf_bot.live_recovery import ReadRetries,RecoveryLoop,transient_read,log_event
from src.gldrubf_bot.models import DataError,iso
from src.gldrubf_bot.operations import atomic_json
from tests.helpers import SQLiteResourceTestCase
from tests.test_api import FakeResponse
from tests import test_live as fixtures


def failure(method='OrdersService/GetOrders',status=None,category='transport_or_request_error'):
    return APIError('private synthetic token/account/trace',method=method,http_status=status,category=category,
                    grpc_code=13 if status==500 else None,api_code=70001 if status==500 else None)


class RetryTests(unittest.TestCase):
    def policy(self):
        self.sleep=Mock();self.logs=[]
        return ReadRetries(sleep=self.sleep,jitter=lambda lo,hi:hi,log=lambda *a,**k:self.logs.append((a,k)))

    def test_temporary_http_bounded_exponential_jitter(self):
        for status in (408,429,500,502,503,504):
            with self.subTest(status=status):
                p=self.policy();category='rate_limited' if status==429 else 'http_timeout' if status==408 else 'server_error'
                operation=Mock(side_effect=failure(status=status,category=category))
                with self.assertRaises(APIError):p.call('OrdersService/GetOrders',operation)
                self.assertEqual(operation.call_count,4);self.assertEqual([c.args[0] for c in self.sleep.call_args_list],[1,2,4])
                self.assertTrue(p.required);self.assertEqual(p.generation,4)

    def test_successful_retry_still_requires_full_reconciliation(self):
        p=self.policy();operation=Mock(side_effect=[failure(),{'orders':[]}])
        self.assertEqual(p.call('OrdersService/GetOrders',operation),{'orders':[]})
        self.assertTrue(p.required);self.assertEqual(p.generation,1)

    def test_official_rate_limit_and_internal_codes(self):
        for status,category,grpc,code in [(429,'rate_limited',8,80002),(500,'server_error',13,70001),
                                        (500,'server_error',13,70002),(500,'server_error',13,70003)]:
            exc=failure(status=status,category=category);exc.grpc_code=grpc;exc.api_code=code
            self.assertTrue(transient_read(exc))
        exc.api_code=80003;self.assertFalse(transient_read(exc))

    def test_jitter_is_bounded_and_used(self):
        jitter=Mock(side_effect=lambda lo,hi:lo);sleep=Mock()
        p=ReadRetries(sleep=sleep,jitter=jitter,log=Mock())
        with self.assertRaises(APIError):p.call('OrdersService/GetOrders',Mock(side_effect=failure()))
        self.assertEqual([c.args for c in jitter.call_args_list],[(.5,1),(1,2),(2,4)])
        self.assertEqual([c.args[0] for c in sleep.call_args_list],[.5,1,2])

    def test_writes_and_critical_errors_are_not_retryable(self):
        errors=[failure('OrdersService/PostOrder',500,'server_error'),failure('OrdersService/CancelOrder',503,'server_error'),
                failure(status=401,category='authentication_error'),failure(status=403,category='permission_denied'),
                failure(status=400,category='http_error'),failure(status=200,category='invalid_json'),
                failure(category='tls_verification_error'),failure(category='tls_error'),failure(category='proxy_tunnel_error'),
                failure(category='invalid_json'),failure('untrusted-secret-method',500,'server_error')]
        for exc in errors:
            with self.subTest(method=exc.method,status=exc.http_status,category=exc.category):
                self.assertFalse(transient_read(exc));p=self.policy();operation=Mock(side_effect=exc)
                with self.assertRaises(APIError):p.call(exc.method,operation)
                operation.assert_called_once();self.sleep.assert_not_called();self.assertFalse(p.required)
        self.assertFalse(transient_read(DataError('fixture')))

    def test_logs_never_echo_arbitrary_exception_or_codes(self):
        exc=failure(status=500,category='server_error');exc.api_code=123456789
        with contextlib.redirect_stderr(io.StringIO()) as output:log_event('API_READ_RETRY',exc,attempt=1)
        self.assertNotIn('private',output.getvalue());self.assertNotIn('123456789',output.getvalue())
        self.assertEqual(json.loads(output.getvalue()).get('api_code'),None)

    def test_transport_recovers_both_broker_and_market_reads_without_nested_retries(self):
        for method,response in [('OrdersService/GetOrders',b'{"orders":[]}'),('MarketDataService/GetTradingStatus',b'{}')]:
            with self.subTest(method=method),patch.dict(os.environ,{'T_INVEST_TOKEN':'synthetic-only'}):
                transport=Mock(side_effect=[urllib.error.URLError('private'),FakeResponse(response)])
                p=self.policy();broker=Broker('12345678',transport=transport,retries=p)
                broker.call(method,{})
                self.assertEqual(transport.call_count,2);self.assertTrue(p.required)
                self.assertIs(broker.read.retries,p)

    def test_real_http_error_envelope_of_operations_incident(self):
        with patch.dict(os.environ,{'T_INVEST_TOKEN':'synthetic-only'}):
            transport=Mock(side_effect=[FakeResponse(b'{"code":13,"description":"70001"}',status=500),FakeResponse(b'{"items":[]}')])
            p=self.policy();broker=Broker('12345678',transport=transport,retries=p)
            self.assertEqual(broker.operations(iso(fixtures.NOW-timedelta(days=1)),fixtures.NOW),[])
            self.assertEqual(transport.call_count,2)
            error=self.logs[0][0][1];self.assertEqual((error.http_status,error.grpc_code,error.api_code),(500,13,70001))

    def test_timeouts_retry_but_tls_certificate_does_not(self):
        for exc,count in [(TimeoutError('private'),4),(urllib.error.URLError(ssl.SSLCertVerificationError('private')),1)]:
            with self.subTest(count=count),patch.dict(os.environ,{'T_INVEST_TOKEN':'synthetic-only'}):
                transport=Mock(side_effect=exc);broker=Broker('12345678',transport=transport,retries=self.policy())
                with self.assertRaises(APIError):broker.orders()
                self.assertEqual(transport.call_count,count)

    def test_response_body_timeout_after_http_200_is_retryable_read(self):
        exc=failure(status=200);self.assertTrue(transient_read(exc))
        with patch.dict(os.environ,{'T_INVEST_TOKEN':'synthetic-only'}):
            response=FakeResponse(b'{}');response.read=Mock(side_effect=TimeoutError('private'))
            transport=Mock(side_effect=[response,FakeResponse(b'{"orders":[]}')])
            broker=Broker('12345678',transport=transport,retries=self.policy())
            self.assertEqual(broker.orders(),[]);self.assertEqual(transport.call_count,2)

    def test_local_permission_failure_and_contradictory_api_codes_are_critical(self):
        error=failure(status=500,category='server_error');error.grpc_code=3;error.api_code=30051
        self.assertFalse(transient_read(error))
        for method in ('OrdersService/GetOrders','MarketDataService/GetTradingStatus'):
            with self.subTest(method=method),patch.dict(os.environ,{'T_INVEST_TOKEN':'synthetic-only'}):
                transport=Mock(side_effect=PermissionError(13,'private file'))
                broker=Broker('12345678',transport=transport,retries=self.policy())
                with self.assertRaises(APIError) as caught:broker.call(method,{})
                self.assertEqual(caught.exception.category,'local_transport_error');transport.assert_called_once()

    def test_mutations_never_retried_on_server_or_transport_failure(self):
        for method in ('OrdersService/PostOrder','OrdersService/CancelOrder'):
            for transport in (Mock(side_effect=TimeoutError('private')),Mock(return_value=FakeResponse(b'{}',status=500))):
                with self.subTest(method=method),patch.dict(os.environ,{'T_INVEST_TOKEN':'synthetic-only'}):
                    p=self.policy();broker=Broker('12345678',transport=transport,retries=p,write_guard=Mock())
                    with self.assertRaises(APIError):broker.call(method,{})
                    transport.assert_called_once();self.sleep.assert_not_called();self.assertFalse(p.required)

    def test_paper_client_stays_unchanged(self):
        with patch.dict(os.environ,{'T_INVEST_TOKEN':'synthetic-only'}):
            transport=Mock();client=ReadOnlyClient(transport=transport)
            with self.assertRaises(APIError):client.call('OrdersService/GetOrders',{})
            transport.assert_not_called();self.assertEqual(client.attempts,3)


class LoopTests(SQLiteResourceTestCase):
    def setUp(self):
        super().setUp();self.tmp=TemporaryDirectory(prefix='live recovery ');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.now=fixtures.NOW;self.c=fixtures.config();self.clock=lambda:self.now
        self.store=live.Store(self.root/'live.sqlite3',live.fingerprint(self.c),fixtures.START,self.now)
        self.controls=live.Controls(self.root,self.store,self.c,True,self.clock)
        atomic_json(self.root/'LIVE_PERMISSION.json',dict(ack=live.ACK,config_hash=live.fingerprint(self.c),ledger_id=self.store.meta()['ledger_id'],
            issued_at=iso(self.now-timedelta(seconds=1)),expires_at=iso(self.now+timedelta(hours=1))))
        self.broker=fixtures.FakeBroker(self.clock);self.broker.guard=self.controls.guard
        self.engine=live.Engine(self.store,self.broker,self.controls,self.clock)
        self.warm=fixtures.UP[:50]+[fixtures.UP[49]];self.store.ingest(fixtures.rows(self.warm,self.now),self.now)

    signal=fixtures.LiveEngineTests.signal
    open_two=fixtures.LiveEngineTests.open_two
    cli_environment=fixtures.LiveEngineTests.cli_environment

    def make_loop(self):
        atomic_json(self.root/'LEDGER_INITIALIZED.json',dict(ledger_id=self.store.meta()['ledger_id']))
        self.broker.operations=Mock(return_value=[]);self.logs=[]
        self.broker.retries=ReadRetries(sleep=Mock(),jitter=lambda lo,hi:hi,log=Mock())
        loop=RecoveryLoop(self.engine,Mock(),clock=self.clock,sleep=Mock(),jitter=lambda lo,hi:hi,
                          log=lambda *a,**k:self.logs.append((a,k)))
        def history(force=False):loop.history_fresh=True;return True
        loop.history=Mock(side_effect=history)
        return loop

    def test_incident_19_nov_getorders_transport_error_recovers_without_halt(self):
        loop=self.make_loop();original=self.broker.orders
        operation=Mock(side_effect=[failure()]*4+[original(),original(),original()])
        self.broker.orders=lambda:self.broker.retries.call('OrdersService/GetOrders',operation)
        self.assertEqual(loop.run(once=True),75);self.assertIsNone(self.store.meta()['halt']);self.assertFalse(self.broker.posts)
        self.assertEqual(loop.run(once=True),0);self.assertFalse(loop.recovering);self.assertFalse(self.broker.retries.required)
        self.assertIsNone(self.store.meta()['halt']);self.assertIn('API_CONNECTION_RECONCILED',[a[0] for a,k in self.logs])

    def test_incident_24_nov_operations_500_70001_13_after_6h11(self):
        loop=self.make_loop();self.assertEqual(loop.run(once=True),0)
        permission=json.loads((self.root/'LIVE_PERMISSION.json').read_text());permission['expires_at']=iso(self.now+timedelta(days=2))
        atomic_json(self.root/'LIVE_PERMISSION.json',permission)
        self.now+=timedelta(hours=6,minutes=11)
        error=failure('OperationsService/GetOperationsByCursor',500,'server_error')
        self.broker.operations=Mock(side_effect=[error,[]])
        self.assertEqual(loop.run(once=True),75);self.assertIsNone(self.store.meta()['halt'])
        self.assertFalse(self.broker.posts);self.assertEqual(loop.run(once=True),0)
        self.assertEqual(self.broker.operations.call_count,2);self.assertIsNone(self.store.meta()['halt'])

    def test_continuous_run_recovers_in_same_process_without_permission_changes(self):
        loop=self.make_loop();self.broker.accounts=Mock(side_effect=[failure(),None])
        permission=(self.root/'LIVE_PERMISSION.json').read_bytes();waits=[]
        def wait(seconds):
            waits.append(seconds)
            if len(waits)==2:
                self.assertFalse(loop.recovering);self.assertIsNone(self.store.meta()['halt'])
                atomic_json(self.root/'STOP',dict(reason='operator test stop'))
        loop.wait=wait
        self.assertEqual(loop.run(),78);self.assertEqual(len(waits),2)
        self.assertEqual((self.root/'LIVE_PERMISSION.json').read_bytes(),permission)
        self.assertIn('API_CONNECTION_RECONCILED',[a[0] for a,k in self.logs]);self.assertFalse(self.broker.posts)

    def test_retry_during_barrier_needs_another_clean_barrier(self):
        loop=self.make_loop();original=self.broker.accounts
        def flapping():
            original();self.broker.retries.required=True;self.broker.retries.generation+=1
        self.broker.accounts=Mock(side_effect=flapping)
        self.assertEqual(loop.run(once=True),75);self.assertFalse(self.broker.posts)
        self.broker.accounts=Mock(side_effect=original)
        self.assertEqual(loop.run(once=True),0);self.broker.accounts.assert_called_once()

    def test_retry_during_final_status_cannot_create_durable_intent(self):
        self.signal();loop=self.make_loop();original=self.broker.call;calls=[]
        def call(method,payload):
            result=original(method,payload)
            if method.endswith('GetTradingStatus'):
                calls.append(1)
                if len(calls)==2:self.broker.retries.required=True
            return result
        self.broker.call=call
        self.assertEqual(loop.run(once=True),75);self.assertFalse(self.broker.posts);self.assertEqual(self.store.orders(),[])

    def test_mismatch_is_critical_and_never_auto_cleared(self):
        loop=self.make_loop();self.broker.positions=1
        self.assertEqual(loop.run(once=True),78);self.assertEqual(self.store.meta()['halt'],'POSITION_RECONCILIATION_REQUIRED')
        self.broker.positions=0;self.assertEqual(loop.run(once=True),78)
        self.assertEqual(self.store.meta()['halt'],'POSITION_RECONCILIATION_REQUIRED');self.assertFalse(self.broker.posts)

    def test_manual_stop_critical_halt_and_expired_permission_stay_blocked(self):
        for kind in ('STOP','HALT','EXPIRED'):
            with self.subTest(kind=kind):
                loop=self.make_loop()
                if kind=='STOP':atomic_json(self.root/'STOP',dict(reason='manual'))
                if kind=='HALT':self.store.halt('MANUAL_AUDIT',self.now)
                if kind=='EXPIRED':self.now+=timedelta(hours=2)
                self.assertEqual(loop.run(once=True),78);self.assertFalse(self.broker.posts)
                if kind=='HALT':self.assertEqual(self.store.meta()['halt'],'MANUAL_AUDIT')
                if kind=='STOP':self.assertEqual(json.loads((self.root/'STOP').read_text())['reason'],'manual')
                # Prepare a separate technical subcase; production never does this.
                (self.root/'STOP').unlink(missing_ok=True)
                with self.store.db() as db:db.execute('UPDATE live_meta SET halt=NULL WHERE id=1')

    def test_open_position_is_preserved_through_long_outage_and_permission_expiry(self):
        self.open_two();loop=self.make_loop();before=len(self.broker.posts)
        self.broker.orders=Mock(side_effect=failure())
        self.assertEqual(loop.run(once=True),75);self.now+=timedelta(minutes=30)
        self.assertEqual(loop.run(once=True),75);self.assertEqual(self.store.position(),2)
        self.assertIsNone(self.store.meta()['halt']);self.assertEqual(len(self.broker.posts),before)
        self.now+=timedelta(hours=2)
        self.assertEqual(loop.run(once=True),78);self.assertEqual(self.store.position(),2)
        self.assertEqual(self.store.meta()['halt'],'LIVE_PERMISSION_OR_KILL');self.assertFalse(self.broker.cancels)

    def test_unknown_post_outcome_is_not_a_read_outage(self):
        self.signal();loop=self.make_loop();self.broker.timeout=True
        with self.assertRaises(APIError):self.engine.step()
        key=self.store.orders()[0]['request_id'];self.assertEqual(loop.run(once=True),78)
        self.assertEqual(self.store.orders()[0]['request_id'],key);self.assertEqual(len(self.broker.posts),1)
        self.assertEqual(self.store.meta()['halt'],'UNKNOWN_POST_OUTCOME')

    def test_partial_fill_then_outage_and_restart_never_reposts(self):
        self.signal();self.engine.step();self.now+=timedelta(seconds=1);self.broker.fill(1)
        loop=self.make_loop();original=self.broker.orders;self.broker.orders=Mock(side_effect=failure())
        self.assertEqual(loop.run(once=True),75);self.assertEqual(self.store.position(),1)
        self.broker.orders=original
        self.assertEqual(loop.run(once=True),0);self.assertEqual(len(self.broker.posts),1)
        self.now+=timedelta(seconds=1);self.broker.fill(2)
        restarted=self.make_loop();self.assertEqual(restarted.run(once=True),0)
        self.assertEqual(self.store.position(),2);self.assertEqual(len(self.broker.posts),1)

    def test_risk_is_checked_again_after_recovery(self):
        self.signal();loop=self.make_loop();self.broker.orders=Mock(side_effect=failure())
        self.assertEqual(loop.run(once=True),75)
        self.broker.orders=Mock(return_value=[]);self.broker.cash=fixtures.D(5000)
        self.assertEqual(loop.run(once=True),0);self.assertFalse(self.broker.posts)
        self.assertEqual(json.loads((self.root/'health.json').read_text())['event'],'WAIT_CAPITAL_TWO_CONTRACTS')

    def test_unknown_without_halt_is_blocked_before_automatic_reconciliation(self):
        self.signal();self.engine.step();loop=self.make_loop()
        with self.store.db() as db:db.execute("UPDATE live_orders SET status='UNKNOWN'")
        self.assertEqual(loop.run(once=True),78);self.assertEqual(self.store.meta()['halt'],'UNKNOWN_EXECUTION_REVIEW')
        self.assertEqual(len(self.broker.posts),1)

    def test_recovery_checks_external_highwater_and_full_sqlite_semantics(self):
        self.open_two();loop=self.make_loop();self.store.path.with_suffix('.highwater.json').unlink()
        with self.assertRaises(DataError):loop.run(once=True)
        self.assertEqual(len(self.broker.posts),1)

    def test_operations_full_range_every_300_seconds_without_losing_old_rows(self):
        loop=self.make_loop();self.assertEqual(loop.run(once=True),0)
        self.now+=timedelta(seconds=299);self.assertEqual(loop.run(once=True),0)
        self.assertEqual(self.broker.operations.call_count,1)
        self.now+=timedelta(seconds=1);self.assertEqual(loop.run(once=True),0)
        self.assertEqual(self.broker.operations.call_count,2)
        for call in self.broker.operations.call_args_list:self.assertEqual(call.args[0],self.store.meta()['started'])

    def test_failed_operations_refresh_keeps_old_snapshot_then_captures_revision(self):
        loop=self.make_loop();old=[dict(id='old',state='OPERATION_STATE_EXECUTED',type='OPERATION_TYPE_ACCRUING_VARMARGIN',payment_rub='10')]
        revised=[dict(old[0],payment_rub='12')]
        self.broker.operations=Mock(side_effect=[old,failure('OperationsService/GetOperationsByCursor',500,'server_error'),revised])
        self.assertEqual(loop.run(once=True),0);self.now+=timedelta(seconds=301)
        self.assertEqual(loop.run(once=True),75)
        self.assertEqual(live.statistics(self.store)['other_costs_by_broker_operations'],old)
        self.assertEqual(loop.run(once=True),0)
        self.assertEqual(live.statistics(self.store)['other_costs_by_broker_operations'],revised)

    def test_stale_history_keeps_recovery_blocked(self):
        loop=self.make_loop();loop.history=Mock(return_value=False)
        self.assertEqual(loop.run(once=True),75);self.assertTrue(loop.recovering);self.assertFalse(self.broker.posts)

    def test_outage_wait_wakes_for_stop_and_has_capped_exponential_backoff(self):
        loop=self.make_loop();self.broker.accounts=Mock(side_effect=failure())
        waits=[]
        def wait(seconds):
            waits.append(seconds)
            if len(waits)==7:raise KeyboardInterrupt()
        loop.wait=wait
        with self.assertRaises(KeyboardInterrupt):loop.run()
        self.assertEqual(waits,[30,60,120,240,300,300,300]);self.assertFalse(self.broker.posts)
        loop.sleep=Mock(side_effect=lambda _:atomic_json(self.root/'STOP',dict(reason='manual')))
        loop.wait=RecoveryLoop.wait.__get__(loop);loop.wait(300)
        loop.sleep.assert_called_once_with(1)

    def test_actual_cli_transient_once_returns_75_without_halt_or_cancel(self):
        cfg,credential,locks,clock,factory=self.cli_environment()
        self.broker.accounts=Mock(side_effect=failure())
        with patch.object(live,'ACCOUNT_LOCK_ROOT',locks),patch.object(live,'datetime',clock), \
             patch.object(live,'Broker',side_effect=factory),contextlib.redirect_stdout(io.StringIO()) as output:
            result=live.main(['run','--allow-live','--once','--config',str(cfg),'--state',str(self.root),'--credential-file',str(credential)])
        self.assertEqual(result,75);self.assertIsNone(self.store.meta()['halt']);self.assertFalse(self.broker.posts);self.assertFalse(self.broker.cancels)
        self.assertNotIn('synthetic-cli-trading-token',output.getvalue())

    def test_critical_read_failure_retains_safe_details_without_clearing_halt(self):
        cfg,credential,locks,clock,factory=self.cli_environment()
        self.broker.accounts=Mock(side_effect=failure('UsersService/GetAccounts',401,'authentication_error'))
        with patch.object(live,'ACCOUNT_LOCK_ROOT',locks),patch.object(live,'datetime',clock), \
             patch.object(live,'Broker',side_effect=factory),contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(live.main(['run','--allow-live','--once','--config',str(cfg),'--state',str(self.root),'--credential-file',str(credential)]),78)
        self.assertTrue(self.store.meta()['halt']);self.assertFalse(self.broker.posts)
        with self.store.db() as db:detail=json.loads(db.execute('SELECT details FROM live_events ORDER BY id DESC LIMIT 1').fetchone()[0])
        self.assertEqual(detail['http_status'],401);self.assertEqual(detail['method'],'UsersService/GetAccounts')
        self.assertNotIn('private',json.dumps(detail));self.assertNotIn('private synthetic token/account/trace',output.getvalue())

    def test_cleanup_cancel_uncertainty_is_critical_and_never_retried(self):
        self.signal();self.engine.step();cfg,credential,locks,clock,factory=self.cli_environment()
        atomic_json(self.root/'STOP',dict(reason='manual'))
        self.broker.cancel=Mock(side_effect=failure('OrdersService/CancelOrder',500,'server_error'))
        with patch.object(live,'ACCOUNT_LOCK_ROOT',locks),patch.object(live,'datetime',clock), \
             patch.object(live,'Broker',side_effect=factory),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(live.main(['run','--allow-live','--once','--config',str(cfg),'--state',str(self.root),'--credential-file',str(credential)]),78)
        self.assertEqual(self.store.meta()['halt'],'CANCEL_OUTCOME_UNCERTAIN')
        self.broker.cancel.assert_called_once();self.assertEqual(len(self.broker.posts),1)
        self.assertEqual(self.store.orders()[0]['cancel_requested'],1)

    def test_cleanup_keeps_prior_unknown_halt_and_records_cancel_uncertainty(self):
        self.signal();self.engine.step();cfg,credential,locks,clock,factory=self.cli_environment()
        self.store.halt('UNKNOWN_POST_OUTCOME',self.now)
        with self.store.db() as db:db.execute("UPDATE live_orders SET status='UNKNOWN'")
        atomic_json(self.root/'STOP',dict(reason='manual'))
        self.broker.cancel=Mock(side_effect=failure('OrdersService/CancelOrder',500,'server_error'))
        with patch.object(live,'ACCOUNT_LOCK_ROOT',locks),patch.object(live,'datetime',clock), \
             patch.object(live,'Broker',side_effect=factory),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(live.main(['run','--allow-live','--once','--config',str(cfg),'--state',str(self.root),'--credential-file',str(credential)]),78)
        self.assertEqual(self.store.meta()['halt'],'UNKNOWN_POST_OUTCOME');self.broker.cancel.assert_called_once()
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT category FROM live_events ORDER BY id DESC LIMIT 1').fetchone()[0],'CANCEL_OUTCOME_UNCERTAIN')
