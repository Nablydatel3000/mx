"""Exercise offline acceptance updates only inside temporary directories."""
import importlib.util
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('readonly_update',Path('deploy/live/update_readonly.py'))
updater=importlib.util.module_from_spec(spec);spec.loader.exec_module(updater)


@unittest.skipUnless(os.name=='posix','Linux flock updater')
class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(prefix='acceptance offline update ');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'source';self.code=self.root/'acceptance/mx'
        self.wrapper=self.root/'bin/gldrubf-live-check';self.state=self.root/'state'
        for path in (self.source/'src/gldrubf_bot',self.source/'deploy/live',self.code/'src/gldrubf_bot',self.wrapper.parent,self.state):path.mkdir(parents=True,exist_ok=True)
        self.core=self.code/'src/gldrubf_bot/live_api.py';self.core.write_text('unchanged core')
        self.old=self.code/'src/gldrubf_bot/live_acceptance.py';self.old.write_text('old readonly')
        self.wrapper.write_text('old wrapper');(self.state/'read_only.lock').touch()
        (self.root/'config.json').write_text('existing private config');(self.root/'token').write_text('synthetic private token')
        for path in ('src/gldrubf_bot/account_diagnostics.py','src/gldrubf_bot/market_diagnostics.py','src/gldrubf_bot/live_acceptance.py','deploy/live/acceptance_check.sh'):
            (self.source/path).write_text('new '+Path(path).name)
        self.enterContext(patch.object(updater,'CORE',updater.digest(self.core)))
        self.enterContext(patch.object(updater,'OLD',{'live_acceptance.py':updater.digest(self.old),'acceptance_check.sh':updater.digest(self.wrapper)}))
    def update(self):return updater.update(self.source,self.code,self.wrapper,self.state)
    def test_update_backup_and_repeat_preserve_unrelated_files(self):
        backup=self.update();self.assertEqual((backup/'live_acceptance.py').read_text(),'old readonly')
        self.assertEqual(self.old.read_text(),'new live_acceptance.py');self.update()
        self.assertEqual(self.core.read_text(),'unchanged core')
        self.assertEqual((self.root/'config.json').read_text(),'existing private config')
        self.assertEqual((self.root/'token').read_text(),'synthetic private token')
        self.assertEqual(sorted(p.name for p in self.state.iterdir()),['read_only.lock'])
        self.assertEqual(self.wrapper.stat().st_mode&0o777,0o755)
    def test_unknown_installed_code_refused_before_replacement(self):
        self.old.write_text('unknown local changes')
        with self.assertRaises(RuntimeError):self.update()
        self.assertEqual(self.wrapper.read_text(),'old wrapper')
    def test_known_previous_diagnostic_module_can_be_updated(self):
        helper=self.code/'src/gldrubf_bot/account_diagnostics.py';helper.write_text('old diagnostic')
        self.old.write_bytes((self.source/'src/gldrubf_bot/live_acceptance.py').read_bytes())
        self.wrapper.write_bytes((self.source/'deploy/live/acceptance_check.sh').read_bytes())
        before=(self.old.stat().st_ino,self.wrapper.stat().st_ino)
        with patch.dict(updater.OLD,{'account_diagnostics.py':updater.digest(helper)}):
            backup=self.update()
        self.assertEqual((backup/'account_diagnostics.py').read_text(),'old diagnostic')
        self.assertEqual(helper.read_text(),'new account_diagnostics.py')
        self.assertEqual(sorted(p.name for p in backup.iterdir()),['account_diagnostics.py'])
        self.assertEqual(before,(self.old.stat().st_ino,self.wrapper.stat().st_ino))
    def test_busy_checker_refuses_update(self):
        import fcntl
        with (self.state/'read_only.lock').open('r+b') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(OSError):self.update()
        self.assertEqual(self.old.read_text(),'old readonly')
    def test_symlink_target_refused(self):
        other=self.root/'other';other.write_text('do not change');self.old.unlink();self.old.symlink_to(other)
        with self.assertRaises(RuntimeError):self.update()
        self.assertEqual(other.read_text(),'do not change')
