"""Non-root entrypoint for systemd-delivered LIVE credentials; trading core unchanged.

Maintenance commands never pass --allow-live. run needs that explicit option
AND all existing LIVE permission/STOP/halt checks. No resume/arm/cancel support.
"""
import argparse
from contextlib import contextmanager,redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from . import live
from .diagnose_live_sync import diagnose
from .models import DataError


class CredentialFailure(Exception):
    def __init__(self,category):self.category=category


def service_identity():
    import pwd
    try:uid=pwd.getpwnam('gldrubf-live').pw_uid
    except KeyError:raise CredentialFailure('service_user_missing') from None
    if uid==0 or os.geteuid()!=uid:raise CredentialFailure('requires_nonroot_service_user')


@contextmanager
def delivered_credential():
    directory=os.environ.get('CREDENTIALS_DIRECTORY')
    if not directory:raise CredentialFailure('credential_delivery_missing')
    folder=Path(directory)
    if folder.is_symlink() or not folder.is_dir():raise CredentialFailure('credential_provider_insecure')
    info=folder.stat()
    if info.st_uid not in (0,os.geteuid()) or info.st_mode&0o027:
        raise CredentialFailure('credential_provider_insecure')
    path=folder/'trading-token';temporary=None
    try:
        fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno());mode=stat.S_IMODE(info.st_mode)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid not in (0,os.geteuid()) or mode not in (0o400,0o440,0o600):
                raise CredentialFailure('credential_provider_insecure')
            raw=stream.read(8193)
        try:value=raw.decode('ascii').strip()
        except UnicodeError:raise CredentialFailure('credential_invalid') from None
        if len(raw)>8192 or not re.fullmatch(r'[A-Za-z0-9._~+/-]+=*',value):raise CredentialFailure('credential_invalid')
        if mode&0o077:
            # systemd may grant named-user access by ACL, reflected as 0440.
            # Legacy secret() rejects that mask. Use only a private /run file,
            # not a persistent second credential or a weakened legacy policy.
            runtime=os.environ.get('RUNTIME_DIRECTORY')
            if not runtime or ':' in runtime:raise CredentialFailure('private_runtime_missing')
            parent=Path(runtime);info=parent.stat()
            if parent.is_symlink() or not parent.is_dir() or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o700:
                raise CredentialFailure('private_runtime_insecure')
            fd,name=tempfile.mkstemp(prefix='credential-',dir=parent);temporary=Path(name)
            with os.fdopen(fd,'w') as stream:stream.write(value+'\n')
            path=temporary
        live.secret(path)  # Existing 0f52907 validation, unchanged.
        yield path
    except PermissionError:raise CredentialFailure('credential_access_denied') from None
    except FileNotFoundError:raise CredentialFailure('credential_file_missing') from None
    except DataError:raise CredentialFailure('credential_invalid_or_insecure') from None
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['credential-check','diagnose','sync','preflight','run'])
    parser.add_argument('--config',type=Path,default=Path('/etc/gldrubf-live/config.json'))
    parser.add_argument('--state',type=Path,default=Path('/var/lib/gldrubf-live'))
    parser.add_argument('--allow-live',action='store_true')
    args=parser.parse_args(argv)
    try:
        service_identity()
        if args.allow_live!=(args.command=='run'):raise CredentialFailure('explicit_run_permission_required_or_misplaced')
        with delivered_credential() as credential:
            if args.command=='credential-check':
                print(json.dumps(dict(category='credential_access_ok',live_enabled=False)));return 0
            if args.command=='diagnose':
                result=diagnose(args.config,args.state,credential)
                print(json.dumps(result,ensure_ascii=False,indent=2))
                return 0 if result['result']=='read_path_passed' else 2
            command=[args.command,'--config',str(args.config),'--state',str(args.state),'--credential-file',str(credential)]
            if args.command=='run':
                # Same explicit flag as the original unit; never issue permission
                # or clear STOP/halt here. Not available in manage.sh.
                return live.main(command+['--allow-live'])
            with redirect_stdout(io.StringIO()) as output:code=live.main(command)
            if code:
                live.secret(credential)  # Detect a credential-access race safely.
                print(json.dumps(dict(category='live_management_failed',command=args.command,halt_not_cleared=True)))
                return code
            result=json.loads(output.getvalue())
            safe=dict(category='live_management_completed',command=args.command,live_enabled=False)
            if args.command=='sync':
                safe.update(halt_present=bool(result.get('halt')),ledger_position=result.get('ledger_position') if type(result.get('ledger_position')) is int and abs(result['ledger_position'])<=2 else None,
                    local_orders_count=len(result['orders']) if isinstance(result.get('orders'),list) else None)
            print(json.dumps(safe));return 0
    except CredentialFailure as exc:
        print(json.dumps(dict(stage='credential_access',category=exc.category,live_enabled=False)));return 78
    except PermissionError:
        print(json.dumps(dict(stage='credential_access',category='credential_access_denied',live_enabled=False)));return 78
    except (Exception,KeyboardInterrupt):
        print(json.dumps(dict(category='maintenance_local_error',live_enabled=False)));return 78


if __name__=='__main__':raise SystemExit(main())
