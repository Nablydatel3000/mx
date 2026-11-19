"""Separate, explicitly armed GLDRUBF LIVE mode. No PAPER state or notifications.

Running tests/help/status never dispatches orders. No write retry on uncertainty.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime,timedelta,time as daytime
from decimal import Decimal as D
import hashlib
import json
import os
from pathlib import Path
import signal
import time
from uuid import uuid4

from .api import APIError
from .autonomous import history_age,same_window
from .history import download_daily,load_snapshot,encoded
from .locking import runner_lock
from .models import DataError,UTC,instant,iso,number
from .operations import atomic_json
from .live_api import Broker,UID,quoted,integer
from .live_store import Store,TERMINAL
from .live_market import limit_status
from .live_errors import DefinitelyNotSent

ACK='LIVE_GLDRUBF_2_CONTRACTS_15000_RUB_EXCLUSIVE'
ACCOUNT_LOCK_ROOT=Path('/var/lib/gldrubf-live/locks')


def load_config(path):
    c=json.loads(Path(path).read_text())
    required={'account_id','capital_rub','contracts','instrument_uid','max_complete_age_hours',
        'quote_max_age_seconds','snapshot_max_age_seconds','margin_stress_multiplier',
        'cash_reserve_rub','max_order_fee_rub','max_slippage_ticks','order_ttl_seconds','poll_seconds','min_session_remaining_seconds'}
    if not isinstance(c,dict) or set(c)!=required:raise DataError('Explicit LIVE configuration required')
    if c['capital_rub']!='15000' or type(c['contracts']) is not int or c['contracts']!=2 or c['instrument_uid']!=UID:
        raise DataError('Only approved GLDRUBF/15000/2 configuration allowed')
    if not isinstance(c['account_id'],str) or not c['account_id'].isascii() or not c['account_id'].isdigit():raise DataError('Explicit broker account required')
    for key in ('quote_max_age_seconds','snapshot_max_age_seconds','order_ttl_seconds','poll_seconds'):
        if type(c[key]) is not int or c[key]<1:raise DataError('Explicit timing policy required')
    if type(c['max_slippage_ticks']) is not int or c['max_slippage_ticks']<0:raise DataError('Explicit price bound required')
    if type(c['min_session_remaining_seconds']) is not int or c['min_session_remaining_seconds']<20:raise DataError('Explicit session-end buffer >=20s required')
    for key in ('margin_stress_multiplier','cash_reserve_rub','max_order_fee_rub'):
        if number(c[key])< (1 if key=='margin_stress_multiplier' else 0):raise DataError('Invalid explicit risk policy')
    history_age([],datetime.now(UTC),c['max_complete_age_hours'])
    return c


def fingerprint(c):
    root=Path(__file__).parent
    software={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in
        ('live.py','live_api.py','live_store.py','live_market.py','live_errors.py','strategy.py','api.py','autonomous.py','locking.py','history.py')}
    return hashlib.sha256(encoded(dict(config=c,software=software))).hexdigest()


def risk_check(c,account,margin,side,fee):
    go=number(margin['long_rub' if side==1 else 'short_rub'],positive=True)
    point=number(margin['rub_per_point'],positive=True)
    available=min(number(account['available_conservative_rub']),number(account['liquid_portfolio_rub']),D('15000'))
    required=2*go*number(c['margin_stress_multiplier'])+2*D('2097.2')*point+number(c['cash_reserve_rub'])+fee
    return dict(required_rub=str(required),available_rub=str(available),current_margin_two_rub=str(2*go),
        historical_daily_drawdown_two_rub=str(2*D('2097.2')*point),
        allowed=not account.get('margin_deficit',True) and available>=required,
        historical_drawdown_is_scenario=True,actual_historical_funding_and_intraday_risk_verified=False)


class Controls:
    def __init__(self,state,store,c,allow_live=False,clock=lambda:datetime.now(UTC)):
        self.state=Path(state);self.store=store;self.config=c;self.allow_live=allow_live;self.clock=clock

    def permitted(self):
        if not self.allow_live or (self.state/'STOP').exists():return False
        try:
            p=json.loads((self.state/'LIVE_PERMISSION.json').read_text())
            return p['ack']==ACK and p['config_hash']==fingerprint(self.config) and p['ledger_id']==self.store.meta()['ledger_id'] and instant(p['issued_at'])<=self.clock()<instant(p['expires_at'])
        except (OSError,ValueError,KeyError):return False

    def guard(self,method,payload):
        if not self.allow_live:raise DataError('Explicit LIVE flag missing')
        row=next((r for r in self.store.orders() if r['request_id']==payload.get('orderId')),None)
        if row is None or payload.get('accountId')!=self.config['account_id']:raise DataError('Unowned LIVE request')
        if method=='OrdersService/PostOrder':
            if not self.permitted() or row['status']!='SENDING' or json.loads(row['payload'])!=payload:
                raise DataError('LIVE dispatch not authorized')
            highwater=json.loads(self.store.path.with_suffix('.highwater.json').read_text())
            if highwater['ledger_id']!=self.store.meta()['ledger_id'] or payload['orderId'] not in highwater['request_ids']:
                raise DataError('LIVE durable intent marker missing')
            if payload.get('instrumentId')!=UID or payload.get('orderType')!='ORDER_TYPE_LIMIT' or payload.get('priceType')!='PRICE_TYPE_POINT' or payload.get('confirmMarginTrade') is not False or payload.get('timeInForce')!='TIME_IN_FORCE_FILL_AND_KILL' or integer(payload['quantity']) not in (1,2):
                raise DataError('Prohibited LIVE order shape')
            if self.clock()<instant(row['created']) or (self.clock()-instant(row['created'])).total_seconds()>self.config['snapshot_max_age_seconds']:
                raise DataError('LIVE dispatch context expired')
            context=getattr(self,'dispatch_context',None)
            if context is None:raise DataError('No fresh dispatch context')
            checked,quote_time,book_time,calendar,status_from,status_to=context;now=self.clock()
            if now<checked or any((now-t).total_seconds()>self.config['quote_max_age_seconds'] or t>now for t in (quote_time,book_time)) or not all(same_window(calendar,now,t) for t in (quote_time,book_time)):
                raise DataError('Dispatch market context expired')
            saved_context=json.loads(row['context'])
            if not instant(saved_context['cycle_from'])<=now or (now-instant(saved_context['cycle_from'])).total_seconds()>self.config['snapshot_max_age_seconds']:
                raise DataError('Dispatch cycle context expired')
            if not status_from<=status_to<=checked<=now or (now-status_from).total_seconds()>self.config['quote_max_age_seconds']:
                raise DataError('Dispatch status confirmation expired')
            if saved_context.get('status_from')!=iso(status_from) or saved_context.get('status_to')!=iso(status_to):
                raise DataError('Dispatch status evidence mismatch')
            if (instant(saved_context['window_to'])-now).total_seconds()<self.config['min_session_remaining_seconds']:raise DataError('Dispatch too close to phase boundary')
        elif method=='OrdersService/CancelOrder':
            if row['status'] in TERMINAL:raise DataError('Cannot cancel terminal order')
        else:raise DataError('Unknown write method')


class Engine:
    def __init__(self,store,broker,controls,clock=lambda:datetime.now(UTC)):
        self.store,self.broker,self.controls,self.clock=store,broker,controls,clock
        self.config=controls.config

    def stop(self,reason):self.store.halt(reason,self.clock())

    def reconcile(self):
        # Query every unresolved UUID, including after crash-before/after PostOrder.
        for order in self.store.orders(active=True):
            self.store.record_order(order['request_id'],self.broker.order(order['request_id']),self.clock())
        if any(r['commission'] is not None and number(r['commission'])>number(self.config['max_order_fee_rub']) for r in self.store.orders()):
            raise DataError('Actual broker commission exceeds configured bound')
        orders=self.broker.orders();known=self.store.orders()
        for raw in orders:
            if raw.get('instrumentUid')!=UID:raise DataError('Foreign broker order')
            row=next((r for r in known if r['request_id']==raw.get('orderRequestId') and r['broker_id']==raw.get('orderId')),None)
            if row is None or row['status'] in TERMINAL:raise DataError('Unexplained broker order')
        account=self.broker.account();self.last_account=account
        self.store.snapshot('ACCOUNT',account,self.clock())
        if account['position']!=self.store.position() or account['blocked']:
            # Broker positions may lag execution reports. Freeze all sends until
            # exact convergence; an unexplained mismatch is NOT adopted.
            return None
        return account

    def cancel(self,force=False):
        active=self.store.orders(active=True)
        if len(active)>1:raise DataError('Multiple owned LIVE orders')
        for order in active:
            if order['cancel_requested'] and not force:continue
            with self.store.db() as db:db.execute('UPDATE live_orders SET cancel_requested=1 WHERE request_id=?',(order['request_id'],))
            self.broker.cancel(order['request_id'])
            # Cancel acknowledgement is not a fill/position confirmation.
            self.store.record_order(order['request_id'],self.broker.order(order['request_id']),self.clock())

    def ingest(self,snapshot):
        manifest,rows=load_snapshot(snapshot);now=self.clock()
        if manifest['uid']!=UID or manifest['from']!=self.store.meta()['source_from'] or instant(manifest['to'])>now or instant(manifest['captured_at'])>now:
            raise DataError('Wrong/future LIVE history')
        if (now-instant(manifest['captured_at'])).total_seconds()>300:raise DataError('LIVE snapshot stale')
        age=history_age(rows,now,self.config['max_complete_age_hours'])
        self.store.snapshot('HISTORY_AGE',age,self.clock())
        if not age or not age['fresh']:return False
        self.store.ingest(rows,now);return True

    def step(self,history_fresh=True):
        now=self.clock();c=self.config
        if not self.controls.permitted():
            self.stop('LIVE_PERMISSION_OR_KILL');self.cancel();return 'HALTED'
        if self.store.meta()['halt']:return 'HALTED'
        start=now;account=self.reconcile()
        if self.store.meta()['halt']:return 'HALTED'
        if account is None:
            monitored=self.store.orders(active=True)
            if monitored and self.last_account['position']==self.store.position():
                account=self.last_account  # Only monitor/cancel; no new dispatch while blocked.
            else:
                self.stop('POSITION_RECONCILIATION_REQUIRED');return 'HALTED'
        # Rejections/cancellations, including terminal partial fills, never spawn replacements.
        orders=self.store.orders()
        if any(r['status'] in ('REJECTED','CANCELLED','NOT_SENT') for r in orders):
            self.stop('REJECTED_OR_CANCELLED_REVIEW');return 'HALTED'
        active=self.store.orders(active=True)
        if len(active)>1:raise DataError('Multiple active intents')
        if active:
            if (now-instant(active[0]['created'])).total_seconds()>=c['order_ttl_seconds']:
                self.cancel();self.stop('ORDER_TTL_REVIEW');return 'HALTED'
            return 'WAIT_ORDER'
        if not history_fresh:return 'WAIT_HISTORY'
        signal=self.store.signal()
        if signal is None:return 'NO_SIGNAL'
        if now<=instant(signal['known']):return 'WAIT_SIGNAL_TIME'
        position=account['position'];target=signal['target']
        if position==target:self.store.done(signal['day']);return 'TARGET_CONFIRMED'
        phase='CLOSE' if position else 'OPEN';side=-1 if position>0 else 1 if position<0 else (1 if target>0 else -1)
        qty=abs(position) if phase=='CLOSE' else 2
        if abs(position+side*qty)>2 or (phase=='CLOSE' and position+side*qty!=0):raise DataError('Exposure envelope')
        instrument=self.broker.read.instrument(UID)
        calendar=self.broker.read.current_sessions(instrument.exchange,now)
        quote=self.broker.read.quote(instrument,self.clock)
        status=self.broker.call('MarketDataService/GetTradingStatus',{'instrumentId':UID})
        if not limit_status(status,UID):
            return 'WAIT_MARKET'
        book=self.broker.call('MarketDataService/GetOrderBook',{'instrumentId':UID,'depth':1})
        at=self.clock()
        if not quote or book.get('instrumentUid')!=UID:return 'WAIT_PRICE'
        book_time=instant(book['orderbookTs'])
        if any(t>at or (at-t).total_seconds()>c['quote_max_age_seconds'] for t in (quote.time,book_time)):
            return 'WAIT_PRICE'
        if not same_window(calendar,at,quote.time) or not same_window(calendar,at,book_time):return 'WAIT_MARKET'
        price=quote.price+side*c['max_slippage_ticks']*instrument.tick
        levels=book.get('asks' if side==1 else 'bids',[])
        if not levels:return 'WAIT_BOOK'
        from .api import quotation
        best=quotation(levels[0]['price'])
        if integer(levels[0].get('quantity','0'))<1:return 'WAIT_BOOK'
        if price<=0 or price%instrument.tick or best<=0 or best%instrument.tick or (side==1 and best>price) or (side==-1 and best<price):return 'WAIT_PRICE_BOUND'
        if not quotation(book['limitDown'])<=price<=quotation(book['limitUp']):return 'WAIT_PRICE_BOUND'
        margin=self.broker.margin(instrument);fee=self.broker.fee(side,qty,price)
        if fee>number(c['max_order_fee_rub']):return 'WAIT_FEE_LIMIT'
        if self.broker.capacity(side)<qty:return 'WAIT_BROKER_CAPACITY'
        # A second fresh broker account/open-order read immediately before dispatch.
        account=self.reconcile()
        if account is None or account['position']!=position or self.store.orders(active=True):raise DataError('Position changed before dispatch')
        if phase=='OPEN':
            risk=risk_check(c,account,margin,side,fee);self.store.snapshot('CAPITAL',risk,self.clock())
            if not risk['allowed']:return 'WAIT_CAPITAL_TWO_CONTRACTS'
        # Financial/reconciliation reads cannot authorize use of an old market status.
        # Refresh calendar first, then instrument status as the last broker read.
        calendar_from=self.clock()
        calendar=self.broker.read.current_sessions(instrument.exchange,calendar_from)
        status_from=self.clock()
        final_status=self.broker.call('MarketDataService/GetTradingStatus',{'instrumentId':UID})
        status_to=self.clock()
        if not limit_status(final_status,UID):return 'WAIT_MARKET'
        current=self.clock()
        if not calendar_from<=status_from<=status_to<=current or (current-status_from).total_seconds()>c['quote_max_age_seconds']:
            return 'WAIT_REFRESH'
        if current<start or (current-start).total_seconds()>c['snapshot_max_age_seconds'] or any((current-t).total_seconds()>c['quote_max_age_seconds'] for t in (quote.time,book_time)) or not same_window(calendar,current,quote.time) or not same_window(calendar,current,book_time):return 'WAIT_REFRESH'
        age=self.latest_age(current)
        if not age:return 'WAIT_HISTORY'
        span=next(( (a,b) for day in calendar.values() for a,b in day.windows if a<=current<b and a<=quote.time<b and a<=book_time<b),None)
        if span is None or (span[1]-current).total_seconds()<c['min_session_remaining_seconds']:return 'WAIT_SESSION_END_BUFFER'
        payload=dict(accountId=c['account_id'],instrumentId=UID,quantity=str(qty),
            direction='ORDER_DIRECTION_BUY' if side==1 else 'ORDER_DIRECTION_SELL',orderType='ORDER_TYPE_LIMIT',
            orderId=str(uuid4()),price=quoted(price),priceType='PRICE_TYPE_POINT',
            timeInForce='TIME_IN_FORCE_FILL_AND_KILL',confirmMarginTrade=False)
        context=dict(window_from=iso(span[0]),window_to=iso(span[1]),quote_time=iso(quote.time),book_time=iso(book_time),
            status_from=iso(status_from),status_to=iso(status_to),calendar_from=iso(calendar_from),cycle_from=iso(start),
            status_gate='UID_NORMAL_API_LIMIT_CONFIRMED')
        selection=getattr(self.broker.read,'last_calendar_evidence',None)
        if isinstance(selection,dict):context['calendar_selection']=selection
        self.store.intent(signal,phase,side,qty,position,payload,current,context)
        self.controls.dispatch_context=(current,quote.time,book_time,calendar,status_from,status_to)
        try:self.store.ack(payload['orderId'],self.broker.post(payload),self.clock())
        except DefinitelyNotSent:
            self.store.not_sent(payload['orderId'],self.clock())
            return 'HALTED'
        except BaseException:
            with self.store.db() as db:db.execute("UPDATE live_orders SET status='UNKNOWN' WHERE request_id=?",(payload['orderId'],))
            self.stop('UNKNOWN_POST_OUTCOME');raise
        return 'ORDER_SENT_'+phase

    def latest_age(self,now):
        with self.store.db() as db:row=db.execute('SELECT payload FROM live_bars ORDER BY day DESC LIMIT 1').fetchone()
        return bool(row and history_age([dict(json.loads(row[0]),is_complete=True)],now,self.config['max_complete_age_hours'])['fresh'])


def statistics(store):
    with store.db() as db:
        last=db.execute("SELECT * FROM live_snapshots WHERE kind='ACCOUNT' ORDER BY id DESC LIMIT 1").fetchone()
        ops=db.execute("SELECT * FROM live_snapshots WHERE kind='OPERATIONS' ORDER BY id DESC LIMIT 1").fetchone()
        reported_order=db.execute("SELECT * FROM live_snapshots WHERE kind='ORDER_REPORT' ORDER BY id DESC LIMIT 1").fetchone()
        errors=[dict(r) for r in db.execute('SELECT time,category,details FROM live_events ORDER BY id DESC LIMIT 20')]
        trades=[dict(**dict(r),price_type_requested='PRICE_TYPE_POINT') for r in db.execute('SELECT * FROM live_trades ORDER BY time,trade_id')]
        orders=[dict(r) for r in db.execute('SELECT * FROM live_orders ORDER BY rowid')]
        position=store.position(db);halt=db.execute('SELECT halt FROM live_meta WHERE id=1').fetchone()[0]
    operations=json.loads(ops['payload']) if ops else None
    vm=None
    if operations is not None:
        vm=str(sum((number(r['payment_rub']) for r in operations if r['state']=='OPERATION_STATE_EXECUTED' and r['type'] in ('OPERATION_TYPE_ACCRUING_VARMARGIN','OPERATION_TYPE_WRITING_OFF_VARMARGIN') and r['payment_rub'] is not None),D(0)))
    fees=[number(r['commission']) for r in orders if r['commission'] is not None]
    return dict(mode='LIVE',as_of=last['time'] if last else None,account=json.loads(last['payload']) if last else None,
        ledger_position=position,orders=orders,broker_fills=trades,errors=errors,halt=halt,
        latest_broker_order_report=json.loads(reported_order['payload']) if reported_order else None,
        realized_settlement_vm_cash_rub=vm,operations_as_of=ops['time'] if ops else None,
        reported_execution_commissions_rub=str(sum(fees,D(0))) if fees else None,
        commission_coverage=dict(orders_reported=len(fees),orders_total=len(orders)),
        closed_trade_pnl_rub=None,other_costs_by_broker_operations=operations,
        pnl_note='VM is broker settled cash including held positions, not modeled round-trip P&L. Do not double-count order fees and operation commissions.')


def secret(path):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>8192:raise DataError('Invalid LIVE credential file')
    if os.name=='posix' and path.stat().st_mode&0o077:raise DataError('LIVE credential must be private 0600')
    value=path.read_text().strip()
    import re
    if not re.fullmatch(r'[A-Za-z0-9._~+/-]+=*',value):raise DataError('Invalid credential encoding')
    return value


def main(argv=None):
    p=argparse.ArgumentParser(description='Separate GLDRUBF LIVE, explicitly gated. Never uses PAPER DB.')
    p.add_argument('command',choices=['init','arm','run','status','preflight','sync','resume','kill','cancel','install-token'])
    p.add_argument('--config',type=Path,default=Path('/etc/gldrubf-live/config.json'))
    p.add_argument('--state',type=Path,default=Path('/var/lib/gldrubf-live'))
    p.add_argument('--credential-file',type=Path,default=Path('/etc/gldrubf-live/trading-token'))
    p.add_argument('--allow-live',action='store_true')
    p.add_argument('--acknowledge');p.add_argument('--expires-utc');p.add_argument('--once',action='store_true')
    p.add_argument('--refresh',action='store_true',help='Status only: refresh broker reads, no orders')
    args=p.parse_args(argv);state=args.state.resolve();store=None;engine=None
    previous_token=os.environ.get('T_INVEST_TOKEN');old_handler=None
    try:
        paper=Path('/var/lib/gldrubf').resolve()
        if state==paper or paper in state.parents or state in paper.parents:raise DataError('PAPER path forbidden')
        if args.command=='install-token':
            import getpass,re,sys,warnings
            if not sys.stdin.isatty() or not sys.stdout.isatty():raise DataError('Local hidden console required')
            with warnings.catch_warnings():
                warnings.simplefilter('error',getpass.GetPassWarning)
                token=getpass.getpass('Trading API token (hidden; LOCAL ONLY): ').strip()
            if not re.fullmatch(r'[A-Za-z0-9._~+/-]+=*',token) or len(token)>8192:raise DataError('Invalid token')
            if args.credential_file.is_symlink():raise DataError('Credential symlink refused')
            # Exclusive creation, private at the first byte. Rotation requires an explicit new file.
            fd=os.open(args.credential_file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as f:f.write(token+'\n')
            print('Private trading credential installed locally; no API requests.');return 0
        state.mkdir(parents=True,exist_ok=True,mode=0o700)
        if args.command=='kill':
            atomic_json(state/'STOP',dict(reason='OPERATOR_KILL',at=iso(datetime.now(UTC))))
            print('STOP set. Running loop cancels owned pending orders; positions are NOT auto-liquidated.');return 0
        if os.name=='posix' and state.stat().st_mode&0o077:raise DataError('LIVE state directory must be private 0700')
        c=load_config(args.config);now=datetime.now(UTC)
        initialized=state/'LEDGER_INITIALIZED.json';dbpath=state/'live.sqlite3'
        if initialized.exists() and (not dbpath.exists() or dbpath.stat().st_size==0):raise DataError('Initialized LIVE ledger missing; no reset')
        if args.command!='init' and not initialized.exists():raise DataError('Explicit LIVE init required')
        if args.command in ('status','preflight'):
            # A short SQLite snapshot is allowed while the trading process owns
            # the runner lock. No intent, order or execution state is modified.
            store=Store.__new__(Store);store.path=dbpath.resolve()
            if store.meta()['config_hash']!=fingerprint(c):raise DataError('Status config mismatch')
            if args.refresh or args.command=='preflight':
                os.environ['T_INVEST_TOKEN']=secret(args.credential_file)
                read_broker=Broker(c['account_id'])  # No write guard: mutations impossible.
                account=read_broker.account();store.snapshot('ACCOUNT',account,datetime.now(UTC))
                if args.command=='preflight':
                    read_broker.accounts();instrument=read_broker.read.instrument(UID);margin=read_broker.margin(instrument)
                    report=dict(no_orders_or_reservation=True,checked_at=iso(datetime.now(UTC)),current_futures_margin=margin,
                        long=risk_check(c,account,margin,1,number(c['max_order_fee_rub'])),
                        short=risk_check(c,account,margin,-1,number(c['max_order_fee_rub'])),
                        fee_source='configured maximum fee reserve; actual order estimate checked again before sending')
                    store.snapshot('CAPITAL_PREFLIGHT',report,datetime.now(UTC))
                    print(json.dumps(report,ensure_ascii=False,indent=2));return 0
                ops=read_broker.operations(store.meta()['started'],datetime.now(UTC));store.snapshot('OPERATIONS',ops,datetime.now(UTC))
            result=statistics(store)
            for row in result['orders']:row.pop('payload',None)
            print(json.dumps(result,ensure_ascii=False,indent=2));return 0
        with ExitStack() as stack:
            # Same account uses a common physical lock irrespective of --state/path aliases.
            digest=hashlib.sha256(c['account_id'].encode()).hexdigest()
            if not ACCOUNT_LOCK_ROOT.is_dir():raise DataError('Common LIVE account lock directory absent')
            stack.enter_context(runner_lock(ACCOUNT_LOCK_ROOT/(digest+'.lock')))
            check=stack.enter_context(runner_lock(dbpath))
            source=datetime.combine(now.date(),daytime.min,UTC)-timedelta(days=730)
            store=Store(dbpath,fingerprint(c),source,now,allow_empty_new=args.command=='init' and not initialized.exists())
            if initialized.exists() and json.loads(initialized.read_text()).get('ledger_id')!=store.meta()['ledger_id']:
                raise DataError('LIVE initialized marker belongs to another ledger')
            atomic_json(initialized,dict(ledger_id=store.meta()['ledger_id']))
            controls=Controls(state,store,c,allow_live=args.allow_live)
            if args.command=='init':
                print('LIVE ledger initialized offline, flat, disarmed. No broker requests.');return 0
            if args.command=='arm':
                if args.acknowledge!=ACK or not args.expires_utc:raise DataError('Explicit LIVE acknowledgement and expiry required')
                expires=instant(args.expires_utc)
                if expires<=now:raise DataError('Explicit future permission expiry required')
                atomic_json(state/'LIVE_PERMISSION.json',dict(ack=ACK,config_hash=fingerprint(c),ledger_id=store.meta()['ledger_id'],issued_at=iso(now),expires_at=iso(expires)))
                print('Local LIVE permission recorded. STOP/halt remains; no broker requests.');return 0
            if args.command=='status' and not args.refresh:
                output=statistics(store)
                # Account IDs inside intent payloads stay in the private DB, not console stats.
                for row in output['orders']:row.pop('payload',None)
                print(json.dumps(output,ensure_ascii=False,indent=2));return 0
            os.environ.pop('SSLKEYLOGFILE',None)
            os.environ['T_INVEST_TOKEN']=secret(args.credential_file)
            broker=Broker(c['account_id'],write_guard=controls.guard);broker.accounts()
            engine=Engine(store,broker,controls)
            def cleanup():
                import sys
                if args.command=='run' and (sys.exc_info()[0] is not None or (state/'STOP').exists() or store.meta()['halt']):
                    try:
                        engine.stop('PROCESS_STOP_OR_FAILURE');engine.cancel()
                    except Exception:engine.stop('CANCEL_OUTCOME_UNCERTAIN')
            # Runs before physical/account locks are released, also on SIGTERM.
            stack.callback(cleanup)
            if args.command in ('sync','status','resume'):
                account=engine.reconcile()
                if account is None:raise DataError('Broker/ledger reconciliation failed')
                ops=broker.operations(store.meta()['started'],datetime.now(UTC));store.snapshot('OPERATIONS',ops,datetime.now(UTC))
                if args.command=='resume':
                    if args.acknowledge!=ACK or not args.allow_live:raise DataError('Explicit resume acknowledgement required')
                    if any(r['status'] in ('UNKNOWN','SENDING','REJECTED','CANCELLED','NOT_SENT') for r in store.orders()):raise DataError('Unresolved/terminal adverse order requires audited recovery, no blind resume')
                    # Temporarily ignoring STOP is not allowed: remove only after verified reconciliation and explicit command.
                    permission=json.loads((state/'LIVE_PERMISSION.json').read_text())
                    if permission['ledger_id']!=store.meta()['ledger_id'] or permission['config_hash']!=fingerprint(c) or not now<instant(permission['expires_at']):raise DataError('Resume permission invalid')
                    (state/'STOP').unlink(missing_ok=True)
                    with store.db() as db:db.execute('UPDATE live_meta SET halt=NULL WHERE id=1')
                result=statistics(store)
                for row in result['orders']:row.pop('payload',None)
                print(json.dumps(result,ensure_ascii=False,indent=2));return 0
            if args.command=='cancel':
                if not args.allow_live:raise DataError('Cancel needs --allow-live')
                engine.stop('OPERATOR_CANCEL');engine.cancel(force=True)
                print('Cancel requested and order state queried; inspect status before any restart.');return 0
            if not args.allow_live or not controls.permitted():raise DataError('LIVE disabled')
            # Never adopt existing manual positions/orders. Restarts reconcile own ledger first.
            if engine.reconcile() is None:raise DataError('Startup position mismatch')
            store.snapshot('OPERATIONS',broker.operations(store.meta()['started'],datetime.now(UTC)),datetime.now(UTC))
            def stop_signal(*unused):
                atomic_json(state/'STOP',dict(reason='PROCESS_STOP',at=iso(datetime.now(UTC))));raise KeyboardInterrupt()
            old_handler=signal.signal(signal.SIGTERM,stop_signal)
            previous=None;last_history=None;history_fresh=False
            while True:
                check();now=datetime.now(UTC)
                if not controls.permitted():event=engine.step(False)
                else:
                    if not last_history or (now-last_history).total_seconds()>=60:
                        instrument=broker.read.instrument(UID)
                        folder=state/'history'/now.strftime('%Y%m%dT%H%M%S_%fZ')
                        snapshot=download_daily(broker.read,instrument,instant(store.meta()['source_from']),now,folder,previous=previous,make_template=False)
                        check();history_fresh=engine.ingest(snapshot);previous=snapshot;last_history=now
                    check();event=engine.step(history_fresh)
                ops=broker.operations(store.meta()['started'],datetime.now(UTC))
                store.snapshot('OPERATIONS',ops,datetime.now(UTC))
                atomic_json(state/'health.json',dict(time=iso(datetime.now(UTC)),event=event,halt=store.meta()['halt'],position=store.position()))
                print(json.dumps(dict(mode='LIVE',event=event,position=store.position())),flush=True)
                if event=='HALTED':return 78
                if args.once:return 0
                time.sleep(c['poll_seconds'])
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        if store is not None and args.command not in ('status','preflight'):
            try:
                detail={k:getattr(exc,k,None) for k in ('method','http_status','content_type','category','grpc_code','api_code')} if isinstance(exc,APIError) else None
                store.halt('LIVE_API_DATA_OR_STATE_ERROR',datetime.now(UTC),detail)
            except Exception:pass
        # Do not reflect a token, account, endpoint response or arbitrary exception text.
        print('STOP: LIVE broker/data/state/permission failure. Inspect private status; no blind retry.')
        return 78
    finally:
        if old_handler is not None:signal.signal(signal.SIGTERM,old_handler)
        if previous_token is None:os.environ.pop('T_INVEST_TOKEN',None)
        else:os.environ['T_INVEST_TOKEN']=previous_token


if __name__=='__main__':raise SystemExit(main())
