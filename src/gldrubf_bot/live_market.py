"""LIVE-only temporal selection and LIMIT eligibility. PAPER parser stays unchanged."""
from datetime import datetime,timedelta,time as daytime
import hashlib
import re

from . import api
from .history import encoded
from .models import DataError,MOSCOW,UTC,Quote,instant,iso

PAIRS=(('startTime','endTime'),('eveningStartTime','eveningEndTime'),
       ('premarketStartTime','premarketEndTime'),('clearingStartTime','clearingEndTime'),
       ('openingAuctionStartTime','openingAuctionEndTime'),('closingAuctionStartTime','closingAuctionEndTime'))
REGULAR={'regular_trading_session','regular_trading_session_morning',
         'regular_trading_session_main','regular_trading_session_evening'}


def boundary(value):
    match=re.search(r'\.\d{6}(\d+)(?:Z|[+-]\d\d:\d\d)$',value) if isinstance(value,str) else None
    if match and any(c!='0' for c in match[1]):raise DataError('Unsupported calendar precision')
    return instant(value)


def execution_request(exchange,now):
    if not isinstance(exchange,str) or not exchange.strip():raise DataError('Invalid execution section')
    now=instant(now);local=now.astimezone(MOSCOW)
    local_end=datetime.combine(local.date()+timedelta(days=1),daytime.min,MOSCOW).astimezone(UTC)
    start=datetime.combine(now.date(),daytime.min,UTC)
    return dict(exchange=exchange,**{'from':iso(start),'to':iso(max(start+timedelta(days=1),local_end))})


def temporal_record(raw):
    """Read every absolute bound before proving a record irrelevant. No phase inference."""
    if not isinstance(raw,dict):raise DataError('Invalid execution calendar day')
    label=boundary(raw['date']).date();flag=raw.get('isTradingDay',False)
    if type(flag) is not bool:raise DataError('Invalid execution calendar flag')
    spans=[];envelopes=[]
    for a,b in PAIRS:
        if bool(raw.get(a))!=bool(raw.get(b)):raise DataError('Incomplete execution legacy pair')
        if raw.get(a):
            x,y=boundary(raw[a]),boundary(raw[b])
            if x>=y:raise DataError('Nonpositive execution legacy pair')
            spans.append((x,y))
            if a in ('startTime','eveningStartTime','premarketStartTime'):envelopes.append((x,y))
    if raw.get('eveningOpeningAuctionStartTime'):
        if not raw.get('eveningStartTime'):raise DataError('Incomplete execution evening auction')
        x,y=boundary(raw['eveningOpeningAuctionStartTime']),boundary(raw['eveningStartTime'])
        if x>=y:raise DataError('Nonpositive execution evening auction')
        spans.append((x,y))
    rows=raw.get('intervals',[])
    if not isinstance(rows,list) or (flag and not rows):raise DataError('Execution detailed bounds missing')
    detailed=set()
    for row in rows:
        if not isinstance(row,dict) or not isinstance(row.get('interval'),dict) or not isinstance(row.get('type'),str) or not row['type']:
            raise DataError('Invalid execution detailed interval')
        a,b=boundary(row['interval']['startTs']),boundary(row['interval']['endTs'])
        if a>=b:raise DataError('Nonpositive execution detailed bounds')
        if row['type'] in REGULAR and (not envelopes or not any(x<=a and b<=y for x,y in envelopes)):
            raise DataError('Execution regular/legacy contradiction')
        detailed.add((a,b));spans.append((a,b))
    ordered=sorted(detailed)
    if any(b[0]<a[1] for a,b in zip(ordered,ordered[1:])):raise DataError('Execution detailed overlap')
    if not flag and spans:raise DataError('Nontrading day contains execution intervals')
    return label,spans


def select_sessions(response,exchange,now):
    now=instant(now);summary=api.calendar_response_summary(response,exchange)
    if not summary['response_shape_valid'] or summary['casefold_matches']!=1:
        raise DataError('Execution section absent or ambiguous')
    record=next(r for r in response['exchanges'] if api.exchange_matches(r['exchange'],exchange))
    rows=record.get('days',[])
    if not isinstance(rows,list):raise DataError('Invalid current calendar days; no permission inferred')
    if not rows:raise DataError('Execution calendar coverage missing')
    local=now.astimezone(MOSCOW)
    start=datetime.combine(local.date(),daytime.min,MOSCOW).astimezone(UTC);end=start+timedelta(days=1)
    selected={};seen={};excluded=[]
    for index,raw in enumerate(rows):
        if not isinstance(raw,dict):raise DataError('Invalid execution calendar day')
        label=boundary(raw['date']).date()
        identity=encoded(raw)
        if label in seen and seen[label]!=identity:raise DataError('Conflicting execution date records')
        seen[label]=identity
        label,spans=temporal_record(raw)
        relevant=label in {now.date(),local.date()} or any(a<end and start<b for a,b in spans)
        if relevant:
            # Unknown types in any current/crossing record still fail closed.
            day=api.parse_day(raw)
            # A permission derived for this MSK day cannot survive its horizon.
            # Keep the official close unchanged; only shorten execution permission.
            windows=tuple((a,min(b,end)) for a,b in day.windows if a<min(b,end))
            selected[day.day]=api.TradingDay(day.day,day.trading,windows,day.close)
        else:excluded.append(index)
    if not selected:raise DataError('Execution calendar coverage missing')
    windows=sorted((a,b) for d in selected.values() for a,b in d.windows)
    if any(b[0]<a[1] for a,b in zip(windows,windows[1:])):raise DataError('Overlapping execution dates')
    evidence=dict(selection='absolute-bounds-msk-day-v1',period_from=iso(start),period_to=iso(end),
        checked_at=iso(now),excluded_record_indexes=excluded,response_sha256=hashlib.sha256(encoded(response)).hexdigest())
    return selected,evidence


def limit_status(status,uid):
    if not isinstance(status,dict) or status.get('instrumentUid')!=uid:raise DataError('LIMIT status UID mismatch')
    for key in ('apiTradeAvailableFlag','limitOrderAvailableFlag','onlyBestPrice'):
        if key in status and type(status[key]) is not bool:raise DataError('LIMIT status flag schema')
    return (status.get('tradingStatus')=='SECURITY_TRADING_STATUS_NORMAL_TRADING'
        and status.get('apiTradeAvailableFlag') is True and status.get('limitOrderAvailableFlag') is True
        and status.get('onlyBestPrice',False) is False)


class LiveReadClient(api.ReadOnlyClient):
    def current_sessions(self,exchange,now):
        now=instant(now);self.last_calendar_evidence=None
        result=self.call('InstrumentsService/TradingSchedules',execution_request(exchange,now))
        calendar,self.last_calendar_evidence=select_sessions(result,exchange,now)
        return calendar

    def quote(self,instrument,now):
        status=self.call('MarketDataService/GetTradingStatus',{'instrumentId':instrument.uid})
        if not limit_status(status,instrument.uid):return None
        result=self.call('MarketDataService/GetLastPrices',{'instrumentId':[instrument.uid],'lastPriceType':'LAST_PRICE_EXCHANGE'})
        rows=result.get('lastPrices',[])
        if not isinstance(rows,list):raise DataError('Invalid LIMIT quote list')
        matches=[r for r in rows if isinstance(r,dict) and r.get('instrumentUid')==instrument.uid]
        if len(matches)!=1 or matches[0].get('lastPriceType','LAST_PRICE_EXCHANGE')!='LAST_PRICE_EXCHANGE':raise DataError('Invalid LIMIT quote source')
        q=Quote(instant(matches[0]['time']),api.quotation(matches[0]['price']))
        if q.time>instant(now() if callable(now) else now) or q.price<=0:raise DataError('Invalid/future LIMIT quote')
        return q
