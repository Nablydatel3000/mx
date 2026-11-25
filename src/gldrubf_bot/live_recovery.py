"""LIVE-only read retries and a fail-closed recovery barrier. Never retries writes."""
from datetime import datetime
import json
import random
import sys
import time

from . import api
from .models import DataError,UTC,instant,iso,number
from .operations import atomic_json

TEMPORARY_HTTP={408,429,500,502,503,504}
TEMPORARY_NETWORK={'transport_or_request_error','timeout','dns_error','network_error',
                   'incomplete_response','http_protocol_error'}
TEMPORARY_CODES={70001,70002,70003,80002}


def transient_read(exc):
    from .live_api import READ
    if not isinstance(exc,api.APIError) or exc.method not in READ|api.ALLOWED:return False
    if exc.grpc_code is not None and exc.grpc_code not in (4,8,13,14):return False
    if exc.api_code is not None and exc.api_code not in TEMPORARY_CODES:return False
    if exc.http_status in TEMPORARY_HTTP:
        return exc.category in {'http_error','http_timeout','rate_limited','server_error'}
    return exc.http_status in (None,200) and exc.category in TEMPORARY_NETWORK


def log_event(event,exc=None,**numbers):
    row=dict(mode='LIVE',event=event)
    if exc is not None and transient_read(exc):
        row.update(method=exc.method,http_status=exc.http_status,category=exc.category,
                   grpc_code=exc.grpc_code if type(exc.grpc_code) is int and 0<=exc.grpc_code<=16 else None,
                   api_code=exc.api_code if exc.api_code in TEMPORARY_CODES else None)
    row.update(numbers)
    # Keep maintenance stdout a single JSON document, even after read retries.
    print(json.dumps(row),file=sys.stderr,flush=True)


class ReadRetries:
    def __init__(self,*,attempts=4,sleep=None,jitter=None,log=log_event):
        if type(attempts) is not int or not 1<=attempts<=4:raise ValueError('Read attempts must be 1..4')
        self.attempts=attempts;self.sleep=sleep or time.sleep;self.jitter=jitter or random.uniform;self.log=log
        self.required=False;self.generation=0

    def call(self,method,operation):
        for attempt in range(self.attempts):
            try:
                result=operation()
            except api.APIError as exc:
                if exc.method!=method or not transient_read(exc):raise
                self.required=True;self.generation+=1
                self.log('API_READ_RETRY' if attempt+1<self.attempts else 'API_READ_BATCH_EXHAUSTED',exc,attempt=attempt+1)
                if attempt+1==self.attempts:raise
                cap=min(2**attempt,8)
                self.sleep(self.jitter(cap/2,cap))
            else:
                if attempt:self.log('API_READ_RESPONSE_RESTORED',attempts=attempt+1)
                return result


class RecoveryLoop:
    """One serial thread: no dispatch during outage or until a clean full barrier.

    A completed retry does not itself authorize trading. Any retry during the
    barrier invalidates that barrier and forces another complete clean pass.
    """
    def __init__(self,engine,check,*,clock=None,sleep=None,jitter=None,log=log_event):
        self.engine=engine;self.store=engine.store;self.broker=engine.broker;self.controls=engine.controls
        self.check=check;self.clock=clock or (lambda:datetime.now(UTC))
        self.sleep=sleep or time.sleep;self.jitter=jitter or random.uniform;self.log=log
        self.previous=None;self.last_history=None;self.history_fresh=False;self.last_ops=None
        self.recovering=True;self.failures=0;self.had_outage=False

    def required(self):
        retries=getattr(self.broker,'retries',None)
        return retries.required if retries is not None else False

    def generation(self):
        retries=getattr(self.broker,'retries',None)
        return retries.generation if retries is not None else 0

    def operations(self):
        end=self.clock();rows=self.broker.operations(self.store.meta()['started'],end)
        self.store.snapshot('OPERATIONS',rows,end);self.last_ops=end

    def history(self,force=False):
        from . import live
        now=self.clock()
        if force or self.last_history is None or (now-self.last_history).total_seconds()>=60:
            instrument=self.broker.read.instrument(live.UID)
            folder=self.controls.state/'history'/now.strftime('%Y%m%dT%H%M%S_%fZ')
            snapshot=live.download_daily(self.broker.read,instrument,instant(self.store.meta()['source_from']),now,
                folder,previous=self.previous,make_template=False,clock=self.clock)
            self.check();self.history_fresh=self.engine.ingest(snapshot)
            self.previous=snapshot;self.last_history=now
        return self.history_fresh and self.engine.latest_age(self.clock())

    def barrier(self):
        from . import live
        self.check()
        # Re-run the existing full SQLite semantics and external highwater checks.
        live.Store(self.store.path,live.fingerprint(self.controls.config),instant(self.store.meta()['source_from']),self.clock())
        marker=json.loads((self.controls.state/'LEDGER_INITIALIZED.json').read_text())
        if marker.get('ledger_id')!=self.store.meta()['ledger_id']:raise DataError('Recovery initialized marker mismatch')
        if any(r['status'] in ('UNKNOWN','SENDING') for r in self.store.orders()):
            self.engine.stop('UNKNOWN_EXECUTION_REVIEW');return 'HALTED'
        generation=self.generation()
        self.broker.accounts()
        account=self.engine.reconcile()
        if account is None:
            self.engine.stop('POSITION_RECONCILIATION_REQUIRED');return 'HALTED'
        instrument=self.broker.read.instrument(live.UID);margin=self.broker.margin(instrument)
        # Re-evaluate current capital/capacity without inventing a liquidation.
        # Insufficient opening capital must still allow the existing CLOSE gate.
        self.store.snapshot('RECOVERY_RISK',dict(
            long=live.risk_check(self.controls.config,account,margin,1,number(self.controls.config['max_order_fee_rub'])),
            short=live.risk_check(self.controls.config,account,margin,-1,number(self.controls.config['max_order_fee_rub'])),
            capacity_long=live.integer(self.broker.capacity(1)),capacity_short=live.integer(self.broker.capacity(-1)),
            actual_dispatch_requires_fresh_engine_checks=True),self.clock())
        self.operations()
        if not self.history(force=True):return 'WAIT_HISTORY'
        self.check()
        if not self.controls.permitted():
            self.engine.stop('LIVE_PERMISSION_OR_KILL');return 'HALTED'
        if self.store.meta()['halt']:return 'HALTED'
        if self.generation()!=generation:return 'WAIT_API_RECOVERY'
        retries=getattr(self.broker,'retries',None)
        if retries is not None:retries.required=False
        self.recovering=False;self.failures=0
        if self.had_outage:self.log('API_CONNECTION_RECONCILED',position=self.store.position())
        self.had_outage=False
        return 'RECONCILED'

    def cycle(self):
        self.check()
        if self.store.meta()['halt']:return 'HALTED'
        if not self.controls.permitted():return self.engine.step(False)
        if self.recovering or self.required():
            event=self.barrier()
            if event!='RECONCILED':return event
        self.history()
        # Full-range authoritative refresh every five minutes; no incremental
        # cursor/checkpoint that could silently discard old broker revisions.
        if self.last_ops is None or (self.clock()-self.last_ops).total_seconds()>=300 or self.store.orders(active=True):
            self.operations()
        if self.required():return 'WAIT_API_RECOVERY'
        event=self.engine.step(self.history_fresh)
        if event=='HALTED':return event
        if self.required():return 'WAIT_API_RECOVERY'
        return event

    def wait(self,seconds):
        remaining=seconds
        while remaining>0:
            self.check()
            if self.store.meta()['halt'] or not self.controls.permitted():break
            part=min(remaining,1);self.sleep(part);remaining-=part

    def run(self,once=False):
        while True:
            try:event=self.cycle()
            except api.APIError as exc:
                if not transient_read(exc) or self.store.meta()['halt'] or any(r['status'] in ('SENDING','UNKNOWN') for r in self.store.orders()):raise
                self.log('API_CONNECTION_UNAVAILABLE',exc,position=self.store.position())
                event='WAIT_API_RECOVERY'
            if event=='WAIT_API_RECOVERY':
                self.recovering=True;self.had_outage=True;self.history_fresh=False;self.last_history=None
                self.failures+=1
            waiting=self.recovering or event=='WAIT_API_RECOVERY'
            atomic_json(self.controls.state/'health.json',dict(time=iso(self.clock()),event=event,
                halt=self.store.meta()['halt'],position=self.store.position(),broker_state_fresh=not waiting,
                operations_to=iso(self.last_ops) if self.last_ops else None))
            self.log(event,position=self.store.position())
            if event=='HALTED':return 78
            if once:return 75 if waiting else 0
            cap=min(30*2**min(self.failures-1,4),300) if self.failures else self.controls.config['poll_seconds']
            self.wait(self.jitter(cap/2,cap) if self.failures else cap)
