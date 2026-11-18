"""Read-only LIVE acceptance. No Engine, Store, permission, order or cancel path.

The trading token is read locally from a protected file descriptor. Only a
closed READ whitelist reaches HTTP, even if a write guard is accidentally set.
"""
import argparse
from copy import deepcopy
from datetime import datetime,timedelta,time as daytime
from decimal import Decimal as D
import getpass
import json
import os
from pathlib import Path
import re
import sys
import warnings

from . import api
from .autonomous import history_age,same_window
from .live import load_config,risk_check
from .live_api import Broker,UID,integer,rub
from .locking import runner_lock
from .models import DataError,UTC,instant,iso,number,Quote
from .operations import atomic_json

ROOT=Path('/var/lib/gldrubf-live-acceptance')
CONFIG=Path('/etc/gldrubf-live-acceptance/config.json')
TOKEN=Path('/etc/gldrubf-live-acceptance/trading-token')
PROTECTED_ROOTS=tuple(Path(p) for p in ('/var/lib/gldrubf','/etc/gldrubf','/opt/gldrubf',
    '/var/lib/gldrubf-live','/etc/gldrubf-live','/opt/gldrubf-live'))
PROPOSED=dict(account_id='REQUIRED_EXPLICIT_DEDICATED_BROKER_ACCOUNT',capital_rub='15000',contracts=2,instrument_uid=UID,
    max_complete_age_hours=96,quote_max_age_seconds=15,snapshot_max_age_seconds=30,
    margin_stress_multiplier='3',cash_reserve_rub='2000',max_order_fee_rub='20',
    max_slippage_ticks=2,order_ttl_seconds=30,poll_seconds=15,min_session_remaining_seconds=60)

FIELDS={
 'UsersService/GetAccounts':['accounts'],
 'InstrumentsService/FutureBy':['instrument'],
 'InstrumentsService/GetFuturesMargin':['minPriceIncrement','minPriceIncrementAmount','initialMarginOnBuy','initialMarginOnSell'],
 'OperationsService/GetPositions':['accountId','money','blocked','futures','limitsLoadingInProgress'],
 'OperationsService/GetPortfolio':['accountId','positions','totalAmountPortfolio'],
 'OperationsService/GetWithdrawLimits':['money','blocked','blockedGuarantee'],
 'UsersService/GetMarginAttributes':['liquidPortfolio','startingMargin','minimalMargin','correctedMargin','amountOfMissingFunds'],
 'MarketDataService/GetTradingStatus':['instrumentUid','tradingStatus','apiTradeAvailableFlag','limitOrderAvailableFlag','marketOrderAvailableFlag'],
 'MarketDataService/GetLastPrices':['lastPrices'],
 'MarketDataService/GetOrderBook':['instrumentUid','orderbookTs','asks','bids','limitUp','limitDown'],
 'InstrumentsService/TradingSchedules':['exchanges'],
 'MarketDataService/GetCandles':['candles'],
 'OrdersService/GetOrders':['orders'],
 'OrdersService/GetMaxLots':['currency','buyLimits','buyMarginLimits','sellLimits','sellMarginLimits'],
 'OrdersService/GetOrderPrice':['executedCommissionRub','executedCommission','serviceCommission','dealCommission','lotsRequested','extraFuture'],
 'OperationsService/GetOperationsByCursor':['items','hasNext','nextCursor'],
 'OrdersService/GetOrderState':['orderRequestId','orderId','instrumentUid','executionReportStatus','lotsRequested','lotsExecuted','stages','executedCommission']}
READ_METHODS=frozenset(FIELDS)  # Independent fixed whitelist, never inherited from a mutation-capable adapter.


def safe_error(exc):
    if isinstance(exc,api.APIError):
        return dict(category=exc.category or 'api_error',http_status=exc.http_status,
            content_type=exc.content_type,grpc_code=exc.grpc_code,api_code=exc.api_code)
    return dict(category='schema_or_validation_error' if isinstance(exc,(DataError,KeyError,TypeError,ValueError)) else 'local_or_transport_error')


def numeric(value,kind):
    try:return str(rub(value) if kind=='rub' else api.quotation(value) if kind=='quotation' else integer(value))
    except Exception:return None


def projection(method,result):
    # Expected field names and selected numeric values only. No arbitrary body,
    # names, account/order IDs, Authorization, messages or descriptions.
    out=dict(fields_present={k:k in result for k in FIELDS.get(method,[])})
    money_fields={
      'InstrumentsService/GetFuturesMargin':['initialMarginOnBuy','initialMarginOnSell'],
      'UsersService/GetMarginAttributes':['liquidPortfolio','startingMargin','minimalMargin','correctedMargin','amountOfMissingFunds'],
      'OrdersService/GetOrderPrice':['executedCommissionRub','executedCommission','serviceCommission','dealCommission']}
    for key in money_fields.get(method,[]):out[key]=numeric(result.get(key),'rub')
    if method=='InstrumentsService/GetFuturesMargin':
        for key in ('minPriceIncrement','minPriceIncrementAmount'):out[key]=numeric(result.get(key),'quotation')
    if method=='OrdersService/GetMaxLots':
        for key in ('buyLimits','buyMarginLimits','sellLimits','sellMarginLimits'):
            view=result.get(key,{})
            out[key]={k:numeric(view[k],'int') for k in ('buyMaxLots','buyMaxMarketLots','sellMaxLots') if isinstance(view,dict) and k in view}
    for key in ('accounts','orders','positions','futures','candles','items','lastPrices','stages'):
        if isinstance(result.get(key),list):out[key+'_count']=len(result[key])
    return out


class ReadOnlyBroker(Broker):
    def __init__(self,account,*,transport=None,clock=lambda:datetime.now(UTC)):
        super().__init__(account,transport=transport,write_guard=None)
        self.clock=clock;self.calls=[];self._original_read=self.read.call
        self.read.call=self.call

    def call(self,method,payload):
        if method not in READ_METHODS:raise DataError('Acceptance forbids all mutation endpoints')
        started=self.clock()
        try:
            result=self._original_read(method,payload) if method in api.ALLOWED else super().call(method,payload)
            self.calls.append(dict(method=method,http_status=200,category='ok',
                elapsed_seconds=max(0,(self.clock()-started).total_seconds()),**projection(method,result)))
            return result
        except Exception as exc:
            self.calls.append(dict(method=method,elapsed_seconds=max(0,(self.clock()-started).total_seconds()),**safe_error(exc)))
            raise

    def post(self,*args,**kwargs):raise DataError('Acceptance cannot post orders')
    def cancel(self,*args,**kwargs):raise DataError('Acceptance cannot cancel orders')


def collect(c,broker,clock=lambda:datetime.now(UTC)):
    started=clock();checks=[];values={};stats={}
    def check(name,action):
        at=clock()
        try:
            value=action();values[name]=value
            if clock()<at:raise DataError('Clock moved backwards')
            checks.append(dict(check=name,result='ok',checked_at=iso(clock()),elapsed_seconds=max(0,(clock()-at).total_seconds())))
            return value
        except Exception as exc:
            checks.append(dict(check=name,result='blocked',checked_at=iso(clock()),**safe_error(exc)))
            return None
    check('account_full_access',broker.accounts)
    instrument=check('instrument',lambda:broker.read.instrument(UID))
    account_started=clock()
    account=check('account_snapshot',broker.account);account_checked=clock()
    if account is not None:
        stats.update(account);stats['account_checked_at']=iso(account_checked)
        if account['position']!=0 or account['blocked']:
            checks.append(dict(check='dedicated_flat_account',result='blocked',category='existing_position_or_blocked_balance'))
    def orders_schema():
        r=broker.orders()
        if not isinstance(r,list) or any(not isinstance(x,dict) for x in r):raise DataError('Orders array schema')
        return r
    orders=check('open_orders',orders_schema)
    if orders is not None:
        stats['open_order_count']=len(orders)
        if orders:checks.append(dict(check='no_open_orders',result='blocked',category='existing_orders_no_cancellation_attempted'))
    margin=check('current_futures_margin',lambda:broker.margin(instrument)) if instrument else None
    if margin is not None:
        stats['current_futures_margin']=margin
        stats['tick_price_points']=str(instrument.tick)
        stats['tick_amount_rub']=str(instrument.tick*number(margin['rub_per_point']))
    status=check('trading_status',lambda:broker.call('MarketDataService/GetTradingStatus',{'instrumentId':UID}))
    if status is not None:
        def status_schema():
            if status.get('instrumentUid')!=UID:raise DataError('Status UID mismatch')
            for key in ('apiTradeAvailableFlag','limitOrderAvailableFlag','marketOrderAvailableFlag'):
                if key in status and type(status[key]) is not bool:raise DataError('Status flag type')
        check('trading_status_schema',status_schema)
    last=check('last_prices',lambda:broker.call('MarketDataService/GetLastPrices',{'instrumentId':[UID],'lastPriceType':'LAST_PRICE_EXCHANGE'}))
    quote=None
    if last is not None:
        def parse_quote():
            matches=[r for r in last.get('lastPrices',[]) if r.get('instrumentUid')==UID]
            if len(matches)!=1 or matches[0].get('lastPriceType','LAST_PRICE_EXCHANGE')!='LAST_PRICE_EXCHANGE':raise DataError('Quote source/UID')
            q=Quote(instant(matches[0]['time']),api.quotation(matches[0]['price']))
            if q.time>clock():raise DataError('Future quote')
            return q
        quote=check('last_price_schema',parse_quote)
    book=check('order_book',lambda:broker.call('MarketDataService/GetOrderBook',{'instrumentId':UID,'depth':1}))
    calendar=check('current_calendar',lambda:broker.read.current_sessions(instrument.exchange,clock())) if instrument else None
    start=datetime.combine(started.date(),daytime.min,UTC)-timedelta(days=14)
    candles=check('day_candles_sample',lambda:broker.read.candles(UID,start,clock()))
    if candles is not None:
        def age():return history_age([dict(time=r['time'],is_complete=r['isComplete']) for r in candles],clock(),c['max_complete_age_hours'])
        a=check('completed_candle_age',age)
        stats['history_sample']=dict(requested_from=iso(start),sample_count=len(candles),
            complete_count=sum(r['isComplete'] for r in candles),complete_age=a,coverage_verified=False,
            sample_not_full_strategy_warmup=True)
    capacities={};fees={}
    for side,label in ((1,'long'),(-1,'short')):
        cap=check('capacity_'+label,lambda side=side:broker.capacity(side))
        capacities[label]=cap
        if quote is not None and instrument is not None:
            price=quote.price+side*c['max_slippage_ticks']*instrument.tick
            fee=check('fee_two_contracts_'+label,lambda side=side,price=price:broker.fee(side,2,price))
            fees[label]=str(fee) if fee is not None else None
            if fee is not None and fee>number(c['max_order_fee_rub']):
                checks.append(dict(check='fee_ceiling_'+label,result='blocked',category='estimated_fee_exceeds_proposed_ceiling'))
    stats['capacity_two_contracts']=capacities;stats['fee_estimates_two_contracts_rub']=fees
    if account is not None and margin is not None:
        stats['capital_checks']={label:dict(**risk_check(c,account,margin,side,
            number(fees[label]) if fees.get(label) is not None else number(c['max_order_fee_rub'])),
            fee_source='broker_estimate' if fees.get(label) is not None else 'proposed_ceiling_not_actual_fee')
            for side,label in ((1,'long'),(-1,'short'))}
    op_start=iso(datetime.combine(started.date(),daytime.min,UTC)-timedelta(days=7))
    operations=check('operations_sample',lambda:broker.operations(op_start,clock()))
    if operations is not None:
        executed=[r for r in operations if r['state']=='OPERATION_STATE_EXECUTED']
        stats['operations_sample_from']=op_start;stats['operations_sample_count']=len(operations)
        stats['executed_trade_operation_count']=sum(r['type'] in ('OPERATION_TYPE_BUY','OPERATION_TYPE_SELL') for r in executed)
        stats['settled_variation_margin_cash_rub']=str(sum((number(r['payment_rub']) for r in executed if r['type'] in
            ('OPERATION_TYPE_ACCRUING_VARMARGIN','OPERATION_TYPE_WRITING_OFF_VARMARGIN') and r['payment_rub'] is not None),D(0)))
        stats['broker_fee_payment_rub']=str(sum((number(r['payment_rub']) for r in executed if r['type']=='OPERATION_TYPE_BROKER_FEE' and r['payment_rub'] is not None),D(0)))
        stats['pnl_scope']='Broker cash/operations in the stated sample, NOT PAPER or modeled closed-trade PnL. Do not double-count commissions.'
    # Only read an existing UUID, never create a probe order to obtain one.
    existing=[r for r in orders or [] if r.get('instrumentUid')==UID and r.get('orderRequestId')]
    if existing:
        def existing_state():
            request=existing[0]['orderRequestId'];r=broker.order(request)
            if r.get('instrumentUid')!=UID or r.get('orderRequestId')!=request:raise DataError('Existing order identity')
            if r.get('executionReportStatus') not in {'EXECUTION_REPORT_STATUS_'+s for s in ('FILL','REJECTED','CANCELLED','NEW','PARTIALLYFILL')}:
                raise DataError('Unknown order status')
            requested,executed=integer(r['lotsRequested']),integer(r.get('lotsExecuted','0'))
            if not 0<=executed<=requested or sum(integer(s['quantity']) for s in r.get('stages',[]))!=executed:raise DataError('Incomplete order stages')
            return r
        raw=check('existing_order_state',existing_state)
        stats['existing_order_state_read']=raw is not None
    else:
        checks.append(dict(check='order_state_runtime',result='not_tested',category='no_existing_request_uuid_no_probe_order_created'))
    finished=clock();market=dict(evaluated_at=iso(finished),tradable_now=False)
    if status is not None:
        market['normal_api_limit_status']=status.get('instrumentUid')==UID and status.get('tradingStatus')=='SECURITY_TRADING_STATUS_NORMAL_TRADING' and status.get('apiTradeAvailableFlag') is True and status.get('limitOrderAvailableFlag') is True
    if quote is not None:
        market.update(quote_time=iso(quote.time),quote_price=str(quote.price),
            quote_fresh=0<=(finished-quote.time).total_seconds()<=c['quote_max_age_seconds'])
    if book is not None:
        def book_checks():
            if book.get('instrumentUid')!=UID:raise DataError('Book UID')
            stamp=instant(book['orderbookTs'])
            if stamp>finished:raise DataError('Future book')
            fresh=0<=(finished-stamp).total_seconds()<=c['quote_max_age_seconds']
            for key in ('bids','asks'):
                if book.get(key) and (api.quotation(book[key][0]['price'])<=0 or integer(book[key][0]['quantity'])<1):raise DataError('Invalid book level')
            return dict(book_time=iso(stamp),book_fresh=fresh,
                best_bid=str(api.quotation(book['bids'][0]['price'])) if book.get('bids') else None,
                best_ask=str(api.quotation(book['asks'][0]['price'])) if book.get('asks') else None,
                limit_down=str(api.quotation(book['limitDown'])),limit_up=str(api.quotation(book['limitUp'])))
        b=check('orderbook_schema',book_checks)
        if b:market.update(b)
    if quote is not None and instrument is not None and market.get('best_ask') and market.get('best_bid'):
        buy=quote.price+c['max_slippage_ticks']*instrument.tick;sell=quote.price-c['max_slippage_ticks']*instrument.tick
        market['bounded_limit_long_points']=str(buy);market['bounded_limit_short_points']=str(sell)
        market['book_within_proposed_limits']=number(market['best_ask'],positive=True)<=buy and number(market['best_bid'],positive=True)>=sell
    if calendar is not None and quote is not None and market.get('book_time'):
        book_time=instant(market['book_time'])
        span=next(((a,b) for day in calendar.values() for a,b in day.windows if all(a<=t<b for t in (finished,quote.time,book_time))),None)
        market['same_continuous_interval']=span is not None
        market['session_buffer_ok']=bool(span and (span[1]-finished).total_seconds()>=c['min_session_remaining_seconds'])
        market['tradable_now']=all(market.get(k,False) for k in ('normal_api_limit_status','quote_fresh','book_fresh','same_continuous_interval','session_buffer_ok','book_within_proposed_limits'))
        market['current_windows']=[[iso(a),iso(b)] for day in calendar.values() for a,b in day.windows]
    stats['account_sample_duration_seconds']=max(0,(account_checked-account_started).total_seconds())
    stats['account_age_at_report_seconds']=max(0,(finished-account_started).total_seconds())
    stats['account_snapshot_fresh']=0<=(finished-account_started).total_seconds()<=c['snapshot_max_age_seconds']
    blockers=[r['check'] for r in checks if r['result']=='blocked']
    for label in ('long','short'):
        if capacities.get(label) is None or capacities[label]<2:blockers.append('capacity_two_'+label)
        if not stats.get('capital_checks',{}).get(label,{}).get('allowed',False):blockers.append('capital_'+label)
    if not stats.get('history_sample',{}).get('complete_age',{}):blockers.append('completed_history_missing')
    elif not stats['history_sample']['complete_age']['fresh']:blockers.append('completed_history_stale')
    if not stats['account_snapshot_fresh']:blockers.append('account_snapshot_stale')
    if finished<started:blockers.append('clock_backwards')
    return dict(schema=1,mode='LIVE_ACCEPTANCE_READ_ONLY',started_at=iso(started),checked_at=iso(finished),
        live_accepted=False,order_submission_count=0,order_cancellation_count=0,
        read_checks_ok=not blockers,blockers=sorted(set(blockers)),checks=checks,method_checks=broker.calls,
        market=market,statistics=stats,ioc=dict(contract_name='TIME_IN_FORCE_FILL_AND_KILL',
            instrument_runtime_support_confirmed=False,no_read_only_ioc_capability_flag=True,
            reason='Enum and flags do not prove exchange/instrument/account acceptance or fills; no probe order sent.'),
        next_stage_blockers=['real IOC/order-state/fill/cancel behavior not exercised','economic tariffs/funding and recovery acceptance still required'],
        no_live_db_permission_or_service_created=True)


def token_text(value):
    value=value.strip()
    if len(value)>8192 or not re.fullmatch(r'[A-Za-z0-9._~+/-]+=*',value):raise DataError('Invalid credential')
    return value


def hidden(prompt):
    if not sys.stdin.isatty() or not sys.stdout.isatty():raise DataError('Hidden local terminal required')
    with warnings.catch_warnings():
        warnings.simplefilter('error',getpass.GetPassWarning)
        return getpass.getpass(prompt).strip()


def status(state,clock=lambda:datetime.now(UTC)):
    state=Path(state)
    attempt=json.loads((state/'attempt.json').read_text())
    result=dict(last_attempt=attempt,live_accepted=False)
    if (state/'report.json').exists():
        report=json.loads((state/'report.json').read_text());age=(clock()-instant(report['checked_at'])).total_seconds()
        result.update(cached_report_age_seconds=age,cache_fresh=0<=age<=300,
            report_matches_last_attempt=report.get('run_id')==attempt.get('run_id'),report=report)
    return result


def display_status(result):
    report=result.get('report',{});s=report.get('statistics',{});m=s.get('current_futures_margin',{})
    print('GLDRUBF — READ-ONLY ACCEPTANCE; LIVE NOT ACCEPTED')
    print('Last attempt:',result['last_attempt'].get('status'),'| Cached report fresh:',result.get('cache_fresh',False),
          '| Matches latest attempt:',result.get('report_matches_last_attempt',False))
    if not result.get('report_matches_last_attempt',False) or result['last_attempt'].get('status')!='DONE':
        print('WARNING: cached values are from an earlier/incomplete run, not current acceptance.')
    for label,key in [('Balance RUB','balance_rub'),('Liquid RUB','liquid_portfolio_rub'),
                      ('Conservative available RUB','available_conservative_rub'),('Broker position','position')]:
        print(label+':',s.get(key,'unavailable'))
    print('GO LONG / SHORT RUB:',m.get('long_rub','unavailable'),'/',m.get('short_rub','unavailable'))
    print('RUB per price point:',m.get('rub_per_point','unavailable'))
    print('Broker settled VM RUB (sample):',s.get('settled_variation_margin_cash_rub','unavailable'))
    print('Broker fee payments RUB (sample, signed):',s.get('broker_fee_payment_rub','unavailable'))
    print('Open broker orders:',s.get('open_order_count','unavailable'))
    print('Executed trade operations (sample):',s.get('executed_trade_operation_count','unavailable'))
    print('Read-check blockers:',', '.join(report.get('blockers',[])) or 'none in cached report')
    print('Market gates now:',report.get('market',{}).get('tradable_now',False),'| IOC runtime support: UNCONFIRMED')
    print('Order submission/cancellation: permanently prohibited in this command.')


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['configure','account','accounts','install-token','check','status'])
    p.add_argument('--config',type=Path,default=CONFIG);p.add_argument('--state',type=Path,default=ROOT)
    p.add_argument('--credential-file',type=Path,default=TOKEN)
    p.add_argument('--credential-stdin',action='store_true')
    p.add_argument('--json',action='store_true',help='Status only: show safe structured diagnostics')
    args=p.parse_args(argv);previous=os.environ.get('T_INVEST_TOKEN');attempt=None
    try:
        os.environ.pop('SSLKEYLOGFILE',None)
        for candidate in (args.state,args.config,args.credential_file):
            path=candidate.resolve()
            if any(path==r.resolve() or r.resolve() in path.parents or path in r.resolve().parents for r in PROTECTED_ROOTS):
                raise DataError('PAPER or trading LIVE path forbidden for acceptance')
        if args.command=='configure':
            if args.config.exists():raise DataError('Existing config will not be overwritten')
            c=deepcopy(PROPOSED);selected=hidden('Dedicated broker account ID (hidden; blank if not yet known): ')
            if selected and (not selected.isascii() or not selected.isdigit()):raise DataError('Invalid account ID')
            if selected:c['account_id']=selected
            args.config.parent.mkdir(parents=True,exist_ok=True)
            fd=os.open(args.config,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o640)
            with os.fdopen(fd,'w') as stream:json.dump(c,stream,indent=2);stream.write('\n')
            print('Read-only acceptance profile written locally; no API/service/permission changes.');return 0
        if args.command=='account':
            old=args.config.stat();c=json.loads(args.config.read_text())
            selected=hidden('Explicit broker account ID from local account list (hidden): ')
            if not selected.isascii() or not selected.isdigit():raise DataError('Invalid account ID')
            if set(c)!=set(PROPOSED):raise DataError('Unexpected acceptance config')
            c['account_id']=selected;atomic_json(args.config,c)
            if os.name=='posix':os.chown(args.config,old.st_uid,old.st_gid)
            os.chmod(args.config,old.st_mode&0o777)
            print('Account explicitly selected in acceptance config only; no API request.');return 0
        if args.command=='install-token':
            value=token_text(hidden('Trading token (hidden, LOCAL ONLY; read-only diagnostic use): '))
            fd=os.open(args.credential_file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as stream:stream.write(value+'\n')
            print('Credential stored privately; no API request made.');return 0
        if args.command=='status':
            result=status(args.state)
            if args.json:print(json.dumps(result,ensure_ascii=False,indent=2))
            else:display_status(result)
            return 0
        args.state.mkdir(parents=True,exist_ok=True,mode=0o700)
        if os.name=='posix' and args.state.stat().st_mode&0o077:raise DataError('Private acceptance state required')
        if args.command=='accounts':
            if not args.credential_stdin or not sys.stdout.isatty():raise DataError('Local account list requires a console and protected stdin')
            os.environ['T_INVEST_TOKEN']=token_text(sys.stdin.buffer.read(8193).decode('ascii'))
            broker=ReadOnlyBroker('');raw=broker.call('UsersService/GetAccounts',{})
            eligible=[dict(account_id=r['id'],full_access=r.get('accessLevel')=='ACCOUNT_ACCESS_LEVEL_FULL_ACCESS')
                for r in raw.get('accounts',[]) if r.get('type')=='ACCOUNT_TYPE_TINKOFF' and r.get('status')=='ACCOUNT_STATUS_OPEN'
                and isinstance(r.get('id'),str) and r['id'].isascii() and r['id'].isdigit()]
            print('LOCAL ACCOUNT IDS; no automatic selection or file/log saved:')
            print(json.dumps(eligible,indent=2));return 0
        from uuid import uuid4
        with runner_lock(args.state/'read_only.lock'):
            attempt=dict(run_id=str(uuid4()),started_at=iso(datetime.now(UTC)),status='RUNNING')
            atomic_json(args.state/'attempt.json',attempt)
            c=load_config(args.config)
            if not args.credential_stdin:raise DataError('Use protected credential stdin via the check wrapper')
            value=token_text(sys.stdin.buffer.read(8193).decode('ascii'))
            os.environ['T_INVEST_TOKEN']=value
            broker=ReadOnlyBroker(c['account_id'])
            report=collect(c,broker);report['run_id']=attempt['run_id']
            atomic_json(args.state/'report.json',report)
            attempt.update(status='DONE',checked_at=report['checked_at'],read_checks_ok=report['read_checks_ok'])
            atomic_json(args.state/'attempt.json',attempt)
            print(json.dumps(dict(read_checks_ok=report['read_checks_ok'],blockers=report['blockers'],live_accepted=False)))
            return 0 if report['read_checks_ok'] else 2
    except (Exception,KeyboardInterrupt):
        if attempt is not None:
            attempt.update(status='FAILED',category='local_config_credential_or_unexpected_failure')
            try:atomic_json(args.state/'attempt.json',attempt)
            except Exception:pass
        print('STOP: read-only acceptance failed. No broker mutation; no credentials/body logged.');return 78
    finally:
        if previous is None:os.environ.pop('T_INVEST_TOKEN',None)
        else:os.environ['T_INVEST_TOKEN']=previous


if __name__=='__main__':raise SystemExit(main())
