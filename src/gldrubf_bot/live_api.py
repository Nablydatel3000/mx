"""Isolated LIVE broker adapter. PAPER's read-only allowlist is unchanged.

Single HTTP attempt for mutations; bounded retries for allowlisted reads only.
Ambiguous outcomes MUST be reconciled
by request UUID, never retried with a new UUID or silently treated as rejection.
"""
import json
import http.client
import urllib.request
import urllib.error
import errno
import ssl
from decimal import Decimal

from . import api
from .api_error_codes import read_error_codes,extract_codes
from .models import DataError,number,instant,iso,UTC
from .live_market import LiveReadClient
from .live_errors import DefinitelyNotSent
from .live_recovery import ReadRetries

UID='b347fe28-0d2a-45bf-b3bd-cda8a6ac64e6'
READ={'UsersService/GetAccounts','UsersService/GetMarginAttributes','OperationsService/GetPositions',
      'OperationsService/GetPortfolio','OperationsService/GetWithdrawLimits','OperationsService/GetOperationsByCursor',
      'OrdersService/GetOrders','OrdersService/GetOrderState','OrdersService/GetMaxLots','OrdersService/GetOrderPrice',
      'MarketDataService/GetOrderBook'}
WRITE={'OrdersService/PostOrder','OrdersService/CancelOrder'}


def integer(value):
    if isinstance(value,bool) or str(int(value))!=str(value):raise DataError('Invalid broker integer')
    return int(value)


def rub(value):
    if not isinstance(value,dict) or value.get('currency','').lower()!='rub':
        raise DataError('Missing/non-RUB broker amount')
    return api.quotation(value)


def money_array(rows):
    values=[]
    for row in rows:
        value=api.quotation(row)
        if row.get('currency','').lower()!='rub' and value:raise DataError('Non-RUB exposure')
        if row.get('currency','').lower()=='rub':values.append(value)
    if len(values)>1:raise DataError('Duplicate currency balance')
    return values[0] if values else Decimal(0)


def quoted(value):
    value=number(value);units=int(value);nano=int((value-units)*10**9)
    if Decimal(units)+Decimal(nano)/10**9!=value:raise DataError('Price exceeds API precision')
    return dict(units=str(units),nano=nano)


class RetryingReadClient(LiveReadClient):
    def __init__(self,retries,**kwargs):
        self.retries=retries
        super().__init__(**kwargs)

    def call(self,method,payload):
        return self.retries.call(method,lambda:super(RetryingReadClient,self).call(method,payload))


class Broker:
    def __init__(self,account,*,transport=None,write_guard=None,retries=None):
        self.retries=retries or ReadRetries()
        raw_transport=transport or urllib.request.build_opener(api.NoRedirect()).open
        def checked_transport(request,**kwargs):
            try:return raw_transport(request,**kwargs)
            except OSError as exc:
                reason=exc.reason if isinstance(exc,urllib.error.URLError) else exc
                if isinstance(reason,OSError) and not isinstance(reason,ssl.SSLError) and reason.errno in (errno.EACCES,errno.EPERM,errno.EINVAL,errno.ENOENT,errno.ENOSPC,errno.ENOMEM,errno.EMFILE,errno.ENFILE):
                    method=request.full_url.removeprefix(api.BASE)
                    raise api._failure(method,None,'unknown','local_transport_error') from None
                raise
        self.account_id=account;self.read=RetryingReadClient(self.retries,transport=checked_transport,attempts=1)
        self.transport=checked_transport
        self.write_guard=write_guard

    def call(self,method,payload):
        if method in api.ALLOWED:return self.read.call(method,payload)
        if method in READ:return self.retries.call(method,lambda:self._call_once(method,payload))
        return self._call_once(method,payload)

    def _call_once(self,method,payload):
        try:
            if method not in READ|WRITE:raise DataError('LIVE endpoint prohibited')
            if payload.get('accountId',self.account_id)!=self.account_id:raise DataError('Wrong account')
            if method in WRITE:
                if self.write_guard is None:raise DataError('LIVE mutations disabled')
                self.write_guard(method,payload)
            req=urllib.request.Request(api.BASE+method,json.dumps(payload).encode(),
                {'Authorization':'Bearer '+self.read._token,'Content-Type':'application/json','Accept':'application/json'},method='POST')
        except Exception:
            if method=='OrdersService/PostOrder':raise DefinitelyNotSent('PostOrder refused before HTTP transport') from None
            raise
        status=None;ctype='unknown'
        try:
            with self.transport(req,timeout=20) as response:
                status,ctype=api._metadata(response)
                if status!=200:raise api._http_failure(method,status,ctype,codes=read_error_codes(response,ctype))
                raw=response.read(api.MAX_RESPONSE_BYTES+1)
            if len(raw)>api.MAX_RESPONSE_BYTES or not raw or ctype!='application/json':
                raise api._failure(method,status,ctype,'unexpected_content_type')
            try:result=json.loads(raw)
            except (ValueError,UnicodeError):raise api._failure(method,status,ctype,'invalid_json') from None
            if not isinstance(result,dict) or any(k in result for k in ('code','message','error')):
                raise api._failure(method,status,ctype,'api_error_envelope',codes=extract_codes(result))
            return result
        except urllib.error.HTTPError as exc:
            try:error=api._http_failure(method,exc.code,api._metadata(exc)[1],codes=read_error_codes(exc,api._metadata(exc)[1]))
            finally:exc.close()
            raise error from None
        except api.APIError:raise
        except DefinitelyNotSent:
            # Transport has been entered: even this exception type is NOT a certificate.
            raise api._failure(method,status,ctype,'transport_outcome_uncertain') from None
        except (OSError,ValueError,http.client.HTTPException) as exc:
            if isinstance(exc,ValueError):raise api._failure(method,status,ctype,'request_or_response_value_error') from None
            if isinstance(exc,(OSError,http.client.HTTPException)):
                network=api._network_failure(method,status,ctype,exc)
                if network.category in ('tls_verification_error','tls_error','proxy_tunnel_error'):
                    raise network from None
            raise api._failure(method,status,ctype,'transport_or_request_error') from None

    def accounts(self):
        rows=self.call('UsersService/GetAccounts',{}).get('accounts',[])
        matches=[r for r in rows if r.get('id')==self.account_id]
        if len(matches)!=1 or matches[0].get('status')!='ACCOUNT_STATUS_OPEN' or matches[0].get('type')!='ACCOUNT_TYPE_TINKOFF' or matches[0].get('accessLevel')!='ACCOUNT_ACCESS_LEVEL_FULL_ACCESS':
            raise DataError('Dedicated broker account/full access not verified')

    def account(self):
        payload={'accountId':self.account_id}
        p=self.call('OperationsService/GetPositions',payload)
        portfolio=self.call('OperationsService/GetPortfolio',dict(**payload,currency='RUB'))
        w=self.call('OperationsService/GetWithdrawLimits',payload)
        m=self.call('UsersService/GetMarginAttributes',payload)
        for result in (p,portfolio):
            if result.get('accountId')!=self.account_id:raise DataError('Broker account echo mismatch')
        if p.get('limitsLoadingInProgress',False):raise DataError('Broker limits loading')
        for key in ('securities','options'):
            if any(integer(r.get('balance','0')) or integer(r.get('blocked','0')) for r in p.get(key,[])):
                raise DataError('Other account exposure')
        rows=p.get('futures',[])
        if any(r.get('instrumentUid')!=UID and (integer(r.get('balance','0')) or integer(r.get('blocked','0'))) for r in rows):
            raise DataError('Other futures exposure')
        own=[r for r in rows if r.get('instrumentUid')==UID]
        if len(own)>1:raise DataError('Ambiguous futures position')
        balance=integer(own[0].get('balance','0')) if own else 0
        blocked=integer(own[0].get('blocked','0')) if own else 0
        pp=[r for r in portfolio.get('positions',[]) if r.get('instrumentUid')==UID]
        if len(pp)>1:raise DataError('Ambiguous portfolio position')
        for r in portfolio.get('positions',[]):
            if r.get('instrumentUid')!=UID and r.get('instrumentType')!='currency' and api.quotation(r.get('quantity',{})):
                raise DataError('Other portfolio instrument')
        quantity=api.quotation(pp[0]['quantity']) if pp else Decimal(0)
        if quantity!=int(quantity) or abs(quantity)>2 or blocked<0 or blocked>2:
            raise DataError('Position limit/quantity violated')
        position=int(quantity)
        if not blocked and balance!=position:raise DataError('Position sources disagree')
        liquid=rub(m['liquidPortfolio']);starting=rub(m['startingMargin']);minimum=rub(m['minimalMargin'])
        corrected=rub(m['correctedMargin']);missing=rub(m['amountOfMissingFunds'])
        if min(starting,minimum,corrected)<0 or starting<minimum:
            raise DataError('Unsafe margin account')
        cash=money_array(p.get('money',[]))
        conservative=min(cash-money_array(p.get('blocked',[])),
            money_array(w.get('money',[]))-money_array(w.get('blocked',[]))-money_array(w.get('blockedGuarantee',[])),
            liquid-starting,liquid-corrected)
        return dict(position=position,blocked=blocked,balance_rub=str(cash),liquid_portfolio_rub=str(liquid),
            portfolio_value_rub=str(rub(portfolio['totalAmountPortfolio'])),available_conservative_rub=str(conservative),
            broker_initial_margin_rub=str(starting),broker_minimal_margin_rub=str(minimum),
            margin_deficit=missing>0 or liquid<max(starting,minimum,corrected),
            broker_var_margin_rub=str(rub(pp[0]['varMargin'])) if pp and 'varMargin' in pp[0] else None,
            broker_expected_yield_raw=str(api.quotation(pp[0]['expectedYield'])) if pp and 'expectedYield' in pp[0] else None)

    def orders(self):return self.call('OrdersService/GetOrders',{'accountId':self.account_id}).get('orders',[])

    def order(self,request_id):
        return self.call('OrdersService/GetOrderState',dict(accountId=self.account_id,orderId=request_id,
            orderIdType='ORDER_ID_TYPE_REQUEST',priceType='PRICE_TYPE_POINT'))

    def post(self,payload):return self.call('OrdersService/PostOrder',payload)

    def cancel(self,request_id):
        return self.call('OrdersService/CancelOrder',dict(accountId=self.account_id,orderId=request_id,orderIdType='ORDER_ID_TYPE_REQUEST'))

    def margin(self,instrument):
        raw=self.call('InstrumentsService/GetFuturesMargin',{'instrumentId':UID})
        tick=api.quotation(raw['minPriceIncrement']);amount=api.quotation(raw['minPriceIncrementAmount'])
        long=rub(raw['initialMarginOnBuy']);short=rub(raw['initialMarginOnSell'])
        if tick!=instrument.tick or min(tick,amount,long,short)<=0:raise DataError('Invalid current futures margin')
        return dict(rub_per_point=str(amount/tick),long_rub=str(long),short_rub=str(short))

    def capacity(self,side):
        raw=self.call('OrdersService/GetMaxLots',dict(accountId=self.account_id,instrumentId=UID))
        if raw.get('currency','').lower()!='rub':raise DataError('Capacity currency')
        key='buyMarginLimits' if side==1 else 'sellMarginLimits'
        return integer(raw[key]['buyMaxLots' if side==1 else 'sellMaxLots'])

    def fee(self,side,quantity,price):
        raw=self.call('OrdersService/GetOrderPrice',dict(accountId=self.account_id,instrumentId=UID,
            direction='ORDER_DIRECTION_BUY' if side==1 else 'ORDER_DIRECTION_SELL',quantity=str(quantity),price=quoted(price)))
        fee=rub(raw['executedCommissionRub'])
        if fee<0:raise DataError('Negative fee estimate')
        return fee

    def operations(self,start,end):
        cursor=None;seen=set();rows={}
        for _ in range(100):
            payload=dict(accountId=self.account_id,**{'from':start,'to':iso(end)},limit=1000,
                         withoutCommissions=False,withoutTrades=False,withoutOvernights=False)
            if cursor:payload['cursor']=cursor
            raw=self.call('OperationsService/GetOperationsByCursor',payload)
            for r in raw.get('items',[]):
                if r.get('brokerAccountId')!=self.account_id:raise DataError('Operation account mismatch')
                stamp=iso(instant(r['date']))
                if not instant(start)<=instant(stamp)<=end:raise DataError('Operation outside requested interval')
                if r.get('instrumentUid') not in (None,'',UID):raise DataError('Foreign operation')
                row=dict(id=r['id'],date=stamp,type=r['type'],state=r['state'],
                    payment_rub=str(rub(r['payment'])) if 'payment' in r else None,
                    commission_rub=str(rub(r['commission'])) if 'commission' in r else None,
                    broker_yield_rub=str(rub(r['yield'])) if 'yield' in r else None)
                if row['id'] in rows and rows[row['id']]!=row:raise DataError('Conflicting broker operation identity')
                rows[row['id']]=row
            if not raw.get('hasNext',False):return list(rows.values())
            cursor=raw.get('nextCursor')
            if not cursor or cursor in seen:raise DataError('Operations pagination ambiguous')
            seen.add(cursor)
        raise DataError('Operations snapshot incomplete')
