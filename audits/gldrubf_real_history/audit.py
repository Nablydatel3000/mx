"""Audit the user-supplied real snapshot; exact rational SMA, no trade/P&L model.

Run from the repository root: python audits/gldrubf_real_history/audit.py
No network, broker token, SQLite or VPS access.
"""
import argparse
from collections import Counter
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from fractions import Fraction
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
import tarfile
from tempfile import TemporaryDirectory

REPO=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO))
from src.gldrubf_bot.strategy import DailyClose, crossover, sma
from src.gldrubf_bot.history import load_snapshot

HERE=Path(__file__).resolve().parent
BASE='3097a1be9994dd6d67dd237db3a3335cfff77b1c'


def require(condition,message):
    if not condition:raise ValueError(message)


def instant(value):
    stamp=datetime.fromisoformat(value.replace('Z','+00:00'))
    require(stamp.tzinfo is not None,'Naive timestamp')
    return stamp.astimezone(timezone.utc)


def sha(data):return hashlib.sha256(data).hexdigest()


def read_json(path):
    def unique(pairs):
        result={}
        for key,value in pairs:
            require(key not in result,'Duplicate JSON key')
            result[key]=value
        return result
    return json.loads(path.read_bytes(),object_pairs_hook=unique)


def formatted(value):
    if value is None:return ''
    with localcontext() as context:
        context.prec=40
        return format(Decimal(value.numerator)/Decimal(value.denominator),'.6f')


def unpack(archive,folder):
    hashes={};total=0
    with tarfile.open(archive,'r:gz') as stream:
        for member in stream.getmembers():
            name=PurePosixPath(member.name)
            require(not name.is_absolute() and '..' not in name.parts,'Unsafe archive path')
            require(member.isfile() or member.isdir(),'Archive links/devices rejected')
            target=folder.joinpath(*name.parts)
            require(target.resolve().is_relative_to(folder.resolve()),'Archive path escapes root')
            if member.isdir():target.mkdir(parents=True,exist_ok=True);continue
            total+=member.size
            require(member.size<=2*1024*1024 and total<=8*1024*1024,'Unexpected archive size')
            require(member.name not in hashes,'Duplicate archive member')
            data=stream.extractfile(member).read()
            target.parent.mkdir(parents=True,exist_ok=True)
            with target.open('xb') as output:output.write(data)
            hashes[member.name]=dict(bytes=len(data),sha256=sha(data))
    roots=list(folder.glob('*/manifest.json'))
    require(len(roots)==1,'Exactly one snapshot required')
    return roots[0].parent,hashes


def quality(root):
    manifest=read_json(root/'manifest.json');validation=read_json(root/'validation.json')
    instrument=read_json(root/'instrument.json');rows=read_json(root/'candles.json')
    start,end,captured=(instant(manifest[k]) for k in ('from','to','captured_at'))
    uid=manifest['uid'];require(start<end<=captured,'Invalid capture/range')
    require(manifest['schema']==1 and manifest['status']=='valid_with_limits','Unusable manifest')
    require(sha((root/'candles.json').read_bytes())==manifest['candles_sha256'],'Candles hash mismatch')
    require(instrument['uid']==uid and instrument['ticker']==manifest['ticker']=='GLDRUBF','Instrument mismatch')
    require(instrument['exchange']==manifest['exchange'],'Exchange mismatch')
    tick=Fraction(instrument['tick']);require(tick>0,'Invalid current tick')
    require(instant(instrument['first_daily'])<=start,'Request before declared first daily candle')
    raw_by_time={};received={};duplicate_times=[];raw_count=0;last_end=start;last_received=None
    for index,item in enumerate(manifest['raw']):
        require(item['index']==index and item['path']==f'raw/{index:04d}.json','Raw index/path mismatch')
        path=root/item['path'];require(sha(path.read_bytes())==item['sha256'],'Raw hash mismatch')
        request=item['request'];at=instant(item['received_at'])
        require(request['instrumentId']==uid and request['interval']=='CANDLE_INTERVAL_DAY','Raw request identity mismatch')
        require(request['candleSourceType']=='CANDLE_SOURCE_EXCHANGE' and 'limit' not in request,'Unexpected source/limit')
        require(instant(request['from'])==last_end and last_end<instant(request['to'])<=end,'Request coverage mismatch')
        require(instant(request['to'])<=at<=captured and (last_received is None or at>=last_received),'Invalid response receipt time')
        last_end=instant(request['to']);last_received=at
        candles=read_json(path)['candles'];previous=None
        for raw in candles:
            time=instant(raw['time']);require(previous is None or previous<=time,'Unordered raw response');previous=time
            require(instant(request['from']).date()<=time.date()<=instant(request['to']).date(),'Raw candle outside request dates')
            require(raw.get('instrumentUid',uid)==uid and raw['candleSource']=='CANDLE_SOURCE_EXCHANGE','Raw UID/source mismatch')
            if not start<=time<end:continue
            raw_count+=1
            if time in raw_by_time:
                require(raw==raw_by_time[time],'Conflicting raw duplicate')
                duplicate_times.append(time.isoformat())
            raw_by_time[time]=raw;received[time]=at
    require(last_end==end and last_received==captured,'Manifest final request/capture mismatch')
    require(set(p.name for p in (root/'raw').glob('*.json'))=={f'{i:04d}.json' for i in range(len(manifest['raw']))},'Unmanifested/missing raw file')
    require(len(rows)==len(raw_by_time),'Raw/normalized count mismatch')
    stamps=[];fields={'uid','time','provider_day','open','high','low','close','volume','is_complete','observed_at'}
    for row in rows:
        require(set(row)==fields,'Unexpected candle fields')
        time=instant(row['time']);stamps.append(time);raw=raw_by_time[time]
        require(row['uid']==uid and row['provider_day']==time.date().isoformat(),'Normalized identity mismatch')
        require(start<=time<end and time<=instant(row['observed_at'])<=captured,'Candle outside range/future')
        require(instant(row['observed_at'])==received[time],'Receipt time not traceable to raw')
        require(type(row['is_complete']) is bool and row['is_complete']==raw['isComplete'],'Completion flag mismatch')
        require(type(row['volume']) is int and row['volume']==int(raw['volume'])>=0,'Volume mismatch')
        require(not row['is_complete'] or row['volume']>0,'Complete candle without volume')
        for field in ('open','high','low','close'):
            value=Fraction(row[field]);quote=raw[field]
            require(value>0 and value==int(quote['units'])+Fraction(quote['nano'],10**9),'OHLC/raw mismatch')
            require(value%tick==0,'Price off current tick grid (historical tick remains unverified)')
        require(Fraction(row['low'])<=min(Fraction(row['open']),Fraction(row['close'])) and
                max(Fraction(row['open']),Fraction(row['close']))<=Fraction(row['high']),'Invalid OHLC bounds')
    require(stamps==sorted(set(stamps)),'Duplicate/unordered normalized time')
    require(len({r['provider_day'] for r in rows})==len(rows),'Duplicate provider date')
    with (root/'candles.csv').open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream);csv_rows=list(reader)
        require(set(reader.fieldnames)==fields and len(csv_rows)==len(rows),'CSV shape mismatch')
    for a,b in zip(rows,csv_rows):
        require(all(str(a[k])==b[k] for k in fields),'CSV/JSON content mismatch')
    gaps=[];absent=[]
    for a,b in zip(stamps,stamps[1:]):
        dates=[(a+timedelta(days=n)).date() for n in range(1,(b.date()-a.date()).days)]
        if dates:gaps.append(dict(after=rows[stamps.index(a)]['time'],before=rows[stamps.index(b)]['time'],unobserved_calendar_dates=len(dates)))
        absent.extend(dates)
    complete=[r for r in rows if r['is_complete']];incomplete=[r for r in rows if not r['is_complete']]
    expected=dict(schema=1,uid=uid,status=manifest['status'],count=len(rows),complete_count=len(complete),
                  incomplete_count=len(incomplete),duplicate_records=raw_count-len(rows),gaps=gaps)
    for key,value in expected.items():require(validation[key]==value,'Validation report disagrees with independently computed '+key)
    require(validation['coverage_confirmed'] is False and validation['trading_dates_verified'] is False,'Unexpected coverage claim')
    # Invoke project loader only AFTER the independent raw/CSV checks.
    loaded_manifest,loaded_rows=load_snapshot(root)
    require(loaded_manifest==manifest and loaded_rows==rows,'Project snapshot loader disagrees')
    weekends=Counter(instant(r['time']).strftime('%A') for r in complete if instant(r['time']).weekday()>=5)
    return rows,dict(uid=uid,exchange=manifest['exchange'],request_from=manifest['from'],request_to=manifest['to'],
        captured_at=manifest['captured_at'],total_count=len(rows),complete_count=len(complete),incomplete_count=len(incomplete),
        complete_first=complete[0]['time'],complete_last=complete[-1]['time'],excluded_incomplete=[r['time'] for r in incomplete],
        raw_files=len(manifest['raw']),raw_in_range_records=raw_count,identical_raw_duplicates=len(duplicate_times),
        raw_duplicate_times=duplicate_times,normalized_duplicates=0,conflicting_raw_duplicates=0,csv_json_mismatches=0,
        hashes_match=True,all_raw_normalized_values_match=True,current_declared_tick=str(instrument['tick']),
        historical_tick_verified=False,first_daily_current_metadata=instrument['first_daily'],
        complete_rows_by_year=dict(sorted(Counter(r['provider_day'][:4] for r in complete).items())),
        completed_weekend_labels=dict(weekends),all_provider_times_utc_midnight=all(t.hour==t.minute==t.second==t.microsecond==0 for t in stamps),
        gap_count=len(gaps),absent_calendar_dates=len(absent),absent_weekday_dates=[d.isoformat() for d in absent if d.weekday()<5],
        max_unobserved_calendar_dates=max(g['unobserved_calendar_dates'] for g in gaps),coverage_verified=False,
        trading_dates_verified=False,validation_claimed_changes=validation['changes'],
        validation_claimed_comparison=validation['comparison'],previous_snapshot_present=False,
        revision_comparison_independently_verified=False,project_load_snapshot_passed=True),gaps


def calculate(rows):
    complete=[r for r in rows if r['is_complete']]
    prices=[Fraction(r['close']) for r in complete]
    signals=[];daily=[];directions=[];float_errors=[];hist=[]
    for index,row in enumerate(complete):
        # Only the prefix through this completed candle is ever inspected.
        prefix=prices[:index+1]
        fast=sum(prefix[-20:])/20 if len(prefix)>=20 else None
        slow=sum(prefix[-50:])/50 if len(prefix)>=50 else None
        hist.append(DailyClose(datetime.fromisoformat(row['provider_day']).date(),float(row['close'])))
        target=0;previous_fast=None;previous_slow=None
        if len(prefix)>=51:
            previous_fast=sum(prefix[-21:-1])/20;previous_slow=sum(prefix[-51:-1])/50
            target=1 if previous_fast<=previous_slow and fast>slow else -1 if previous_fast>=previous_slow and fast<slow else 0
            actual=crossover(hist).target
            require(actual==target,'Exact arithmetic/project crossover mismatch at '+row['time'])
            float_errors.extend([abs(sma([b.close for b in hist],20)-float(fast)),abs(sma([b.close for b in hist],50)-float(slow))])
        directions.append(target)
        item=dict(complete_index=index+1,provider_day=row['provider_day'],provider_time_utc=row['time'],close=row['close'],
                  sma20=formatted(fast),sma50=formatted(slow),previous_sma20=formatted(previous_fast),
                  previous_sma50=formatted(previous_slow),signal='LONG' if target==1 else 'SHORT' if target==-1 else '')
        daily.append(item)
        if target:signals.append(dict(item,project_target=target,exact_target=target))
    require(len(daily)==len(complete) and all(r['provider_time_utc'] not in {x['time'] for x in rows if not x['is_complete']} for r in daily),'Incomplete candle used')
    # Re-evaluate EVERY prefix: deleting all later real candles leaves the current output unchanged.
    prefix_checked=0
    for end in range(51,len(complete)+1):
        data=complete[:end];values=[Fraction(r['close']) for r in data]
        now=sum(values[-20:])/20-sum(values[-50:])/50
        before=sum(values[-21:-1])/20-sum(values[-51:-1])/50
        expected=1 if before<=0<now else -1 if before>=0>now else 0
        require(expected==directions[end-1],'Prefix changed after truncating future candles')
        prefix_checked+=1
    return daily,signals,dict(method='Exact fractions from decimal close strings; trailing 20/50 completed observations, no calendar fill',
        warmup_observations=50,first_eligible_candle=complete[50]['time'],eligible_prefixes=prefix_checked,
        strategy_direction_mismatches=0,max_float_sma_absolute_difference=max(float_errors),
        incomplete_used=0,future_prefix_invariance_checks=prefix_checked,
        signal_count=len(signals),long_signals=sum(r['signal']=='LONG' for r in signals),short_signals=sum(r['signal']=='SHORT' for r in signals),
        signals_by_year=dict(sorted(Counter(r['provider_day'][:4] for r in signals).items())),
        historical_signal_availability_verified=False,performance_calculated=False)


def write_csv(path,rows):
    with path.open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]),lineterminator='\n');writer.writeheader();writer.writerows(rows)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,default=HERE/'source/GLDRUBF_history.tar.gz')
    parser.add_argument('--output',type=Path,default=HERE/'results')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    with TemporaryDirectory(prefix='gldrubf real audit ') as temp:
        root,files=unpack(args.archive,Path(temp))
        rows,checks,gaps=quality(root)
        daily,signals,calculation=calculate(rows)
    archive_hash=sha(args.archive.read_bytes())
    report=dict(schema=1,baseline=BASE,archive_sha256=archive_hash,archive_bytes=args.archive.stat().st_size,
        source_files=files,quality=checks,calculation=calculation,
        source_sha256={name:sha((REPO/'src/gldrubf_bot'/name).read_bytes()) for name in ('strategy.py','engine.py','history.py','research.py','api.py')})
    (args.output/'audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    write_csv(args.output/'daily_sma.csv',daily);write_csv(args.output/'signals.csv',signals)
    (args.output/'gaps.json').write_text(json.dumps(gaps,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(archive_sha256=archive_hash,quality=checks,calculation=calculation),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
