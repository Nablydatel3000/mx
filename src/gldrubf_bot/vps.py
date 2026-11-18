"""Linux VPS entry points; read-only API and local PAPER, no deployment/LIVE code."""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
from datetime import datetime

from .api import APIError
from .autonomous import history_age
from .cli import parser as bot_parser, run_paper
from .engine import Costs
from .models import DataError, UTC
from .operations import Observer, credential, monitor, deliver, enqueue


def load_config(path):
    data=json.loads(Path(path).read_text())
    required={'costs','max_complete_age_hours','alert_channel','history_retention_days'}
    optional={'poll_seconds','max_quote_age','history_days','min_free_mb','heartbeat_max_age','unhealthy_grace','backup_keep'}
    if not isinstance(data,dict) or not required<=set(data) or set(data)-required-optional:
        raise DataError('Invalid/missing VPS configuration fields')
    history_age([],datetime.now(UTC),data['max_complete_age_hours'])
    values=dict(poll_seconds=60,max_quote_age=120,history_days=730,min_free_mb=512,
                heartbeat_max_age=600,unhealthy_grace=300,backup_keep=7)
    values.update(data)
    for name in ('poll_seconds','max_quote_age','history_days','min_free_mb','heartbeat_max_age','unhealthy_grace','backup_keep','history_retention_days'):
        if type(values[name]) is not int or values[name]<1:
            raise DataError('Invalid numeric VPS policy')
    if values['poll_seconds']<60 or values['alert_channel'] not in ('telegram','webhook'):
        raise DataError('Explicit notification channel / polling policy required')
    costs=values['costs']
    base={'commission_rub','slippage_ticks','initial_cash_rub'}
    if not isinstance(costs,dict) or not base<=set(costs) or (set(costs)-base) not in ({'confirmation_cost_rub'},{'funding_csv'}):
        raise DataError('Explicit unchanged economic scenario required')
    Costs(1,1,costs['commission_rub'],costs['slippage_ticks'],costs['initial_cash_rub'],costs.get('confirmation_cost_rub'))
    if 'funding_csv' in costs and (not Path(costs['funding_csv']).is_absolute() or not Path(costs['funding_csv']).is_file()):
        raise DataError('Configured financing CSV absent/not absolute')
    return values


def arguments(config,state,output):
    costs=config['costs']
    args=['paper','--db',str(Path(state)/'paper.sqlite3'),'--output',str(output),
          '--commission-rub',str(costs['commission_rub']),'--slippage-ticks',str(costs['slippage_ticks']),
          '--initial-cash-rub',str(costs['initial_cash_rub']),
          '--max-complete-age-hours',str(config['max_complete_age_hours']),
          '--poll-seconds',str(config['poll_seconds']),'--max-quote-age',str(config['max_quote_age']),
          '--history-days',str(config['history_days'])]
    if 'funding_csv' in costs:args+=['--funding-csv',costs['funding_csv']]
    else:args+=['--confirmation-cost-rub',str(costs['confirmation_cost_rub'])]
    return args


def run(config,state,output):
    observer=Observer(state,output,retention_days=config['history_retention_days'],min_free_mb=config['min_free_mb'])
    def stop(*args):raise KeyboardInterrupt()
    previous=signal.signal(signal.SIGTERM,stop)
    previous_token=os.environ.get('T_INVEST_TOKEN')
    try:
        observer(dict(status='STARTING'))
        db=Path(state)/'paper.sqlite3'
        if (Path(state)/'restore_review_required.json').exists():
            raise DataError('Restored ledger requires reconciliation before execution')
        if (Path(state)/'ledger_initialized.json').exists() and (not db.is_file() or db.stat().st_size==0):
            raise DataError('Previously initialized database missing; restore explicitly')
        token=credential('api-token')
        if not re.fullmatch(r'[A-Za-z0-9._~+/-]{1,8192}=*',token):
            raise DataError('Invalid API credential format')
        os.environ['T_INVEST_TOKEN']=token
        run_paper(bot_parser().parse_args(arguments(config,state,output)),observer=observer)
    except KeyboardInterrupt:
        observer(dict(status='STOPPED'))
        return 0
    except (APIError,DataError,sqlite3.DatabaseError):
        observer(dict(status='STOP'))
        return 78  # systemd must not restart corrupt data/config as a "repair".
    except Exception:
        observer(dict(status='STOP'))
        return 1
    finally:
        signal.signal(signal.SIGTERM,previous)
        if previous_token is None:os.environ.pop('T_INVEST_TOKEN',None)
        else:os.environ['T_INVEST_TOKEN']=previous_token
    return 0


def main(argv=None):
    parser=argparse.ArgumentParser(description='GLDRUBF Linux PAPER operations. No LIVE/orders.')
    parser.add_argument('command',choices=['run','check','backup','restore','validate-config','configure'])
    parser.add_argument('--config',type=Path,default=Path('/etc/gldrubf/paper.json'))
    parser.add_argument('--state',type=Path,default=Path('/var/lib/gldrubf'))
    parser.add_argument('--output',type=Path,default=Path('/var/log/gldrubf'))
    parser.add_argument('--backup',type=Path)
    parser.add_argument('--authoritative-ledger',action='store_true',
        help='Operator confirms source is the complete latest ledger after clean stop/reconciliation; NOT an unreconciled old backup')
    args=parser.parse_args(argv)
    os.environ.pop('SSLKEYLOGFILE',None)
    try:
        if args.command=='configure':
            from .vps_setup import configure
            configure(args.config.parent)
            return 0
        config=load_config(args.config)
        if args.command=='validate-config':
            print('Configuration valid; no API requests made.');return 0
        if args.command=='run':return run(config,args.state,args.output)
        if args.command=='check':
            result=monitor(args.state,datetime.now(UTC),max_age=config['heartbeat_max_age'],grace=config['unhealthy_grace'])
            count=deliver(args.state,config['alert_channel'])
            pending=len(list((args.state/'outbox').glob('*.json')))
            print(json.dumps(dict(**result,notifications_delivered=count,notifications_pending=pending)))
            return 0 if result['healthy'] and pending==0 else 1
        from .maintenance import backup,restore
        db=args.state/'paper.sqlite3'
        if args.command=='backup':
            path=backup(db,args.state,config,keep=config['backup_keep'])
            enqueue(args.state/'outbox','backup:'+path.name,dict(event='BACKUP_VERIFIED',time=datetime.now(UTC).isoformat()))
            print('Verified local SQLite backup: '+path.name);return 0
        if not args.backup:raise DataError('Explicit backup file required for restore')
        marker=args.state/'restore_review_required.json'
        result=restore(args.backup,db,config,review_marker=marker)
        if args.authoritative_ledger:marker.unlink()
        print(json.dumps(dict(restored=True,paused_for_review=marker.exists(),**result)));return 0
    except (KeyboardInterrupt,EOFError):
        print('Operation cancelled; no broker orders.');return 1
    except Exception:
        if args.command=='backup':
            try:enqueue(args.state/'outbox','backup_failed:'+datetime.now(UTC).date().isoformat(),
                        dict(event='BACKUP_FAILED',time=datetime.now(UTC).isoformat()))
            except Exception:pass
        # No traceback, URL, environment or credential reflection into journald.
        print('STOP: VPS data/configuration/storage/notification failure. No LIVE.');return 78


if __name__=='__main__':raise SystemExit(main())
