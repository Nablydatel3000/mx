"""Allowlisted READ-ONLY T-Investments REST client. No accounts/order endpoints."""
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time as daytime
from decimal import Decimal

from .models import Bar, Quote, DataError, UTC, instant, iso, number

BASE = 'https://invest-public-api.tinkoff.ru/rest/tinkoff.public.invest.api.contract.v1.'
ALLOWED = {'InstrumentsService/FindInstrument', 'InstrumentsService/FutureBy',
           'InstrumentsService/TradingSchedules', 'InstrumentsService/GetFuturesMargin',
           'MarketDataService/GetCandles', 'MarketDataService/GetLastPrices',
           'MarketDataService/GetTradingStatus'}


class APIError(RuntimeError):
    pass


def quotation(value):
    try:
        if not isinstance(value, dict):
            raise ValueError()
        u, n = value.get('units', '0'), value.get('nano', 0)
        if isinstance(u, bool) or isinstance(n, bool) or str(int(u)) != str(u) or str(int(n)) != str(n):
            raise ValueError()
        units, nano = int(u), int(n)
        if not -(2**63) <= units < 2**63 or abs(nano) >= 1_000_000_000 or (units > 0 and nano < 0) or (units < 0 and nano > 0):
            raise ValueError()
        return number(Decimal(units) + Decimal(nano) / Decimal(1_000_000_000))
    except (KeyError, TypeError, ValueError) as exc:
        raise DataError('Invalid API quotation') from exc


@dataclass(frozen=True)
class Instrument:
    uid: str
    figi: str
    ticker: str
    exchange: str
    tick: Decimal
    first_daily: datetime | None = None


@dataclass(frozen=True)
class TradingDay:
    day: date
    trading: bool
    windows: tuple
    close: datetime | None

    def contains(self, timestamp):
        timestamp = instant(timestamp)
        return any(a <= timestamp < b for a, b in self.windows)


def parse_day(raw):
    day = instant(raw['date']).date()  # API date label; independent MOEX alignment still needs live verification.
    if type(raw.get('isTradingDay', False)) is not bool:
        raise DataError('Missing calendar trading flag')
    if not raw.get('isTradingDay', False):
        return TradingDay(day, False, (), None)

    def pair(start, end):
        if bool(raw.get(start)) != bool(raw.get(end)):
            raise DataError('Incomplete calendar interval: ' + start + '/' + end)
        if not raw.get(start):
            return None
        a, b = instant(raw[start]), instant(raw[end])
        if a >= b:
            raise DataError('Invalid calendar interval: ' + start + '/' + end)
        return a, b

    windows = []
    for start, end in [('startTime', 'endTime'), ('eveningStartTime', 'eveningEndTime'),
                       ('premarketStartTime', 'premarketEndTime')]:
        span = pair(start, end)
        if span:
            windows.append(span)
    if not windows:
        raise DataError('Trading day without supported session windows')
    known_spans = set(windows)
    # Official completion must NOT be shortened by removing a trailing clearing/auction.
    ends = [b for _, b in windows]
    exclusions = []
    for start, end in [('clearingStartTime', 'clearingEndTime'),
                       ('openingAuctionStartTime', 'openingAuctionEndTime'),
                       ('closingAuctionStartTime', 'closingAuctionEndTime')]:
        span = pair(start, end)
        if span:
            known_spans.add(span)
            exclusions.append(span)
            if start != 'clearingStartTime':
                ends.append(span[1])
    if raw.get('eveningOpeningAuctionStartTime'):
        if not raw.get('eveningStartTime'):
            raise DataError('Evening auction lacks a supported regular-session start')
        a, b = instant(raw['eveningOpeningAuctionStartTime']), instant(raw['eveningStartTime'])
        if a >= b:
            raise DataError('Evening auction cannot be separated from regular session')
        known_spans.add((a, b))
        exclusions.append((a, b))
    for a, b in exclusions:
        split = []
        for x, y in windows:
            if y <= a or x >= b:
                split.append((x, y))
            else:
                if x < a:
                    split.append((x, a))
                if b < y:
                    split.append((b, y))
        windows = split
    if not windows:
        raise DataError('No supported continuous trading window remains')
    # Free-text type is not classified by guesswork. Only exact mirrors of known
    # legacy spans are supported; an extra break/session must be reviewed explicitly.
    for item in raw.get('intervals', []):
        interval = item['interval']
        span = (instant(interval['startTs']), instant(interval['endTs']))
        if span not in known_spans:
            raise DataError('Unrecognized detailed trading interval; calendar mapping needs review')
    return TradingDay(day, True, tuple(sorted(windows)), max(ends))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the Authorization header to another location.


class ReadOnlyClient:
    def __init__(self, *, transport=None, sleep=time.sleep, attempts=3):
        self._token = os.environ.get('T_INVEST_TOKEN', '')
        if not self._token or any(c.isspace() for c in self._token):
            raise APIError('Set T_INVEST_TOKEN in the process environment; never send it in chat')
        if not 1 <= attempts <= 5:
            raise ValueError('Attempts must be 1..5')
        self.transport = transport or urllib.request.build_opener(NoRedirect()).open
        self.sleep, self.attempts = sleep, attempts

    def call(self, method, payload):
        if method not in ALLOWED:
            raise APIError('Endpoint prohibited by read-only allowlist')
        request = urllib.request.Request(BASE + method,
                  json.dumps(payload).encode(), {'Authorization': 'Bearer ' + self._token,
                  'Content-Type': 'application/json', 'Accept': 'application/json'}, method='POST')
        for attempt in range(self.attempts):
            try:
                with self.transport(request, timeout=20) as response:
                    data = response.read(16 * 1024 * 1024 + 1)
                if len(data) > 16 * 1024 * 1024:
                    raise APIError('API response too large')
                result = json.loads(data)
                if not isinstance(result, dict) or 'code' in result:
                    raise APIError('Unexpected API response')
                return result
            except urllib.error.HTTPError as exc:
                retry = exc.code == 429 or 500 <= exc.code < 600
                # Never expose response body, request headers, token or transport exception text.
                error = 'API HTTP ' + str(exc.code)
            except (urllib.error.URLError, TimeoutError, OSError):
                retry, error = True, 'API connection/timeout failure'
            except (json.JSONDecodeError, UnicodeError):
                raise APIError('Invalid API JSON') from None
            if not retry or attempt + 1 == self.attempts:
                raise APIError(error) from None
            self.sleep(min(2 ** attempt, 4))
        raise APIError('API unavailable')

    def instrument(self, uid=None):
        if not uid:
            response = self.call('InstrumentsService/FindInstrument', {'query': 'GLDRUBF'})
            candidates = [x for x in response.get('instruments', []) if x.get('ticker') == 'GLDRUBF'
                          and (x.get('instrumentKind') == 'INSTRUMENT_TYPE_FUTURES' or x.get('instrumentType') == 'futures')]
            uids = {x.get('uid') for x in candidates if x.get('uid')}
            if len(uids) != 1:
                raise DataError('GLDRUBF futures UID absent or ambiguous; specify --uid after checking official data')
            uid = uids.pop()
        raw = self.call('InstrumentsService/FutureBy', {'idType': 'INSTRUMENT_ID_TYPE_UID', 'id': uid}).get('instrument', {})
        if not isinstance(uid, str) or raw.get('uid') != uid or raw.get('ticker') != 'GLDRUBF' or raw.get('lot') != 1 or raw.get('currency', '').lower() != 'rub':
            raise DataError('UID is not verified GLDRUBF RUB future with lot=1')
        tick = quotation(raw.get('minPriceIncrement'))
        if tick <= 0 or not raw.get('exchange') or not raw.get('figi'):
            raise DataError('Incomplete instrument metadata')
        return Instrument(uid, raw['figi'], 'GLDRUBF', raw['exchange'], tick,
                          instant(raw['first1dayCandleDate']) if raw.get('first1dayCandleDate') else None)

    def point_value(self, instrument):
        # Current official contract requires instrumentId; do not use deprecated FIGI.
        raw = self.call('InstrumentsService/GetFuturesMargin', {'instrumentId': instrument.uid})
        tick, amount = quotation(raw.get('minPriceIncrement')), quotation(raw.get('minPriceIncrementAmount'))
        if tick != instrument.tick or amount <= 0:
            raise DataError('Invalid/currently changed tick monetary specification')
        return amount / tick

    def schedules(self, exchange, start, end):
        start, end = instant(start), instant(end)
        days = {}
        cursor = datetime.combine(start.date(), daytime.min, UTC)
        while cursor < end:
            finish = min(cursor + timedelta(days=7), end)
            result = self.call('InstrumentsService/TradingSchedules', {'exchange': exchange, 'from': iso(cursor), 'to': iso(finish)})
            matching = [x for x in result.get('exchanges', []) if x.get('exchange') == exchange]
            if len(matching) != 1:
                raise DataError('Missing/ambiguous exchange calendar')
            for raw in matching[0].get('days', []):
                day = parse_day(raw)
                if day.day in days and days[day.day] != day:
                    raise DataError('Conflicting calendar records')
                days[day.day] = day
            cursor = finish
        for n in range((end.date() - start.date()).days + 1):
            d = start.date() + timedelta(days=n)
            if datetime.combine(d, daytime.min, UTC) < end and d not in days:
                raise DataError('Calendar date missing: ' + d.isoformat())
        return days

    def candles(self, uid, start, end, interval='CANDLE_INTERVAL_DAY'):
        start, end = instant(start), instant(end)
        if start >= end or interval not in {'CANDLE_INTERVAL_DAY', 'CANDLE_INTERVAL_1_MIN'}:
            raise DataError('Invalid candle range/interval')
        step = timedelta(days=90 if interval == 'CANDLE_INTERVAL_DAY' else 1)
        cursor, result = start, {}
        while cursor < end:
            finish = min(cursor + step, end)
            response = self.call('MarketDataService/GetCandles', {'instrumentId': uid, 'from': iso(cursor), 'to': iso(finish), 'interval': interval, 'candleSourceType': 'CANDLE_SOURCE_EXCHANGE', 'limit': 2400})
            if not isinstance(response.get('candles'), list):
                raise DataError('Missing candles array')
            for raw in response['candles']:
                raw = dict(raw)
                complete = raw.get('isComplete', False)
                if type(complete) is not bool:
                    raise DataError('Invalid API candle completion flag')
                raw['isComplete'] = complete
                try:
                    volume = raw.get('volume', '0')
                    if isinstance(volume, bool) or str(int(volume)) != str(volume) or int(volume) < 0:
                        raise ValueError()
                    raw['volume'] = str(int(volume))
                except (ValueError, TypeError):
                    raise DataError('Invalid API candle volume') from None
                prices = [quotation(raw.get(k)) for k in ('open','high','low','close')]
                o,h,l,c = prices
                if min(prices) <= 0 or not (l <= o <= h and l <= c <= h):
                    raise DataError('Invalid API OHLC')
                if complete and int(raw['volume']) == 0:
                    raise DataError('Completed API candle has no volume')
                if raw.get('candleSource', 'CANDLE_SOURCE_EXCHANGE') != 'CANDLE_SOURCE_EXCHANGE':
                    raise DataError('Non-exchange candle rejected')
                t = instant(raw['time'])
                if interval == 'CANDLE_INTERVAL_DAY':
                    # Official DAY requests ignore the time-of-day and include endpoint dates.
                    if not cursor.date() <= t.date() <= finish.date():
                        raise DataError('API daily candle outside requested dates')
                elif t < cursor or t > finish:
                    raise DataError('API candle outside requested range')
                # The API may include a chunk endpoint. Next chunk must agree, not duplicate it.
                if t >= end:
                    continue
                if t in result and result[t] != raw:
                    raise DataError('Conflicting duplicate candle')
                result[t] = raw
            cursor = finish
        return [result[t] for t in sorted(result)]

    def completed_daily(self, instrument, start, now):
        start, now = instant(start), instant(now)
        if instrument.first_daily:
            first = datetime.combine(instrument.first_daily.date(), daytime.min, UTC)
            start = max(start, first)  # documented provider listing boundary, never infer from first returned bar
        if start >= now:
            raise DataError('No completed history in requested/listed range')
        calendar = self.schedules(instrument.exchange, start, now)
        raw = self.candles(instrument.uid, start, now)
        bars, deferred = {}, set()
        for x in raw:
            t = instant(x['time'])
            # Supported mapping is the API UTC-date label, never the host/Moscow wall date.
            # The contract alone does not prove a universal overnight trade-date mapping.
            day = calendar.get(t.date())
            if day is None or not day.trading:
                raise DataError('Candle cannot be mapped to official trading date')
            if any(other.day != day.day and other.contains(t) for other in calendar.values()):
                raise DataError('Daily timestamp overlaps another trading-day label; overnight mapping needs verification')
            if type(x.get('isComplete', False)) is not bool:
                raise DataError('Missing candle completion flag')
            if not x.get('isComplete', False):
                if day.day == now.date():
                    deferred.add(day.day)
                continue
            if day.close > now:
                continue
            bar = Bar(day.day, t, day.close, *(quotation(x[k]) for k in ('open', 'high', 'low', 'close')), int(x['volume']))
            if day.day in bars:
                raise DataError('Multiple daily bars for a trading date')
            bars[day.day] = bar
        expected = {d for d, s in calendar.items() if s.trading and s.close <= now and d >= start.date() and d not in deferred}
        if set(bars) != expected:
            raise DataError('Missing completed daily candles; no gap filling is permitted')
        return sorted(bars.values(), key=lambda b: b.day), calendar

    def quote(self, instrument, now):
        status = self.call('MarketDataService/GetTradingStatus', {'instrumentId': instrument.uid})
        if status.get('instrumentUid') != instrument.uid:
            raise DataError('Trading status UID mismatch')
        if status.get('tradingStatus') != 'SECURITY_TRADING_STATUS_NORMAL_TRADING' or not status.get('marketOrderAvailableFlag') or not status.get('apiTradeAvailableFlag'):
            return None
        data = self.call('MarketDataService/GetLastPrices', {'instrumentId': [instrument.uid], 'lastPriceType': 'LAST_PRICE_EXCHANGE'})
        matches = [x for x in data.get('lastPrices', []) if x.get('instrumentUid') == instrument.uid]
        if len(matches) != 1:
            raise DataError('Missing/ambiguous latest price')
        if matches[0].get('lastPriceType', 'LAST_PRICE_EXCHANGE') != 'LAST_PRICE_EXCHANGE':
            raise DataError('Non-exchange last price rejected')
        quote = Quote(instant(matches[0]['time']), quotation(matches[0]['price']))
        now = instant(now() if callable(now) else now)
        if quote.time > now:
            raise DataError('Future market timestamp')
        return quote
