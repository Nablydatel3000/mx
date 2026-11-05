"""Allowlisted READ-ONLY T-Investments REST client. No accounts/order endpoints."""
import json
import http.client
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time as daytime
from decimal import Decimal

from .models import Bar, Quote, DataError, UTC, instant, iso, number
from .api_error_codes import extract_codes, read_error_codes

BASE = 'https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1.'
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
ALLOWED = {'InstrumentsService/FindInstrument', 'InstrumentsService/FutureBy',
           'InstrumentsService/TradingSchedules', 'InstrumentsService/GetFuturesMargin',
           'MarketDataService/GetCandles', 'MarketDataService/GetLastPrices',
           'MarketDataService/GetTradingStatus'}


class APIError(RuntimeError):
    def __init__(self, message, *, method=None, http_status=None, content_type='unknown',
                 category=None, retryable=False, grpc_code=None, api_code=None):
        super().__init__(message)
        self.method, self.http_status = method, http_status
        self.content_type, self.category = content_type, category
        self.retryable = retryable
        self.grpc_code, self.api_code = grpc_code, api_code
        self.request_parameters = None


def _metadata(response):
    status = getattr(response, 'status', None)
    if type(status) is not int or not 100 <= status <= 599:
        status = None
    headers = getattr(response, 'headers', None)
    value = headers.get('Content-Type', '') if headers is not None else ''
    media_type = value.partition(';')[0].strip().lower()
    # Never echo arbitrary header values (a proxy could reflect credentials there).
    known = {'application/json', 'application/problem+json', 'text/html',
             'text/plain', 'application/octet-stream'}
    safe_type = media_type if media_type in known else ('missing' if not value else 'other')
    return status, safe_type


def _failure(method, status, content_type, category, *, retryable=False, codes=(None, None)):
    # Only the allowlisted method, numeric status and fixed labels reach diagnostics.
    message = (f'API failure: method={method} http_status={status if status is not None else "unknown"} '
               f'content_type={content_type} category={category} '
               f'grpc_code={codes[0] if codes[0] is not None else "unknown"} '
               f'api_code={codes[1] if codes[1] is not None else "unknown"}')
    return APIError(message, method=method, http_status=status, content_type=content_type,
                    category=category, retryable=retryable, grpc_code=codes[0], api_code=codes[1])


def _http_failure(method, status, content_type, *, codes=(None, None)):
    category = {401: 'authentication_error', 403: 'permission_denied',
                407: 'proxy_authentication_error', 408: 'http_timeout',
                429: 'rate_limited'}.get(status, 'http_error')
    if status is not None and 300 <= status < 400:
        category = 'redirect_blocked'
    elif status is not None and 500 <= status < 600:
        category = 'server_error'
    elif status is not None and 200 <= status < 300:
        category = 'unexpected_http_status'  # REST methods specify 200, not 204/206.
    retryable = status in (408, 429) or (status is not None and 500 <= status < 600)
    return _failure(method, status, content_type, category, retryable=retryable, codes=codes)


def schedule_request(exchange, start, end):
    """First seven-day request, preserving the API's exact exchange identifier."""
    start, end = instant(start), instant(end)
    if not isinstance(exchange, str) or not exchange or start >= end:
        raise DataError('Invalid schedule exchange/range')
    cursor = datetime.combine(start.date(), daytime.min, UTC)
    return {'exchange': exchange, 'from': iso(cursor), 'to': iso(min(cursor + timedelta(days=7), end))}


def public_exchange(value):
    # Official markets.md values; unknown response text must not be echoed.
    known = {'MOEX', 'MOEX_PLUS', 'MOEX_WEEKEND', 'MOEX_EVENING_WEEKEND',
             'moex_extended', 'FORTS', 'FORTS_EVENING', 'FX', 'FX_MTL', 'otc_ncc',
             'dealer_realestate', 'dealer_int_exchange', 'moex_close', 'LSE_MORNING', 'spb_close'}
    return value if isinstance(value, str) and value in known else 'other'


def _schedule_error_context(error, payload):
    if not isinstance(payload, dict) or error.request_parameters is not None:
        return
    parameters = {'exchange': public_exchange(payload.get('exchange'))}
    for key in ('from', 'to'):
        try:
            parameters[key] = iso(payload.get(key))
        except DataError:
            parameters[key] = 'invalid'
    error.request_parameters = parameters
    error.args = (str(error) + ' exchange=' + parameters['exchange'] +
                  ' from_utc=' + parameters['from'] + ' to_utc=' + parameters['to'],)


def _network_failure(method, status, content_type, exc):
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    retryable = True
    if isinstance(reason, ssl.SSLCertVerificationError):
        category, retryable = 'tls_verification_error', False
    elif isinstance(reason, ssl.SSLError):
        category, retryable = 'tls_error', False
    elif isinstance(reason, TimeoutError):
        category = 'timeout'
    elif isinstance(reason, socket.gaierror):
        category = 'dns_error'
    elif isinstance(reason, str) and reason.startswith('Tunnel connection failed:'):
        category = 'proxy_tunnel_error'  # Do not include CONNECT reason/URL/credentials.
    elif isinstance(reason, http.client.IncompleteRead):
        category = 'incomplete_response'
    elif isinstance(reason, http.client.HTTPException):
        category = 'http_protocol_error'
    else:
        category = 'network_error'
    return _failure(method, status, content_type, category, retryable=retryable)


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
            status, content_type = None, 'unknown'
            try:
                with self.transport(request, timeout=20) as response:
                    status, content_type = _metadata(response)
                    if status != 200:
                        raise _http_failure(method, status, content_type,
                                            codes=read_error_codes(response, content_type))
                    data = response.read(MAX_RESPONSE_BYTES + 1)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise _failure(method, status, content_type, 'response_too_large')
                if not data.strip():
                    raise _failure(method, status, content_type, 'empty_response')
                if content_type not in {'application/json', 'application/problem+json'}:
                    raise _failure(method, status, content_type, 'unexpected_content_type')
                try:
                    result = json.loads(data)
                except (json.JSONDecodeError, UnicodeError, RecursionError):
                    raise _failure(method, status, content_type, 'invalid_json') from None
                if not isinstance(result, dict):
                    raise _failure(method, status, content_type, 'unexpected_json_type')
                if content_type == 'application/problem+json' or any(k in result for k in ('code', 'error', 'message')):
                    raise _failure(method, status, content_type, 'api_error_envelope', codes=extract_codes(result))
                return result
            except urllib.error.HTTPError as exc:
                try:
                    status, content_type = _metadata(exc)
                    error = _http_failure(method, status, content_type,
                                          codes=read_error_codes(exc, content_type))
                finally:
                    exc.close()  # Only bounded numeric diagnostics are extracted; no body is logged.
            except APIError as exc:
                error = exc
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                error = _network_failure(method, status, content_type, exc)
            except UnicodeError:
                # Header/request encoding errors must not be mislabeled as JSON errors.
                error = _failure(method, status, content_type, 'request_encoding_error')
            if method == 'InstrumentsService/TradingSchedules':
                _schedule_error_context(error, payload)
            if not error.retryable or attempt + 1 == self.attempts:
                raise error from None
            self.sleep(min(2 ** attempt, 4))

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
            payload = schedule_request(exchange, cursor, end)
            finish = instant(payload['to'])
            result = self.call('InstrumentsService/TradingSchedules', payload)
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
