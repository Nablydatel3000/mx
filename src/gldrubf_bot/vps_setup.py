"""Local interactive setup, invoked explicitly by the server administrator."""
import getpass
import json
import os
import sys
import warnings
from pathlib import Path
from urllib.parse import urlsplit

from .diagnose_api import read_token
from .models import DataError
from .operations import atomic_json


def configure(folder):
    if os.name!='posix' or os.geteuid()!=0:raise DataError('Run local configuration as root')
    if not sys.stdin.isatty() or not sys.stdout.isatty():raise DataError('Local interactive console required')
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True);os.chmod(folder,0o750)
    print('Local PAPER configuration. Existing DB is never reset. Use identical costs to resume.')
    config=dict(costs=dict(
        commission_rub=input('Commission RUB per contract SIDE (explicit scenario): ').strip(),
        slippage_ticks=int(input('Adverse slippage ticks: ')),
        initial_cash_rub=input('Virtual initial cash RUB (same as existing DB if resuming): ').strip()),
        max_complete_age_hours=int(input('Maximum completed candle UTC age in HOURS (explicit policy): ')),
        history_retention_days=int(input('Keep local raw/check diagnostics for how many days (older files removed, last two valid snapshots kept): ')))
    funding=input('Absolute funding CSV path, or blank for fixed confirmation-cost scenario: ').strip()
    if funding:config['costs']['funding_csv']=funding
    else:config['costs']['confirmation_cost_rub']=input('Signed RUB per NEW confirmed candle; 0 explicitly ignores funding: ').strip()
    channel=input('Notification channel: telegram or webhook: ').strip()
    config['alert_channel']=channel
    # Validate ALL non-secret settings before prompting for or writing secrets.
    from .vps import load_config
    candidate=folder/'paper.candidate.json'
    try:
        atomic_json(candidate,config);load_config(candidate)
    finally:candidate.unlink(missing_ok=True)
    secrets={'api-token':read_token(),'alert-token':'','alert-chat':'','alert-url':''}
    def hidden(prompt):
        with warnings.catch_warnings():
            warnings.simplefilter('error',getpass.GetPassWarning)
            return getpass.getpass(prompt).strip()
    if channel=='telegram':
        secrets['alert-token']=hidden('Telegram bot token (hidden, NOT broker token): ')
        secrets['alert-chat']=hidden('Telegram chat ID (hidden): ')
        import re
        if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+',secrets['alert-token']) or not re.fullmatch(r'-?\d+',secrets['alert-chat']):
            raise DataError('Invalid Telegram settings')
    else:
        value=hidden('HTTPS webhook URL (hidden; do not use broker API URL): ')
        parts=urlsplit(value)
        if parts.scheme!='https' or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise DataError('Invalid HTTPS webhook')
        secrets['alert-url']=value
    for name,value in secrets.items():
        path=folder/name
        if path.is_symlink():raise DataError('Refuse symlink credential file')
        with path.open('w',encoding='utf-8',newline='\n') as stream:
            os.chmod(path,0o600);stream.write(value+'\n')
    atomic_json(folder/'paper.json',config);os.chmod(folder/'paper.json',0o640)
    print('Configuration written locally. No service started; no API requests made.')
