"""Read-only market data and PAPER only; no LIVE mode or broker orders."""
import argparse
import csv
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, time as daytime
from pathlib import Path

from .api import ReadOnlyClient, APIError, quotation
from .engine import PaperEngine, Costs
from .io import records, bars_csv, quotes_csv, funding_csv, calendar_json, save_calendar
from .models import DataError, UTC, instant, iso, number
from .strategy import DailyClose, crossover
from .history import download_daily, load_snapshot, digest, encoded, write_json
from .research import replay_history, forward_step, refresh_forward, bind_mode, timing_evidence
from . import autonomous


def read_daily_closes(path):
    bars = [DailyClose(date.fromisoformat(r['day']), float(number(r['close'].replace(',', '.'), positive=True)))
            for r in records(path, {'day', 'close'})]
    if len(bars) < 51:
        raise DataError('Need at least 51 completed daily closes')
    crossover(bars)  # validate ALL chronology/prices before producing output
    return bars


@contextmanager
def runner_lock(path):
    path = Path(str(path) + '.runner.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as f:
        if f.seek(0, os.SEEK_END) == 0:
            f.write(b'0')
            f.flush()
        f.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DataError('Another runner holds this database lock') from None
        try:
            yield
        finally:
            f.seek(0)
            if os.name == 'nt':
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def market_open(calendar, timestamp):
    return any(s.contains(timestamp) for s in calendar.values())


def paper_step(engine, client, instrument, clock=lambda: datetime.now(UTC), max_age=120):
    """Legacy calendar-backed regression harness; CLI Forward uses forward_step."""
    if client.point_value(instrument) != engine.costs.point_value_rub:
        raise DataError('Monetary tick specification changed; stop before revaluing persistent positions')
    now = instant(clock())
    last = engine.latest_day()
    start_day = last - timedelta(days=7) if last else now.date() - timedelta(days=200)
    start = datetime.combine(start_day, daytime.min, UTC)
    bars, calendar = client.completed_daily(instrument, start, now)
    if len(bars) < 51 and last is None:
        raise DataError('API history has fewer than 51 completed daily bars')
    observed_at = instant(clock())
    if observed_at < now:
        raise DataError('Local clock moved backwards during fetch')
    engine.process_bars(bars, observed_at, coverage_start=start.date(), coverage_end=now.date())
    quote = client.quote(instrument, clock)
    current = instant(clock())
    if quote is None or not market_open(calendar, quote.time):
        return 'WAIT_MARKET'
    if quote.time > current:
        raise DataError('Future quote')
    if (current - quote.time).total_seconds() > max_age:
        return 'WAIT_FRESH_PRICE'
    return 'PAPER_FILL' if engine.observe(quote) else 'NO_FILL'


def costs_from(args, point_value, tick):
    return Costs(number(point_value), number(tick), number(args.commission_rub), args.slippage_ticks,
                 number(args.initial_cash_rub), number(args.funding_per_contract_day_rub)
                 if args.funding_per_contract_day_rub is not None else None)


def protect_research_inputs(args):
    history = args.history.resolve() if args.history else None
    output, db = args.output.resolve(), args.db.resolve()
    if history and (history == output or history in output.parents or history == db or history in db.parents):
        raise DataError('Research output/database must not overwrite a source snapshot')
    inputs = ([args.evidence] if args.evidence else []) + ([args.quotes] if hasattr(args, 'quotes') else [])
    if any(p.resolve() == db or p.resolve() == output or output in p.resolve().parents for p in inputs):
        raise DataError('Keep evidence/quotes outside derived output and database paths')


def replay(args):
    bars, quotes = bars_csv(args.bars), quotes_csv(args.quotes)
    calendar = calendar_json(args.calendar, args.uid)
    for b in bars:
        if b.day not in calendar or not calendar[b.day].trading or calendar[b.day].close != b.closed_at:
            raise DataError('Bar completion differs from provided official calendar')
    horizon = max(bars[-1].closed_at, quotes[-1].time)
    expected = {d for d, s in calendar.items() if d >= bars[0].day and s.trading and s.close <= horizon}
    if expected != {b.day for b in bars}:
        raise DataError('Missing replay trading-day bars')
    # Require even closed dates to be represented; an absent date is not a holiday proof.
    for n in range((max(bars[-1].day, quotes[-1].time.date()) - bars[0].day).days + 1):
        if bars[0].day + timedelta(days=n) not in calendar:
            raise DataError('Replay calendar has missing dates')
    if any(not market_open(calendar, q.time) for q in quotes):
        raise DataError('Replay quote outside verified calendar windows')
    rates = funding_csv(args.funding_csv) if args.funding_csv else None
    with runner_lock(args.db):
        engine = PaperEngine(args.db, args.uid, costs_from(args, args.point_value_rub, args.tick_size), funding_rates=rates)
        bind_mode(engine, 'calendar-replay', dict(bars=digest(args.bars.read_bytes()),
            quotes=digest(args.quotes.read_bytes()), calendar=digest(args.calendar.read_bytes())))
        # Replay restarts re-consume identical events: duplicates are verified by payload and ignored.
        events = [(b.closed_at, 0, b) for b in bars] + [(q.time, 1, q) for q in quotes]
        for _, kind, event in sorted(events, key=lambda e: (e[0], e[1])):
            if kind == 0:
                engine.process_bars([event], event.closed_at)
            else:
                engine.observe(event)
        engine.export(args.output)
        print(json.dumps(engine.summary(), ensure_ascii=False, indent=2))
        print('PAPER event replay only; NOT a validated backtest. No broker orders.')


def fetch(args):
    start, end = instant(args.start), instant(args.end)
    now = datetime.now(UTC)
    if end > now or start >= end:
        raise DataError('Historical export must end no later than now')
    if args.interval == 'day':
        if start.time() != daytime.min:
            raise DataError('Daily export --from must be UTC midnight (official date labels)')
    client = ReadOnlyClient()
    instrument = client.instrument(args.uid)
    if args.interval == 'day':
        download_daily(client, instrument, start, end, args.output, previous=args.compare)
        print('GetCandles snapshot saved; inspect validation.json. Calendar/timing coverage is NOT certified.')
        return
    else:
        args.output.mkdir(parents=True, exist_ok=True)
        rows = client.candles(instrument.uid, start, end, 'CANDLE_INTERVAL_1_MIN')
        with (args.output / 'minute.csv').open('w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
            w.writerow(['time', 'open', 'high', 'low', 'close', 'volume', 'is_complete'])
            for row in rows:
                w.writerow([iso(row['time']), *(str(quotation(row[k])) for k in ('open', 'high', 'low', 'close')), row['volume'], row['isComplete']])
    (args.output / 'instrument.json').write_text(json.dumps(dict(uid=instrument.uid, ticker=instrument.ticker,
        exchange=instrument.exchange, tick=str(instrument.tick), first_daily=iso(instrument.first_daily) if instrument.first_daily else None, fetched_at=iso(datetime.now(UTC)),
        note='Current metadata only; not historical monetary specification.'), ensure_ascii=False, indent=2), encoding='utf-8')
    print('Read-only export complete. UID=' + instrument.uid)


def run_paper(args):
    if args.poll_seconds < 60 or args.max_quote_age < 1:
        raise DataError('Forward polling must be >=60s and quote age positive')
    protect_research_inputs(args)
    if args.evidence:
        if not args.history:
            raise DataError('Legacy verified mode requires --history')
        timing_evidence(args.history, args.evidence, forward=True)
    initial = load_snapshot(args.history)[0] if args.history else None
    args.output.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger('paper')
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(args.output / 'operations.log', encoding='utf-8')
    logger.addHandler(handler)
    try:
        with runner_lock(args.db):
            client = ReadOnlyClient()
            while True:
                try:
                    instrument = client.instrument(args.uid)
                    costs = costs_from(args, client.point_value(instrument), instrument.tick)
                    break
                except APIError as exc:
                    logger.warning('API_STARTUP_UNAVAILABLE: %s',exc)
                    if args.once or not exc.retryable:
                        raise
                    print('API startup unavailable: waiting; no virtual execution.',flush=True)
                    time.sleep(args.poll_seconds)
            if initial and initial['uid'] != instrument.uid:
                raise DataError('Initial history UID differs from API instrument')
            rates = funding_csv(args.funding_csv) if args.funding_csv else None
            engine = PaperEngine(args.db, instrument.uid, costs, funding_rates=rates)
            start = autonomous.source_start(engine,datetime.now(UTC),
                requested=initial['from'] if initial else None,history_days=args.history_days) if not args.evidence else None
            previous = args.history
            checks = args.output / 'forward_checks'
            checks.mkdir(exist_ok=True)
            def audit(payload):
                path = checks / (digest(encoded(payload)) + '.json')
                if not path.exists():
                    write_json(path, payload)
            while True:
                try:
                    if args.funding_csv:
                        engine.rates = {date.fromisoformat(k): v for k, v in funding_csv(args.funding_csv).items()}
                    refresh = args.output / 'history' / datetime.now(UTC).strftime('%Y%m%dT%H%M%S_%fZ')
                    if args.evidence:
                        snapshot = refresh_forward(client, instrument, previous, refresh)
                        event = forward_step(engine, client, instrument, snapshot, args.evidence,
                                             max_age=args.max_quote_age, audit=audit)
                    else:
                        snapshot = download_daily(client,instrument,start,datetime.now(UTC),refresh,
                                                  previous=previous,make_template=False)
                        event = autonomous.step(engine,client,instrument,snapshot,max_age=args.max_quote_age,audit=audit)
                    previous = snapshot
                    engine.export(args.output)
                    message = json.dumps(dict(time=iso(datetime.now(UTC)), event=event,
                        mode='legacy-verified' if args.evidence else autonomous.MODE, **engine.summary()), ensure_ascii=False)
                    logger.info(message)
                    print(message, flush=True)
                except APIError as exc:
                    logger.warning('API_UNAVAILABLE: %s', exc)
                    print('API unavailable: no fill; persistent state retained.', flush=True)
                    if args.once or not exc.retryable:
                        raise
                except DataError as exc:
                    logger.error('DATA_STOP: %s', exc)
                    raise
                if args.once:
                    break
                time.sleep(args.poll_seconds)
    finally:
        logger.removeHandler(handler)
        handler.close()


def run_historical_replay(args):
    protect_research_inputs(args)
    manifest, _ = load_snapshot(args.history)
    if args.uid and args.uid != manifest['uid']:
        raise DataError('Replay UID differs from snapshot')
    rates = funding_csv(args.funding_csv) if args.funding_csv else None
    with runner_lock(args.db):
        engine = PaperEngine(args.db, manifest['uid'], costs_from(args, args.point_value_rub, args.tick_size), funding_rates=rates)
        report = replay_history(engine, args.history, args.evidence, args.quotes)
        engine.export(args.output)
        # SQLite stores the same immutable context; this report is replaceable output.
        path = args.output / 'research.json'
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(dict(**report, **engine.summary()), ensure_ascii=False, indent=2))


def parser():
    p = argparse.ArgumentParser(description='GLDRUBF SMA20/50 READ-ONLY DATA + PAPER. No LIVE mode.')
    commands = p.add_subparsers(dest='command', required=True)
    signals = commands.add_parser('signals', help='Original day,close format: signals only, no fills')
    signals.add_argument('--csv', type=Path, required=True)
    f = commands.add_parser('fetch', help='Download actual candles; token only from T_INVEST_TOKEN')
    f.add_argument('--from', dest='start', required=True)
    f.add_argument('--to', dest='end', required=True)
    f.add_argument('--interval', choices=['day', '1min'], default='day')
    f.add_argument('--output', type=Path, default=Path('data'))
    f.add_argument('--uid')
    f.add_argument('--compare', type=Path, help='Previous immutable day snapshot for revision detection')
    for name in ('replay', 'historical-replay', 'paper'):
        c = commands.add_parser(name)
        c.add_argument('--db', type=Path, default=Path('paper_auto_v2.sqlite3' if name=='paper' else 'paper.sqlite3'))
        c.add_argument('--output', type=Path, default=Path('logs'))
        c.add_argument('--uid', required=name == 'replay')
        c.add_argument('--commission-rub', required=True, help='RUB per one-contract side, explicit scenario')
        c.add_argument('--slippage-ticks', required=True, type=int)
        c.add_argument('--initial-cash-rub', default='100000', help='Research balance, not broker funds')
        g = c.add_mutually_exclusive_group(required=True)
        funding_flags = ['--funding-per-contract-day-rub'] + (['--confirmation-cost-rub'] if name=='paper' else [])
        g.add_argument(*funding_flags, dest='funding_per_contract_day_rub',
            help=('Autonomous PAPER: cost per newly confirmed provider candle; legacy/replay: daily scenario. '
                  'Positive = long pays / short receives; zero explicitly ignores this cost.'))
        g.add_argument('--funding-csv', type=Path, help='day,long_cost_rub; actual data or clearly labelled assumptions')
        if name in {'replay', 'historical-replay'}:
            if name == 'replay':
                required_paths = ('bars', 'quotes', 'calendar')
            else:
                required_paths = ('history', 'evidence', 'quotes')
            for arg in required_paths:
                c.add_argument('--' + arg, type=Path, required=True)
            c.add_argument('--point-value-rub', required=True, help='Historical RUB per price point per contract: do not invent')
            c.add_argument('--tick-size', required=True)
        else:
            c.add_argument('--history', type=Path, help='Optional initial snapshot; otherwise automatically fetch history')
            c.add_argument('--history-days',type=int,default=730,help='Initial calendar lookback; pinned in SQLite on restart')
            c.add_argument('--evidence', type=Path, help='Optional legacy verified mode; autonomous mode needs no evidence')
            c.add_argument('--poll-seconds', type=int, default=60)
            c.add_argument('--max-quote-age', type=int, default=120)
            c.add_argument('--once', action='store_true')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'signals':
            bars = read_daily_closes(args.csv)
            for i in range(51, len(bars) + 1):
                s = crossover(bars[:i])
                print(s.day, s.target)
        elif args.command == 'fetch':
            fetch(args)
        elif args.command == 'replay':
            replay(args)
        elif args.command == 'historical-replay':
            run_historical_replay(args)
        else:
            run_paper(args)
    except (APIError, DataError, ValueError) as exc:
        raise SystemExit('STOP: ' + str(exc)) from None
    except (OSError, sqlite3.Error, KeyError, TypeError, csv.Error, ArithmeticError) as exc:
        # Do not dump raw API bodies, environment variables or traceback containing credentials.
        raise SystemExit('STOP: data/storage failure (' + type(exc).__name__ + '); no broker orders.') from None
    except KeyboardInterrupt:
        print('Stopped; committed PAPER state is retained. No broker orders.')


if __name__ == '__main__':
    main()
