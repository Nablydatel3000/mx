"""Issue #2 audit probes against UNMODIFIED source. Synthetic data, no network/token.

These assert observed baseline behavior, including defects. They are evidence,
not acceptance tests that bless the behavior. All database mutations are confined
to disposable TemporaryDirectory fixtures. No production DB is opened.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from datetime import timedelta
from decimal import Decimal as D
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from src.gldrubf_bot.engine import Costs, PaperEngine
from src.gldrubf_bot.models import Quote, DataError, instant, iso
from src.gldrubf_bot import autonomous
from src.gldrubf_bot.api import APIError, Instrument, ReadOnlyClient
from src.gldrubf_bot.history import download_daily
from src.gldrubf_bot.cli import runner_lock


START = instant('2026-08-06T00:00:00Z')
NOW = instant('2026-11-17T05:59:57Z')
UID = 'issue2-synthetic-uid'
INST = Instrument(UID,'fixture-figi','GLDRUBF','forts_futures_weekend',D(1))
COSTS = Costs(D(1),D(1),D('.5'),1,D(10000),D(2))  # Explicit synthetic costs, NOT tariffs.


def rows(count=None):
    count = (NOW.date()-START.date()).days if count is None else count
    result = []
    for index in range(count):
        t = START+timedelta(days=index)
        price = '100' if index<50 else '99'
        result.append(dict(uid=UID,time=iso(t),provider_day=t.date().isoformat(),
            open=price,high=price,low=price,close=price,volume=1,is_complete=True,observed_at=iso(NOW)))
    return result


def prepare(path, data=None, now=NOW):
    engine = PaperEngine(path,UID,COSTS)
    autonomous.source_start(engine,now,requested=START)
    engine.process_observed_candles(rows() if data is None else data,now,source_start=START,source_end=now)
    return engine


def frame(engine):
    return engine.summary()


def bootstrap(root):
    path = root/'bootstrap.db';engine = prepare(path)
    with engine.db() as db:
        last = dict(db.execute('SELECT day,target,known_at FROM bars WHERE target!=0 ORDER BY day DESC LIMIT 1').fetchone())
    assert last['day']=='2026-09-25' and last['target']==-1
    assert engine.observe(Quote(NOW,D(99))) is False
    stamp = NOW+timedelta(seconds=1)
    assert engine.observe(Quote(stamp,D(99))) is True
    before = frame(engine)
    restored = PaperEngine(path,UID,COSTS)
    assert restored.observe(Quote(stamp,D(99))) is False and frame(restored)==before
    return dict(last_cross=last,last_complete=rows()[-1]['provider_day'],
                signal_age_days=(NOW.date()-instant(last['day']+'T00:00:00Z').date()).days,
                position=before['position'],fills=before['fills'],restart_duplicate_fills=0)


def stale_history(root):
    engine = PaperEngine(root/'stale.db',UID,COSTS)
    source = snapshot(root,'stale_source',NOW,data=rows(51))
    assert autonomous.step(engine,client(NOW,NOW),INST,source,clock=lambda:NOW)=='NO_FILL'
    later = NOW+timedelta(seconds=1)
    event=autonomous.step(engine,client(later,later),INST,source,clock=lambda:later)
    assert event=='PAPER_FILL'
    return dict(requested_to=iso(NOW),received_at=iso(NOW),last_complete=rows(51)[-1]['provider_day'],
                position=frame(engine)['position'],accepted=True,event=event)


class Response:
    status = 200
    headers = {'Content-Type':'application/json'}
    def __init__(self, data):self.data=json.dumps(data).encode()
    def read(self, limit):return self.data[:limit]
    def __enter__(self):return self
    def __exit__(self,*args):pass


def client(now, quote_time, *, fail_calendar=False, market=True, data=None):
    def quotation(value):return dict(units=str(value),nano=0)
    def transport(request,timeout):
        method = request.full_url.split('contract.v1.')[1]
        payload = json.loads(request.data)
        if method.endswith('/GetCandles'):
            source = rows() if data is None else data
            candles = [dict(time=r['time'],**{k:quotation(r[k]) for k in ('open','high','low','close')},
                            volume='1',isComplete=r['is_complete']) for r in source
                       if instant(payload['from']).date()<=instant(r['time']).date()<=instant(payload['to']).date()]
            return Response(dict(candles=candles))
        if method.endswith('/GetFuturesMargin'):
            return Response(dict(minPriceIncrement=quotation(1),minPriceIncrementAmount=quotation(1)))
        if method.endswith('/TradingSchedules'):
            if fail_calendar:raise APIError('synthetic retryable outage',retryable=True)
            day = dict(date='2026-11-17T00:00:00Z',isTradingDay=True,startTime='2026-11-17T04:00:00Z',
                       endTime='2026-11-17T20:50:00Z',intervals=[dict(type='regular_trading_session',
                           interval=dict(startTs='2026-11-17T'+a+'Z',endTs='2026-11-17T'+b+'Z'))
                           for a,b in [('04:00:00','05:59:59'),('06:00:00','15:59:59'),('16:00:00','20:50:00')]])
            return Response(dict(exchanges=[dict(exchange=INST.exchange.upper(),days=[day])]))
        if method.endswith('/GetTradingStatus'):
            return Response(dict(instrumentUid=UID,tradingStatus='SECURITY_TRADING_STATUS_NORMAL_TRADING',
                                 marketOrderAvailableFlag=market,apiTradeAvailableFlag=market))
        if method.endswith('/GetLastPrices'):
            return Response(dict(lastPrices=[dict(instrumentUid=UID,time=iso(quote_time),price=quotation(99))]))
        raise AssertionError('Unexpected API method')
    with patch.dict(os.environ,{'T_INVEST_TOKEN':'synthetic-test-credential'}):
        return ReadOnlyClient(transport=transport,attempts=1)


def snapshot(root,name,now,data=None):
    return download_daily(client(now,now,data=data),INST,START,now,root/name,clock=lambda:now,make_template=False)


def cross_phase(root):
    engine = PaperEngine(root/'phases.db',UID,COSTS)
    first = snapshot(root,'first',NOW)
    assert autonomous.step(engine,client(NOW,NOW),INST,first,clock=lambda:NOW)=='NO_FILL'
    current = instant('2026-11-17T06:00:00Z')
    quote = instant('2026-11-17T05:59:58Z')
    second = snapshot(root,'second',current)
    cal = client(current,quote).current_sessions(INST.exchange,current)
    same_span = any(a<=current<b and a<=quote<b for day in cal.values() for a,b in day.windows)
    assert not same_span
    event = autonomous.step(engine,client(current,quote),INST,second,clock=lambda:current)
    assert event=='PAPER_FILL'
    with engine.db() as db:fill = dict(db.execute('SELECT time,signal_day,kind FROM fills').fetchone())
    return dict(event=event,current=iso(current),quote=iso(quote),same_individual_window=same_span,
                fill=fill,quote_age_seconds=(current-quote).total_seconds())


def guards(root):
    engine = PaperEngine(root/'guards.db',UID,COSTS)
    initial = snapshot(root,'guard_initial',NOW)
    try:autonomous.step(engine,client(NOW,NOW,fail_calendar=True),INST,initial,clock=lambda:NOW)
    except APIError:pass
    else:raise AssertionError('outage not raised')
    assert frame(engine)['pending']==-1 and frame(engine)['fills']==0
    restored = PaperEngine(root/'guards.db',UID,COSTS)
    later = NOW+timedelta(seconds=1)
    assert autonomous.step(restored,client(later,later),INST,initial,clock=lambda:later)=='PAPER_FILL'
    before = frame(restored)
    current = instant('2026-11-17T10:00:00Z')
    fresh = snapshot(root,'guard_fresh',current)
    assert autonomous.step(restored,client(current,current-timedelta(seconds=121)),INST,fresh,
                           clock=lambda:current)=='WAIT_FRESH_PRICE'
    assert autonomous.step(restored,client(current,current,market=False),INST,fresh,
                           clock=lambda:current)=='WAIT_MARKET'
    try:autonomous.step(restored,client(current,current+timedelta(seconds=1)),INST,fresh,clock=lambda:current)
    except DataError:pass
    else:raise AssertionError('future quote accepted')
    assert frame(restored)==before
    changed=rows();changed[0].update(close='101',high='101')
    try:restored.process_observed_candles(changed,current,source_start=START,source_end=current)
    except DataError:pass
    else:raise AssertionError('revision accepted')
    assert frame(restored)==before
    incomplete=rows();incomplete[-1].update(open='200',high='200',low='200',close='200',is_complete=False)
    other=prepare(root/'incomplete.db',incomplete)
    assert frame(other)['pending']==-1 and other.latest_day().isoformat()==incomplete[-2]['provider_day']
    incomplete[-1]['is_complete']=True
    other.process_observed_candles(incomplete,current,source_start=START,source_end=current)
    assert frame(other)['pending']==1
    return dict(outage_pending_retained=True,restart_fill_once=True,stale_quote_blocked=True,
                closed_status_blocked=True,future_quote_blocked=True,revision_atomic_stop=True,
                incomplete_candle_excluded=True,completion_updates_target=True)


def logical_corruption(root):
    path=root/'corrupt.db';engine=prepare(path)
    assert engine.observe(Quote(NOW+timedelta(seconds=1),D(99)))
    with engine.db() as db:
        db.execute("UPDATE quotes SET price='-1'")
        db.execute("UPDATE fills SET signal_day='2099-01-01'")
    restored = PaperEngine(path,UID,COSTS)  # Unexpectedly accepted.
    with restored.db() as db:
        fill_day=db.execute('SELECT signal_day FROM fills').fetchone()[0]
        price=db.execute('SELECT price FROM quotes').fetchone()[0]
    result=frame(restored)
    assert price=='-1' and fill_day=='2099-01-01'
    return dict(restart_accepted=True,quote_price=price,fill_signal_day=fill_day,equity=result['equity'])


def lock_alias(root):
    path=root/'lock.db';PaperEngine(path,UID,COSTS)
    alias=root/'alias.db';alias.symlink_to(path)
    with runner_lock(path),runner_lock(alias):
        assert path.stat().st_ino==alias.stat().st_ino
        return dict(same_physical_db=True,two_runner_locks_acquired=True)


def crash_child(path,phase):
    path=Path(path).resolve()
    if path.name!='crash.db' or not (path.parent/'.issue2-fixture').is_file():
        raise RuntimeError('Crash probes may open only marked disposable audit fixtures')
    engine=PaperEngine(path,UID,COSTS)
    if phase=='before_commit':
        with engine.db() as db:
            db.execute('UPDATE state SET position=1')
            os._exit(23)
    assert engine.observe(Quote(NOW+timedelta(seconds=1),D(99)))
    os._exit(24)  # Commit succeeded, no CSV/export/log update.


def crashes(root):
    path=root/'crash.db';engine=prepare(path);before=frame(engine)
    a=subprocess.run([sys.executable,__file__,'--crash',str(path),'before_commit'],capture_output=True)
    assert a.returncode==23,(a.returncode,a.stderr)
    assert frame(PaperEngine(path,UID,COSTS))==before
    b=subprocess.run([sys.executable,__file__,'--crash',str(path),'after_commit'],capture_output=True)
    assert b.returncode==24,(b.returncode,b.stderr)
    restored=PaperEngine(path,UID,COSTS)
    assert frame(restored)['fills']==1
    assert restored.observe(Quote(NOW+timedelta(seconds=1),D(99))) is False
    restored.export(root/'recovered_export')
    assert (root/'recovered_export/fills.csv').exists()
    return dict(uncommitted_transaction_rolled_back=True,committed_fill_retained=True,
                duplicate_after_crash=False,export_rebuilt_from_sqlite=True,power_failure_tested=False)


def run():
    evidence = dict(synthetic_only=True,no_network=True,no_real_csv_or_sqlite=True)
    with TemporaryDirectory(prefix='mx issue2 audit ') as temporary:
        root=Path(temporary)
        (root/'.issue2-fixture').write_text('disposable audit fixtures only\n')
        for name,probe in [('bootstrap',bootstrap),('stale_history',stale_history),('cross_phase',cross_phase),
                           ('guards',guards),('logical_corruption',logical_corruption),('lock_alias',lock_alias),('crashes',crashes)]:
            evidence[name]=probe(root)
    return evidence


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--crash',nargs=2,metavar=('TEMP_DB','PHASE'))
    args=parser.parse_args()
    if args.crash:crash_child(*args.crash)
    else:print(json.dumps(run(),ensure_ascii=False,indent=2))
