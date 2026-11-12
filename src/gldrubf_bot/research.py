"""Explicit timing evidence and mode isolation around the existing SMA/PAPER engine."""
import json
import platform
import sqlite3
from datetime import date, datetime, timedelta, time as daytime
from pathlib import Path

from .history import digest, encoded, load_snapshot, download_daily, candle_hash
from .io import quotes_csv
from .models import Bar, DataError, UTC, instant, iso


def software_identity():
    root = Path(__file__).parent
    return dict(model='SMA20/50-multiclock-paper-v2', python=platform.python_version(), sqlite=sqlite3.sqlite_version,
                sources={name: digest((root/name).read_bytes()) for name in
                         ('strategy.py','engine.py','api.py','history.py','research.py','autonomous.py')})


def timing_evidence(snapshot, path, *, forward=False):
    manifest, rows = load_snapshot(snapshot)
    raw = Path(path).read_bytes()
    evidence = json.loads(raw)
    if not isinstance(evidence, dict):
        raise DataError('Timing evidence must be an object')
    allowed = {'verified'} if forward else {'verified', 'historical_assumption'}
    if (evidence.get('schema') != 1 or evidence.get('uid') != manifest['uid'] or
            evidence.get('kind') not in allowed or not isinstance(evidence.get('source'), str) or not evidence['source'].strip() or
            (not forward and evidence.get('snapshot_sha256') != manifest['candles_sha256'])):
        raise DataError('Unresolved/mismatched timing evidence; Forward requires verified provenance')
    completed = {r['time']: r for r in rows if r['is_complete']}
    if len(completed) < 51:
        raise DataError('Need at least 51 completed daily candles with timing evidence')
    mapping = evidence['rows']
    if not isinstance(mapping, list) or any(not isinstance(r, dict) for r in mapping):
        raise DataError('Timing evidence rows must be objects')
    if len({r['time'] for r in mapping}) != len(mapping) or {r['time'] for r in mapping} != set(completed):
        raise DataError('Every completed candle needs one explicit trading-date/timing record')
    events, identities = [], []
    previous = None
    for item in mapping:
        if any(not isinstance(item.get(k), str) or not item[k] for k in
               ('time','candle_sha256','trade_day','completed_at','available_at')):
            raise DataError('Unresolved trading date/completion/availability in timing evidence')
        row = completed[item['time']]
        if item.get('candle_sha256') != candle_hash(row):
            raise DataError('Timing evidence refers to different candle values')
        trade_day = date.fromisoformat(item['trade_day'])
        closed, available = instant(item['completed_at']), instant(item['available_at'])
        t = instant(row['time'])
        if closed <= t or available < closed or closed > instant(row['observed_at']):
            raise DataError('Completion/availability timing violates observed data')
        if previous and (trade_day <= previous[0] or closed <= previous[1] or available < previous[2]):
            raise DataError('Trading-date mapping/timing must be strictly chronological')
        previous = trade_day, closed, available
        bar = Bar(trade_day, t, closed, *(row[k] for k in ('open','high','low','close')), row['volume'])
        events.append((bar, available))
        identities.append(dict(time=row['time'], candle_sha256=candle_hash(row), trade_day=trade_day.isoformat(), completed_at=iso(closed),
                               available_at=iso(available), source=evidence['source'], kind=evidence['kind']))
    if forward:
        if (not isinstance(evidence.get('coverage_source'), str) or not evidence['coverage_source'].strip() or
                not isinstance(evidence.get('coverage_through'), str) or
                evidence.get('expected_trade_days') != [b.day.isoformat() for b, _ in events]):
            raise DataError('Forward history coverage is unverified or a required trading day is absent')
    windows = [(instant(a), instant(b)) for a, b in evidence['execution_windows']]
    if any(a >= b for a, b in windows):
        raise DataError('Invalid explicit execution window')
    return manifest, evidence, events, identities, windows, digest(raw)


def bind_mode(engine, mode, settings):
    config = encoded(dict(mode=mode, settings=settings)).decode()
    with engine.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS workflow_context(id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL)')
        previous = db.execute('SELECT config FROM workflow_context WHERE id=1').fetchone()
        if previous and previous[0] != config:
            raise DataError('Database mode/assumptions mismatch; use a separate database')
        if not previous and any(db.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchone() for table in ('bars','quotes','fills')):
            raise DataError('Unlabelled existing database cannot be adopted silently')
        db.execute('INSERT OR IGNORE INTO workflow_context VALUES (1,?)', (config,))


def remember_evidence(engine, identities):
    with engine.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS timing_evidence(time TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        cached = {r[0] for r in db.execute('SELECT time FROM timing_evidence')}
        if not cached.issubset({item['time'] for item in identities}):
            raise DataError('Previously verified timing/candle disappeared')
        for item in identities:
            payload = encoded(item).decode()
            old = db.execute('SELECT payload FROM timing_evidence WHERE time=?', (item['time'],)).fetchone()
            if old and old[0] != payload:
                raise DataError('Previously accepted trading-date/timing evidence changed')
            db.execute('INSERT OR IGNORE INTO timing_evidence VALUES (?,?)', (item['time'], payload))


def replay_history(engine, snapshot, evidence_path, quotes_path):
    manifest, evidence, bars, _, windows, evidence_hash = timing_evidence(snapshot, evidence_path)
    if engine.uid != manifest['uid']:
        raise DataError('Replay engine UID differs from source snapshot')
    quote_hash = digest(Path(quotes_path).read_bytes())
    if (evidence.get('quotes_sha256') != quote_hash or evidence.get('quote_uid') != manifest['uid'] or
            not evidence.get('quote_source') or not windows):
        raise DataError('Replay requires UID-bound independent quotes and explicit eligible windows')
    quotes = quotes_csv(quotes_path)
    now = datetime.now(UTC)
    if (instant(manifest['captured_at']) > now or any(t > now or b.day > now.date() for b, t in bars)
            or any(q.time > now for q in quotes)):
        raise DataError('Future data cannot enter historical replay')
    if any(not any(a <= q.time < b for a, b in windows) for q in quotes):
        raise DataError('Replay quote outside declared execution windows')
    bind_mode(engine, 'historical-replay', dict(uid=manifest['uid'], snapshot=manifest['candles_sha256'],
               timing=evidence_hash, quotes=quote_hash))
    events = [(known, 0, bar) for bar, known in bars] + [(q.time, 1, q) for q in quotes]
    for timestamp, kind, event in sorted(events, key=lambda e: (e[0], e[1])):
        if kind == 0:
            engine.process_bars([event], timestamp)
        else:
            engine.observe(event)
    return dict(mode='historical-replay', timing_kind=evidence['kind'], source=evidence['source'],
                coverage_verified=False, completion_and_availability_are_assumptions=evidence['kind']=='historical_assumption',
                costs=engine.costs.payload(), snapshot_sha256=manifest['candles_sha256'], evidence_sha256=evidence_hash,
                quotes_sha256=quote_hash, software=software_identity())


def forward_step(engine, client, instrument, snapshot, evidence_path, *, clock=lambda: datetime.now(UTC), max_age=120, audit=None):
    manifest, evidence, events, identities, _, evidence_hash = timing_evidence(snapshot, evidence_path, forward=True)
    if instrument.uid != engine.uid or manifest['uid'] != engine.uid or instrument.tick != engine.costs.tick:
        raise DataError('Forward UID/tick mismatch')
    if client.point_value(instrument) != engine.costs.point_value_rub:
        raise DataError('Monetary tick specification changed')
    now = instant(clock())
    if instant(manifest['captured_at']) > now or any(available > now for _, available in events):
        raise DataError('Future snapshot/evidence cannot enter Forward')
    if len(events) < 51:
        raise DataError('Need 51 verified completed candles before Forward PAPER')
    through = date.fromisoformat(evidence['coverage_through'])
    if through < max(now.date()-timedelta(days=1), events[-1][0].day) or through > now.date():
        raise DataError('Verified history coverage is stale; absence is not proof of a holiday')
    # Only the current UTC day, never history, is requested from TradingSchedules.
    today = datetime.combine(now.date(), daytime.min, UTC)
    calendar = client.schedules(instrument.exchange, today, now + timedelta(microseconds=1))
    current_day = calendar.get(today.date())
    if (current_day and current_day.trading and current_day.close <= now and
            not any(bar.day == today.date() for bar, _ in events)):
        raise DataError('Official current trading day ended but completed candle/mapping is missing')
    for bar, _ in events:
        if bar.day == today.date():
            official = calendar.get(bar.day)
            if not official or not official.trading or official.close != bar.closed_at or official.close > now:
                raise DataError('Current daily completion differs from official schedule')
    observed = instant(clock())
    if observed < now:
        raise DataError('Clock moved backwards')
    bind_mode(engine, 'forward-paper', dict(uid=engine.uid, history_from=manifest['from'], coverage_source=evidence['coverage_source']))
    remember_evidence(engine, identities)
    bars = [bar for bar, _ in events]
    # Catch-up is learned now, not at backdated historical fills.
    engine.process_bars(bars, observed, coverage_start=date.fromisoformat(evidence['expected_trade_days'][0]),
                        coverage_end=date.fromisoformat(evidence['expected_trade_days'][-1]))
    quote = client.quote(instrument, clock)
    current = instant(clock())
    if current < observed:
        raise DataError('Clock moved backwards')
    def record_gate(event):
        if audit is not None:
            audit(dict(mode='forward-paper', event=event, uid=engine.uid, observed_at=iso(observed), checked_at=iso(current),
                snapshot_sha256=manifest['candles_sha256'], evidence_sha256=evidence_hash, costs=engine.costs.payload(),
                quote=dict(time=iso(quote.time), price=str(quote.price)) if quote else None,
                status_gate_passed=quote is not None, software=software_identity(),
                calendar=[dict(day=day.isoformat(), trading=s.trading, close=iso(s.close) if s.close else None,
                    windows=[[iso(a), iso(b)] for a,b in s.windows]) for day,s in sorted(calendar.items())]))
    if quote is None:
        record_gate('WAIT_MARKET')
        return 'WAIT_MARKET'
    if quote.time > current:
        raise DataError('Future quote')
    if not any(s.contains(quote.time) and s.contains(current) for s in calendar.values()):
        record_gate('WAIT_MARKET')
        return 'WAIT_MARKET'
    if (current-quote.time).total_seconds() > max_age:
        record_gate('WAIT_FRESH_PRICE')
        return 'WAIT_FRESH_PRICE'
    record_gate('MARKET_GATES_PASSED')  # Engine still checks signal timing/idempotence.
    return 'PAPER_FILL' if engine.observe(quote) else 'NO_FILL'


def refresh_forward(client, instrument, previous, output, *, clock=lambda: datetime.now(UTC)):
    manifest, _ = load_snapshot(previous)
    return download_daily(client, instrument, instant(manifest['from']), instant(clock()), output,
                          previous=previous, clock=clock)
