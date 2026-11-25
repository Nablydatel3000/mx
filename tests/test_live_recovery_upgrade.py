"""Offline upgrades of private synthetic SQLite, never a VPS database."""
from contextlib import closing
import contextlib
from datetime import timedelta
import hashlib
import json
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.gldrubf_bot import live,live_recovery_upgrade as upgrade
from src.gldrubf_bot.locking import runner_lock
from src.gldrubf_bot.models import DataError,iso
from src.gldrubf_bot.operations import atomic_json
from tests.helpers import SQLiteResourceTestCase
from tests import test_live as fixtures


@unittest.skipUnless(os.name=='posix','Linux offline deployment utility')
class UpgradeTests(SQLiteResourceTestCase):
    def setUp(self):
        super().setUp();self.tmp=TemporaryDirectory(prefix='offline recovery upgrade ');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.state=self.root/'state';self.state.mkdir(mode=0o700)
        self.config=fixtures.config();self.cfg=self.root/'config.json';self.cfg.write_text(json.dumps(self.config))
        self.locks=self.root/'locks';self.locks.mkdir()
        self.enterContext(patch.object(upgrade,'service_identity'))
        self.enterContext(patch.object(live,'ACCOUNT_LOCK_ROOT',self.locks))
        self.old=upgrade.legacy_fingerprint(self.config);self.new=live.fingerprint(self.config)
        self.store=live.Store(self.state/'live.sqlite3',self.old,fixtures.START,fixtures.NOW)
        atomic_json(self.state/'LEDGER_INITIALIZED.json',dict(ledger_id=self.store.meta()['ledger_id']))
        self.store.halt('LIVE_API_DATA_OR_STATE_ERROR',fixtures.NOW)
        atomic_json(self.state/'STOP',dict(reason='manual'))
        atomic_json(self.state/'LIVE_PERMISSION.json',dict(config_hash=self.old))

    def run_upgrade(self,**kwargs):return upgrade.upgrade(self.cfg,self.state,**kwargs)
    def apply(self,**kwargs):return self.run_upgrade(apply=True,acknowledge=upgrade.ACK,**kwargs)

    def test_dry_run_is_byte_identical_and_needs_no_token(self):
        before={p.name:p.read_bytes() for p in self.state.iterdir() if p.is_file()}
        self.assertEqual(self.run_upgrade()['category'],'offline_upgrade_compatible')
        self.assertEqual({p.name:p.read_bytes() for p in self.state.iterdir() if p.is_file()},before)

    def test_apply_preserves_ledger_halt_stop_highwater_permission_and_verified_backup(self):
        before_meta=self.store.meta()
        before={name:(self.state/name).read_bytes() for name in ('STOP','LIVE_PERMISSION.json','live.highwater.json','LEDGER_INITIALIZED.json')}
        result=self.apply();self.assertEqual(result['category'],'offline_upgrade_completed')
        after=self.store.meta();self.assertEqual(after.pop('config_hash'),self.new)
        before_meta.pop('config_hash');self.assertEqual(after,before_meta)
        for name,raw in before.items():self.assertEqual((self.state/name).read_bytes(),raw)
        saved=self.state/result['backup']
        with closing(upgrade.read_db(saved/'live.sqlite3')) as db:
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('SELECT config_hash FROM live_meta').fetchone()[0],self.old)
        live.Store(self.store.path,self.new,fixtures.START,fixtures.NOW)
        with self.assertRaises(DataError):live.Store(self.store.path,self.old,fixtures.START,fixtures.NOW)

    def test_explicit_ack_required_and_unknown_versions_never_adopted(self):
        before=self.store.path.read_bytes()
        with self.assertRaises(DataError):self.run_upgrade(apply=True)
        self.assertEqual(self.store.path.read_bytes(),before)
        with self.store.db() as db:db.execute("UPDATE live_meta SET config_hash='unknown'")
        before=self.store.path.read_bytes()
        with self.assertRaises(DataError):self.apply()
        self.assertEqual(self.store.path.read_bytes(),before)

    def test_missing_or_tampered_highwater_and_marker_refused(self):
        marker=self.state/'live.highwater.json';saved=marker.read_bytes();marker.unlink()
        with self.assertRaises(FileNotFoundError):self.apply()
        marker.write_bytes(saved)
        atomic_json(self.state/'LEDGER_INITIALIZED.json',dict(ledger_id='wrong'))
        with self.assertRaises(DataError):self.apply()
        self.assertEqual(self.store.meta()['config_hash'],self.old)

    def test_concurrent_runner_prevents_upgrade(self):
        with runner_lock(self.store.path):
            with self.assertRaises(DataError):self.apply()
        self.assertEqual(self.store.meta()['config_hash'],self.old)

    def test_corrupt_database_is_not_recreated(self):
        self.store.path.write_bytes(b'not sqlite')
        with self.assertRaises(Exception):self.apply()
        self.assertEqual(self.store.path.read_bytes(),b'not sqlite')

    def test_backup_durability_failure_leaves_old_database_untouched(self):
        before=self.store.path.read_bytes();stop=(self.state/'STOP').read_bytes()
        with patch.object(upgrade,'sync_directory',side_effect=OSError('private fsync error')):
            with self.assertRaises(OSError):self.apply()
        self.assertEqual(self.store.path.read_bytes(),before)
        self.assertEqual((self.state/'STOP').read_bytes(),stop)
        self.assertEqual(self.store.meta()['config_hash'],self.old)

    def test_pending_legacy_order_cannot_be_migrated(self):
        # Build the old version's authorized technical intent, never a real order.
        with self.store.db() as db:db.execute('UPDATE live_meta SET halt=NULL')
        (self.state/'STOP').unlink();now=fixtures.NOW
        atomic_json(self.state/'LIVE_PERMISSION.json',dict(ack=live.ACK,config_hash=self.old,ledger_id=self.store.meta()['ledger_id'],
            issued_at=iso(now-timedelta(seconds=1)),expires_at=iso(now+timedelta(hours=1))))
        warm=fixtures.UP[:50]+[fixtures.UP[49]];self.store.ingest(fixtures.rows(warm,now),now)
        now+=timedelta(seconds=1);self.store.ingest(fixtures.rows(warm+[fixtures.UP[-1]],now),now);now+=timedelta(seconds=1)
        controls=live.Controls(self.state,self.store,self.config,True,lambda:now)
        broker=fixtures.FakeBroker(lambda:now);broker.guard=controls.guard
        engine=live.Engine(self.store,broker,controls,lambda:now)
        with patch.object(live,'fingerprint',return_value=self.old):self.assertEqual(engine.step(),'ORDER_SENT_OPEN')
        before=self.store.path.read_bytes()
        with self.assertRaisesRegex(DataError,'Unresolved order'):self.apply()
        self.assertEqual(self.store.path.read_bytes(),before);self.assertEqual(self.store.orders()[0]['status'],'NEW')

    def test_rollback_changes_version_without_restoring_or_deleting_database(self):
        result=self.apply();backup=self.state/result['backup']
        self.store.snapshot('ACCOUNT',dict(position=0),fixtures.NOW)
        with self.store.db() as db:count=db.execute('SELECT COUNT(*) FROM live_snapshots').fetchone()[0]
        self.apply(rollback_backup=backup)
        self.assertEqual(self.store.meta()['config_hash'],self.old)
        self.assertEqual(self.store.meta()['halt'],'LIVE_API_DATA_OR_STATE_ERROR')
        with self.store.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM live_snapshots').fetchone()[0],count)
        self.assertTrue((self.state/'STOP').exists())

    def test_corrupt_backup_prevents_rollback(self):
        result=self.apply();backup=self.state/result['backup'];(backup/'live.sqlite3').write_bytes(b'broken')
        with self.assertRaises(DataError):self.apply(rollback_backup=backup)
        self.assertEqual(self.store.meta()['config_hash'],self.new)

    def test_filled_order_after_upgrade_prevents_blind_rollback(self):
        result=self.apply();backup=self.state/result['backup']
        # Artificial operator setup for the test ONLY; production never clears STOP here.
        with self.store.db() as db:db.execute('UPDATE live_meta SET halt=NULL')
        (self.state/'STOP').unlink()
        now=fixtures.NOW
        atomic_json(self.state/'LIVE_PERMISSION.json',dict(ack=live.ACK,config_hash=self.new,ledger_id=self.store.meta()['ledger_id'],
            issued_at=iso(now-timedelta(seconds=1)),expires_at=iso(now+timedelta(hours=1))))
        warm=fixtures.UP[:50]+[fixtures.UP[49]];self.store.ingest(fixtures.rows(warm,now),now)
        now+=timedelta(seconds=1);self.store.ingest(fixtures.rows(warm+[fixtures.UP[-1]],now),now);now+=timedelta(seconds=1)
        controls=live.Controls(self.state,self.store,self.config,True,lambda:now)
        broker=fixtures.FakeBroker(lambda:now);broker.guard=controls.guard
        engine=live.Engine(self.store,broker,controls,lambda:now)
        self.assertEqual(engine.step(),'ORDER_SENT_OPEN');now+=timedelta(seconds=1);broker.fill(2)
        self.assertEqual(engine.step(),'TARGET_CONFIRMED')
        with self.assertRaises(DataError):self.apply(rollback_backup=backup)
        self.assertEqual(self.store.position(),2);self.assertEqual(self.store.meta()['config_hash'],self.new)

    def test_new_empty_database_never_initialized_by_upgrade(self):
        self.store.path.unlink()
        with self.assertRaises(DataError):self.apply()
        self.assertFalse(self.store.path.exists())

    def test_cli_error_reason_never_echoes_arbitrary_private_details(self):
        with patch.object(upgrade,'upgrade',side_effect=DataError('private token/account')),contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(upgrade.main([]),78)
        result=json.loads(output.getvalue());self.assertEqual(result['reason'],'state_or_local_validation_failed')
        self.assertNotIn('private',output.getvalue());self.assertTrue(result['halt_not_cleared'])

    def test_pinned_legacy_hash_matches_reference_core_bundle(self):
        pinned=json.loads(Path('docs/live_credential_recovery/CORE_SHA256.json').read_text())['sha256']
        self.assertEqual(upgrade.LEGACY_SOFTWARE,pinned)
        for name,value in pinned.items():
            if name not in ('live.py','live_api.py'):
                self.assertEqual(hashlib.sha256((Path('src/gldrubf_bot')/name).read_bytes()).hexdigest(),value)
