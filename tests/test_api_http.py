"""Offline HTTP contract, resource lifetime and diagnostic redaction regressions."""
import http.client
import io
import json
import socket
import ssl
import traceback
import unittest
import urllib.error
from datetime import datetime
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from src.gldrubf_bot.api import APIError, NoRedirect
from src.gldrubf_bot.cli import main
from src.gldrubf_bot.models import UTC
from tests.test_api import FakeResponse, make_client, UID, day, candle, q


METHOD = 'InstrumentsService/FindInstrument'
SECRET = 'fake-sensitive-token-never-print'


class HTTPDiagnosticsTests(unittest.TestCase):
    def assert_safe_error(self, error, category, method=METHOD, status=200, content_type='application/json'):
        self.assertIsInstance(error, APIError)
        self.assertEqual((error.category, error.method, error.http_status, error.content_type),
                         (category, method, status, content_type))
        message = str(error)
        self.assertIn('method=' + method, message)
        self.assertIn('http_status=' + (str(status) if status is not None else 'unknown'), message)
        self.assertIn('content_type=' + content_type, message)
        self.assertIn('category=' + category, message)
        self.assertNotIn(SECRET, message)
        self.assertNotIn('Authorization', message)
        self.assertNotIn(SECRET, ''.join(traceback.format_exception(error)))

    def test_official_rest_url_headers_and_json_request(self):
        response = FakeResponse(b'{"instruments":[]}')
        transport = Mock(return_value=response)
        client = make_client(token=SECRET, transport=transport)
        self.assertEqual(client.call(METHOD, {'query': 'GLDRUBF'}), {'instruments': []})
        request = transport.call_args.args[0]
        self.assertEqual(request.full_url,
                         'https://invest-public-api.tbank.ru/rest/'
                         'tinkoff.public.invest.api.contract.v1.InstrumentsService/FindInstrument')
        self.assertEqual(request.method, 'POST')
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + SECRET)
        self.assertEqual(request.get_header('Content-type'), 'application/json')
        self.assertEqual(request.get_header('Accept'), 'application/json')
        self.assertEqual(json.loads(request.data), {'query': 'GLDRUBF'})
        self.assertEqual(transport.call_args.kwargs['timeout'], 20)
        self.assertTrue(response.closed)

    def test_200_invalid_bodies_and_content_types_are_rejected_safely(self):
        cases = [
            (b'', 'application/json', 'empty_response', 'application/json'),
            (b' \r\n\t', 'application/json', 'empty_response', 'application/json'),
            (('<html>' + SECRET).encode(), 'text/html', 'unexpected_content_type', 'text/html'),
            (SECRET.encode(), 'application/json', 'invalid_json', 'application/json'),
            (b'{"instruments":', 'application/json', 'invalid_json', 'application/json'),
            (b'\xff\xfe\x00', 'application/json', 'invalid_json', 'application/json'),
            (b'[]', 'application/json', 'unexpected_json_type', 'application/json'),
            (b'null', 'application/json', 'unexpected_json_type', 'application/json'),
            (json.dumps({'code': 16, 'message': SECRET}).encode(), 'application/json',
             'api_error_envelope', 'application/json'),
            (json.dumps({'error': SECRET}).encode(), 'application/json', 'api_error_envelope', 'application/json'),
            (json.dumps({'message': SECRET}).encode(), 'application/json', 'api_error_envelope', 'application/json'),
            (b'{"status":400}', 'application/problem+json', 'api_error_envelope', 'application/problem+json'),
            (b'{}', 'text/plain', 'unexpected_content_type', 'text/plain'),
            (b'{}', None, 'unexpected_content_type', 'missing'),
            (b'{}', SECRET, 'unexpected_content_type', 'other'),
        ]
        for body, header, category, safe_type in cases:
            with self.subTest(category=category, content_type=safe_type):
                response = FakeResponse(body, content_type=header)
                transport, sleep = Mock(return_value=response), Mock()
                client = make_client(token=SECRET, transport=transport, sleep=sleep)
                with self.assertRaises(APIError) as cm:
                    client.call(METHOD, {'query': 'GLDRUBF'})
                self.assert_safe_error(cm.exception, category, content_type=safe_type)
                self.assertTrue(response.closed)
                transport.assert_called_once()
                sleep.assert_not_called()

    def test_content_type_parameters_and_case_do_not_leak(self):
        response = FakeResponse(b'{"instruments":[]}', content_type='APPLICATION/JSON; charset=utf-8; ignored=' + SECRET)
        client = make_client(token=SECRET, transport=Mock(return_value=response))
        self.assertEqual(client.call(METHOD, {}), {'instruments': []})
        self.assertTrue(response.closed)

    def test_http_failures_close_without_reading_bodies(self):
        categories = {302: 'redirect_blocked', 400: 'http_error', 401: 'authentication_error',
                      403: 'permission_denied', 407: 'proxy_authentication_error',
                      408: 'http_timeout', 429: 'rate_limited', 500: 'server_error', 502: 'server_error'}
        for status, category in categories.items():
            with self.subTest(status=status):
                body = io.BytesIO(SECRET.encode())
                body.read = Mock(side_effect=AssertionError('Error body must not be read'))
                headers = Message()
                headers['Content-Type'] = 'text/html; private=' + SECRET
                headers['Location'] = 'https://example.invalid/' + SECRET
                failure = urllib.error.HTTPError('https://example.invalid/' + SECRET, status,
                                                 SECRET, headers, body)
                transport = Mock(side_effect=failure)
                client = make_client(token=SECRET, transport=transport, attempts=1)
                with self.assertRaises(APIError) as cm:
                    client.call(METHOD, {})
                self.assert_safe_error(cm.exception, category, status=status, content_type='text/html')
                self.assertTrue(body.closed)
                body.read.assert_not_called()

    def test_nonraising_http_transport_cannot_return_error_as_success(self):
        for status, category in [(204, 'unexpected_http_status'), (302, 'redirect_blocked'), (503, 'server_error')]:
            with self.subTest(status=status):
                response = FakeResponse(b'{"instruments":[]}', status=status)
                client = make_client(transport=Mock(return_value=response), attempts=1)
                with self.assertRaises(APIError) as cm:
                    client.call(METHOD, {})
                self.assert_safe_error(cm.exception, category, status=status)
                self.assertTrue(response.closed)

    def test_network_proxy_tls_and_protocol_diagnostics(self):
        cases = [
            (urllib.error.URLError(SECRET), 'network_error', True),
            (urllib.error.URLError('Tunnel connection failed: 403 ' + SECRET), 'proxy_tunnel_error', True),
            (urllib.error.URLError(socket.gaierror(-2, SECRET)), 'dns_error', True),
            (urllib.error.URLError(TimeoutError(SECRET)), 'timeout', True),
            (TimeoutError(SECRET), 'timeout', True),
            (ConnectionResetError(SECRET), 'network_error', True),
            (urllib.error.URLError(ssl.SSLCertVerificationError(1, SECRET)), 'tls_verification_error', False),
            (ssl.SSLError(1, SECRET), 'tls_error', False),
            (http.client.BadStatusLine(SECRET), 'http_protocol_error', True),
            (UnicodeEncodeError('ascii', '\u044f', 0, 1, SECRET), 'request_encoding_error', False),
        ]
        for failure, category, retryable in cases:
            with self.subTest(category=category):
                client = make_client(token=SECRET, transport=Mock(side_effect=failure), attempts=1)
                with self.assertRaises(APIError) as cm:
                    client.call(METHOD, {})
                self.assert_safe_error(cm.exception, category, status=None, content_type='unknown')
                self.assertEqual(cm.exception.retryable, retryable)

    def test_tls_verification_failure_is_not_retried_or_bypassed(self):
        transport = Mock(side_effect=urllib.error.URLError(ssl.SSLCertVerificationError(1, SECRET)))
        sleep = Mock()
        client = make_client(token=SECRET, transport=transport, sleep=sleep, attempts=3)
        with self.assertRaises(APIError) as cm:
            client.call(METHOD, {})
        self.assert_safe_error(cm.exception, 'tls_verification_error', status=None, content_type='unknown')
        transport.assert_called_once()
        sleep.assert_not_called()

    def test_transient_http_and_timeout_errors_retry_then_succeed(self):
        for failure in [urllib.error.HTTPError('https://example.invalid', 503, SECRET, {}, None),
                        urllib.error.HTTPError('https://example.invalid', 429, SECRET, {}, None),
                        TimeoutError(SECRET)]:
            with self.subTest(failure_type=type(failure).__name__):
                response = FakeResponse(b'{"instruments":[]}')
                transport = Mock(side_effect=[failure, response])
                sleep = Mock()
                client = make_client(token=SECRET, transport=transport, sleep=sleep, attempts=2)
                self.assertEqual(client.call(METHOD, {}), {'instruments': []})
                self.assertEqual(transport.call_count, 2)
                sleep.assert_called_once_with(1)
                self.assertTrue(response.closed)

    def test_partial_response_is_closed_and_retried(self):
        partial = FakeResponse(b'')
        partial.read = Mock(side_effect=http.client.IncompleteRead(SECRET.encode(), 100))
        complete = FakeResponse(b'{"instruments":[]}')
        transport = Mock(side_effect=[partial, complete])
        client = make_client(token=SECRET, transport=transport, sleep=lambda _: None)
        self.assertEqual(client.call(METHOD, {}), {'instruments': []})
        self.assertTrue(partial.closed)
        self.assertTrue(complete.closed)
        self.assertEqual(transport.call_count, 2)

    def test_exhausted_partial_response_reports_last_http_metadata(self):
        response = FakeResponse(b'')
        response.read = Mock(side_effect=http.client.IncompleteRead(SECRET.encode(), 100))
        client = make_client(token=SECRET, transport=Mock(return_value=response), attempts=1)
        with self.assertRaises(APIError) as cm:
            client.call(METHOD, {})
        self.assert_safe_error(cm.exception, 'incomplete_response')
        self.assertTrue(response.closed)

    def test_retry_does_not_reuse_old_http_status_or_headers(self):
        headers = Message()
        headers['Content-Type'] = 'text/html'
        transport = Mock(side_effect=[urllib.error.HTTPError('https://example.invalid', 503, SECRET, headers, None),
                                      urllib.error.URLError(TimeoutError(SECRET))])
        client = make_client(token=SECRET, transport=transport, sleep=lambda _: None, attempts=2)
        with self.assertRaises(APIError) as cm:
            client.call(METHOD, {})
        self.assert_safe_error(cm.exception, 'timeout', status=None, content_type='unknown')

    def test_size_limit_remains_enforced(self):
        response = FakeResponse(b'x' * 100)
        response.read = Mock(wraps=response.read)
        client = make_client(token=SECRET, transport=Mock(return_value=response))
        with patch('src.gldrubf_bot.api.MAX_RESPONSE_BYTES', 32):
            with self.assertRaises(APIError) as cm:
                client.call(METHOD, {})
        self.assert_safe_error(cm.exception, 'response_too_large')
        response.read.assert_called_once_with(33)
        self.assertTrue(response.closed)

    def test_redirect_handler_refuses_to_forward_authorization(self):
        import urllib.request
        request = urllib.request.Request('https://invest-public-api.tbank.ru',
                                         headers={'Authorization': 'Bearer ' + SECRET})
        self.assertIsNone(NoRedirect().redirect_request(request, None, 302, '', {},
                                                       'https://example.invalid/' + SECRET))

    def test_cli_identifies_each_failed_method_and_stops_before_later_calls(self):
        responses = {
            METHOD: {'instruments': [{'uid': UID, 'ticker': 'GLDRUBF', 'instrumentType': 'futures'}]},
            'InstrumentsService/FutureBy': {'instrument': dict(uid=UID, ticker='GLDRUBF', lot=1,
                currency='rub', figi='fixture', exchange='MOEX_FUT', minPriceIncrement=q(1))},
            'InstrumentsService/TradingSchedules': {'exchanges': [{'exchange': 'MOEX_FUT', 'days': [day()]}]},
            'MarketDataService/GetCandles': {'candles': [candle()]},
        }
        methods = list(responses)
        for index, failed_method in enumerate(methods):
            with self.subTest(method=failed_method), TemporaryDirectory() as directory:
                calls = []

                def transport(request, timeout):
                    method = request.full_url.split('contract.v1.')[1]
                    calls.append(method)
                    if method == failed_method:
                        return FakeResponse(('<html>' + SECRET).encode(), content_type='text/html')
                    return FakeResponse(json.dumps(responses[method]).encode())

                client = make_client(token=SECRET, transport=transport)
                with patch('src.gldrubf_bot.cli.ReadOnlyClient', return_value=client), \
                     patch('src.gldrubf_bot.cli.datetime', wraps=datetime) as clock:
                    clock.now.return_value = datetime(2026, 1, 2, 19, tzinfo=UTC)
                    with self.assertRaises(SystemExit) as cm:
                        main(['fetch', '--from', '2026-01-02T00:00:00Z', '--to', '2026-01-02T19:00:00Z',
                              '--interval', 'day', '--output', str(Path(directory) / 'export')])
                self.assertIn('method=' + failed_method, str(cm.exception))
                self.assertIn('http_status=200', str(cm.exception))
                self.assertIn('content_type=text/html', str(cm.exception))
                self.assertNotIn(SECRET, ''.join(traceback.format_exception(cm.exception)))
                self.assertEqual(calls, methods[:index + 1])
                self.assertFalse(list(Path(directory).rglob('*.csv')))
