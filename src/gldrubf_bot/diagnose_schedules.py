"""Read-only TradingSchedules probes; no calendar synthesis or PAPER execution."""
import json
import os
from datetime import date, datetime, timedelta, time as daytime

from .api import APIError, ALLOWED, ReadOnlyClient, public_exchange, schedule_request
from .api_error_codes import KNOWN_API_CODES
from .diagnose_api import read_token
from .models import DataError, UTC, instant, iso


METHOD = 'InstrumentsService/TradingSchedules'


def _safe_error(error):
    status = error.http_status
    grpc, api = error.grpc_code, error.api_code
    return dict(http_status=status if type(status) is int and 100 <= status <= 599 else 'unknown',
                category='api_error',
                grpc_code=grpc if type(grpc) is int and 0 <= grpc <= 16 else 'unknown',
                api_code=api if type(api) is int and api in KNOWN_API_CODES else 'unknown')


def _probe(client, stage, payload, exchange):
    details = dict(stage=stage, method=METHOD, exchange=public_exchange(payload.get('exchange'))
                   if 'exchange' in payload else 'omitted',
                   from_utc=payload['from'], to_utc=payload['to'])
    try:
        response = client.call(METHOD, payload)
        exchanges = response.get('exchanges', [])
        if not isinstance(exchanges, list) or any(not isinstance(x, dict) for x in exchanges):
            raise DataError('Invalid calendar response')
        matched = any(row.get('exchange') == exchange for row in exchanges)
        details.update(http_status=200, category='calendar_received' if matched else 'calendar_exchange_missing',
                       instrument_exchange_present=matched)
        passed = matched
    except APIError as error:
        details.update(_safe_error(error))
        passed = False
    except DataError:
        details.update(http_status=200, category='invalid_calendar_response')
        passed = False
    print(json.dumps(details, sort_keys=True), flush=True)
    return passed


def run(client, start, end, now):
    """Probe the actual first history chunk, then two same-day controls."""
    start, end, now = instant(start), instant(end), instant(now)
    if start >= end:
        raise DataError('Invalid range')
    instrument = client.instrument()
    # Same listing boundary as completed_daily; do not infer it from first candle.
    effective = start
    if instrument.first_daily:
        effective = max(effective, datetime.combine(instrument.first_daily.date(), daytime.min, UTC))
    print(json.dumps(dict(stage='instrument', exchange_source='FutureBy',
                          exchange=public_exchange(instrument.exchange), requested_from_utc=iso(start),
                          effective_from_utc=iso(effective)), sort_keys=True), flush=True)
    if effective >= end:
        raise DataError('No listed history in range')
    requested = schedule_request(instrument.exchange, effective, end)
    today = datetime.combine(now.date(), daytime.min, UTC)
    control = schedule_request(instrument.exchange, today, today + timedelta(days=1))
    discovery = {k: v for k, v in control.items() if k != 'exchange'}
    # No auto-retry: each stage is one calendar request. Diagnostic controls do
    # not turn a failed history request into successful output or a saved calendar.
    outcomes = [_probe(client, 'requested_first_chunk', requested, instrument.exchange),
                _probe(client, 'current_day_control', control, instrument.exchange),
                _probe(client, 'current_day_discovery', discovery, instrument.exchange)]
    return int(not all(outcomes))


def main():
    os.environ.pop('SSLKEYLOGFILE', None)
    try:
        start = datetime.combine(date.fromisoformat(input('First date YYYY-MM-DD: ').strip()), daytime.min, UTC)
        end = datetime.combine(date.fromisoformat(input('End date YYYY-MM-DD (exclusive): ').strip()), daytime.min, UTC)
        if start >= end:
            raise DataError('Invalid range')
        token = read_token()
        previous = os.environ.get('T_INVEST_TOKEN')
        try:
            os.environ['T_INVEST_TOKEN'] = token
            client = ReadOnlyClient(attempts=1)
        finally:
            if previous is None:
                os.environ.pop('T_INVEST_TOKEN', None)
            else:
                os.environ['T_INVEST_TOKEN'] = previous
        return run(client, start, end, datetime.now(UTC))
    except APIError as error:
        details = _safe_error(error)
        details.update(stage='instrument', method=error.method if error.method in ALLOWED else 'unknown')
        print(json.dumps(details, sort_keys=True))
    except (KeyboardInterrupt, EOFError):
        print('stage=diagnostic category=cancelled')
    except Exception:
        print('stage=diagnostic category=input_or_data_error')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
