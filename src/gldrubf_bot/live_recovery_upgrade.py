"""Explicit OFFLINE fingerprint transition; no broker/credential or automatic resume.

Only the known 0f52907/11de6c6/7210db7 core is accepted. Schema/strategy/store
semantics are unchanged. Never restore an old SQLite over a current ledger.
"""
import argparse
from contextlib import ExitStack,closing
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from . import live
from .history import encoded
from .live_credential_entrypoint import service_identity
from .live_store import Store,TERMINAL
from .locking import runner_lock
from .models import DataError,UTC,iso
from .operations import atomic_json

ACK='OFFLINE_LIVE_READ_RECOVERY_UPGRADE'
LEGACY_SOFTWARE={'live.py': 'ea5524e1b6b367a30f960f787dd5ad52e6ef58b03c95a3ddb2075fad0202c475', 'live_api.py': 'dde6116ff327d72b6f4ff09c179bd23fc80d32b829fa56cb3fd6cf4e176edf3e', 'live_store.py': '1429cf31f775ba36c7e026fbcad63218c9316e271204b9fffbea24b9b78bbad8', 'live_market.py': 'a82e3f29e8050a2db663caac534c50a36247e4c2779699ea7acbc981d612143e', 'live_errors.py': '459e34519030438b8b39c6016d4f98df516e2c4520463aaa0df36caba98fb7dd', 'strategy.py': '87b9ed31e8d7ecb69aa79e4255fb75962d928e63f3513f630c01477696d26313', 'api.py': '22edc672ffdf8ef542d68fca304aa94d5ccdf5f5b3fd879d43b2a420783e27ad', 'autonomous.py': 'e242ddd5ebdb201bd314441a4d7b5e0c385df2584070fc482d25d3416ce9dacd', 'locking.py': 'eded44812b11cba29f6bb70901c97d540d802cf246c04249ed2e927e3b7932a9', 'history.py': '3b4599fdf6fa7d1d960fc43d85244b9a3546b1b8fe01719a8e6aca0631e7e2f3'}


def legacy_fingerprint(config):
    return hashlib.sha256(encoded(dict(config=config,software=LEGACY_SOFTWARE))).hexdigest()


def sync_directory(path):
    if os.name=='posix':
        fd=os.open(path,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
        try:os.fsync(fd)
        finally:os.close(fd)


def execution_digest(db):
    data={table:[dict(r) for r in db.execute('SELECT * FROM '+table+' ORDER BY rowid')]
          for table in ('live_orders','live_trades')}
    return hashlib.sha256(encoded(data)).hexdigest()


def read_db(path):
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row
    return db


def upgrade(config_path,state,*,apply=False,acknowledge=None,rollback_backup=None):
    service_identity()
    state=Path(state).resolve();paper=Path('/var/lib/gldrubf').resolve()
    if state==paper or paper in state.parents or state in paper.parents:raise DataError('PAPER forbidden')
    if not state.is_dir() or (os.name=='posix' and state.stat().st_mode&0o077):raise DataError('Private existing state required')
    path=state/'live.sqlite3'
    if path.is_symlink() or not path.is_file() or not path.stat().st_size:raise DataError('Existing physical database required')
    if any(Path(str(path)+suffix).exists() for suffix in ('-wal','-journal')):raise DataError('Offline sidecars require review')
    config=live.load_config(config_path);old=legacy_fingerprint(config);new=live.fingerprint(config)
    for name,digest in LEGACY_SOFTWARE.items():
        if name not in ('live.py','live_api.py') and hashlib.sha256((Path(live.__file__).parent/name).read_bytes()).hexdigest()!=digest:
            raise DataError('Unchanged core differs from reviewed legacy bundle')
    source,target=(new,old) if rollback_backup is not None else (old,new)
    if source==target:raise DataError('Software transition required')
    if not live.ACCOUNT_LOCK_ROOT.is_dir():raise DataError('Existing account lock directory required')
    key=hashlib.sha256(config['account_id'].encode()).hexdigest()+'.lock'
    store=Store.__new__(Store);store.path=path
    with ExitStack() as stack:
        stack.enter_context(runner_lock(live.ACCOUNT_LOCK_ROOT/key))
        check=stack.enter_context(runner_lock(path))
        with closing(read_db(path)) as db:
            meta=dict(db.execute('SELECT * FROM live_meta WHERE id=1').fetchone())
            if meta['config_hash']!=source:raise DataError('Unknown source fingerprint; no adoption')
            required={'live_meta','live_bars','live_signals','live_orders','live_trades','live_events','live_snapshots'}
            if {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}!=required:raise DataError('Unknown schema')
            store.validate(db)
            if any(r['status'] not in TERMINAL for r in db.execute('SELECT status FROM live_orders')):raise DataError('Unresolved order: audited recovery required')
            marker=json.loads((state/'LEDGER_INITIALIZED.json').read_text())
            highwater=json.loads(path.with_suffix('.highwater.json').read_text())
            ids={r[0] for r in db.execute('SELECT request_id FROM live_orders')}
            if marker.get('ledger_id')!=meta['ledger_id'] or highwater.get('ledger_id')!=meta['ledger_id'] or set(highwater['request_ids'])!=ids:raise DataError('Ledger/highwater mismatch')
            digest=execution_digest(db)
            if rollback_backup is not None:
                backup=Path(rollback_backup).resolve()
                if backup.parent!=state/'recovery-upgrades':raise DataError('Local upgrade backup required')
                manifest=json.loads((backup/'manifest.json').read_text())
                if manifest['source']!=old or manifest['target']!=new or manifest['ledger_id']!=meta['ledger_id'] or manifest['executions_sha256']!=digest:raise DataError('Rollback after changed executions forbidden')
                if hashlib.sha256((backup/'live.sqlite3').read_bytes()).hexdigest()!=manifest['sqlite_sha256']:raise DataError('Backup integrity failed')
                with closing(read_db(backup/'live.sqlite3')) as saved:
                    store.validate(saved)
                    saved_meta=saved.execute('SELECT config_hash,ledger_id FROM live_meta WHERE id=1').fetchone()
                    if tuple(saved_meta)!=(old,meta['ledger_id']) or saved.execute('PRAGMA quick_check').fetchone()[0]!='ok' or execution_digest(saved)!=digest:raise DataError('Backup validation failed')
            if not apply:return dict(category='offline_upgrade_compatible',rollback=rollback_backup is not None,halt_preserved=True)
            if acknowledge!=ACK:raise DataError('Explicit offline upgrade acknowledgement required')
            now=datetime.now(UTC);directory=state/'recovery-upgrades'/now.strftime('%Y%m%dT%H%M%S_%fZ')
            directory.parent.mkdir(mode=0o700,exist_ok=True);directory.mkdir(mode=0o700)
            dest=directory/'live.sqlite3';fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
            with closing(sqlite3.connect(dest)) as copy:
                db.backup(copy)
                if copy.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise DataError('Backup integrity failed')
            for name in ('LEDGER_INITIALIZED.json','live.highwater.json','STOP','LIVE_PERMISSION.json'):
                original=state/name
                if original.exists():
                    fd=os.open(directory/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                    with os.fdopen(fd,'wb') as output:output.write(original.read_bytes());output.flush();os.fsync(output.fileno())
            atomic_json(directory/'manifest.json',dict(source=source,target=target,ledger_id=meta['ledger_id'],
                sqlite_sha256=hashlib.sha256(dest.read_bytes()).hexdigest(),executions_sha256=digest))
            with dest.open('rb') as saved:os.fsync(saved.fileno())
            sync_directory(directory);sync_directory(directory.parent);sync_directory(state)
        # Fail closed even after a crash between STOP and the SQL transaction.
        if not (state/'STOP').exists():atomic_json(state/'STOP',dict(reason='OFFLINE_RECOVERY_UPGRADE_REVIEW',at=iso(now)))
        sync_directory(state)
        check()
        with store.db() as db:
            if execution_digest(db)!=digest:raise DataError('Executions changed while offline')
            changed=db.execute('UPDATE live_meta SET config_hash=? WHERE id=1 AND config_hash=?',(target,source)).rowcount
            if changed!=1:raise DataError('Concurrent version change')
            db.execute('INSERT INTO live_events(time,category,details) VALUES(?,?,?)',
                (iso(now),'OFFLINE_RECOVERY_ROLLBACK' if rollback_backup is not None else 'OFFLINE_RECOVERY_UPGRADE',
                 encoded(dict(source=source,target=target,backup=directory.name)).decode()))
        return dict(category='offline_upgrade_completed',backup='recovery-upgrades/'+directory.name,rollback=rollback_backup is not None,
                    halt_preserved=True,stop_present=True,permission_not_updated=True)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,default=Path('/etc/gldrubf-live/config.json'))
    p.add_argument('--state',type=Path,default=Path('/var/lib/gldrubf-live'))
    p.add_argument('--apply',action='store_true');p.add_argument('--acknowledge')
    p.add_argument('--rollback-backup',type=Path)
    a=p.parse_args(argv)
    try:
        result=upgrade(a.config,a.state,apply=a.apply,acknowledge=a.acknowledge,rollback_backup=a.rollback_backup)
        print(json.dumps(result));return 0
    except Exception as exc:
        reasons={'Unknown source fingerprint; no adoption':'source_fingerprint_not_supported',
            'Unchanged core differs from reviewed legacy bundle':'reviewed_core_mismatch',
            'Unresolved order: audited recovery required':'unresolved_execution',
            'Rollback after changed executions forbidden':'execution_history_changed',
            'Explicit offline upgrade acknowledgement required':'explicit_ack_required',
            'LIVE ledger rollback/highwater mismatch':'highwater_mismatch',
            'Ledger/highwater mismatch':'ledger_or_highwater_mismatch',
            'Backup integrity failed':'backup_integrity_failed',
            'Another runner holds this physical database':'runner_active',
            'Offline sidecars require review':'sqlite_sidecars_require_review'}
        reason=reasons.get(str(exc),'state_or_local_validation_failed') if isinstance(exc,DataError) else ('required_file_missing' if isinstance(exc,FileNotFoundError) else 'local_validation_failed')
        print(json.dumps(dict(category='offline_upgrade_refused',reason=reason,halt_not_cleared=True)));return 78


if __name__=='__main__':raise SystemExit(main())
