"""Interactive Windows helper: enter data/settings, never write Python code."""
import getpass
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

from .cli import main
from .models import UTC


def ask(message, default=None):
    value = input(message + (f' [{default}]' if default is not None else '') + ': ').strip()
    return value or default or ''


def token():
    if not os.environ.get('T_INVEST_TOKEN'):
        if not sys.stdin.isatty():
            raise SystemExit('Open this launcher in a console, or set T_INVEST_TOKEN in its environment.')
        os.environ['T_INVEST_TOKEN'] = getpass.getpass('Read-only API token (hidden, current process only): ')


def run():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'paper'
    print('GLDRUBF: READ-ONLY API / VIRTUAL PAPER. No real orders. SMA20/50 is fixed.')
    if mode == 'fetch':
        token()
        start = date.fromisoformat(ask('First date YYYY-MM-DD'))
        end = date.fromisoformat(ask('End date YYYY-MM-DD (exclusive)'))
        interval = ask('Interval day or 1min', 'day')
        folder = Path('data') / datetime.now(UTC).strftime('fetch_%Y%m%dT%H%M%S_%fZ')
        args = ['fetch', '--from', start.isoformat()+'T00:00:00Z', '--to', end.isoformat()+'T00:00:00Z',
                '--interval', interval, '--output', str(folder)]
    else:
        args = [mode]
        if mode == 'paper':
            token()
        elif mode == 'replay':
            folder = Path(ask('Folder containing daily.csv, calendar.json, instrument.json'))
            metadata = json.loads((folder/'instrument.json').read_text(encoding='utf-8-sig'))
            args += ['--bars', str(folder/'daily.csv'), '--calendar', str(folder/'calendar.json'),
                     '--uid', metadata['uid'], '--quotes', ask('Actual quotes CSV path (time,price)'),
                     '--point-value-rub', ask('HISTORICAL RUB per price point per contract'),
                     '--tick-size', ask('HISTORICAL tick size')]
        else:
            raise SystemExit('Unsupported mode; there is no LIVE mode.')
        args += ['--db', ask('NEW PAPER database path (reuse exactly to resume)', 'paper_v1.sqlite3'),
                 '--output', ask('Journal folder', 'logs'),
                 '--commission-rub', ask('Commission RUB per contract SIDE (explicit assumption)'),
                 '--slippage-ticks', ask('Adverse slippage in whole ticks (explicit assumption)'),
                 '--initial-cash-rub', ask('Virtual initial cash RUB', '100000')]
        funding = ask('Funding rates CSV path, or leave blank for FIXED daily scenario')
        if funding:
            args += ['--funding-csv', funding]
        else:
            args += ['--funding-per-contract-day-rub', ask('Daily RUB: positive long pays/short receives; 0 means ignored by assumption')]
    main(args)


if __name__ == '__main__':
    try:
        run()
    except (ValueError, KeyError, OSError):
        raise SystemExit('STOP: invalid input/file. Token is never written to files.') from None
