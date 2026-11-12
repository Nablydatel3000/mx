"""Transactional virtual fills only. Never constructs or sends broker orders."""
import csv
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from .models import Bar, Quote, DataError, MOSCOW, instant, iso, number
from .strategy import DailyClose, crossover


@dataclass(frozen=True)
class Costs:
    point_value_rub: Decimal
    tick: Decimal
    commission_rub: Decimal
    slippage_ticks: int
    initial_cash_rub: Decimal = Decimal('100000')
    funding_daily_rub: Decimal | None = None

    def __post_init__(self):
        for name in ('point_value_rub', 'tick'):
            object.__setattr__(self, name, number(getattr(self, name), positive=True))
        for name in ('commission_rub', 'initial_cash_rub'):
            value = number(getattr(self, name))
            if value < 0:
                raise DataError('Negative cost/initial cash')
            object.__setattr__(self, name, value)
        if type(self.slippage_ticks) is not int or self.slippage_ticks < 0:
            raise DataError('Slippage ticks must be a nonnegative integer')
        if self.funding_daily_rub is not None:
            object.__setattr__(self, 'funding_daily_rub', number(self.funding_daily_rub))

    def payload(self):
        return {k: str(v.normalize()) if isinstance(v, Decimal) else v for k, v in asdict(self).items()}


class PaperEngine:
    def __init__(self, path, uid, costs, *, funding_rates=None):
        if not uid or not isinstance(uid, str):
            raise DataError('Explicit instrument UID is required')
        self.path, self.uid, self.costs = Path(path), uid, costs
        self.rates = {date.fromisoformat(k): number(v) for k, v in (funding_rates or {}).items()}
        if costs.funding_daily_rub is not None and self.rates:
            raise DataError('Choose fixed scenario funding OR explicit rates')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            legacy = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='paper_events'").fetchone()
            if legacy and db.execute('SELECT 1 FROM paper_events LIMIT 1').fetchone():
                raise DataError('Legacy target-only DB has no fills: keep it unchanged and choose a NEW database')
            db.execute('CREATE TABLE IF NOT EXISTS metadata (id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL)')
            config = json.dumps(dict(schema=1, uid=uid, strategy='SMA20/50', costs=costs.payload()), sort_keys=True)
            previous = db.execute('SELECT config FROM metadata WHERE id=1').fetchone()
            if previous and previous[0] != config:
                raise DataError('Database UID/cost/strategy mismatch; no silent revaluation')
            db.execute('INSERT OR IGNORE INTO metadata VALUES (1,?)', (config,))
            db.execute('''CREATE TABLE IF NOT EXISTS bars(day TEXT PRIMARY KEY, payload TEXT NOT NULL,
                          known_at TEXT NOT NULL, target INTEGER NOT NULL CHECK(target IN (-1,0,1)))''')
            db.execute('''CREATE TABLE IF NOT EXISTS quotes(time TEXT PRIMARY KEY, price TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY, time TEXT NOT NULL,
                       side INTEGER NOT NULL, kind TEXT NOT NULL, price TEXT NOT NULL,
                       gross TEXT NOT NULL, commission TEXT NOT NULL, signal_day TEXT NOT NULL,
                       position_after INTEGER NOT NULL CHECK(position_after IN (-1,0,1)))''')
            db.execute('''CREATE TABLE IF NOT EXISTS funding(day TEXT PRIMARY KEY, effective_at TEXT NOT NULL,
                       long_cost TEXT NOT NULL, position INTEGER NOT NULL, cash TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1),
                       position INTEGER NOT NULL CHECK(position IN (-1,0,1)), entry TEXT NOT NULL,
                       gross TEXT NOT NULL, fees TEXT NOT NULL, funding TEXT NOT NULL,
                       pending INTEGER NOT NULL, pending_day TEXT, pending_after TEXT)''')
            db.execute("INSERT OR IGNORE INTO state VALUES (1,0,'0','0','0','0',0,NULL,NULL)")
            self._validate_state(db)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(str(self.path), timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _position_before(self, db, cutoff):
        # Same-time CLOSE/OPEN are both excluded: funding is settled immediately BEFORE cutoff.
        row = db.execute('SELECT position_after FROM fills WHERE time < ? ORDER BY id DESC LIMIT 1',
                         (iso(cutoff),)).fetchone()
        return row[0] if row else 0

    def _funding_cutoff(self, row):
        payload = json.loads(row['payload'])
        if payload.get('kind') == 'provider_daily_observed':
            from .history import candle_hash, validate_rows
            source = payload['candle']
            confirmed = instant(payload['first_confirmed_at'])
            validate_rows([dict(source, observed_at=iso(confirmed))], self.uid,
                          instant(source['time']), instant(source['time']) + timedelta(microseconds=1))
            if (not source['is_complete'] or source['provider_day'] != row['day'] or
                    iso(confirmed) != row['known_at'] or candle_hash(source) != payload['candle_sha256'] or
                    payload['provider_time_msk'] != instant(source['time']).astimezone(MOSCOW).isoformat()):
                raise DataError('Corrupt observed candle identity/confirmation time')
            return confirmed
        return instant(payload['closed_at'])

    def _validate_state(self, db):
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise DataError('SQLite integrity check failed')
        state = db.execute('SELECT * FROM state WHERE id=1').fetchone()
        position, entry, gross, fees = 0, Decimal(0), Decimal(0), Decimal(0)
        previous = None
        for f in db.execute('SELECT * FROM fills ORDER BY id'):
            stamp = instant(f['time'])
            if f['time'] != iso(stamp):
                raise DataError('Noncanonical fill timestamp; chronology cannot be certified')
            if previous is not None and stamp < previous:
                raise DataError('Corrupt fill chronology')
            previous = stamp
            price = number(f['price'], positive=True)
            if f['side'] not in (-1, 1) or number(f['commission']) != self.costs.commission_rub:
                raise DataError('Corrupt fill cost/side')
            if f['kind'] == 'OPEN' and position == 0:
                position, entry, realized = f['side'], price, Decimal(0)
            elif f['kind'] == 'CLOSE' and position and f['side'] == -position:
                realized = (price-entry)*position*self.costs.point_value_rub
                position, entry = 0, Decimal(0)
            else:
                raise DataError('Corrupt position lifecycle')
            if number(f['gross']) != realized or f['position_after'] != position:
                raise DataError('Fill P&L/position mismatch')
            gross += realized
            fees += number(f['commission'])
        bar_rows = {r['day']: r for r in db.execute('SELECT * FROM bars')}
        funding_rows = {r['day']: r for r in db.execute('SELECT * FROM funding')}
        if set(bar_rows) != set(funding_rows):
            raise DataError('Funding coverage disagrees with processed daily bars')
        funding = Decimal(0)
        for f in funding_rows.values():
            cutoff = self._funding_cutoff(bar_rows[f['day']])
            if instant(f['effective_at']) != cutoff:
                raise DataError('Funding cutoff disagrees with bar model clock')
            if f['position'] != self._position_before(db, cutoff):
                raise DataError('Funding position disagrees with historical fills at cutoff')
            if self.costs.funding_daily_rub is not None and number(f['long_cost']) != self.costs.funding_daily_rub:
                raise DataError('Funding rate disagrees with fixed scenario configuration')
            if f['position'] not in (-1, 0, 1) or number(f['cash']) != -f['position']*number(f['long_cost']):
                raise DataError('Funding ledger mismatch')
            if self.costs.funding_daily_rub is None and f['position'] and date.fromisoformat(f['day']) in self.rates:
                if number(f['long_cost']) != self.rates[date.fromisoformat(f['day'])]:
                    raise DataError('Previously applied funding rate was revised')
            funding += number(f['cash'])
        if (state['position'] != position or number(state['entry']) != entry or number(state['gross']) != gross
                or number(state['fees']) != fees or number(state['funding']) != funding):
            raise DataError('Persistent state disagrees with ledger; no automatic repair')
        if state['pending'] not in (-1, 0, 1) or (bool(state['pending']) != bool(state['pending_day'] and state['pending_after'])):
            raise DataError('Invalid pending signal state')
        if state['pending']:
            instant(state['pending_after'])
        if bar_rows and any(json.loads(r['payload']).get('kind')=='provider_daily_observed' for r in bar_rows.values()):
            history, latest = [], None
            for label,row in sorted(bar_rows.items()):
                payload=json.loads(row['payload'])
                if payload.get('kind')!='provider_daily_observed':
                    raise DataError('Mixed daily clocks in autonomous ledger')
                history.append(DailyClose(date.fromisoformat(label),float(payload['candle']['close'])))
                target=crossover(history).target if len(history)>=51 else 0
                if row['target']!=target:
                    raise DataError('Stored SMA target disagrees with completed candle prefix')
                if target:
                    latest=row
            if state['pending']:
                if (latest is None or state['pending']!=latest['target'] or state['pending_day']!=latest['day']
                        or state['pending_after']!=latest['known_at']):
                    raise DataError('Pending signal disagrees with latest SMA crossing')
                if db.execute('SELECT 1 FROM quotes WHERE time>? LIMIT 1',(latest['known_at'],)).fetchone():
                    raise DataError('Pending signal was already consumed')
            elif latest:
                consumed=db.execute('SELECT 1 FROM quotes WHERE time>? LIMIT 1',(latest['known_at'],)).fetchone()
                if not consumed or state['position']!=latest['target']:
                    raise DataError('Consumed SMA target disagrees with position/observations')
            elif state['position']:
                raise DataError('Position exists without an observed SMA crossing')

    def latest_day(self):
        with self.db() as db:
            row = db.execute('SELECT MAX(day) FROM bars').fetchone()
            return date.fromisoformat(row[0]) if row[0] else None

    def process_bars(self, bars, known_at, *, coverage_start=None, coverage_end=None):
        """One atomic catch-up. Signals become known NOW, never at an invented old price.

        Multiple missed crossings supersede an unfilled target. No virtual historical fills.
        """
        known_at = instant(known_at)
        if any(not isinstance(b, Bar) for b in bars):
            raise DataError('Validated daily bars required')
        if (coverage_start is None) != (coverage_end is None):
            raise DataError('Both source coverage boundaries are required')
        if coverage_start is None and bars:
            coverage_start, coverage_end = bars[0].day, bars[-1].day
        if coverage_start is not None and (type(coverage_start) is not date or type(coverage_end) is not date or coverage_start > coverage_end):
            raise DataError('Invalid source coverage dates')
        if [b.day for b in bars] != sorted({b.day for b in bars}):
            raise DataError('Daily batch must be unique and ordered')
        if bars and self.costs.funding_daily_rub is None:
            dates = {b.day for b in bars}
            if any(bars[0].day <= d <= bars[-1].day and d not in dates for d in self.rates):
                raise DataError('Funding on a non-candle date is unsupported by the daily settlement model')
        with self.db() as db:
            if coverage_start is not None:
                cached = {r[0] for r in db.execute('SELECT day FROM bars WHERE day>=? AND day<=?',
                          (coverage_start.isoformat(), coverage_end.isoformat()))}
                if not cached.issubset({b.day.isoformat() for b in bars}):
                    raise DataError('Previously cached daily bar disappeared from refreshed calendar/history')
            for bar in bars:
                if not isinstance(bar, Bar) or not bar.complete or bar.closed_at > known_at:
                    raise DataError('Only completed, historically available bars may form signals')
                payload = json.dumps(bar.payload(), sort_keys=True)
                existing = db.execute('SELECT payload FROM bars WHERE day=?', (bar.day.isoformat(),)).fetchone()
                if existing:
                    if existing[0] != payload:
                        raise DataError('Conflicting revision of completed daily bar')
                    old_rate = db.execute('SELECT long_cost,position FROM funding WHERE day=?', (bar.day.isoformat(),)).fetchone()
                    if self.costs.funding_daily_rub is None and old_rate and old_rate['position']:
                        current_rate = self.rates.get(bar.day)
                        if current_rate is None or current_rate != number(old_rate['long_cost']):
                            raise DataError('Missing/revised previously applied funding rate')
                    continue
                last = db.execute('SELECT day,payload,known_at FROM bars ORDER BY day DESC LIMIT 1').fetchone()
                if last and (bar.day.isoformat() <= last['day'] or bar.closed_at <= instant(json.loads(last['payload'])['closed_at']) or known_at < instant(last['known_at'])):
                    raise DataError('Out-of-order bar/observation')
                last_quote = db.execute('SELECT time FROM quotes ORDER BY time DESC LIMIT 1').fetchone()
                if last_quote and known_at < instant(last_quote['time']):
                    raise DataError('Signal observation cannot precede an already consumed quote')
                # Daily financing scenario: position held immediately before official session end.
                held = self._position_before(db, bar.closed_at)
                rate = self.costs.funding_daily_rub
                if rate is None:
                    rate = self.rates.get(bar.day)
                    if rate is None and held:
                        raise DataError('Missing funding rate for held trading day; atomic batch stopped')
                    rate = rate if rate is not None else Decimal(0)
                cash = -held * rate
                db.execute('INSERT INTO funding VALUES (?,?,?,?,?)', (bar.day.isoformat(), iso(bar.closed_at), str(rate), held, str(cash)))
                state = db.execute('SELECT * FROM state WHERE id=1').fetchone()
                db.execute('UPDATE state SET funding=? WHERE id=1', (str(number(state['funding']) + cash),))
                history = [DailyClose(date.fromisoformat(r['day']), float(json.loads(r['payload'])['close']))
                           for r in db.execute('SELECT day,payload FROM bars ORDER BY day')]
                history.append(DailyClose(bar.day, float(bar.close)))
                target = crossover(history).target if len(history) >= 51 else 0
                db.execute('INSERT INTO bars VALUES (?,?,?,?)', (bar.day.isoformat(), payload, iso(known_at), target))
                if target:
                    db.execute('UPDATE state SET pending=?,pending_day=?,pending_after=? WHERE id=1',
                               (target, bar.day.isoformat(), iso(known_at)))

    def process_observed_candles(self, rows, known_at, *, source_start, source_end):
        """Accept API-complete observations, without inventing historical closes.

        day is a provider UTC label used for ordering, NOT a verified session date.
        Funding is an explicit per-newly-confirmed-candle scenario at observation
        time, not an assertion of broker settlement time. Bootstrap is flat.
        """
        from .history import candle_identity, candle_hash, validate_rows
        known_at, source_start, source_end = map(instant, (known_at, source_start, source_end))
        validate_rows(rows, self.uid, source_start, source_end)
        if source_end > known_at or any(instant(r['observed_at']) > known_at for r in rows):
            raise DataError('Future candle observations cannot form signals')
        complete = {r['provider_day']: r for r in rows if r['is_complete']}
        if len(complete) < 51:
            raise DataError('Need 51 API-completed provider candles; no artificial warmup')
        with self.db() as db:
            cached = list(db.execute('SELECT * FROM bars ORDER BY day'))
            for old in cached:
                payload = json.loads(old['payload'])
                if payload.get('kind') != 'provider_daily_observed':
                    raise DataError('Cannot mix session-timed and provider-observed bars')
                source = payload['candle']
                current = complete.get(old['day'])
                if current is None or candle_identity(current) != source:
                    raise DataError('Previously accepted completed candle changed/disappeared')
                if self.costs.funding_daily_rub is None:
                    applied = db.execute('SELECT long_cost,position FROM funding WHERE day=?',(old['day'],)).fetchone()
                    rate = self.rates.get(date.fromisoformat(old['day']))
                    if applied and applied['position'] and (rate is None or rate != number(applied['long_cost'])):
                        raise DataError('Missing/revised previously applied financing scenario')
            last_known = instant(cached[-1]['known_at']) if cached else None
            quote = db.execute('SELECT time FROM quotes ORDER BY time DESC LIMIT 1').fetchone()
            if (last_known and known_at < last_known) or (quote and known_at < instant(quote[0])):
                raise DataError('Clock/observation moved backwards')
            history = [DailyClose(date.fromisoformat(r['day']), float(json.loads(r['payload'])['candle']['close'])) for r in cached]
            seen = {r['day'] for r in cached}
            for label, row in complete.items():
                if label in seen:
                    continue
                if history and date.fromisoformat(label) <= history[-1].day:
                    raise DataError('Late insertion changes an already accepted SMA prefix; review data')
                rate = self.costs.funding_daily_rub
                held = self._position_before(db, known_at)
                if rate is None:
                    rate = self.rates.get(date.fromisoformat(label))
                    if rate is None and held:
                        raise DataError('Missing provider-label financing scenario; batch stopped')
                    rate = rate if rate is not None else Decimal(0)
                payload = dict(kind='provider_daily_observed', candle=candle_identity(row),
                    candle_sha256=candle_hash(row), first_confirmed_at=iso(known_at),
                    provider_time_msk=instant(row['time']).astimezone(MOSCOW).isoformat())
                history.append(DailyClose(date.fromisoformat(label), float(row['close'])))
                target = crossover(history).target if len(history) >= 51 else 0
                db.execute('INSERT INTO bars VALUES (?,?,?,?)', (label,json.dumps(payload,sort_keys=True),iso(known_at),target))
                cash = -held*rate
                db.execute('INSERT INTO funding VALUES (?,?,?,?,?)', (label,iso(known_at),str(rate),held,str(cash)))
                state = db.execute('SELECT funding FROM state WHERE id=1').fetchone()
                db.execute('UPDATE state SET funding=? WHERE id=1', (str(number(state[0])+cash),))
                if target:
                    db.execute('UPDATE state SET pending=?,pending_day=?,pending_after=? WHERE id=1',
                               (target,label,iso(known_at)))

    def observe(self, quote):
        """Fill at the first eligible OBSERVED price strictly after signal knowledge.

        A sampled last-trade price + adverse slippage is a model, not an executable bid/ask.
        """
        if not isinstance(quote, Quote):
            raise DataError('Validated quote required')
        if quote.price % self.costs.tick:
            raise DataError('Quote is off the instrument tick grid')
        with self.db() as db:
            existing = db.execute('SELECT price FROM quotes WHERE time=?', (iso(quote.time),)).fetchone()
            if existing:
                if number(existing[0]) != quote.price:
                    raise DataError('Different prices at one observation timestamp: order unknown')
                return False
            last = db.execute('SELECT time FROM quotes ORDER BY time DESC LIMIT 1').fetchone()
            if last and quote.time < instant(last[0]):
                raise DataError('Stale/out-of-order observation')
            watermark = db.execute('SELECT known_at FROM bars ORDER BY day DESC LIMIT 1').fetchone()
            if watermark and quote.time < instant(watermark[0]):
                # A late quote must not backdate a fill into an already processed funding snapshot.
                # Do not mark/consume it: wait for a causally eligible observation.
                return False
            db.execute('INSERT INTO quotes VALUES (?,?)', (iso(quote.time), str(quote.price)))
            state = db.execute('SELECT * FROM state WHERE id=1').fetchone()
            if not state['pending'] or quote.time <= instant(state['pending_after']):
                return False
            position, entry = state['position'], number(state['entry'])
            target = state['pending']
            gross, fees = number(state['gross']), number(state['fees'])
            executed = False
            if position != target:
                legs = [('CLOSE', -position)] if position else []
                legs.append(('OPEN', target))
                for kind, side in legs:
                    price = quote.price + side * self.costs.slippage_ticks * self.costs.tick
                    if price <= 0:
                        raise DataError('Slippage produces invalid price')
                    realized = (price - entry) * position * self.costs.point_value_rub if kind == 'CLOSE' else Decimal(0)
                    position = 0 if kind == 'CLOSE' else target
                    entry = Decimal(0) if kind == 'CLOSE' else price
                    gross += realized
                    fees += self.costs.commission_rub
                    db.execute('INSERT INTO fills(time,side,kind,price,gross,commission,signal_day,position_after) VALUES (?,?,?,?,?,?,?,?)',
                               (iso(quote.time), side, kind, str(price), str(realized), str(self.costs.commission_rub), state['pending_day'], position))
                executed = True
            db.execute('UPDATE state SET position=?,entry=?,gross=?,fees=?,pending=0,pending_day=NULL,pending_after=NULL WHERE id=1',
                       (position, str(entry), str(gross), str(fees)))
            return executed

    def summary(self):
        with self.db() as db:
            return self._summary(db)

    def _summary(self, db):
        state = dict(db.execute('SELECT * FROM state WHERE id=1').fetchone())
        last = db.execute('SELECT time,price FROM quotes ORDER BY time DESC LIMIT 1').fetchone()
        watermark = db.execute('SELECT known_at FROM bars ORDER BY day DESC LIMIT 1').fetchone()
        mark = number(last['price']) if last else None
        unrealized = ((mark - number(state['entry'])) * state['position'] * self.costs.point_value_rub
                      if mark is not None and state['position'] else Decimal(0))
        net = number(state['gross']) - number(state['fees']) + number(state['funding'])
        return dict(uid=self.uid, position=state['position'], entry=state['entry'], pending=state['pending'],
                    pending_after=state['pending_after'], observation_watermark=watermark[0] if watermark else None, realized_gross=state['gross'], commission=state['fees'],
                    funding=state['funding'], realized_net=str(net), unrealized=str(unrealized),
                    equity=str(self.costs.initial_cash_rub + net + unrealized),
                    mark_time=last['time'] if last else None,
                    fills=db.execute('SELECT COUNT(*) FROM fills').fetchone()[0])


    def export(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            for table in ('bars', 'quotes', 'fills', 'funding'):
                cursor = db.execute('SELECT * FROM ' + table + ' ORDER BY 1')  # constant allowlist only
                with (directory / (table + '.csv.tmp')).open('w', encoding='utf-8-sig', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow([d[0] for d in cursor.description])
                    writer.writerows(cursor)
            summary = self._summary(db)
        # Each export is replaceable; SQLite is the authoritative recovery source.
        for table in ('bars', 'quotes', 'fills', 'funding'):
            os.replace(directory / (table + '.csv.tmp'), directory / (table + '.csv'))
        (directory / 'summary.json.tmp').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(directory / 'summary.json.tmp', directory / 'summary.json')
