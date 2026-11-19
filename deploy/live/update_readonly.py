"""Offline, bounded update of the existing acceptance installation. No API call.

Does not read/write config, credentials, reports, SQLite or systemd. Unknown
installed code is refused. Backup contains only the replaced code files.
"""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile

OLD={
 'market_diagnostics.py':'1e54c20d12e7e12d7a27f0d94aeb1f858229f0a5894845ce74ffe0d6a5251c60',
 'account_diagnostics.py':'f10a2089e7408e70c09c42d2a3a33e805d1f8727ec641f5cc566bbeb9e13331d',
 'live_acceptance.py':{'5896fca66bdfbec1cf781c9444e0c69befca5da53f3322b55f3bf39dd8aeeff9',
                       'bead0e1f50fcec201c76600d771c5a285ab3dc0a088b66f1ed8dab0377f2b1c6',
                       'e81394706d1be910f6d72a663af640e5b85084ed12e3327d1a15dd57e6d3a5a3'},
 'acceptance_check.sh':{'9c7800a50668023794c7f1d8c2d1d93fc2fa01072ec91bf7bb78bca7b2d8e858',
                        'ce84f7c031de50579a118dc9337e1d17e48026d0b332fbb88df15fb130aa4840'}}
CORE='3e23641059ff41a7edfb26b81b1738a3807f5737990f50dabed984fa448d5afa'
FROZEN={
 'api.py':'22edc672ffdf8ef542d68fca304aa94d5ccdf5f5b3fd879d43b2a420783e27ad',
 'models.py':'5fd937b351a117c3d45f6d37c08ca0e824e8d612fce4be29cc40935807af9329',
 'history.py':'3b4599fdf6fa7d1d960fc43d85244b9a3546b1b8fe01719a8e6aca0631e7e2f3',
 'live.py':'f409fac7aa85c28acd583e9f388b470973c2baf2bee82c6289a394459a64ccbe',
 'live_store.py':'4111cb05b7b9c86290790079180c94c973fb6bca9dea44b38cbff4c30344b69e'}


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def physical(path):
    if path.resolve()!=path.absolute():raise RuntimeError('Symlink/alternative installation path; review required')
    if path.exists() and (not path.is_file() or path.stat().st_nlink!=1):raise RuntimeError('Expected regular, non-hardlinked file')


def update(source,code,wrapper,state):
    import fcntl  # Linux updater; no service or trading imports.
    source,code,wrapper,state=map(Path,(source,code,wrapper,state))
    core=code/'src/gldrubf_bot/live_api.py';physical(core)
    if digest(core)!=CORE:raise RuntimeError('Unknown base broker adapter; no files replaced')
    for name,expected in FROZEN.items():
        path=code/'src/gldrubf_bot'/name;physical(path)
        if not path.is_file() or digest(path)!=expected:raise RuntimeError('Unknown frozen acceptance dependency; no files replaced')
    files=[(source/'src/gldrubf_bot/account_diagnostics.py',code/'src/gldrubf_bot/account_diagnostics.py',0o644),
           (source/'src/gldrubf_bot/live_market.py',code/'src/gldrubf_bot/live_market.py',0o644),
           (source/'src/gldrubf_bot/market_diagnostics.py',code/'src/gldrubf_bot/market_diagnostics.py',0o644),
           (source/'src/gldrubf_bot/live_acceptance.py',code/'src/gldrubf_bot/live_acceptance.py',0o644),
           (source/'deploy/live/acceptance_check.sh',wrapper,0o755)]
    for src,dst,mode in files:
        physical(src);physical(dst)
        if not src.is_file():raise RuntimeError('Missing update source')
        known=OLD.get(src.name,());known={known} if isinstance(known,str) else set(known)
        if dst.exists() and digest(dst) not in known|{digest(src)}:
            raise RuntimeError('Unknown installed acceptance version; preserve and review')
        if not dst.exists() and src.name not in ('account_diagnostics.py','market_diagnostics.py','live_market.py'):raise RuntimeError('Existing acceptance installation required')
    files=[(src,dst,mode) for src,dst,mode in files if not dst.exists() or digest(src)!=digest(dst)]
    lock=state/'read_only.lock';physical(lock)
    if not lock.exists():raise RuntimeError('Existing acceptance lock required; no runtime files created')
    with lock.open('r+b') as stream:
        fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        # Recheck inputs under the same physical lock used by the diagnostic.
        if os.fstat(stream.fileno()).st_ino!=lock.stat().st_ino:raise RuntimeError('Lock identity changed')
        backup=Path(tempfile.mkdtemp(prefix='acceptance-code-',dir=code.parent))
        try:
            for src,dst,mode in files:
                if dst.exists():shutil.copy2(dst,backup/src.name)
            for src,dst,mode in files:
                fd,temporary=tempfile.mkstemp(prefix='.'+dst.name+'.',dir=dst.parent)
                try:
                    with os.fdopen(fd,'wb') as output:
                        output.write(src.read_bytes());output.flush();os.fsync(output.fileno());os.fchmod(output.fileno(),mode)
                    os.replace(temporary,dst)
                finally:Path(temporary).unlink(missing_ok=True)
        finally:fcntl.flock(stream,fcntl.LOCK_UN)
    return backup


def main():
    if os.name!='posix' or os.geteuid()!=0:
        print('Use sudo python3 deploy/live/update_readonly.py on Linux.');return 1
    try:
        source=Path(__file__).resolve().parents[2]
        backup=update(source,Path('/opt/gldrubf-live-acceptance/mx'),
            Path('/usr/local/bin/gldrubf-live-check'),Path('/var/lib/gldrubf-live-acceptance'))
    except Exception:
        print('STOP: read-only code update refused/failed; inspect installation locally. No credential/config/service changes.');return 1
    print('Read-only code updated. Code backup:',backup)
    print('Trading core/configuration/token/reports/PAPER/systemd unchanged. Run calendar-diagnostics next.');return 0


if __name__=='__main__':raise SystemExit(main())
