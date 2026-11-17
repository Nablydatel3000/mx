"""Opt-in local-console-only exchange identifiers; never called by PAPER/loggers."""
import json
import os
import re
import sys
from datetime import datetime

from .api import APIError, ALLOWED, ReadOnlyClient, current_schedule_request
from .diagnose_api import read_token
from .diagnose_schedules import _safe_error
from .models import DataError, UTC


METHOD = 'InstrumentsService/TradingSchedules'


def require_console():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise DataError('Interactive local console required; redirection is disabled')


def local_code(value, token):
    """Not a venue allowlist: preserve new identifiers exactly, redact unsafe text.

    Only exchange fields are projected, never arbitrary JSON or error messages.
    Do not render a reflected credential, control characters or long free text.
    """
    if (not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', value)
            or (token and token in value)):
        return '[redacted]'
    return value


def run(client, now, *, token):
    require_console()
    instrument = client.instrument()
    requested = current_schedule_request(instrument.exchange, now)
    print(json.dumps(dict(stage='instrument', exchange_source='FutureBy',
        instrument_exchange=local_code(instrument.exchange, token))), flush=True)
    outcomes = []
    for stage, payload in [('current_filtered', requested),
                           ('current_unfiltered', {k: v for k, v in requested.items() if k != 'exchange'})]:
        details = dict(stage=stage, method=METHOD, from_utc=payload['from'], to_utc=payload['to'],
            exchange_filter='present' if 'exchange' in payload else 'omitted',
            requested_exchange=local_code(instrument.exchange, token))
        try:
            response = client.call(METHOD, payload)
            rows = response.get('exchanges', [])
            if (not isinstance(rows, list) or len(rows) > 256 or
                    any(not isinstance(x, dict) or not isinstance(x.get('exchange'), str) for x in rows)):
                raise DataError('Invalid exchange identifiers response')
            codes = [x['exchange'] for x in rows]  # Preserve ordering and duplicates.
            matched = sum(code == instrument.exchange for code in codes)
            details.update(http_status=200, category='codes_observed', returned_count=len(codes),
                returned_exchanges=[local_code(code, token) for code in codes], exact_matches=matched)
            passed = matched == 1
        except APIError as error:
            details.update(_safe_error(error))
            passed = False
        except (DataError, AttributeError, TypeError):
            details.update(category='invalid_calendar_response')
            passed = False
        print(json.dumps(details), flush=True)
        outcomes.append(passed)
    # This inspects identifiers only, not days or execution permission.
    return int(not all(outcomes))


def main():
    os.environ.pop('SSLKEYLOGFILE', None)
    try:
        require_console()  # Before token input, client construction or any API call.
        print('LOCAL CONSOLE ONLY. Do not redirect or record a terminal transcript. No PAPER execution.', flush=True)
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
        return run(client, datetime.now(UTC), token=token)
    except APIError as error:
        details = _safe_error(error)
        details.update(stage='instrument', method=error.method if error.method in ALLOWED else 'unknown')
        print(json.dumps(details))
    except (KeyboardInterrupt, EOFError):
        print('category=cancelled')
    except Exception:
        # Never print exception text or tracebacks, including on malformed responses.
        print('category=console_required_or_diagnostic_failure')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
