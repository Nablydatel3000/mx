"""Strict import/export formats; CSV input is data, never instructions."""
import csv
import json
from datetime import date
from pathlib import Path

from .api import TradingDay
from .models import Bar, Quote, DataError, instant, iso, number


def records(path, required):
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        sample = stream.read(4096)
        stream.seek(0)
        if not sample.strip():
            raise DataError('Empty CSV')
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=';,')
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(stream, dialect=dialect)
        if not required.issubset(reader.fieldnames or []):
            raise DataError('CSV missing columns: ' + ','.join(sorted(required)))
        result = list(reader)
        if any(None in r or any(v is None for v in r.values()) for r in result):
            raise DataError('Malformed CSV row')
        return result


def bars_csv(path):
    required = {'day', 'time', 'closed_at', 'open', 'high', 'low', 'close', 'volume', 'complete'}
    result = []
    for r in records(path, required):
        if r['complete'].lower() not in {'true', '1'}:
            raise DataError('Replay requires completed candles only')
        result.append(Bar(date.fromisoformat(r['day']), instant(r['time']), instant(r['closed_at']),
                      *(number(r[k].replace(',', '.'), positive=True) for k in ('open', 'high', 'low', 'close')), int(r['volume'])))
    if not result or [b.day for b in result] != sorted({b.day for b in result}):
        raise DataError('Bars must be nonempty, unique, ordered')
    return result


def quotes_csv(path):
    result = [Quote(instant(r['time']), number(r['price'].replace(',', '.'), positive=True))
              for r in records(path, {'time', 'price'})]
    if not result or [q.time for q in result] != sorted({q.time for q in result}):
        raise DataError('Quotes must be nonempty, unique, ordered; do not manufacture execution prices from closes')
    return result


def funding_csv(path):
    result = {}
    for r in records(path, {'day', 'long_cost_rub'}):
        day = date.fromisoformat(r['day']).isoformat()
        if day in result:
            raise DataError('Duplicate funding date')
        result[day] = number(r['long_cost_rub'].replace(',', '.'))
    return result


def save_calendar(path, instrument, days):
    data = dict(uid=instrument.uid, exchange=instrument.exchange, source='T-Investments TradingSchedules',
                days=[dict(day=d.isoformat(), trading=s.trading,
                           windows=[[iso(a), iso(b)] for a, b in s.windows], close=iso(s.close) if s.close else None)
                      for d, s in sorted(days.items())])
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def calendar_json(path, uid):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if data.get('uid') != uid:
        raise DataError('Calendar instrument UID mismatch')
    days = {}
    for raw in data['days']:
        day = date.fromisoformat(raw['day'])
        if day in days or type(raw['trading']) is not bool:
            raise DataError('Duplicate/invalid calendar day')
        windows = tuple((instant(a), instant(b)) for a, b in raw['windows'])
        close = instant(raw['close']) if raw['close'] else None
        if raw['trading'] and (not windows or close is None or any(a >= b or b > close for a, b in windows)):
            raise DataError('Invalid calendar windows')
        if not raw['trading'] and (windows or close):
            raise DataError('Closed calendar day with sessions')
        days[day] = TradingDay(day, raw['trading'], windows, close)
    return days
