"""Immutable GetCandles snapshots. Provider labels are not a trading calendar."""
import csv
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

from .api import quotation
from .models import DataError, UTC, instant, iso, number


FIELDS = ('uid', 'time', 'provider_day', 'open', 'high', 'low', 'close', 'volume', 'is_complete', 'observed_at')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def candle_identity(row):
    return {k: row[k] for k in FIELDS if k != 'observed_at'}


def candle_hash(row):
    return digest(encoded(candle_identity(row)))


def write_json(path, value):
    # Exclusive writes: neither a raw response nor a previous report is overwritten.
    with Path(path).open('xb') as stream:
        stream.write(encoded(value) + b'\n')


def validate_rows(rows, uid, start, end):
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise DataError('Snapshot rows must be objects')
    previous = None
    days = set()
    for row in rows:
        t, observed = instant(row['time']), instant(row['observed_at'])
        if row['uid'] != uid or not start <= t < end or t > observed:
            raise DataError('Snapshot UID/range/future timestamp mismatch')
        if row['provider_day'] != t.date().isoformat() or row['provider_day'] in days:
            raise DataError('Ambiguous provider daily label')
        if previous is not None and t <= previous:
            raise DataError('Snapshot duplicate or chronology error')
        o, h, l, c = (number(row[k], positive=True) for k in ('open', 'high', 'low', 'close'))
        if not (l <= o <= h and l <= c <= h):
            raise DataError('Snapshot OHLC mismatch')
        if type(row['is_complete']) is not bool or type(row['volume']) is not int or row['volume'] < 0:
            raise DataError('Snapshot completion/volume mismatch')
        if row['is_complete'] and row['volume'] == 0:
            raise DataError('Completed candle has no volume')
        previous = t
        days.add(row['provider_day'])


def load_snapshot(directory):
    root = Path(directory)
    manifest = json.loads((root / 'manifest.json').read_bytes())
    raw = (root / 'candles.json').read_bytes()
    if digest(raw) != manifest['candles_sha256']:
        raise DataError('Snapshot candles hash mismatch')
    if manifest['status'] != 'valid_with_limits' or manifest['schema'] != 1:
        raise DataError('Snapshot is not usable; inspect validation.json')
    validation = json.loads((root / 'validation.json').read_bytes())
    if validation.get('status') != manifest['status'] or validation.get('uid') != manifest['uid']:
        raise DataError('Snapshot validation report disagrees with manifest')
    for item in manifest['raw']:
        # Raw paths are generated indexes, never arbitrary filenames from a manifest.
        expected = f"raw/{item['index']:04d}.json"
        if item['path'] != expected or digest((root / expected).read_bytes()) != item['sha256']:
            raise DataError('Snapshot raw response hash mismatch')
        if item['request'].get('instrumentId') != manifest['uid'] or item['request'].get('interval') != 'CANDLE_INTERVAL_DAY':
            raise DataError('Snapshot raw request UID/interval mismatch')
    rows = json.loads(raw)
    if (not isinstance(rows, list) or instant(manifest['from']) >= instant(manifest['to']) or
            instant(manifest['to']) > instant(manifest['captured_at']) or
            any(instant(row['observed_at']) > instant(manifest['captured_at']) for row in rows)):
        raise DataError('Snapshot capture/observation range mismatch')
    validate_rows(rows, manifest['uid'], instant(manifest['from']), instant(manifest['to']))
    return manifest, rows


def compare_rows(old_manifest, old_rows, uid, rows, start, end):
    if old_manifest['uid'] != uid:
        raise DataError('Comparison snapshot UID mismatch')
    current = {r['time']: r for r in rows}
    changes = []
    for old in old_rows:
        if not start <= instant(old['time']) < end:
            continue
        new = current.get(old['time'])
        if new is None or candle_identity(old) != candle_identity(new):
            kind = ('completed_disappeared' if new is None else 'completed_revised') if old['is_complete'] else (
                'became_complete' if new is not None and new['is_complete'] else 'incomplete_changed')
            changes.append(dict(time=old['time'], kind=kind))
    return changes


def download_daily(client, instrument, start, end, directory, *, previous=None, clock=lambda: datetime.now(UTC), make_template=True):
    start, end, now = instant(start), instant(end), instant(clock())
    if instrument.ticker != 'GLDRUBF' or not instrument.uid or start >= end or end > now:
        raise DataError('Invalid GLDRUBF snapshot UID/range')
    root = Path(directory)
    if root.exists() and any(root.iterdir()):
        raise DataError('Snapshot output must be new or empty; source files are immutable')
    root.mkdir(parents=True, exist_ok=True)
    (root / 'raw').mkdir()
    write_json(root / 'instrument.json', dict(uid=instrument.uid, ticker=instrument.ticker,
        exchange=instrument.exchange, tick=str(instrument.tick), first_daily=iso(instrument.first_daily)
        if instrument.first_daily else None, note='Current verified metadata, not historical contract specification.'))
    raw_manifest, raw_rows = [], []
    observed = {}
    last_observation = now

    def record(payload, raw):
        nonlocal last_observation
        received = instant(clock())
        if received < last_observation or payload.get('instrumentId') != instrument.uid:
            raise DataError('Clock/request UID changed during download')
        last_observation = received
        index = len(raw_manifest)
        path = f'raw/{index:04d}.json'
        with (root / path).open('xb') as stream:
            stream.write(raw)  # Original successful API bytes, not normalized candles.
        raw_manifest.append(dict(index=index, path=path, sha256=digest(raw), request=payload,
                                 received_at=iso(received)))
        candles = json.loads(raw).get('candles', [])
        if not isinstance(candles, list):
            raise DataError('Invalid raw candle array')
        times = []
        for row in candles:
            t = instant(row['time'])
            times.append(t)
            if start <= t < end:
                raw_rows.append(row)
                observed[iso(t)] = iso(received)
        if times != sorted(times):
            raise DataError('Raw response is not chronological')

    prior_callback = client.on_candles
    client.on_candles = record
    report = dict(schema=1, uid=instrument.uid, status='failed', coverage_confirmed=False,
                  trading_dates_verified=False, limitations=[
                      'Absent candle does not prove a non-trading day.',
                      'Provider UTC label is not verified GLDRUBF trading date.',
                      'isComplete does not supply historical completion/availability timestamps.'])
    try:
        rows = []
        candles = client.candles(instrument.uid, start, end)
        for raw in candles:
            t = iso(raw['time'])
            rows.append(dict(uid=instrument.uid, time=t, provider_day=instant(t).date().isoformat(),
                             **{k: format(quotation(raw[k]).normalize(), 'f') for k in ('open','high','low','close')},
                             volume=int(raw['volume']), is_complete=raw['isComplete'], observed_at=observed[t]))
        validate_rows(rows, instrument.uid, start, end)
        changes = []
        comparison = None
        if previous:
            old_manifest, old_rows = load_snapshot(previous)
            changes = compare_rows(old_manifest, old_rows, instrument.uid, rows, start, end)
            comparison = dict(previous_candles_sha256=old_manifest['candles_sha256'],
                previous_range_fully_covered=start <= instant(old_manifest['from']) and end >= instant(old_manifest['to']),
                **{'from': iso(start), 'to': iso(end)})
        blocked = any(c['kind'].startswith('completed_') for c in changes)
        gaps = [dict(after=a['time'], before=b['time'], unobserved_calendar_dates=(instant(b['time']).date()-instant(a['time']).date()).days-1)
                for a, b in zip(rows, rows[1:]) if (instant(b['time']).date()-instant(a['time']).date()).days > 1]
        report.update(status='blocked_revision' if blocked else 'valid_with_limits', count=len(rows),
                      complete_count=sum(r['is_complete'] for r in rows), incomplete_count=sum(not r['is_complete'] for r in rows),
                      duplicate_records=len(raw_rows)-len(rows), gaps=gaps, changes=changes,
                      comparison_performed=previous is not None, comparison=comparison)
        write_json(root / 'candles.json', rows)
        with (root / 'candles.csv').open('x', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader(); writer.writerows(rows)
        manifest = dict(schema=1, uid=instrument.uid, ticker=instrument.ticker, exchange=instrument.exchange,
                        **{'from': iso(start), 'to': iso(end)}, captured_at=iso(last_observation),
                        status=report['status'], candles_sha256=digest((root/'candles.json').read_bytes()), raw=raw_manifest)
        write_json(root / 'manifest.json', manifest)
        if make_template:
            write_json(root / 'evidence_template.json', dict(schema=1, uid=instrument.uid,
                snapshot_sha256=manifest['candles_sha256'], kind='unresolved', source='', coverage_source='',
                expected_trade_days=[], coverage_through=None, execution_windows=[], quote_uid=instrument.uid, quotes_sha256='', quote_source='',
                rows=[dict(time=r['time'], candle_sha256=candle_hash(r), trade_day=None, completed_at=None, available_at=None)
                      for r in rows if r['is_complete']]))
        if blocked:
            raise DataError('Previously completed candles changed/disappeared; inspect validation.json')
    except BaseException as exc:
        report['failure_category'] = type(exc).__name__
        if report['status'] != 'blocked_revision':
            report['status'] = 'failed'
        raise
    finally:
        client.on_candles = prior_callback
        write_json(root / 'validation.json', report)
        if not (root / 'manifest.json').exists():
            write_json(root / 'requests.json', raw_manifest)
    return root
