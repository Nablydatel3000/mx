"""Sanitized local TradingSchedules evidence, independent of parse_day/PAPER."""
import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path

from .api import APIError, ReadOnlyClient, current_schedule_request, exchange_matches
from .diagnose_api import read_token
from .diagnose_exchange_codes import local_code
from .diagnose_schedules import _safe_error
from .models import DataError, UTC, instant


METHOD = 'InstrumentsService/TradingSchedules'
MAX_JSON_BYTES = 2*1024*1024
LEGACY = ('startTime', 'endTime', 'openingAuctionStartTime', 'closingAuctionEndTime',
          'eveningOpeningAuctionStartTime', 'eveningStartTime', 'eveningEndTime',
          'clearingStartTime', 'clearingEndTime', 'premarketStartTime', 'premarketEndTime',
          'closingAuctionStartTime', 'openingAuctionEndTime')
PAIRS = (('startTime','endTime'), ('openingAuctionStartTime','openingAuctionEndTime'),
         ('closingAuctionStartTime','closingAuctionEndTime'), ('eveningStartTime','eveningEndTime'),
         ('clearingStartTime','clearingEndTime'), ('premarketStartTime','premarketEndTime'))
STAMP = re.compile(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d{1,9})?(Z|[+-]\d\d:\d\d)')


def utc_stamp(value):
    if value is None:
        return None
    match = STAMP.fullmatch(value) if isinstance(value,str) else None
    if not match:
        raise DataError('Invalid timestamp')
    base, fraction, offset = match.groups()
    # Preserve all protobuf nanoseconds; datetime alone would truncate to micros.
    return instant(base+offset).strftime('%Y-%m-%dT%H:%M:%S')+(fraction or '')+'Z'


def time_key(value):
    match = STAMP.fullmatch(utc_stamp(value))
    base, fraction, _ = match.groups()
    return base, (fraction or '.')[1:].ljust(9,'0')


def identifier(value, token):
    if local_code(value,token)=='[redacted]':
        raise DataError('Unsafe exchange identifier')
    return value


def interval_type(value, token):
    if value is None:
        return None
    if (not isinstance(value,str) or len(value)>160 or not all(c.isprintable() for c in value) or
            (token and token in value) or
            re.search(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}',value)):
        raise DataError('Unsafe interval type')
    return value


def project(response, exchange, payload, *, token):
    rows = response.get('exchanges',[]) if isinstance(response,dict) else None
    if not isinstance(rows,list) or any(not isinstance(row,dict) for row in rows):
        raise DataError('Invalid calendar shape')
    matches = [row for row in rows if exchange_matches(row.get('exchange'),exchange)]
    if len(matches)!=1:
        raise DataError('Missing/ambiguous section')
    record = matches[0]
    days = record.get('days',[])
    if not isinstance(days,list) or len(days)>32:
        raise DataError('Invalid calendar days')
    result = dict(requestedExchange=identifier(exchange,token),exchange=identifier(record.get('exchange'),token),
                  **{'from':utc_stamp(payload['from']),'to':utc_stamp(payload['to'])},days=[])
    for raw in days:
        if not isinstance(raw,dict):
            raise DataError('Invalid calendar day')
        flag = raw.get('isTradingDay')
        if flag is not None and type(flag) is not bool:
            raise DataError('Invalid trading day flag')
        day = dict(date=utc_stamp(raw.get('date')),isTradingDay=flag,
                   **{key:utc_stamp(raw.get(key)) for key in LEGACY},intervals=None)
        intervals = raw.get('intervals')
        if intervals is not None:
            if not isinstance(intervals,list) or len(intervals)>512:
                raise DataError('Invalid detailed intervals')
            day['intervals'] = []
            for item in intervals:
                if not isinstance(item,dict):
                    raise DataError('Invalid detailed interval')
                span = item.get('interval')
                if span is None:
                    span = {}
                if not isinstance(span,dict):
                    raise DataError('Invalid detailed interval boundaries')
                day['intervals'].append(dict(type=interval_type(item.get('type'),token),
                    interval=dict(startTs=utc_stamp(span.get('startTs')),endTs=utc_stamp(span.get('endTs')))))
        result['days'].append(day)
    # Defense in depth: no reflected credential in any projected field.
    if token and token in json.dumps(result,ensure_ascii=False):
        raise DataError('Credential reflected in projection')
    return result


def validate(data):
    """Validate only the diagnostic schema, never authorize a trading interval."""
    if not isinstance(data,dict) or set(data)!={'requestedExchange','exchange','from','to','days'}:
        raise DataError('Unexpected diagnostic fields')
    identifier(data['requestedExchange'],None);identifier(data['exchange'],None)
    if not exchange_matches(data['exchange'],data['requestedExchange']):
        raise DataError('Diagnostic section mismatch')
    if any(not data[k] or utc_stamp(data[k])!=data[k] for k in ('from','to')):
        raise DataError('Diagnostic request is not UTC')
    if time_key(data['from'])>=time_key(data['to']):
        raise DataError('Invalid diagnostic request range')
    if not isinstance(data['days'],list) or len(data['days'])>32:
        raise DataError('Invalid diagnostic days')
    errors, warnings, dates, count = [], [], [], 0
    def pair(a,b,label):
        if a is None or b is None:
            warnings.append(label+':missing_boundary')
        elif time_key(a)>=time_key(b):
            errors.append(label+':nonpositive_range')
    for index,day in enumerate(data['days']):
        label = 'days['+str(index)+']'
        if not isinstance(day,dict) or set(day)!={'date','isTradingDay','intervals',*LEGACY}:
            raise DataError('Unexpected diagnostic day fields')
        for key in ('date',*LEGACY):
            if utc_stamp(day[key])!=day[key]:
                raise DataError('Non-UTC diagnostic timestamp')
        if day['date'] is None:
            errors.append(label+':missing_date')
        else:
            dates.append(day['date'])
        if day['isTradingDay'] is None:
            warnings.append(label+':flag_omitted_proto_default_false')
        elif type(day['isTradingDay']) is not bool:
            raise DataError('Invalid diagnostic flag')
        for a,b in PAIRS:
            if day[a] is not None or day[b] is not None:
                pair(day[a],day[b],label+':'+a+'/'+b)
        if day['eveningOpeningAuctionStartTime'] is not None:
            pair(day['eveningOpeningAuctionStartTime'],day['eveningStartTime'],label+':evening_auction')
        spans = day['intervals']
        if day['isTradingDay'] and not spans and not any(day[key] for key in LEGACY):
            warnings.append(label+':trading_day_without_time_fields')
        if spans is None:
            warnings.append(label+':intervals_omitted')
            continue
        if not isinstance(spans,list) or len(spans)>512:
            raise DataError('Invalid diagnostic intervals')
        starts = []
        if not spans and day['isTradingDay']:
            warnings.append(label+':trading_day_without_detailed_intervals')
        for n,span in enumerate(spans):
            if not isinstance(span,dict) or set(span)!={'type','interval'} or not isinstance(span['interval'],dict) or set(span['interval'])!={'startTs','endTs'}:
                raise DataError('Unexpected diagnostic interval fields')
            interval_type(span['type'],None)
            if span['type'] is None or not span['type'].strip():
                warnings.append(label+':interval['+str(n)+']:missing_type')
            for value in span['interval'].values():
                if utc_stamp(value)!=value:
                    raise DataError('Non-UTC diagnostic interval')
            a,b = span['interval']['startTs'],span['interval']['endTs']
            pair(a,b,label+':interval['+str(n)+']')
            if a is not None:
                starts.append(time_key(a))
            count += 1
        if starts!=sorted(starts):
            warnings.append(label+':intervals_not_chronological_preserved')
    if not dates:
        warnings.append('no_observed_dates')
    if len(dates)!=len(set(dates)):
        warnings.append('duplicate_dates_preserved')
    if dates!=sorted(dates,key=time_key):
        warnings.append('dates_not_chronological_preserved')
    return dict(status='invalid' if errors else 'needs_review' if warnings else 'valid_for_diagnostics',
                errors=errors,warnings=warnings,days_count=len(data['days']),intervals_count=count,
                coverage_confirmed=False,interval_type_semantics_verified=False,execution_permission=False)


def capture(client, now, directory, *, token):
    instrument = client.instrument()
    payload = current_schedule_request(instrument.exchange,now)
    data = project(client.call(METHOD,payload),instrument.exchange,payload,token=token)
    report = validate(data)
    if len(json.dumps(data,ensure_ascii=False,indent=2).encode('utf-8'))+1>MAX_JSON_BYTES:
        raise DataError('Diagnostic file too large')
    directory = Path(directory)
    directory.mkdir(parents=True,exist_ok=False)
    for name,content in [('schedule.json',data),('validation.json',report)]:
        with (directory/name).open('x',encoding='utf-8',newline='\n') as stream:
            json.dump(content,stream,ensure_ascii=False,indent=2)
            stream.write('\n')
    # Verify the exact bytes destined for sharing, not only the in-memory object.
    loaded = json.loads((directory/'schedule.json').read_text(encoding='utf-8'))
    if validate(loaded)!=report:
        raise DataError('Diagnostic read-back mismatch')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local sanitized GLDRUBF schedule snapshot; no PAPER execution')
    parser.add_argument('--validate',type=Path,help='Offline check of schedule.json; no token or API')
    args = parser.parse_args(argv)
    os.environ.pop('SSLKEYLOGFILE',None)
    try:
        if args.validate:
            if args.validate.stat().st_size>MAX_JSON_BYTES:
                raise DataError('Diagnostic file too large')
            report = validate(json.loads(args.validate.read_text(encoding='utf-8')))
        else:
            token = read_token()
            previous = os.environ.get('T_INVEST_TOKEN')
            try:
                os.environ['T_INVEST_TOKEN'] = token
                client = ReadOnlyClient(attempts=1)
            finally:
                if previous is None:
                    os.environ.pop('T_INVEST_TOKEN',None)
                else:
                    os.environ['T_INVEST_TOKEN'] = previous
            now = datetime.now(UTC)
            directory = Path('data/schedule_diagnostics')/now.strftime('%Y%m%dT%H%M%S_%fZ')
            report = capture(client,now,directory,token=token)
            print('Local diagnostic: '+str(directory))
        print(json.dumps(report,sort_keys=True))
        return int(report['status']!='valid_for_diagnostics')
    except APIError as error:
        print(json.dumps(_safe_error(error),sort_keys=True))
    except (KeyboardInterrupt,EOFError):
        print('category=cancelled')
    except Exception:
        print('category=diagnostic_data_or_storage_failure; no execution permission')
    return 1


if __name__=='__main__':
    raise SystemExit(main())
