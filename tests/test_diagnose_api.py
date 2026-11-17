"""No real API/token: transport parity, stdin-only curl secrets and safe output."""
import contextlib
import getpass
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
import urllib.error
import warnings
from unittest.mock import Mock, patch

from src.gldrubf_bot import diagnose_api as diag
from tests.test_api import FakeResponse


TOKEN = 't.fake-test-only_secret-123'


class DiagnosticTests(unittest.TestCase):
    def test_token_is_hidden_and_not_taken_from_environment(self):
        with patch.dict(os.environ, {'T_INVEST_TOKEN': 'different-token'}), \
             patch.object(diag.sys.stdin, 'isatty', return_value=True), \
             patch.object(diag.getpass, 'getpass', return_value=TOKEN) as prompt:
            self.assertEqual(diag.read_token(), TOKEN)
            prompt.assert_called_once_with('Read-only API token (hidden): ')

    def test_nonconsole_input_and_getpass_echo_fallback_are_rejected(self):
        with patch.object(diag.sys.stdin, 'isatty', return_value=False), \
             patch.object(diag.getpass, 'getpass') as prompt:
            with self.assertRaises(ValueError):
                diag.read_token()
            prompt.assert_not_called()

        def fallback(*args):
            warnings.warn('Echo would be enabled', getpass.GetPassWarning)

        with patch.object(diag.sys.stdin, 'isatty', return_value=True), \
             patch.object(diag.getpass, 'getpass', side_effect=fallback):
            with self.assertRaises(getpass.GetPassWarning):
                diag.read_token()

    def test_header_and_config_injection_tokens_are_rejected(self):
        for token in ['', 'Bearer ' + TOKEN, TOKEN + '\nheader="leak"', TOKEN + '\r',
                      TOKEN + '"', TOKEN + '\\', TOKEN + '\x00', '\u044f', 'a' * 8193]:
            with self.subTest(case=repr(token[:12])), \
                 patch.object(diag.sys.stdin, 'isatty', return_value=True), \
                 patch.object(diag.getpass, 'getpass', return_value=token):
                with self.assertRaises(ValueError):
                    diag.read_token()

    def test_urllib_sends_one_find_request_and_never_reads_body(self):
        for status in (200, 502):
            with self.subTest(status=status):
                response = FakeResponse(TOKEN.encode(), status=status)
                response.read = Mock(side_effect=AssertionError('Body must not be read'))
                opener = Mock()
                opener.open.return_value = response
                with patch.object(diag.urllib.request, 'build_opener', return_value=opener) as build:
                    result = diag.diagnose_urllib(TOKEN)
                self.assertEqual(result.http_status, status)
                self.assertNotIn(TOKEN, result.line())
                opener.open.assert_called_once()
                self.assertIsInstance(build.call_args.args[0], diag.NoRedirect)
                request = opener.open.call_args.args[0]
                self.assertEqual(request.full_url, diag.URL)
                self.assertTrue(request.full_url.endswith('/FindInstrument'))
                self.assertEqual(request.method, 'POST')
                self.assertEqual(request.data, diag.BODY)
                for key, value in diag._headers(TOKEN).items():
                    self.assertEqual(request.get_header(key.capitalize()), value)
                self.assertTrue(response.closed)
                response.read.assert_not_called()

    def test_urllib_http_error_closes_body_without_reading_or_retry(self):
        body = io.BytesIO(TOKEN.encode())
        body.read = Mock(side_effect=AssertionError('Body must not be read'))
        error = urllib.error.HTTPError('https://example.invalid/' + TOKEN, 502, TOKEN,
                                       {'Content-Type': 'text/html; secret=' + TOKEN}, body)
        opener = Mock()
        opener.open.side_effect = error
        with patch.object(diag.urllib.request, 'build_opener', return_value=opener):
            result = diag.diagnose_urllib(TOKEN)
        self.assertEqual(result, diag.Result('urllib', 502, 'server_error'))
        self.assertTrue(body.closed)
        body.read.assert_not_called()
        opener.open.assert_called_once()

    def test_urllib_network_exception_is_sanitized(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError('Tunnel connection failed: 502 ' + TOKEN)
        with patch.object(diag.urllib.request, 'build_opener', return_value=opener):
            result = diag.diagnose_urllib(TOKEN)
        self.assertEqual(result, diag.Result('urllib', None, 'proxy_tunnel_error'))
        self.assertNotIn(TOKEN, result.line())
        opener.open.assert_called_once()

    def test_curl_token_only_in_stdin_not_argv_env_or_file(self):
        completed = subprocess.CompletedProcess([], 0, b'502 text/html; secret=' + TOKEN.encode(), TOKEN.encode())
        with patch.object(diag.shutil, 'which', return_value='C:/Windows/System32/curl.exe'), \
             patch.dict(os.environ, {'T_INVEST_TOKEN': TOKEN, 'SSLKEYLOGFILE': 'private-debug-file'}), \
             patch.object(diag.subprocess, 'run', return_value=completed) as run, \
             patch('builtins.open', side_effect=AssertionError('No credential files')):
            result = diag.diagnose_curl(TOKEN)
        run.assert_called_once()
        self.assertEqual(result, diag.Result('curl.exe', 502, 'server_error'))
        command = run.call_args.args[0]
        options = run.call_args.kwargs
        self.assertEqual(command, ['C:/Windows/System32/curl.exe', '--disable', '--config', '-'])
        self.assertNotIn(TOKEN, ' '.join(command))
        self.assertNotIn(TOKEN, repr(options['env']))
        self.assertNotIn('SSLKEYLOGFILE', options['env'])
        self.assertIs(options['stderr'], subprocess.DEVNULL)
        self.assertIs(options['stdout'], subprocess.PIPE)
        self.assertNotIn('shell', options)
        config = options['input'].decode('ascii')
        self.assertEqual(config.count('Authorization: Bearer ' + TOKEN), 1)
        self.assertEqual(config.count('url = '), 1)
        self.assertIn('url = "' + diag.URL + '"', config)
        self.assertIn('data-binary = "{\\"query\\": \\"GLDRUBF\\"}"', config)
        self.assertIn('http1.1\n', config)
        self.assertIn('retry = "0"', config)
        self.assertIn('max-redirs = "0"', config)
        self.assertIn('proto = "=https"', config)
        self.assertIn('output = ' + diag._config_quote(os.devnull), config)
        for option in ('insecure', 'verbose', 'trace', 'location', 'dump-header', 'cookie-jar'):
            self.assertNotIn(option, config)
        self.assertNotIn(TOKEN, result.line())

    def test_curl_output_and_failures_cannot_echo_secrets(self):
        for code, output, category in [
            (0, TOKEN.encode(), 'invalid_client_metadata'),
            (0, b'200 ' + TOKEN.encode(), 'unexpected_content_type'),
            (0, b'401 application/json', 'authentication_error'),
            (0, b'302 text/html', 'redirect_blocked'),
            (6, b'000 ' + TOKEN.encode(), 'dns_error'),
            (28, b'200 application/json', 'timeout'),
            (60, b'000 ' + TOKEN.encode(), 'tls_verification_error'),
        ]:
            with self.subTest(code=code, category=category), \
                 patch.object(diag.shutil, 'which', return_value='curl.exe'), \
                 patch.object(diag.subprocess, 'run', return_value=subprocess.CompletedProcess([], code, output)) as run:
                result = diag.diagnose_curl(TOKEN)
                self.assertEqual(result.category, category)
                self.assertNotIn(TOKEN, result.line())
                run.assert_called_once()

    def test_missing_curl_or_subprocess_errors_are_safe(self):
        with patch.object(diag.shutil, 'which', return_value=None), patch.object(diag.subprocess, 'run') as run:
            self.assertEqual(diag.diagnose_curl(TOKEN).category, 'client_unavailable')
            run.assert_not_called()
        for error, category in [(OSError(TOKEN), 'client_start_error'),
                                (subprocess.TimeoutExpired([TOKEN], 25, output=TOKEN.encode()), 'timeout')]:
            with patch.object(diag.shutil, 'which', return_value='curl.exe'), \
                 patch.object(diag.subprocess, 'run', side_effect=error):
                result = diag.diagnose_curl(TOKEN)
                self.assertEqual(result.category, category)
                self.assertNotIn(TOKEN, result.line())

    def test_main_shares_one_token_and_prints_only_two_safe_results(self):
        for statuses in [(502, 502), (502, 200), (200, 502), (200, 200)]:
            with self.subTest(statuses=statuses):
                results = [diag.Result(client, status, 'http_ok' if status == 200 else 'server_error')
                           for client, status in zip(('urllib', 'curl.exe'), statuses)]
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(diag, 'read_token', return_value=TOKEN) as prompt, \
                     patch.object(diag, 'diagnose_urllib', return_value=results[0]) as python, \
                     patch.object(diag, 'diagnose_curl', return_value=results[1]) as curl, \
                     patch.dict(os.environ, {'SSLKEYLOGFILE': 'private-debug-file'}), \
                     contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = diag.main()
                    self.assertNotIn('SSLKEYLOGFILE', os.environ)
                prompt.assert_called_once()
                python.assert_called_once_with(TOKEN)
                curl.assert_called_once_with(TOKEN)
                self.assertEqual(stdout.getvalue().splitlines(), [r.line() for r in results])
                self.assertEqual(stderr.getvalue(), '')
                self.assertEqual(code, 0 if statuses == (200, 200) else 1)

    def test_main_never_prints_unexpected_errors_or_prompt_exceptions(self):
        for error in [ValueError(TOKEN), EOFError(TOKEN), KeyboardInterrupt()]:
            with patch.object(diag, 'read_token', side_effect=error), \
                 patch.object(diag, 'diagnose_urllib') as python, \
                 patch.object(diag, 'diagnose_curl') as curl, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(diag.main(), 1)
            self.assertNotIn(TOKEN, output.getvalue())
            self.assertEqual(len(output.getvalue().splitlines()), 2)
            python.assert_not_called()
            curl.assert_not_called()
        with patch.object(diag, 'read_token', return_value=TOKEN), \
             patch.object(diag, 'diagnose_urllib', side_effect=RuntimeError(TOKEN)), \
             patch.object(diag, 'diagnose_curl', side_effect=RuntimeError(TOKEN)), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(diag.main(), 1)
        self.assertNotIn(TOKEN, output.getvalue())
        self.assertEqual(output.getvalue().count('category=internal_error'), 2)
        with patch.object(diag, 'read_token', return_value=TOKEN), \
             patch.object(diag, 'diagnose_urllib', side_effect=KeyboardInterrupt()), \
             patch.object(diag, 'diagnose_curl') as curl, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(diag.main(), 1)
        curl.assert_not_called()
        self.assertEqual(output.getvalue().count('category=cancelled'), 2)

    def test_real_curl_accepts_stdin_config_without_network_request(self):
        executable = shutil.which('curl.exe') or shutil.which('curl')
        if not executable:
            self.skipTest('curl is not installed')
        # --version parses the real config but performs no network request.
        result = subprocess.run([executable, '--disable', '--config', '-', '--version'],
                                input=diag._curl_config(TOKEN), capture_output=True, timeout=10, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn(b'curl ', result.stdout)
        self.assertNotIn(TOKEN.encode(), result.stdout + result.stderr)

    def test_module_rejects_noninteractive_input_without_echo_or_traceback(self):
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run([sys.executable, '-m', 'src.gldrubf_bot.diagnose_api'],
                                   input=TOKEN.encode(), capture_output=True, timeout=10, cwd=root)
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stderr, b'')
        self.assertEqual(completed.stdout.decode().splitlines(), [
            'client=urllib http_status=unknown category=token_input_unavailable_or_invalid',
            'client=curl.exe http_status=unknown category=token_input_unavailable_or_invalid',
        ])
        self.assertNotIn(TOKEN.encode(), completed.stdout + completed.stderr)

    def test_windows_launcher_has_crlf_and_no_token_arguments(self):
        data = (Path(__file__).resolve().parents[1] / 'diagnose_api_windows.cmd').read_bytes()
        self.assertEqual(data.count(b'\n'), data.count(b'\r\n'))
        self.assertIn(b'-m src.gldrubf_bot.diagnose_api\r\n', data)
        self.assertIn(b'exit /b %DIAG_EXIT%', data)
        self.assertNotIn(b'T_INVEST_TOKEN', data)
        self.assertNotIn(b'%*', data)
