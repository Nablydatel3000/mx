"""Read-only sync diagnosis for 0f52907. No arming, writes or migration.

Real state is opened mode=ro and copied to :memory:. Only Engine.reconcile
and statistics run against that copy. The broker has an independent read seal.
This module is deliberately outside the trading fingerprint.
"""
import argparse
from contextlib import contextmanager,ExitStack
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace

from . import api
from .account_diagnostics import REASONS as ACCOUNT_REASONS
from .api_error_codes import KNOWN_API_CODES
from .live import ACCOUNT_LOCK_ROOT,Engine,fingerprint,load_config,secret,statistics
from .live_api import Broker,UID
from .live_store import Store
from .models import DataError,UTC,instant,iso

FIELDS={
 'UsersService/GetAccounts':('accounts',),
 'OrdersService/GetOrders':('orders',),
 'OrdersService/GetOrderState':('instrumentUid','orderRequestId','lotsRequested','lotsExecuted','stages'),
 'OperationsService/GetPositions':('accountId','money','blocked','futures','limitsLoadingInProgress'),
 'OperationsService/GetPortfolio':('accountId','positions','totalAmountPortfolio'),
 'OperationsService/GetWithdrawLimits':('money','blocked','blockedGuarantee'),
 'UsersService/GetMarginAttributes':('liquidPortfolio','startingMargin','minimalMargin','correctedMargin','amountOfMissingFunds'),
 'OperationsService/GetOperationsByCursor':('items','hasNext','nextCursor')}
READ=frozenset(FIELDS)
REASONS={**ACCOUNT_REASONS,
 'Dedicated broker account/full access not verified':'account_permissions_not_confirmed',
 'Actual broker commission exceeds configured bound':'reported_commission_above_bound',
 'Foreign broker order':'foreign_open_order',
 'Unexplained broker order':'unexplained_open_order',
 'Broker/ledger reconciliation failed':'broker_position_or_blocked_balance_mismatch',
 'Operation account mismatch':'operation_account_echo_mismatch',
 'Operation outside requested interval':'operation_outside_requested_range',
 'Foreign operation':'operation_foreign_instrument',
 'Conflicting broker operation identity':'operation_identity_conflict',
 'Operations pagination ambiguous':'operation_cursor_ambiguous',
 'Operations snapshot incomplete':'operation_pages_limit',
 'Diagnostic fingerprint mismatch':'runtime_or_config_fingerprint_mismatch',
 'Diagnostic initialized marker mismatch':'initialized_marker_mismatch',
 'Diagnostic highwater mismatch':'intent_highwater_mismatch',
 'Diagnostic lock identity':'lock_not_single_regular_file',
 'Diagnostic lock changed':'locked_file_identity_changed',
 'Diagnostic write access missing':'sync_write_access_not_available',
 'Diagnostic state mode':'state_permissions_not_private',
 'Diagnostic missing state':'existing_state_required',
 'Diagnostic paper path':'paper_path_forbidden',
 'Diagnostic unsupported OS':'linux_required',
 'Diagnostic ledger tables':'ledger_schema_not_complete',
 'Diagnostic sqlite sidecars':'sqlite_sidecars_require_offline_review',
 'LIVE SQLite corruption':'sqlite_integrity_failed',
 'Missing LIVE bootstrap':'bootstrap_provenance_invalid',
 'Invalid LIVE order state':'order_state_invalid',
 'Broker order identity mismatch':'broker_order_identity_mismatch',
 'Broker order quantities inconsistent':'broker_order_quantities_inconsistent',
 'Broker execution detail incomplete':'broker_execution_details_incomplete',
 'Broker execution disappeared':'broker_execution_disappeared',
 'Broker report contradicts NOT_SENT certificate':'broker_report_contradicts_not_sent'}
SAFE_FIELDS=frozenset(k for keys in FIELDS.values() for k in keys)|{'date','id','type','state','payment','commission','yield','config_hash','ledger_id','request_ids'}


def safe_error(exc):
    if isinstance(exc,api.APIError):
        categories={'http_error','invalid_json','api_error_envelope','unexpected_content_type',
                    'transport_or_request_error','transport_outcome_uncertain','empty_response','response_too_large'}
        return dict(category='api_error',method=exc.method if isinstance(exc.method,str) and exc.method in READ else None,
            http_status=exc.http_status if type(exc.http_status) is int and 100<=exc.http_status<=599 else None,
            grpc_code=exc.grpc_code if type(exc.grpc_code) is int and 0<=exc.grpc_code<=16 else None,
            api_code=exc.api_code if type(exc.api_code) is int and exc.api_code in KNOWN_API_CODES else None,
            api_category=exc.category if isinstance(exc.category,str) and exc.category in categories else 'other_api_error')
    if isinstance(exc,DataError):return dict(category='validation_error',reason=REASONS.get(str(exc),'validation_unclassified'))
    if isinstance(exc,KeyError):return dict(category='missing_field',field=exc.args[0] if exc.args and isinstance(exc.args[0],str) and exc.args[0] in SAFE_FIELDS else None)
    if isinstance(exc,BlockingIOError):return dict(category='runner_lock_busy')
    if isinstance(exc,KeyboardInterrupt):return dict(category='interrupted')
    if isinstance(exc,PermissionError):return dict(category='filesystem_permission_error')
    if isinstance(exc,FileNotFoundError):return dict(category='required_file_missing')
    if isinstance(exc,sqlite3.Error):return dict(category='sqlite_error')
    if isinstance(exc,(TypeError,ValueError,AttributeError)):return dict(category='schema_type_or_value_error')
    if isinstance(exc,OSError):return dict(category='filesystem_or_transport_error')
    return dict(category='unexpected_local_error')


def projection(method,raw,account):
    out=dict(fields_present={k:k in raw for k in FIELDS[method]})
    for key in ('accounts','orders','items','positions','futures','stages'):
        if isinstance(raw.get(key),list):out[key+'_count']=len(raw[key])
    if method.endswith('/GetOperationsByCursor') and isinstance(raw.get('items'),list):
        rows=[r for r in raw['items'] if isinstance(r,dict)]
        out.update(operation_rows_are_objects=len(rows)==len(raw['items']),
            missing_account_echo_count=sum('brokerAccountId' not in r for r in rows),
            mismatched_account_echo_count=sum(r.get('brokerAccountId')!=account for r in rows),
            foreign_instrument_count=sum(r.get('instrumentUid') not in (None,'',UID) for r in rows))
        out['money_field_shapes']={}
        for field in ('payment','commission','yield'):
            values=[r[field] for r in rows if field in r]
            out['money_field_shapes'][field]=dict(present_count=len(values),
                non_rub_or_invalid_object_count=sum(not isinstance(v,dict) or not isinstance(v.get('currency'),str) or v['currency'].lower()!='rub' for v in values))
    return out


class ProbeBroker(Broker):
    def __init__(self,account,*,transport=None):
        super().__init__(account,transport=transport,write_guard=None);self.calls=[]

    def call(self,method,payload):
        if method not in READ:raise DataError('Diagnostic read seal')
        try:
            result=super().call(method,payload)
            self.calls.append(dict(method=method,http_status=200,**projection(method,result,self.account_id)))
            return result
        except Exception as exc:
            self.calls.append({**safe_error(exc),'method':method});raise

    def post(self,*args,**kwargs):raise DataError('Diagnostic read seal')
    def cancel(self,*args,**kwargs):raise DataError('Diagnostic read seal')


class MemoryStore(Store):
    @contextmanager
    def db(self):
        try:
            self.connection.execute('BEGIN IMMEDIATE')
            yield self.connection;self.connection.commit()
        except BaseException:self.connection.rollback();raise


@contextmanager
def existing_lock(path):
    if os.name!='posix':raise DataError('Diagnostic unsupported OS')
    import fcntl
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise DataError('Diagnostic lock identity')
        if not os.access(path,os.W_OK):raise DataError('Diagnostic write access missing')
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
        current=path.stat()
        if (current.st_dev,current.st_ino)!=(info.st_dev,info.st_ino):raise DataError('Diagnostic lock changed')
    finally:os.close(fd)


def diagnose(config_path,state,credential_path,*,broker_factory=ProbeBroker,clock=lambda:datetime.now(UTC)):
    report=dict(mode='LIVE_SYNC_DIAGNOSTIC_READ_ONLY',live_enabled=False,real_state_writes=False,
        disk_write_path_exercised=False,checks=[],broker_calls=[])
    stage='config';previous=os.environ.get('T_INVEST_TOKEN');previous_keylog=os.environ.get('SSLKEYLOGFILE');broker=None
    try:
        os.environ.pop('SSLKEYLOGFILE',None)
        c=load_config(config_path);state=Path(state).resolve()
        stage='state_access'
        paper=Path('/var/lib/gldrubf').resolve()
        if state==paper or paper in state.parents or state in paper.parents:raise DataError('Diagnostic paper path')
        if not state.is_dir():raise DataError('Diagnostic missing state')
        if state.stat().st_mode&0o077:raise DataError('Diagnostic state mode')
        if not os.access(state,os.W_OK|os.X_OK):raise DataError('Diagnostic write access missing')
        dbpath=state/'live.sqlite3'
        with ExitStack() as stack:
            stage='account_runner_lock'
            key=hashlib.sha256(c['account_id'].encode()).hexdigest()+'.lock'
            stack.enter_context(existing_lock(ACCOUNT_LOCK_ROOT/key))
            stage='database_runner_lock';stack.enter_context(existing_lock(dbpath))
            stage='ledger_version'
            if any(Path(str(dbpath)+suffix).exists() for suffix in ('-wal','-shm','-journal')):raise DataError('Diagnostic sqlite sidecars')
            source=sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True);source.row_factory=sqlite3.Row
            stack.callback(source.close)
            tables={r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables!={'live_meta','live_bars','live_signals','live_orders','live_trades','live_events','live_snapshots'}:
                raise DataError('Diagnostic ledger tables')
            meta=source.execute('SELECT * FROM live_meta WHERE id=1').fetchone()
            if meta is None or meta['config_hash']!=fingerprint(c):raise DataError('Diagnostic fingerprint mismatch')
            stage='ledger_initialized_marker'
            marker=json.loads((state/'LEDGER_INITIALIZED.json').read_text())
            if marker.get('ledger_id')!=meta['ledger_id']:raise DataError('Diagnostic initialized marker mismatch')
            stage='ledger_snapshot_validation'
            memory=sqlite3.connect(':memory:');memory.row_factory=sqlite3.Row;stack.callback(memory.close)
            source.backup(memory)
            store=MemoryStore.__new__(MemoryStore);store.connection=memory;store.path=None
            with store.db() as con:store.validate(con)
            stage='ledger_highwater'
            orders=store.orders();ids={r['request_id'] for r in orders}
            path=dbpath.with_suffix('.highwater.json')
            if not path.exists() and not orders:report['sync_would_create_empty_highwater']=True
            else:
                highwater=json.loads(path.read_text())
                if highwater.get('ledger_id')!=meta['ledger_id'] or not set(highwater['request_ids'])<=ids or any(r['status']!='SENDING' and r['request_id'] not in highwater['request_ids'] for r in orders):
                    raise DataError('Diagnostic highwater mismatch')
            report['checks'].append(dict(stage='ledger',result='ok',orders_count=len(orders),halt_present=bool(meta['halt'])))
            stage='credential_access';os.environ['T_INVEST_TOKEN']=secret(credential_path)
            stage='broker_permissions';broker=broker_factory(c['account_id']);broker.accounts()
            report['checks'].append(dict(stage=stage,result='ok'))
            stage='broker_reconciliation'
            engine=Engine(store,broker,SimpleNamespace(config=c))
            if engine.reconcile() is None:raise DataError('Broker/ledger reconciliation failed')
            report['checks'].append(dict(stage=stage,result='ok'))
            stage='broker_operations';end=clock()
            report['operations_range']=dict(from_utc=iso(instant(meta['started'])),to_utc=iso(end))
            operations=broker.operations(meta['started'],end)
            report['checks'].append(dict(stage=stage,result='ok',operations_count=len(operations)))
            stage='statistics_in_memory';store.snapshot('OPERATIONS',operations,end);statistics(store)
            report['checks'].append(dict(stage=stage,result='ok'))
            stage='snapshot_consistency'
        report.update(result='read_path_passed',failure=None,
            remaining_hypotheses=['original_transient_failure','original_disk_write_path_not_exercised'])
    except (Exception,KeyboardInterrupt) as exc:report.update(result='failed',failure=dict(stage=stage,**safe_error(exc)))
    finally:
        if broker is not None:report['broker_calls']=broker.calls
        if previous is None:os.environ.pop('T_INVEST_TOKEN',None)
        else:os.environ['T_INVEST_TOKEN']=previous
        if previous_keylog is None:os.environ.pop('SSLKEYLOGFILE',None)
        else:os.environ['SSLKEYLOGFILE']=previous_keylog
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path('/etc/gldrubf-live/config.json'))
    parser.add_argument('--state',type=Path,default=Path('/var/lib/gldrubf-live'))
    parser.add_argument('--credential-file',type=Path,default=Path('/etc/gldrubf-live/trading-token'))
    args=parser.parse_args(argv)
    result=diagnose(args.config,args.state,args.credential_file)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['result']=='read_path_passed' else 2


if __name__=='__main__':raise SystemExit(main())
