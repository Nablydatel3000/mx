"""Credential delivery and halted sync only; never a real trading service."""
import contextlib
from datetime import datetime,timedelta
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock,patch

from src.gldrubf_bot import live,live_credential_entrypoint as entry
from src.gldrubf_bot.live_acceptance import PROPOSED
from src.gldrubf_bot.live_store import Store
from src.gldrubf_bot.models import UTC,iso
from src.gldrubf_bot.operations import atomic_json
from tests.helpers import SQLiteResourceTestCase
from tests.test_live import FakeBroker

TOKEN='synthetic-credential-only'


@unittest.skipUnless(os.name=='posix','Linux systemd credential entrypoint')
class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(prefix='credential delivery ');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.provider=self.root/'provider';self.provider.mkdir(mode=0o700)
        self.runtime=self.root/'runtime';self.runtime.mkdir(mode=0o700)
        self.token=self.provider/'trading-token';self.token.write_text(TOKEN);self.token.chmod(0o400)
        self.enterContext(patch.dict(os.environ,{'CREDENTIALS_DIRECTORY':str(self.provider),'RUNTIME_DIRECTORY':str(self.runtime)}))

    def invoke(self,args):
        with patch.object(entry,'service_identity'),contextlib.redirect_stdout(io.StringIO()) as output:
            code=entry.main(args)
        text=output.getvalue();self.assertNotIn(TOKEN,text)
        return code,json.loads(text)

    def test_private_systemd_copy_works_with_original_secret_without_copy(self):
        with entry.delivered_credential() as path:
            self.assertEqual(path,self.token);self.assertEqual(live.secret(path),TOKEN)
        self.assertEqual(list(self.runtime.iterdir()),[])
        self.assertEqual(self.invoke(['credential-check'])[0],0)

    def test_acl_mask_0440_uses_only_private_runtime_file_and_removes_it(self):
        if os.name!='posix':self.skipTest('POSIX permissions')
        self.token.chmod(0o440)
        with entry.delivered_credential() as path:
            self.assertNotEqual(path,self.token);self.assertEqual(path.parent,self.runtime)
            self.assertEqual(path.stat().st_mode&0o777,0o600);self.assertEqual(live.secret(path),TOKEN)
        self.assertFalse(path.exists());self.assertEqual(self.token.stat().st_mode&0o777,0o440)

    def test_copy_cleanup_on_failure(self):
        self.token.chmod(0o440)
        with self.assertRaises(RuntimeError):
            with entry.delivered_credential() as path:raise RuntimeError('fixture')
        self.assertFalse(path.exists())

    def test_public_writable_or_group_master_modes_are_not_silently_accepted(self):
        if os.name!='posix':self.skipTest('POSIX permissions')
        for mode in (0o644,0o660,0o640,0o666):
            self.token.chmod(mode)
            with self.subTest(mode=mode):
                code,result=self.invoke(['credential-check'])
                self.assertEqual(code,78);self.assertEqual(result['category'],'credential_provider_insecure')

    def test_permission_error_is_fixed_category_before_legacy_main(self):
        with patch.object(entry.os,'open',side_effect=PermissionError(TOKEN)),patch.object(entry.live,'main') as delegate:
            code,result=self.invoke(['sync'])
        delegate.assert_not_called();self.assertEqual(code,78);self.assertEqual(result['category'],'credential_access_denied')

    @unittest.skipUnless(os.name=='posix' and os.geteuid()!=0,'Real denial requires non-root POSIX user')
    def test_actual_filesystem_denial_is_safe_and_changes_no_files(self):
        self.token.chmod(0)
        try:
            with patch.object(entry.live,'main') as delegate:
                code,result=self.invoke(['sync'])
            delegate.assert_not_called();self.assertEqual(code,78)
            self.assertEqual(result['category'],'credential_access_denied')
        finally:self.token.chmod(0o400)
        self.assertEqual(self.token.read_text(),TOKEN)

    def test_identity_rejects_root_before_credential_or_database_access(self):
        from types import SimpleNamespace
        with patch('pwd.getpwnam',return_value=SimpleNamespace(pw_uid=12345)),patch.object(os,'geteuid',return_value=0):
            with self.assertRaises(entry.CredentialFailure) as caught:entry.service_identity()
        self.assertEqual(caught.exception.category,'requires_nonroot_service_user')

    def test_provider_missing_invalid_and_symlink_rejected(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(self.invoke(['credential-check'])[1]['category'],'credential_delivery_missing')
        self.token.chmod(0o600);self.token.write_text('invalid secret with spaces');self.token.chmod(0o400)
        self.assertEqual(self.invoke(['credential-check'])[1]['category'],'credential_invalid')
        other=self.root/'other';other.write_text(TOKEN);self.token.unlink()
        try:self.token.symlink_to(other)
        except OSError:self.skipTest('symlink unavailable')
        self.assertEqual(self.invoke(['credential-check'])[0],78)

    def test_run_requires_explicit_permission_and_maintenance_cannot_pass_it(self):
        with patch.object(entry.live,'main') as delegate:
            self.assertEqual(self.invoke(['run'])[0],78)
            self.assertEqual(self.invoke(['sync','--allow-live'])[0],78)
        delegate.assert_not_called()

    def test_future_service_run_delegates_existing_gates_without_rearming(self):
        with patch.object(entry.live,'main',return_value=78) as delegate:
            with patch.object(entry,'service_identity'):
                self.assertEqual(entry.main(['run','--allow-live']),78)
        args=delegate.call_args.args[0]
        self.assertEqual(args[0],'run');self.assertIn('--allow-live',args)
        self.assertNotIn('resume',args);self.assertNotIn('arm',args)
        self.assertEqual(list(self.runtime.iterdir()),[])

    def test_management_hides_legacy_stdout_and_keeps_allow_flag_absent(self):
        def delegated(args):
            self.assertNotIn('--allow-live',args)
            print(json.dumps(dict(halt='LIVE_API_DATA_OR_STATE_ERROR',ledger_position=0,orders=[],private=TOKEN)))
            return 0
        with patch.object(entry.live,'main',side_effect=delegated):code,result=self.invoke(['sync'])
        self.assertEqual(code,0);self.assertTrue(result['halt_present']);self.assertEqual(result['ledger_position'],0)

    def test_read_retry_logs_do_not_break_management_json(self):
        from src.gldrubf_bot.live_recovery import log_event
        def delegated(args):
            log_event('API_READ_RESPONSE_RESTORED',attempts=2)
            print(json.dumps(dict(halt='LIVE_API_DATA_OR_STATE_ERROR',ledger_position=0,orders=[])))
            return 0
        with patch.object(entry.live,'main',side_effect=delegated),contextlib.redirect_stderr(io.StringIO()) as logs:
            code,result=self.invoke(['sync'])
        self.assertEqual(code,0);self.assertTrue(result['halt_present'])
        self.assertEqual(json.loads(logs.getvalue())['event'],'API_READ_RESPONSE_RESTORED')

    def test_master_install_token_policy_unchanged(self):
        target=self.root/'master'
        args=['install-token','--credential-file',str(target),'--state',str(self.root/'unused')]
        output=io.StringIO()
        with contextlib.redirect_stdout(output),patch('sys.stdin.isatty',return_value=True),patch.object(output,'isatty',return_value=True),patch('getpass.getpass',return_value=TOKEN):
            self.assertEqual(live.main(args),0);before=target.read_bytes()
            self.assertEqual(live.main(args),78)
        self.assertEqual(target.read_bytes(),before)
        if os.name=='posix':self.assertEqual(target.stat().st_mode&0o777,0o600)

    def test_launcher_cannot_start_trading_or_resume(self):
        script=Path('deploy/live/manage.sh')
        if os.name!='posix':self.skipTest('Linux shell')
        for command in ('run','resume','arm','cancel'):
            r=subprocess.run(['bash',str(script),command],capture_output=True,text=True)
            self.assertEqual(r.returncode,78);self.assertIn('nontrading_commands_only',r.stdout)
        text=script.read_text()
        self.assertIn('--property=User=gldrubf-live',text);self.assertIn('--property=LoadCredential=',text)
        self.assertNotIn('systemctl start',text);self.assertNotIn('systemctl enable',text)


@unittest.skipUnless(os.name=='posix','Linux systemd credential entrypoint')
class HaltedSyncTests(SQLiteResourceTestCase):
    def test_existing_halted_database_syncs_without_permission_reset_or_trades(self):
        with TemporaryDirectory(prefix='halted recovery ') as folder:
            root=Path(folder);state=root/'state';state.mkdir(mode=0o700);locks=root/'locks';locks.mkdir()
            provider=root/'provider';provider.mkdir(mode=0o700);token=provider/'trading-token';token.write_text(TOKEN);token.chmod(0o400)
            c=dict(PROPOSED,account_id='12345678');cfg=root/'config.json';cfg.write_text(json.dumps(c))
            now=datetime(2026,11,19,12,tzinfo=UTC)
            store=Store(state/'live.sqlite3',live.fingerprint(c),now-timedelta(days=730),now)
            atomic_json(state/'LEDGER_INITIALIZED.json',dict(ledger_id=store.meta()['ledger_id']))
            store.halt('LIVE_API_DATA_OR_STATE_ERROR',now);(state/'STOP').write_text('preserve this stop')
            marker=store.path.with_suffix('.highwater.json');before_marker=marker.read_bytes();before_meta=store.meta()
            broker=FakeBroker(lambda:now);broker.operations=Mock(return_value=[])
            class Clock(datetime):
                @classmethod
                def now(cls,tz=None):return now
            with patch.object(entry,'service_identity'),patch.dict(os.environ,{'CREDENTIALS_DIRECTORY':str(provider)}), \
                 patch.object(live,'ACCOUNT_LOCK_ROOT',locks),patch.object(live,'datetime',Clock), \
                 patch.object(live,'Broker',return_value=broker),contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(entry.main(['sync','--config',str(cfg),'--state',str(state)]),0)
            result=json.loads(output.getvalue());self.assertTrue(result['halt_present'])
            self.assertEqual(store.meta(),before_meta);self.assertEqual(marker.read_bytes(),before_marker)
            self.assertEqual((state/'STOP').read_text(),'preserve this stop')
            self.assertEqual(store.orders(),[]);self.assertEqual(store.position(),0)
            self.assertFalse((state/'LIVE_PERMISSION.json').exists());self.assertFalse(broker.posts);self.assertFalse(broker.cancels)
            with store.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM live_snapshots').fetchone()[0],2)


@unittest.skipUnless(os.name=='posix' and os.geteuid()==0,'Actual non-root permission probe needs root in isolated test environment')
class ActualFileAccessTests(unittest.TestCase):
    def test_nonroot_cannot_read_root_master_but_can_use_delivered_copy(self):
        import pwd
        try:account=pwd.getpwnam('nobody')
        except KeyError:self.skipTest('nobody unavailable')
        with TemporaryDirectory(prefix='actual credential access ') as folder:
            root=Path(folder);root.chmod(0o711)
            provider=root/'provider';provider.mkdir(mode=0o700);os.chown(provider,account.pw_uid,account.pw_gid)
            runtime=root/'runtime';runtime.mkdir(mode=0o700);os.chown(runtime,account.pw_uid,account.pw_gid)
            token=provider/'trading-token';token.write_text(TOKEN);token.chmod(0o600)
            code="""import os,pwd
from types import SimpleNamespace
from unittest.mock import patch
from src.gldrubf_bot.live_credential_entrypoint import main
os.setgid(int(os.environ['TEST_GID']));os.setuid(int(os.environ['TEST_UID']))
with patch('pwd.getpwnam',return_value=SimpleNamespace(pw_uid=os.geteuid())):
    raise SystemExit(main(['credential-check']))
"""
            env=dict(os.environ,CREDENTIALS_DIRECTORY=str(provider),RUNTIME_DIRECTORY=str(runtime),TEST_UID=str(account.pw_uid),TEST_GID=str(account.pw_gid),PYTHONDONTWRITEBYTECODE='1')
            failed=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,env=env)
            self.assertEqual(failed.returncode,78,failed.stderr);self.assertIn('credential_access_denied',failed.stdout)
            os.chown(token,account.pw_uid,account.pw_gid);token.chmod(0o400)
            passed=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,env=env)
            self.assertEqual(passed.returncode,0,passed.stderr);self.assertNotIn(TOKEN,passed.stdout)
