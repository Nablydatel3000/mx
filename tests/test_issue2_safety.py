"""Issue #2 regressions, synthetic data and disposable SQLite; no live credentials."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from datetime import timedelta
from decimal import Decimal
import unittest

from src.gldrubf_bot import autonomous
from src.gldrubf_bot.engine import PaperEngine
from src.gldrubf_bot.history import load_snapshot
from src.gldrubf_bot.locking import runner_lock
from src.gldrubf_bot.models import DataError, Quote, instant, iso
from tests.helpers import SQLiteResourceTestCase, costs, UP
from tests.history_helpers import INST, START, NOW, candles, snapshot, client_for


class Issue2SafetyTests(SQLiteResourceTestCase):
    def setUp(self):
        super().setUp()
        self.tmp=TemporaryDirectory(prefix='mx issue2 fixes ');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def engine(self,name='paper.db'):
        return PaperEngine(self.root/name,INST.uid,costs())

    def accept(self,engine,prices,name,now):
        path,_,_,_=snapshot(self.root,candles(prices),name,clock=now)
        manifest,rows=load_snapshot(path)
        autonomous.source_start(engine,now,requested=manifest['from'])
        engine.process_observed_candles(rows,now,source_start=manifest['from'],source_end=manifest['to'])
        return path

    def pending(self):
        engine=self.engine();warm=UP[:50]+[UP[49]]
        self.accept(engine,warm,'boot',NOW)
        later=NOW+timedelta(seconds=1)
        self.accept(engine,warm+[UP[-1]],'new',later)
        self.assertEqual(engine.summary()['pending'],1)
        return engine,later

    def test_cold_bootstrap_persisted_and_does_not_open_on_historical_cross(self):
        engine=self.engine();self.accept(engine,UP,'history',NOW)
        self.assertEqual(engine.summary()['pending'],0)
        self.assertFalse(engine.observe(Quote(NOW+timedelta(seconds=1),Decimal(100))))
        restored=self.engine()
        self.assertFalse(restored.observe(Quote(NOW+timedelta(seconds=2),Decimal(100))))
        self.assertEqual((restored.summary()['position'],restored.summary()['fills']),(0,0))
        with restored.db() as db:
            policy=db.execute('SELECT * FROM forward_bootstrap').fetchone()
            self.assertEqual(policy['origin'],'cold')
            self.assertEqual(policy['through_day'],(START+timedelta(days=50)).date().isoformat())

    def test_pending_and_filled_position_survive_restart_without_duplicate(self):
        engine,known=self.pending();state=engine.summary()
        restored=self.engine();self.assertEqual(restored.summary(),state)
        quote=Quote(known+timedelta(seconds=1),Decimal(100))
        self.assertTrue(restored.observe(quote))
        before=restored.summary();again=self.engine()
        self.assertFalse(again.observe(quote))
        self.assertEqual(again.summary(),before)

    def test_legacy_migration_preserves_old_position_and_pending(self):
        for filled in (False,True):
            name='old_'+str(filled)+'.db';engine=self.engine(name)
            self.accept(engine,UP,'old_'+str(filled),NOW)
            with engine.db() as db:
                signal=db.execute('SELECT * FROM bars WHERE target!=0 ORDER BY day DESC LIMIT 1').fetchone()
                db.execute('UPDATE state SET pending=?,pending_day=?,pending_after=?',
                           (signal['target'],signal['day'],signal['known_at']))
                db.execute("UPDATE forward_bootstrap SET origin='legacy',through_day=NULL,known_at=NULL,review_required=0")
            if filled:engine.observe(Quote(NOW+timedelta(seconds=1),Decimal(100)))
            with engine.db() as db:
                db.execute('DROP TABLE forward_bootstrap');db.execute('PRAGMA user_version=0')
            with engine.db() as db:before=dict(db.execute('SELECT * FROM state').fetchone())
            restored=self.engine(name)
            with restored.db() as db:
                self.assertEqual(dict(db.execute('SELECT * FROM state').fetchone()),before)
                self.assertEqual(db.execute('SELECT origin FROM forward_bootstrap').fetchone()[0],'legacy')
            self.assertEqual(restored.summary()['migration_review_required'],not filled)
            if not filled:
                self.assertFalse(restored.observe(Quote(NOW+timedelta(seconds=2),Decimal(100))))
                self.assertEqual(restored.summary()['pending'],before['pending'])
                self.accept(restored,UP+[1,1],'after_migration',NOW+timedelta(seconds=3))
                self.assertFalse(restored.summary()['migration_review_required'])
                self.assertEqual(restored.summary()['pending'],-1)
                self.assertTrue(restored.observe(Quote(NOW+timedelta(seconds=4),Decimal(100))))

    def test_fifty_initial_candles_can_warm_up_and_the_new_fifty_first_can_signal(self):
        engine=self.engine()
        self.accept(engine,UP[:50],'fifty',NOW)
        self.assertEqual(engine.summary()['pending'],0)
        self.accept(engine,UP,'fifty_one',NOW+timedelta(seconds=1))
        self.assertEqual(engine.summary()['pending'],1)

    def test_orphan_pending_metadata_is_not_treated_as_no_signal(self):
        engine=self.engine()
        with engine.db() as db:db.execute("UPDATE state SET pending_day='2026-01-01'")
        with self.assertRaises(DataError):self.engine()

    def test_missing_bootstrap_marker_not_silently_treated_as_legacy(self):
        engine=self.engine();self.accept(engine,UP,'boot',NOW)
        with engine.db() as db:db.execute('DELETE FROM forward_bootstrap')
        with self.assertRaises(DataError):self.engine()

    def test_missing_state_or_metadata_is_not_recreated_as_a_new_empty_account(self):
        for index,statement in enumerate(('DELETE FROM state','DELETE FROM metadata','DROP TABLE state','DROP TABLE quotes')):
            name=f'missing_{index}.db';engine=self.engine(name)
            self.accept(engine,UP,f'missing_source_{index}',NOW)
            with engine.db() as db:db.execute(statement)
            before=engine.path.read_bytes()
            with self.subTest(statement=statement),self.assertRaises(DataError):self.engine(name)
            self.assertEqual(before,engine.path.read_bytes())

    def test_quote_ledger_negative_offtick_or_noncanonical_time_rejected(self):
        for index,statement in enumerate(["UPDATE quotes SET price='-1'", "UPDATE quotes SET price='100.5'",
                                          "UPDATE quotes SET time='2026-02-23T10:00:02+00:00'"]):
            engine=self.engine(str(index)+'.db');self.accept(engine,UP,'boot_'+str(index),NOW)
            engine.observe(Quote(NOW+timedelta(seconds=1),Decimal(100)))
            with engine.db() as db:db.execute(statement)
            with self.subTest(statement=statement),self.assertRaises(DataError):self.engine(str(index)+'.db')

    def test_fill_signal_quote_price_slippage_and_causality_validated(self):
        for index,statement in enumerate(["UPDATE fills SET signal_day='2099-01-01'", 'DELETE FROM quotes',
                                          "UPDATE quotes SET price='101'", "UPDATE bars SET known_at='2026-02-23T10:00:09.000000Z' WHERE target=1"]):
            # Separate fixture directories preserve each independent corruption case.
            old_root=self.root;self.root=old_root/str(index);self.root.mkdir()
            try:
                engine,known=self.pending();engine.observe(Quote(known+timedelta(seconds=1),Decimal(100)))
                with engine.db() as db:db.execute(statement)
                with self.subTest(statement=statement),self.assertRaises(DataError):self.engine()
            finally:self.root=old_root

    def test_fresh_response_with_old_completed_candles_cannot_bootstrap(self):
        now=NOW+timedelta(days=20)
        path,_,_,_=snapshot(self.root,candles(UP),'stale',clock=now)
        engine=self.engine();client,_,_=client_for(candles(),quote_time=now)
        before=engine.summary()
        event=autonomous.step(engine,client,INST,path,clock=lambda:now,max_complete_age_hours=48)
        self.assertEqual(event,'WAIT_COMPLETE_HISTORY')
        self.assertEqual(engine.summary(),before)
        self.assertIsNone(engine.latest_day())

    def test_age_policy_required_and_utc_boundary_includes_exact_limit(self):
        _,_,_,_=snapshot(self.root,candles(UP),'source')
        manifest,rows=load_snapshot(self.root/'source')
        last=instant(rows[-1]['time'])
        self.assertTrue(autonomous.history_age(rows,last+timedelta(hours=48),48)['fresh'])
        self.assertFalse(autonomous.history_age(rows,last+timedelta(hours=48,microseconds=1),48)['fresh'])
        for value in (None,0,-1,True,1.5):
            with self.assertRaises(DataError):autonomous.history_age(rows,NOW,value)

    def test_quote_and_execution_need_same_individual_window(self):
        from src.gldrubf_bot.api import parse_day
        from tests.test_detailed_sessions import reported_day
        calendar={1:parse_day(reported_day('2026-11-17'))}
        self.assertFalse(autonomous.same_window(calendar,instant('2026-11-17T06:00:00Z'),instant('2026-11-17T05:59:58Z')))
        self.assertTrue(autonomous.same_window(calendar,instant('2026-11-17T06:00:01Z'),instant('2026-11-17T06:00:00Z')))

    def test_full_forward_rejects_prior_phase_price_with_pending_then_fills_in_current_phase(self):
        from src.gldrubf_bot.api import TradingDay
        engine=self.engine();warm=UP[:50]+[UP[49]]
        self.accept(engine,warm,'warm',NOW-timedelta(seconds=5))
        source=self.accept(engine,warm+[UP[-1]],'pending',NOW-timedelta(seconds=3))
        calendar={NOW.date():TradingDay(NOW.date(),True,
            ((NOW-timedelta(hours=1),NOW-timedelta(seconds=1)),(NOW,NOW+timedelta(hours=1))),NOW+timedelta(hours=1))}
        client,_,_=client_for(candles(),quote_time=NOW-timedelta(seconds=2))
        client.current_sessions=lambda *args:calendar
        self.assertEqual(autonomous.step(engine,client,INST,source,clock=lambda:NOW,max_complete_age_hours=168),'WAIT_MARKET')
        self.assertEqual((engine.summary()['pending'],engine.summary()['fills']),(1,0))
        later=NOW+timedelta(seconds=1)
        client,_,_=client_for(candles(),quote_time=later);client.current_sessions=lambda *args:calendar
        self.assertEqual(autonomous.step(engine,client,INST,source,clock=lambda:later,max_complete_age_hours=168),'PAPER_FILL')

    def test_failed_bootstrap_transaction_does_not_commit_a_partial_boundary(self):
        import sqlite3
        engine=self.engine()
        with engine.db() as db:
            db.execute("CREATE TRIGGER fail_boot BEFORE INSERT ON bars WHEN NEW.target!=0 BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.accept(engine,UP,'failed',NOW)
        with engine.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM bars').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM forward_bootstrap').fetchone()[0],0)
            db.execute('DROP TRIGGER fail_boot')
        restored=self.engine();self.accept(restored,UP,'retry',NOW+timedelta(seconds=1))
        self.assertEqual(restored.summary()['pending'],0)

    def test_process_crash_before_and_after_commit_preserves_pending_and_deduplicates_fill(self):
        engine,known=self.pending();before=engine.summary()
        code="""import os,sys
from src.gldrubf_bot.engine import PaperEngine
from src.gldrubf_bot.models import Quote,instant
from decimal import Decimal
from tests.helpers import costs
engine=PaperEngine(sys.argv[1],sys.argv[2],costs())
if sys.argv[4]=='before':
    with engine.db() as db:
        db.execute('UPDATE state SET position=-1')
        os._exit(13)
engine.observe(Quote(instant(sys.argv[3]),Decimal(100)))
os._exit(12)
"""
        stamp=known+timedelta(seconds=1)
        args=[sys.executable,'-c',code,str(engine.path),INST.uid,iso(stamp)]
        self.assertEqual(subprocess.run(args+['before'],capture_output=True).returncode,13)
        self.assertEqual(self.engine().summary(),before)
        self.assertEqual(subprocess.run(args+['after'],capture_output=True).returncode,12)
        restored=self.engine();self.assertEqual(restored.summary()['fills'],1)
        self.assertFalse(restored.observe(Quote(stamp,Decimal(100))))
        self.assertEqual(self.engine().summary()['position'],1)

    def test_physical_lock_blocks_symlink_relative_path_and_releases_after_error(self):
        path=self.root/'paper.db';self.engine()
        alias=self.root/'alias.db'
        try:alias.symlink_to(path)
        except OSError:self.skipTest('Symlink creation not available')
        with runner_lock(path):
            for other in (alias,path.parent/'x'/'..'/'paper.db'):
                with self.assertRaises(DataError):
                    with runner_lock(other):pass
        with self.assertRaises(RuntimeError):
            with runner_lock(alias):raise RuntimeError('fixture')
        with runner_lock(path):pass

    def test_hard_links_rejected_not_given_independent_lock(self):
        path=self.root/'paper.db';self.engine();alias=self.root/'hard.db'
        try:os.link(path,alias)
        except OSError:self.skipTest('Hard links unavailable')
        for name in (path,alias):
            with self.assertRaises(DataError):
                with runner_lock(name):pass

    def test_actual_second_process_blocked_and_killed_owner_releases_lock(self):
        path=self.root/'paper.db';self.engine()
        child_path=self.root/'child-alias.db'
        try:child_path.symlink_to(path)
        except OSError:child_path=path
        code="from src.gldrubf_bot.locking import runner_lock; import sys;\nwith runner_lock(sys.argv[1]):\n print('ready',flush=True)\n sys.stdin.read()\n"
        with subprocess.Popen([sys.executable,'-c',code,str(child_path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,text=True) as child:
            try:
                self.assertEqual(child.stdout.readline().strip(),'ready')
                with self.assertRaises(DataError):
                    with runner_lock(path):pass
            finally:child.kill();child.communicate(timeout=10)
        with runner_lock(path):pass

    def test_replaced_database_detected_before_next_poll(self):
        if os.name=='nt':self.skipTest('Windows prevents replacing an open DB file')
        path=self.root/'paper.db';self.engine()
        with runner_lock(path) as check:
            replacement=self.root/'new.db';replacement.write_bytes(path.read_bytes())
            os.replace(replacement,path)
            with self.assertRaises(DataError):check()
