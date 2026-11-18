"""Offline VPS lifecycle, health, notifications and verified SQLite recovery."""
import contextlib
import copy
import hashlib
import io
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import timedelta
from decimal import Decimal
import unittest
import urllib.error
from unittest.mock import Mock, patch

from src.gldrubf_bot import operations as ops, vps, maintenance
from src.gldrubf_bot.engine import PaperEngine
from src.gldrubf_bot.locking import runner_lock
from src.gldrubf_bot.models import DataError, Quote, iso
from src.gldrubf_bot.history import load_snapshot
from src.gldrubf_bot import autonomous
from tests.helpers import SQLiteResourceTestCase, costs, UP
from tests.history_helpers import INST, NOW, candles, snapshot
from tests.test_api import FakeResponse


SECRET='synthetic-vps-secret'


def config():
    # Test-only policy, not a proposed production fee or candle-age threshold.
    return dict(costs=dict(commission_rub='.5',slippage_ticks=1,initial_cash_rub='1000',confirmation_cost_rub='2'),
                max_complete_age_hours=168,alert_channel='webhook',history_retention_days=7,min_free_mb=1)


@unittest.skipUnless(sys.platform=='linux','Linux VPS runtime uses /proc and systemd; Windows engine tests remain enabled')
class VPSOperationsTests(SQLiteResourceTestCase):
    def setUp(self):
        super().setUp();self.tmp=TemporaryDirectory(prefix='mx vps fixtures ');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.config_path=self.root/'paper.json';self.config_path.write_text(json.dumps(config()))
        self.config=vps.load_config(self.config_path)
        self.state=self.root/'state';self.state.mkdir()
        self.output=self.root/'logs'

    def observer(self,clock=None):
        return ops.Observer(self.state,self.output,retention_days=7,min_free_mb=1,clock=clock or (lambda:NOW))

    def live_engine(self):
        engine=PaperEngine(self.state/'paper.sqlite3',INST.uid,costs())
        warm=UP[:50]+[UP[49]]
        for name,prices,now in [('boot',warm,NOW),('signal',warm+[UP[-1]],NOW+timedelta(seconds=1))]:
            source,_,_,_=snapshot(self.root,candles(prices),name,clock=now)
            manifest,rows=load_snapshot(source)
            autonomous.source_start(engine,now,requested=manifest['from'])
            engine.process_observed_candles(rows,now,source_start=manifest['from'],source_end=manifest['to'])
        engine.observe(Quote(NOW+timedelta(seconds=2),Decimal(100)))
        return engine

    def test_pid_and_fresh_heartbeat_alone_do_not_count_as_healthy(self):
        observer=self.observer();observer(dict(status='STARTING'))
        data=json.loads((self.state/'health.json').read_text())
        result=ops.assess(data,NOW,identity=lambda pid:data['process_identity'])
        self.assertFalse(result['healthy']);self.assertTrue(result['pending'])
        data['updated_at']=iso(NOW+timedelta(seconds=400))
        self.assertFalse(ops.assess(data,NOW+timedelta(seconds=400),identity=lambda pid:data['process_identity'])['healthy'])

    def test_healthy_iteration_retries_stale_data_and_stale_heartbeat(self):
        observer=self.observer();observer(dict(status='RUNNING',event='NO_FILL',summary=dict(position=0,pending=0,fills=0)))
        data=json.loads((self.state/'health.json').read_text());identity=lambda pid:data['process_identity']
        self.assertTrue(ops.assess(data,NOW,identity=identity)['healthy'])
        data.update(status='API_RETRY',updated_at=iso(NOW+timedelta(seconds=400)))
        self.assertFalse(ops.assess(data,NOW+timedelta(seconds=400),identity=identity)['healthy'])
        data.update(complete_history_age=dict(fresh=False))
        self.assertEqual(ops.assess(data,NOW+timedelta(seconds=400),identity=identity)['reason'],'completed_history_stale')
        data['updated_at']=iso(NOW-timedelta(seconds=601))
        self.assertEqual(ops.assess(data,NOW,identity=identity)['reason'],'heartbeat_stale')
        data['updated_at']=iso(NOW+timedelta(seconds=1))
        self.assertEqual(ops.assess(data,NOW,identity=identity)['reason'],'clock_or_future_heartbeat')

    def test_observer_projects_fields_and_keeps_files_private(self):
        observer=self.observer();observer(dict(status='READY'))
        observer(dict(status='CHECKED',complete_history_age=dict(fresh=True,age_seconds=1,token=SECRET)))
        observer(dict(status='RUNNING',event='PAPER_FILL',summary=dict(position=1,pending=0,fills=1,token=SECRET,accountId=SECRET)))
        self.assertTrue((self.state/'ledger_initialized.json').exists())
        self.assertEqual(len(list((self.state/'outbox').glob('*.json'))),1)
        for path in self.state.rglob('*.json'):
            self.assertNotIn(SECRET,path.read_text())
            if os.name=='posix':self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_disk_reserve_blocks_before_poll(self):
        observer=self.observer()
        with patch.object(ops.shutil,'disk_usage',return_value=Mock(free=0)):
            with self.assertRaises(DataError):observer(dict(status='FETCHING'))

    def test_monitor_deduplicates_incidents_and_emits_recovery(self):
        observer=self.observer();observer(dict(status='RUNNING',event='NO_FILL',summary={}))
        ops.monitor(self.state,NOW);ops.monitor(self.state,NOW)
        self.assertEqual(len(list((self.state/'outbox').glob('*.json'))),1)
        observer(dict(status='STOP'));ops.monitor(self.state,NOW)
        ops.monitor(self.state,NOW)
        self.assertEqual(len(list((self.state/'outbox').glob('*.json'))),2)
        observer(dict(status='RUNNING',event='NO_FILL',summary={}))
        ops.monitor(self.state,NOW)
        self.assertEqual(len(list((self.state/'outbox').glob('*.json'))),3)

    def test_retention_keeps_current_and_last_valid_sources_even_through_api_outage(self):
        history=self.output/'history';history.mkdir(parents=True)
        paths=[]
        for day in range(1,7):
            path=history/f'2026010{day}T000000_000000Z';path.mkdir();paths.append(path)
            if day in (1,2):(path/'manifest.json').write_text(json.dumps(dict(status='valid_with_limits')))
            os.utime(path,(0,0))
        ops.prune(self.output,NOW,7)
        self.assertTrue(paths[0].exists());self.assertTrue(paths[1].exists())
        self.assertFalse(paths[2].exists());self.assertFalse(paths[3].exists())
        self.assertTrue(paths[4].exists());self.assertTrue(paths[5].exists())

    def test_notification_failure_retained_then_acknowledged_without_secret_output(self):
        creds=self.root/'creds';creds.mkdir();(creds/'alert-url').write_text('https://example.invalid/'+SECRET)
        ops.enqueue(self.state/'outbox','fixture',dict(event='ALERT',reason='STOP'))
        body=io.BytesIO(SECRET.encode())
        error=urllib.error.HTTPError('https://example.invalid/'+SECRET,502,SECRET,{},body)
        with patch.dict(os.environ,{'CREDENTIALS_DIRECTORY':str(creds)}),contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(ops.deliver(self.state,'webhook',transport=Mock(side_effect=error)),0)
            self.assertTrue(body.closed)
            self.assertEqual(len(list((self.state/'outbox').glob('*.json'))),1)
            self.assertEqual(ops.deliver(self.state,'webhook',transport=Mock(return_value=FakeResponse(b'{}'))),1)
        self.assertEqual(output.getvalue(),'')
        self.assertFalse(list((self.state/'outbox').glob('*.json')))
        self.assertNotIn(SECRET,(self.state/'delivery.json').read_text())

    def test_telegram_200_requires_ok_true_and_uses_only_notification_credentials(self):
        creds=self.root/'creds';creds.mkdir()
        (creds/'alert-token').write_text('123:notification_TOKEN');(creds/'alert-chat').write_text('-100123')
        ops.enqueue(self.state/'outbox','telegram',dict(event='ALERT',reason='STOP'))
        with patch.dict(os.environ,{'CREDENTIALS_DIRECTORY':str(creds),'T_INVEST_TOKEN':SECRET}):
            self.assertEqual(ops.deliver(self.state,'telegram',transport=Mock(return_value=FakeResponse(b'{"ok":false}'))),0)
            send=Mock(return_value=FakeResponse(b'{"ok":true}'))
            self.assertEqual(ops.deliver(self.state,'telegram',transport=send),1)
        request=send.call_args.args[0]
        self.assertNotIn(SECRET,request.full_url);self.assertNotIn(SECRET,request.data.decode())
        self.assertNotIn('Authorization',dict(request.header_items()))

    def test_online_backup_and_restore_preserve_position_and_do_not_duplicate_fill(self):
        engine=self.live_engine();before=engine.summary()
        with runner_lock(engine.path):
            path=maintenance.backup(engine.path,self.state,self.config)
            with self.assertRaises(DataError):maintenance.restore(path,engine.path,self.config)
        digest=hashlib.sha256(path.read_bytes()).hexdigest()
        restored_path=self.root/'restored.sqlite3'
        report=maintenance.restore(path,restored_path,self.config)
        self.assertEqual(report['position'],1)
        restored=PaperEngine(restored_path,INST.uid,costs())
        self.assertEqual(restored.summary(),before)
        self.assertFalse(restored.observe(Quote(NOW+timedelta(seconds=2),Decimal(100))))
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),digest)

    def test_bad_backup_or_wrong_costs_do_not_create_destination(self):
        engine=self.live_engine();source=maintenance.backup(engine.path,self.state,self.config)
        candidate=self.root/'corrupt.sqlite3';maintenance.copy_database(source,candidate)
        from contextlib import closing
        import sqlite3
        with closing(sqlite3.connect(candidate)) as db:
            db.execute("UPDATE quotes SET price='-1'");db.commit()
        destination=self.root/'must_not_exist.db'
        with self.assertRaises(DataError):maintenance.restore(candidate,destination,self.config)
        self.assertFalse(destination.exists())
        wrong=copy.deepcopy(self.config);wrong['costs']['commission_rub']='999'
        with self.assertRaises(DataError):maintenance.restore(source,destination,wrong)
        self.assertFalse(destination.exists())

    def test_restore_is_paused_until_operator_confirms_complete_authoritative_ledger(self):
        engine=self.live_engine();backup=maintenance.backup(engine.path,self.state,self.config)
        target=self.root/'target';target.mkdir()
        args=['restore','--config',str(self.config_path),'--state',str(target),'--backup',str(backup)]
        with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(vps.main(args),0)
        self.assertTrue((target/'restore_review_required.json').exists())
        with patch('src.gldrubf_bot.vps.credential') as token:
            self.assertEqual(vps.run(self.config,target,self.output),78)
            token.assert_not_called()
        with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(vps.main(args+['--authoritative-ledger']),0)
        self.assertFalse((target/'restore_review_required.json').exists())

    def test_missing_previously_initialized_database_is_blocked_before_api(self):
        ops.atomic_json(self.state/'ledger_initialized.json',dict(initialized=True))
        with patch('src.gldrubf_bot.vps.credential') as token:
            self.assertEqual(vps.run(self.config,self.state,self.output),78)
            token.assert_not_called()
        self.assertFalse((self.state/'paper.sqlite3').exists())

    def test_sigterm_path_gracefully_stops_and_restores_token_environment(self):
        with patch('src.gldrubf_bot.vps.credential',return_value='fake-read-only'), \
             patch('src.gldrubf_bot.vps.run_paper',side_effect=KeyboardInterrupt), \
             patch.dict(os.environ,{'T_INVEST_TOKEN':'prior'}):
            self.assertEqual(vps.run(self.config,self.state,self.output),0)
            self.assertEqual(os.environ['T_INVEST_TOKEN'],'prior')
        self.assertEqual(json.loads((self.state/'health.json').read_text())['status'],'STOPPED')

    def test_physical_sqlite_corruption_stops_without_automatic_restart_policy(self):
        import sqlite3
        with patch('src.gldrubf_bot.vps.credential',return_value='fake-read-only'), \
             patch('src.gldrubf_bot.vps.run_paper',side_effect=sqlite3.DatabaseError('synthetic corrupt file')):
            self.assertEqual(vps.run(self.config,self.state,self.output),78)
        self.assertEqual(json.loads((self.state/'health.json').read_text())['status'],'STOP')

    def test_placeholder_configuration_and_missing_age_are_blocked_offline(self):
        root=Path(__file__).resolve().parents[1]
        with self.assertRaises((DataError,ValueError)):vps.load_config(root/'deploy/linux/paper.example.json')
        data=config();data.pop('max_complete_age_hours');self.config_path.write_text(json.dumps(data))
        with self.assertRaises(DataError):vps.load_config(self.config_path)
        args=vps.arguments(self.config,self.state,self.output)
        self.assertNotIn('--token',args);self.assertNotIn(SECRET,' '.join(args))

    def test_systemd_security_and_install_script_do_not_start_services(self):
        root=Path(__file__).resolve().parents[1]/'deploy/linux'
        service=(root/'gldrubf-paper.service').read_text()
        for text in ('LoadCredential=api-token:', 'RestartPreventExitStatus=78','User=gldrubf','UMask=0077','ProtectSystem=strict'):
            self.assertIn(text,service)
        health=(root/'gldrubf-health.service').read_text()
        self.assertNotIn('api-token',health)
        installer=(root/'install.sh').read_text()
        commands=[line for line in installer.splitlines() if line.startswith('systemctl ')]
        self.assertEqual(commands,['systemctl daemon-reload'])

    def test_local_setup_writes_credentials_privately_without_network_or_service_start(self):
        from src.gldrubf_bot import vps_setup
        class Console(io.StringIO):
            def isatty(self):return True
        folder=self.root/'configuration'
        answers=['.5','1','1000','168','7','','2','webhook']
        with patch.object(vps_setup.os,'geteuid',return_value=0), \
             patch.object(vps_setup.sys,'stdin',Console()),contextlib.redirect_stdout(Console()) as output, \
             patch('builtins.input',side_effect=answers),patch.object(vps_setup,'read_token',return_value=SECRET), \
             patch.object(vps_setup.getpass,'getpass',return_value='https://example.invalid/notification-secret'):
            vps_setup.configure(folder)
        for text in (SECRET,'notification-secret'):self.assertNotIn(text,output.getvalue())
        for name in ('api-token','alert-url','alert-chat','alert-token'):
            self.assertEqual((folder/name).stat().st_mode&0o777,0o600)
        self.assertEqual(vps.load_config(folder/'paper.json')['max_complete_age_hours'],168)
        self.assertFalse((folder/'paper.candidate.json').exists())

    def test_invalid_local_setup_policy_does_not_prompt_for_token(self):
        from src.gldrubf_bot import vps_setup
        class Console(io.StringIO):
            def isatty(self):return True
        with patch.object(vps_setup.os,'geteuid',return_value=0),patch.object(vps_setup.sys,'stdin',Console()), \
             contextlib.redirect_stdout(Console()),patch('builtins.input',side_effect=['.5','1','1000','0','7','','2','webhook']), \
             patch.object(vps_setup,'read_token') as token:
            with self.assertRaises(DataError):vps_setup.configure(self.root/'bad_config')
            token.assert_not_called()
