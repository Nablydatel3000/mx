"""SQLite online backup and explicit offline restore; never copy a live DB blindly."""
from contextlib import closing
from pathlib import Path
import json
import os
import sqlite3
from tempfile import TemporaryDirectory
from datetime import datetime
from uuid import uuid4

from .engine import Costs, PaperEngine
from .io import funding_csv
from .locking import runner_lock
from .models import DataError, UTC
from .operations import atomic_json


def ro(path):
    return sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=15)


def copy_database(source,target):
    target=Path(target)
    with target.open('xb'):pass
    try:
        with closing(ro(source)) as src,closing(sqlite3.connect(target)) as dst:
            src.backup(dst,pages=1000,sleep=.01)
            if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                raise DataError('Backup integrity check failed')
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def verify(path,config):
    # Validate a WORKING COPY only: original backup files stay immutable.
    with closing(ro(path)) as db:
        metadata=json.loads(db.execute('SELECT config FROM metadata WHERE id=1').fetchone()[0])
    actual=metadata['costs'];wanted=config['costs']
    rates=funding_csv(wanted['funding_csv']) if 'funding_csv' in wanted else None
    costs=Costs(actual['point_value_rub'],actual['tick'],wanted['commission_rub'],wanted['slippage_ticks'],
                wanted['initial_cash_rub'],wanted.get('confirmation_cost_rub'))
    engine=PaperEngine(path,metadata['uid'],costs,funding_rates=rates)
    return {k:v for k,v in engine.summary().items() if k in ('position','pending','pending_day','fills','bootstrap_origin','migration_review_required')}


def backup(db,state,config,*,keep=7):
    folder=Path(state)/'backups';folder.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now(UTC).strftime('%Y%m%dT%H%M%S_%fZ')+'_'+uuid4().hex
    temporary=folder/('db_'+stamp+'.tmp');final=temporary.with_suffix('.sqlite3')
    try:
        copy_database(db,temporary);verify(temporary,config)
        os.replace(temporary,final)
    finally:temporary.unlink(missing_ok=True)
    for old in sorted(folder.glob('db_*.sqlite3'))[:-keep]:
        if not old.is_symlink():old.unlink()
    return final


def restore(source,db,config,*,review_marker=None):
    with TemporaryDirectory(prefix='gldrubf restore ') as temporary:
        candidate=Path(temporary)/'verified.sqlite3'
        copy_database(source,candidate);report=verify(candidate,config)
        # Refuse an active runner, including through symlinks/hard links.
        with runner_lock(db) as check:
            check()
            if review_marker is not None:
                atomic_json(review_marker,dict(status='restore_requires_reconciliation',
                    restored_at=datetime.now(UTC).isoformat(),snapshot_state=report))
            # SQLite backup preserves the destination inode and its runner lock.
            with closing(ro(candidate)) as src,closing(sqlite3.connect(Path(db).resolve())) as dst:
                src.backup(dst,pages=1000,sleep=.01)
            check();verify(db,config)
        return report
