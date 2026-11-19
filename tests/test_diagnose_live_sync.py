"""All broker calls mocked; actual ledger must remain byte-for-byte unchanged."""
import contextlib
from datetime import datetime,timedelta
from decimal import Decimal as D
import hashlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import urllib.error
from unittest.mock import Mock,patch

from src.gldrubf_bot import diagnose_live_sync as diag,live
from src.gldrubf_bot.live_api import Broker,UID,quoted
from src.gldrubf_bot.live_store import Store
from src.gldrubf_bot.live_acceptance import PROPOSED
from src.gldrubf_bot.models import DataError,UTC,iso
from src.gldrubf_bot.operations import atomic_json
from tests.helpers import SQLiteResourceTestCase
from tests.test_api import FakeResponse

TOKEN='synthetic-private-diagnostic-token'
ACCOUNT='12345678'


def money(value):return dict(currency='rub',**quoted(D(value)))


@unittest.skipUnless(os.name=='posix','Jino Linux read-only flock diagnostic')
class SyncDiagnosticTests(SQLiteResourceTestCase):
    def setUp(self):
        super().setUp();self.tmp=TemporaryDirectory(prefix='sync diagnostic ');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.state=self.root/'state';self.state.mkdir(mode=0o700)
        self.locks=self.root/'locks';self.locks.mkdir()
        self.now=datetime(2026,11,19,12,tzinfo=UTC);self.c=dict(PROPOSED,account_id=ACCOUNT)
        self.config=self.root/'config.json';self.config.write_text(json.dumps(self.c))
        self.credential=self.root/'token';self.credential.write_text(TOKEN);self.credential.chmod(0o600)
        self.store=Store(self.state/'live.sqlite3',live.fingerprint(self.c),self.now-timedelta(days=730),self.now)
        atomic_json(self.state/'LEDGER_INITIALIZED.json',dict(ledger_id=self.store.meta()['ledger_id']))
        self.store.halt('LIVE_API_DATA_OR_STATE_ERROR',self.now)
        self.lock=self.locks/(hashlib.sha256(ACCOUNT.encode()).hexdigest()+'.lock');self.lock.touch()
        self.enterContext(patch.object(diag,'ACCOUNT_LOCK_ROOT',self.locks))
        self.requests=[];self.changes={};self.api_failure=None;self.operation=None

    def transport(self,request,timeout):
        method=request.full_url.split('contract.v1.')[1];self.requests.append(method)
        if method==self.api_failure:
            raise urllib.error.HTTPError(request.full_url,502,TOKEN,{'Content-Type':'text/html'},io.BytesIO(TOKEN.encode()))
        if method.endswith('/GetAccounts'):r=dict(accounts=[dict(id=ACCOUNT,type='ACCOUNT_TYPE_TINKOFF',status='ACCOUNT_STATUS_OPEN',accessLevel='ACCOUNT_ACCESS_LEVEL_FULL_ACCESS')])
        elif method.endswith('/GetOrders'):r=dict(orders=[])
        elif method.endswith('/GetPositions'):r=dict(accountId=ACCOUNT,money=[money(15100)],blocked=[],futures=[])
        elif method.endswith('/GetPortfolio'):r=dict(accountId=ACCOUNT,positions=[],totalAmountPortfolio=money(15100))
        elif method.endswith('/GetWithdrawLimits'):r=dict(money=[money(15100)],blocked=[],blockedGuarantee=[])
        elif method.endswith('/GetMarginAttributes'):r=dict(liquidPortfolio=money(15100),startingMargin=money(0),minimalMargin=money(0),correctedMargin=money(0),amountOfMissingFunds=money(0))
        elif method.endswith('/GetOperationsByCursor'):r=dict(items=[self.operation] if self.operation is not None else [],hasNext=False)
        elif method.endswith('/FutureBy'):r=dict(instrument=dict(uid=UID,ticker='GLDRUBF',figi='fixture',lot=1,currency='rub',exchange='forts_futures_weekend',minPriceIncrement=quoted('.1')))
        elif method.endswith('/GetFuturesMargin'):r=dict(minPriceIncrement=quoted('.1'),minPriceIncrementAmount=quoted('.1'),initialMarginOnBuy=money('1035.82'),initialMarginOnSell=money('1036.67'))
        else:raise AssertionError('Unexpected mock method')
        if method in self.changes:self.changes[method](r)
        r.update(debug=TOKEN,accountName=TOKEN)
        return FakeResponse(json.dumps(r).encode())

    def factory(self,account):return diag.ProbeBroker(account,transport=self.transport)
    def run_probe(self):return diag.diagnose(self.config,self.state,self.credential,broker_factory=self.factory,clock=lambda:self.now)
    def files(self):return {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
    def sample_operation(self):return dict(id='private-operation',brokerAccountId=ACCOUNT,date=iso(self.now),type='OPERATION_TYPE_INPUT',state='OPERATION_STATE_EXECUTED',payment=money(15100))

    def test_preflight_passes_original_sync_loses_reason_new_probe_identifies_it(self):
        self.operation=self.sample_operation();self.operation.pop('brokerAccountId')
        case=self
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return case.now
        def broker(account,**kwargs):return Broker(account,transport=self.transport)  # Never give the test a write guard.
        args=['--state',str(self.state),'--config',str(self.config),'--credential-file',str(self.credential)]
        with patch.object(live,'ACCOUNT_LOCK_ROOT',self.locks),patch.object(live,'datetime',Clock),patch.object(live,'Broker',side_effect=broker),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(live.main(['preflight',*args]),0)
            self.assertEqual(live.main(['sync',*args]),78)
        with self.store.db() as db:row=db.execute('SELECT * FROM live_events ORDER BY id DESC LIMIT 1').fetchone()
        self.assertIsNone(row['details']);before=self.files();result=self.run_probe()
        self.assertEqual(result['failure'],dict(stage='broker_operations',category='validation_error',reason='operation_account_echo_mismatch'))
        self.assertEqual(before,self.files());self.assertEqual(self.store.meta()['halt'],'LIVE_API_DATA_OR_STATE_ERROR')
        self.assertNotIn(TOKEN,json.dumps(result));self.assertNotIn(ACCOUNT,json.dumps(result))

    def test_empty_flat_halted_state_passes_reads_without_clearing_halt(self):
        before=self.files();result=self.run_probe()
        self.assertEqual(result['result'],'read_path_passed',result)
        self.assertEqual(before,self.files());self.assertTrue(result['checks'][0]['halt_present'])
        self.assertFalse(result['disk_write_path_exercised']);self.assertFalse(result['live_enabled'])
        self.assertFalse((self.state/'LIVE_PERMISSION.json').exists())

    def test_probe_mutation_entrypoints_cannot_reach_transport_even_with_guard(self):
        with patch.dict(os.environ,{'T_INVEST_TOKEN':TOKEN}):broker=self.factory(ACCOUNT)
        broker.write_guard=Mock()
        for action in (lambda:broker.post({}),lambda:broker.cancel('private'),lambda:Broker.post(broker,{}),
                       lambda:broker.call('OrdersService/PostOrder',{}),lambda:broker.call('OrdersService/ReplaceOrder',{})):
            with self.assertRaises(DataError):action()
        self.assertEqual(self.requests,[]);broker.write_guard.assert_not_called()

    def test_http_200_validation_failure_shows_stage_and_safe_method_evidence(self):
        self.changes['OperationsService/GetPortfolio']=lambda r:r.update(accountId=TOKEN)
        r=self.run_probe();self.assertEqual(r['failure']['stage'],'broker_reconciliation')
        self.assertEqual(r['failure']['reason'],'account_echo_mismatch')
        self.assertTrue(any(c['method']=='OperationsService/GetPortfolio' and c['http_status']==200 for c in r['broker_calls']))
        self.assertNotIn(TOKEN,json.dumps(r))

    def test_http_502_is_sanitized_and_does_not_touch_source(self):
        self.api_failure='OrdersService/GetOrders';before=self.files();r=self.run_probe()
        self.assertEqual(r['failure']['stage'],'broker_reconciliation');self.assertEqual(r['failure']['http_status'],502)
        self.assertEqual(r['failure']['method'],self.api_failure);self.assertEqual(before,self.files())
        self.assertNotIn(TOKEN,json.dumps(r));self.assertNotIn('Authorization',json.dumps(r))

    def test_missing_operation_field_is_fixed_name_not_exception_text(self):
        self.operation=self.sample_operation();self.operation.pop('date')
        r=self.run_probe();self.assertEqual(r['failure'],dict(stage='broker_operations',category='missing_field',field='date'))
        self.assertNotIn('private-operation',json.dumps(r))

    def test_foreign_open_order_and_position_mismatch_are_distinguishable(self):
        self.changes['OrdersService/GetOrders']=lambda r:r.update(orders=[dict(instrumentUid=TOKEN)])
        self.assertEqual(self.run_probe()['failure']['reason'],'foreign_open_order')
        self.changes={
            'OperationsService/GetPositions':lambda r:r.update(futures=[dict(instrumentUid=UID,balance='1',blocked='0')]),
            'OperationsService/GetPortfolio':lambda r:r.update(positions=[dict(instrumentUid=UID,instrumentType='futures',quantity=quoted(1))])}
        self.assertEqual(self.run_probe()['failure']['reason'],'broker_position_or_blocked_balance_mismatch')

    def test_operation_range_instrument_and_money_failures_are_distinguished(self):
        for change,reason in ((dict(date=iso(self.now-timedelta(seconds=1))),'operation_outside_requested_range'),
                              (dict(instrumentUid=TOKEN),'operation_foreign_instrument'),
                              (dict(commission={}), 'missing_or_non_rub_amount')):
            self.operation=self.sample_operation();self.operation.update(change)
            r=self.run_probe();self.assertEqual(r['failure']['stage'],'broker_operations')
            self.assertEqual(r['failure']['reason'],reason);self.assertNotIn(TOKEN,json.dumps(r))
        method=r['broker_calls'][-1]
        self.assertEqual(method['money_field_shapes']['commission']['non_rub_or_invalid_object_count'],1)

    def test_repeated_cursor_is_reported_without_cursor_content(self):
        self.changes['OperationsService/GetOperationsByCursor']=lambda r:r.update(hasNext=True,nextCursor=TOKEN)
        r=self.run_probe();self.assertEqual(r['failure']['reason'],'operation_cursor_ambiguous')
        self.assertNotIn(TOKEN,json.dumps(r));self.assertEqual(self.requests.count('OperationsService/GetOperationsByCursor'),2)

    def test_statistics_failure_is_isolated_to_memory_and_reported(self):
        before=self.files()
        with patch.object(diag,'statistics',side_effect=KeyError(TOKEN)):r=self.run_probe()
        self.assertEqual(r['failure'],dict(stage='statistics_in_memory',category='missing_field',field=None))
        self.assertEqual(before,self.files());self.assertNotIn(TOKEN,json.dumps(r))

    def test_lock_directory_missing_is_not_reported_as_api_failure(self):
        self.lock.unlink();before=self.files();r=self.run_probe()
        self.assertEqual(r['failure'],dict(stage='account_runner_lock',category='required_file_missing'))
        self.assertEqual(self.requests,[]);self.assertEqual(before,self.files())

    def test_busy_runner_is_not_bypassed(self):
        import fcntl
        with self.lock.open('rb') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            r=self.run_probe()
        self.assertEqual(r['failure']['category'],'runner_lock_busy');self.assertEqual(self.requests,[])

    def test_fingerprint_and_marker_mismatch_fail_before_credential_or_network(self):
        with patch.object(diag,'fingerprint',return_value='different'),patch.object(diag,'secret') as secret:
            r=self.run_probe();secret.assert_not_called()
        self.assertEqual(r['failure']['reason'],'runtime_or_config_fingerprint_mismatch')
        atomic_json(self.state/'LEDGER_INITIALIZED.json',dict(ledger_id='private-mismatch'))
        r=self.run_probe();self.assertEqual(r['failure']['reason'],'initialized_marker_mismatch')
        self.assertEqual(self.requests,[])

    def test_sqlite_sidecars_are_not_recovered_or_deleted(self):
        path=Path(str(self.store.path)+'-wal');path.write_bytes(b'private wal placeholder')
        before=self.files();r=self.run_probe()
        self.assertEqual(r['failure']['reason'],'sqlite_sidecars_require_offline_review')
        self.assertEqual(before,self.files());self.assertEqual(self.requests,[])

    def test_environment_restored_and_unknown_exception_text_never_reflected(self):
        with patch.dict(os.environ,{'T_INVEST_TOKEN':'prior-synthetic','SSLKEYLOGFILE':'prior-private-path'}),patch.object(diag,'Engine',side_effect=RuntimeError(TOKEN)):
            r=self.run_probe();self.assertEqual(os.environ['T_INVEST_TOKEN'],'prior-synthetic')
            self.assertEqual(os.environ['SSLKEYLOGFILE'],'prior-private-path')
        self.assertEqual(r['failure']['category'],'unexpected_local_error');self.assertNotIn(TOKEN,json.dumps(r))

    def test_cli_rejects_activation_flag_and_never_constructs_controls(self):
        with patch.object(diag,'diagnose') as probe,contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):diag.main(['--allow-live'])
        probe.assert_not_called()
