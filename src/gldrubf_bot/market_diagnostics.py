"""Read-only calendar and IOC-bound evidence; existing session parser is authoritative."""
import re
from . import api
from .diagnose_schedule_snapshot import LEGACY,utc_stamp
from .models import DataError,MOSCOW,instant,iso,number
from .live_market import execution_request,select_sessions

REASONS={
 'Execution section absent or ambiguous':'missing_or_ambiguous_exchange',
 'Execution calendar coverage missing':'calendar_coverage_missing',
 'Execution detailed bounds missing':'missing_detailed_intervals',
 'Execution regular/legacy contradiction':'detailed_legacy_contradiction',
 'Incomplete execution legacy pair':'incomplete_legacy_pair',
 'Nonpositive execution legacy pair':'nonpositive_legacy_pair',
 'Conflicting execution date records':'conflicting_day_records',
 'Nontrading day contains execution intervals':'nontrading_with_intervals',
 'Execution detailed overlap':'overlapping_detailed_intervals',
 'Overlapping execution dates':'overlapping_calendar_records',
 'Invalid execution calendar day':'invalid_day_object',
 'Invalid execution calendar flag':'invalid_trading_day_flag',
 'Invalid execution detailed interval':'invalid_detailed_interval_shape',
 'Nonpositive execution detailed bounds':'nonpositive_detailed_interval',
 'Unsupported calendar precision':'unsupported_timestamp_precision',
 'Incomplete execution evening auction':'evening_auction_missing_session',
 'Nonpositive execution evening auction':'evening_auction_invalid_bounds',
 'Missing calendar trading flag':'invalid_trading_day_flag',
 'Calendar precision exceeds supported microseconds':'unsupported_timestamp_precision',
 'Trading day without legacy envelope for detailed-session validation':'missing_legacy_envelope',
 'Evening auction lacks a supported regular-session start':'evening_auction_missing_session',
 'Evening auction cannot be separated from regular session':'evening_auction_invalid_bounds',
 'Detailed trading intervals required; legacy envelope is not execution permission':'missing_detailed_intervals',
 'Invalid detailed trading interval':'invalid_detailed_interval_shape',
 'Unknown detailed trading phase; execution blocked':'unknown_detailed_phase',
 'Incomplete detailed trading interval':'missing_detailed_boundary',
 'Invalid detailed trading interval bounds':'nonpositive_detailed_interval',
 'Detailed phase contradicts legacy bounds':'detailed_legacy_contradiction',
 'Detailed evening phase contradicts legacy evening bounds':'evening_legacy_contradiction',
 'Same detailed interval assigned contradictory phases':'contradictory_phase_aliases',
 'Same detailed interval assigned different regular sessions':'contradictory_regular_aliases',
 'Overlapping non-identical detailed phases; execution blocked':'overlapping_detailed_intervals',
 'No supported continuous trading window remains':'no_supported_regular_windows',
 'Conflicting current session records':'conflicting_day_records',
 'Invalid current calendar days; no permission inferred':'invalid_days_array',
 'Timestamp must include UTC offset':'timestamp_without_timezone',
 'Invalid timezone-aware timestamp':'invalid_timestamp'}


def reason(exc):
    text=str(exc)
    if text in REASONS:return REASONS[text]
    for prefix,code in (('Incomplete calendar interval: ','incomplete_legacy_pair'),
                        ('Invalid calendar interval: ','nonpositive_legacy_pair'),
                        ('Invalid current exchange calendar response;','invalid_exchange_response'),
                        ('Missing/ambiguous current exchange calendar;','missing_or_ambiguous_exchange')):
        if text.startswith(prefix):return code
    return 'invalid_calendar_schema'  # Never echo exception text or server content.


class CalendarError(DataError):
    def __init__(self,code):
        super().__init__('Read-only current calendar validation failed');self.reason=code


def stamp(value):
    try:return dict(valid=True,utc=utc_stamp(value)) if value is not None else dict(valid=False,utc=None)
    except (DataError,ValueError,TypeError):return dict(valid=False,utc=None)


def day_evidence(raw,index,token=''):
    item=dict(index=index)
    if not isinstance(raw,dict):return dict(**item,parse_ok=False,reason='day_not_object')
    item.update(date=stamp(raw.get('date')),is_trading_day=raw.get('isTradingDay',False) if type(raw.get('isTradingDay',False)) is bool else None,
        legacy={key:dict(present=key in raw,**stamp(raw.get(key))) for key in LEGACY})
    rows=raw.get('intervals');item['intervals_is_list']=isinstance(rows,list)
    item['intervals_count']=len(rows) if isinstance(rows,list) else None
    item['intervals']=[]
    for n,row in enumerate(rows[:512] if isinstance(rows,list) else []):
        row=row if isinstance(row,dict) else {};kind=row.get('type')
        # Phase names only, not arbitrary text, numeric IDs or UUIDs.
        safe=kind if isinstance(kind,str) and re.fullmatch('[a-z_]{1,80}',kind) and not (token and token in kind) else 'redacted_or_invalid_type'
        bounds=row.get('interval');bounds=bounds if isinstance(bounds,dict) else {}
        item['intervals'].append(dict(index=n,type=safe,start=stamp(bounds.get('startTs')),end=stamp(bounds.get('endTs'))))
    try:
        parsed=api.parse_day(raw)
        item.update(parse_ok=True,windows_utc=[[iso(a),iso(b)] for a,b in parsed.windows])
    except (DataError,KeyError,TypeError,ValueError,AttributeError) as exc:
        item.update(parse_ok=False,reason=reason(exc))
    return item


def inspect_calendar(client,exchange,now):
    now=instant(now);payload=execution_request(exchange,now)
    response=client.call('InstrumentsService/TradingSchedules',payload)
    rows=response.get('exchanges',[]) if isinstance(response,dict) else None
    matches=[r for r in rows if isinstance(r,dict) and api.exchange_matches(r.get('exchange'),exchange)] if isinstance(rows,list) else []
    raw_days=matches[0].get('days',[]) if len(matches)==1 else None
    token=getattr(getattr(client,'read',client),'_token','')
    summary=api.calendar_response_summary(response,exchange,token=token)
    def section(value):
        if isinstance(value,str) and value.casefold()=='forts_futures_weekend' and not (token and token in value):return value
        return api.public_exchange(value,token=token)
    summary['requested_exchange']=section(exchange)
    summary['returned_exchanges']=sorted({section(row.get('exchange')) if isinstance(row,dict) else 'other' for row in rows}) if isinstance(rows,list) else []
    # Preserve case-match counts, but only public allowlisted codes from summary.
    report=dict(schema=1,mode='CURRENT_CALENDAR_DIAGNOSTICS_READ_ONLY',
        evaluated_at_utc=iso(now),evaluated_at_moscow=now.astimezone(MOSCOW).isoformat(),
        utc_date=now.date().isoformat(),moscow_date=now.astimezone(MOSCOW).date().isoformat(),
        request_from_utc=payload['from'],request_to_utc=payload['to'],exchange_summary=summary,
        days=[day_evidence(d,i,token) for i,d in enumerate(raw_days[:32])] if isinstance(raw_days,list) else [],
        days_truncated=isinstance(raw_days,list) and len(raw_days)>32,
        calendar_validated=False,inside_confirmed_window=False,end_semantics='start_inclusive_end_exclusive',
        live_accepted=False)
    try:calendar,selection=select_sessions(response,exchange,now)
    except (DataError,KeyError,TypeError,ValueError,AttributeError) as exc:
        report.update(failure=dict(reason=reason(exc)),state='calendar_validation_error');return None,report
    windows=[(a,b) for d in calendar.values() for a,b in d.windows]
    inside=any(a<=now<b for a,b in windows)
    report.update(calendar_validated=True,validation_scope='current_execution_only',selection=selection,inside_confirmed_window=inside,
        has_current_utc_date_label=now.date() in calendar,
        state='inside_confirmed_window' if inside else 'outside_confirmed_windows',
        windows_utc=[[iso(a),iso(b)] for a,b in windows],failure=None)
    return calendar,report


def price_bounds(quote,instrument,market,config):
    """Same last-price +/- ticks formula as LIVE; evidence never sends an order."""
    tick=instrument.tick;buy=quote.price+config['max_slippage_ticks']*tick;sell=quote.price-config['max_slippage_ticks']*tick
    result=dict(reference='exchange_last_price',tick_price_points=str(tick),
        max_slippage_ticks=config['max_slippage_ticks'],bounded_limit_long_points=str(buy),bounded_limit_short_points=str(sell))
    down=number(market['limit_down']);up=number(market['limit_up'])
    for label,price,key in (('long',buy,'best_ask'),('short',sell,'best_bid')):
        best=number(market[key]) if market.get(key) is not None else None
        valid_grid=bool(price>0 and not price%tick and (best is None or best>0 and not best%tick))
        within=bool(best is not None and (best<=price if label=='long' else best>=price))
        result[label+'_price_checks']=dict(book_side='ask' if label=='long' else 'bid',
            best_price_points=str(best) if best is not None else None,limit_price_points=str(price),
            limit_and_best_on_tick_grid=valid_grid,limit_inside_exchange_bounds=down<=price<=up,
            book_within_limit=within,adverse_distance_ticks=str((best-quote.price)/tick if label=='long' else (quote.price-best)/tick) if best is not None else None,
            allowed=valid_grid and down<=price<=up and within)
    # Keep aggregate two-sided readiness conservative; per-side fields explain it.
    result['book_within_proposed_limits']=all(result[label+'_price_checks']['allowed'] for label in ('long','short'))
    return result
