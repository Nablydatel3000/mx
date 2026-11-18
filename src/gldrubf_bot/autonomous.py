"""Forward PAPER on first-observed API-complete daily aggregates, no manual evidence."""
import json
from datetime import datetime, timedelta, time as daytime

from .history import load_snapshot
from .models import DataError, MOSCOW, UTC, instant, iso
from .research import bind_mode, software_identity


MODE = 'autonomous-forward-v1'


def history_age(rows, now, maximum_hours):
    if type(maximum_hours) is not int or not 1<=maximum_hours<=8760:
        raise DataError('Explicit max complete candle age (1..8760 UTC hours) required')
    complete=[r for r in rows if r['is_complete']]
    if not complete:
        return None
    latest=max(instant(r['time']) for r in complete)
    age=(instant(now)-latest).total_seconds()
    if age<0:
        raise DataError('Future completed candle')
    return dict(latest_complete=iso(latest),age_seconds=age,maximum_hours=maximum_hours,
                fresh=age<=maximum_hours*3600)


def same_window(calendar, now, quote_time):
    return any(a<=now<b and a<=quote_time<b for day in calendar.values() for a,b in day.windows)


def source_start(engine, now, *, requested=None, history_days=730):
    """Pin the original lookback across restart, never slide away accepted history."""
    now = instant(now)
    with engine.db() as db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_context'").fetchone()
        previous = db.execute('SELECT config FROM workflow_context WHERE id=1').fetchone() if exists else None
    if previous:
        context = json.loads(previous[0])
        if context['mode'] != MODE:
            raise DataError('Use a separate database for autonomous Forward; old ledger is preserved')
        start = instant(context['settings']['source_from'])
        if requested is not None and instant(requested) != start:
            raise DataError('Source history boundary changed; do not hide earlier accepted candles')
    else:
        if type(history_days) is not int or not 1 <= history_days <= 3650:
            raise DataError('History lookback must be 1..3650 calendar days, not a trading-day count')
        start = instant(requested) if requested is not None else datetime.combine(now.date(),daytime.min,UTC)-timedelta(days=history_days)
    bind_mode(engine,MODE,dict(uid=engine.uid,source_from=iso(start),
        labels='provider_utc_not_verified_session_dates',completion='API_isComplete_first_observed',
        financing='per_new_complete_candle_at_first_confirmation'))
    return start


def step(engine,client,instrument,snapshot,*,clock=lambda:datetime.now(UTC),max_age=120,audit=None,
         max_complete_age_hours=None):
    manifest,rows=load_snapshot(snapshot)
    now=instant(clock())
    captured,end=instant(manifest['captured_at']),instant(manifest['to'])
    if (manifest['uid']!=engine.uid or instrument.uid!=engine.uid or instrument.tick!=engine.costs.tick
            or manifest['ticker']!='GLDRUBF'):
        raise DataError('Forward source UID/instrument/tick mismatch')
    if captured>now or end>now:
        raise DataError('Future history observation/window')
    if (now-captured).total_seconds()>300 or (now-end).total_seconds()>300:
        if audit is not None:
            audit(dict(mode=MODE,event='WAIT_HISTORY_REFRESH',checked_at=iso(now),
                       captured_at=iso(captured),requested_to=iso(end)))
        return 'WAIT_HISTORY_REFRESH'
    if max_age<1:
        raise DataError('Quote freshness must be positive')
    if client.point_value(instrument)!=engine.costs.point_value_rub:
        raise DataError('Monetary tick specification changed')
    observed=instant(clock())
    if observed<now:
        raise DataError('Clock moved backwards')
    source_start(engine,observed,requested=manifest['from'])
    age=history_age(rows,observed,max_complete_age_hours)
    if age is not None and not age['fresh']:
        if audit is not None:
            audit(dict(mode=MODE,event='WAIT_COMPLETE_HISTORY',checked_at=iso(observed),complete_history_age=age))
        return 'WAIT_COMPLETE_HISTORY'
    complete_count=sum(r['is_complete'] for r in rows)
    if not complete_count:
        if audit is not None:
            audit(dict(mode=MODE,event='WAIT_HISTORY',observed_at=iso(observed),
                complete_count=complete_count,coverage_verified=False,
                snapshot_sha256=manifest['candles_sha256']))
        return 'WAIT_HISTORY'
    engine.process_observed_candles(rows,observed,source_start=manifest['from'],source_end=manifest['to'])
    if complete_count<51:
        if audit is not None:audit(dict(event='WAIT_HISTORY',complete_count=complete_count,checked_at=iso(observed)))
        return 'WAIT_HISTORY'
    if engine.summary()['migration_review_required']:
        if audit is not None:audit(dict(event='WAIT_MIGRATION_REVIEW',checked_at=iso(observed)))
        return 'WAIT_MIGRATION_REVIEW'
    # Current permission is independent of historical provider labels.
    calendar=client.current_sessions(instrument.exchange,observed)
    quote=client.quote(instrument,clock)
    current=instant(clock())
    if current<observed:
        raise DataError('Clock moved backwards')
    if quote is not None and quote.time>current:
        raise DataError('Future quote')
    event='MARKET_GATES_PASSED'
    age=history_age(rows,current,max_complete_age_hours)
    if age is not None and not age['fresh']:
        event='WAIT_COMPLETE_HISTORY'
    elif not calendar:
        event='WAIT_CALENDAR'
    elif quote is None or not same_window(calendar,current,quote.time):
        event='WAIT_MARKET'
    else:
        mark=engine.summary()['mark_time']
        if (current-quote.time).total_seconds()>max_age or (mark and quote.time<instant(mark)):
            event='WAIT_FRESH_PRICE'
    if audit is not None:
        audit(dict(mode=MODE,event=event,uid=engine.uid,observed_at=iso(observed),checked_at=iso(current),
            checked_at_msk=current.astimezone(MOSCOW).isoformat(),snapshot_sha256=manifest['candles_sha256'],
            data_clock='API_complete_first_confirmation_not_historical_close',coverage_verified=False,
            trading_dates_verified=False,costs=engine.costs.payload(),software=software_identity(),
            complete_history_age=age,
            quote=dict(time=iso(quote.time),price=str(quote.price)) if quote else None,
            calendar=[dict(day=day.isoformat(),trading=s.trading,close=iso(s.close) if s.close else None,
                windows=[[iso(a),iso(b)] for a,b in s.windows]) for day,s in sorted(calendar.items())]))
    if event!='MARKET_GATES_PASSED':
        return event
    return 'PAPER_FILL' if engine.observe(quote) else 'NO_FILL'
