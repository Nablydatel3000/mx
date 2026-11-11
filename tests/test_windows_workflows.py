"""Launcher argument plumbing; actual cmd.exe/API runs are separate Windows checks."""
import sys
import contextlib
import io
from pathlib import Path
import unittest
from unittest.mock import patch

from src.gldrubf_bot import windows


class WindowsWorkflowTests(unittest.TestCase):
    def test_download_prompts_for_previous_snapshot_and_uses_no_calendar(self):
        with patch.object(sys,'argv',['windows','fetch']), \
             patch.object(windows,'ask',side_effect=['2025-01-01','2026-01-01','day','prior_snapshot']), \
             patch.object(windows,'token') as token,patch.object(windows,'main') as main, \
             contextlib.redirect_stdout(io.StringIO()):
            windows.run()
        args=main.call_args.args[0]
        self.assertEqual(args[0],'fetch')
        self.assertIn('--compare',args)
        self.assertNotIn('--calendar',args)
        self.assertEqual(args[args.index('--compare')+1],'prior_snapshot')
        token.assert_called_once()

    def test_forward_requires_snapshot_and_verified_evidence(self):
        inputs=['history_folder','verified.json','new_forward.db','logs','.5','1','1000','','2']
        with patch.object(sys,'argv',['windows','paper']),patch.object(windows,'ask',side_effect=inputs), \
             patch.object(windows,'token') as token,patch.object(windows,'main') as main, \
             contextlib.redirect_stdout(io.StringIO()):
            windows.run()
        args=main.call_args.args[0]
        self.assertEqual(args[:5],['paper','--history','history_folder','--evidence','verified.json'])
        token.assert_called_once()

    def test_historical_replay_is_offline_and_accepts_explicit_assumptions(self):
        inputs=['history_folder','assumptions.json','quotes.csv','2','1','replay.db','logs','.5','1','1000','','2']
        with patch.object(sys,'argv',['windows','historical-replay']),patch.object(windows,'ask',side_effect=inputs), \
             patch.object(windows,'token') as token,patch.object(windows,'main') as main, \
             contextlib.redirect_stdout(io.StringIO()):
            windows.run()
        args=main.call_args.args[0]
        self.assertEqual(args[0],'historical-replay')
        self.assertIn('--evidence',args)
        self.assertNotIn('--calendar',args)
        token.assert_not_called()

    def test_launcher_files_preserve_crlf_and_error_status(self):
        root=Path(__file__).resolve().parents[1]
        for name in ['download_candles_windows.cmd','run_paper_windows.cmd','replay_windows.cmd','historical_replay_windows.cmd']:
            data=(root/name).read_bytes()
            self.assertEqual(data.count(b'\n'),data.count(b'\r\n'))
            self.assertIn(b'exit /b %BOT_EXIT%',data)
            self.assertNotIn(b'T_INVEST_TOKEN',data)
