"""Official schedule limitations, numeric-only errors and Windows diagnostic probes."""
import contextlib
import http.client
import io
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import traceback
import unittest
import urllib.error
from unittest.mock import Mock, patch

from src.gldrubf_bot.api import APIError, Instrument, public_exchange, schedule_request
from src.gldrubf_bot.api_error_codes import ERROR_BODY_LIMIT, extract_codes, read_error_codes
from src.gldrubf_bot.cli import main
from src.gldrubf_bot import diagnose_schedules as diag
from src.gldrubf_bot.models import DataError, UTC, instant, iso
from tests.test_api import FakeResponse, make_client, q


METHOD = 'InstrumentsService/TradingSchedules'
SECRET = 't.fake-sensitive-never-print'
TODAY = datetime(2026, 11, 5, 12, tzinfo=UTC)
START = datetime(2024, 11, 5, tzinfo=UTC)
END = datetime(2026, 11, 5, tzinfo=UTC)


class TradingSchedulesTests(unittest.TestCase):
    def test_http_400_extracts_numeric_codes_parameters_and_closes(self):
        body = io.BytesIO(json.dumps({'code': 3, 'description': '30003', 'message': SECRET}).encode())
        body.read = Mock(wraps=body.read)
        error = urllib.error.HTTPError('https://example.invalid/' + SECRET, 400, SECRET,
                                       {'Content-Type': 'application/json'}, body)
        transport = Mock(side_effect=error)
        client = make_client(token=SECRET, transport=transport)
        with self.assertRaises(APIError) as cm:
            client.schedules('FORTS_EVENING', START, END)
        failure = cm.exception
        self.assertEqual((failure.http_status, failure.grpc_code, failure.api_code), (400, 3, 30003))
        self.assertFalse(failure.retryable)
        expected = {'exchange': 'FORTS_EVENING', 'from': iso(START), 'to': iso(START + timedelta(days=7))}
        self.assertEqual(json.loads(transport.call_args.args[0].data), expected)
        self.assertEqual(failure.request_parameters, expected)
        self.assertIn('api_code=30003', str(failure))
        self.assertIn('from_utc=' + iso(START), str(failure))
        self.assertNotIn(SECRET, ''.join(traceback.format_exception(failure)))
        self.assertNotIn('Authorization', str(failure))
        self.assertTrue(body.closed)
        body.read.assert_called_once_with(ERROR_BODY_LIMIT + 1)
        transport.assert_called_once()

    def test_numeric_codes_whitelist_rejects_arbitrary_values(self):
        for value, expected in [
            ({'code': 3, 'description': 30003}, (3, 30003)),
            ({'code': '3', 'description': '50001'}, (3, 50001)),
            ({'code': '30003'}, (None, 30003)),
            ({'code': True, 'description': SECRET}, (None, None)),
            ({'code': 99999, 'description': '30003 ' + SECRET}, (None, None)),
            ({'code': SECRET, 'message': SECRET}, (None, None)),
            ([], (None, None)),
        ]:
            with self.subTest(expected=expected):
                self.assertEqual(extract_codes(value), expected)

    def test_error_body_parser_is_bounded_and_fails_closed(self):
        cases = [(b'not JSON ' + SECRET.encode(), 'application/json'),
                 (b'', 'application/json'), (b'[]', 'application/json'),
                 (b'x' * (ERROR_BODY_LIMIT + 1), 'application/json'),
                 (b'\xff\x00', 'application/json')]
        for raw, content_type in cases:
            with self.subTest(size=len(raw)):
                response = FakeResponse(raw)
                response.read = Mock(wraps=response.read)
                self.assertEqual(read_error_codes(response, content_type), (None, None))
                response.read.assert_called_once_with(ERROR_BODY_LIMIT + 1)
                response.close()
        for failure in (TimeoutError(SECRET), http.client.IncompleteRead(SECRET.encode(), 100)):
            response = Mock()
            response.read.side_effect = failure
            self.assertEqual(read_error_codes(response, 'application/json'), (None, None))
        response = Mock()
        self.assertEqual(read_error_codes(response, 'text/html'), (None, None))
        response.read.assert_not_called()

    def test_200_error_envelope_has_codes_and_is_not_success(self):
        response = FakeResponse(b'{"code":3,"description":"30003"}')
        client = make_client(transport=Mock(return_value=response))
        with self.assertRaises(APIError) as cm:
            client.call(METHOD, schedule_request('FORTS_EVENING', START, END))
        self.assertEqual((cm.exception.category, cm.exception.grpc_code, cm.exception.api_code),
                         ('api_error_envelope', 3, 30003))
        self.assertTrue(response.closed)

    def test_unknown_exchange_and_invalid_dates_cannot_leak_in_errors(self):
        response = FakeResponse(b'{"code":3,"description":"30003","message":"private"}', status=400)
        client = make_client(token=SECRET, transport=Mock(return_value=response))
        with self.assertRaises(APIError) as cm:
            client.call(METHOD, {'exchange': SECRET, 'from': SECRET, 'to': SECRET})
        self.assertEqual(cm.exception.request_parameters, {'exchange': 'other', 'from': 'invalid', 'to': 'invalid'})
        self.assertNotIn(SECRET, str(cm.exception))
        self.assertEqual(public_exchange('FORTS_EVENING'), 'FORTS_EVENING')
        self.assertEqual(public_exchange(SECRET), 'other')

    def test_exchange_is_taken_from_future_by_without_mapping(self):
        requests = []
        def transport(request, timeout):
            payload = json.loads(request.data)
            requests.append(payload)
            if request.full_url.endswith('/FindInstrument'):
                return FakeResponse(b'{"instruments":[{"uid":"fixture-uid","ticker":"GLDRUBF","instrumentType":"futures"}]}')
            if request.full_url.endswith('/FutureBy'):
                return FakeResponse(json.dumps({'instrument': dict(uid='fixture-uid', ticker='GLDRUBF',
                    lot=1, currency='rub', figi='fixture', exchange='FORTS_EVENING', minPriceIncrement=q(1))}).encode())
            return FakeResponse(b'{"code":3,"description":"30003"}', status=400)
        client = make_client(transport=transport, attempts=1)
        instrument = client.instrument()
        self.assertEqual(instrument.exchange, 'FORTS_EVENING')
        with self.assertRaises(APIError):
            client.schedules(instrument.exchange, START, END)
        self.assertEqual(requests[-1]['exchange'], 'FORTS_EVENING')
        self.assertEqual(requests[1]['idType'], 'INSTRUMENT_ID_TYPE_UID')

    def test_first_chunk_has_utc_midnight_and_at_most_seven_days(self):
        payload = schedule_request('FORTS_EVENING', '2026-11-05T05:00:00+03:00', '2026-12-01T00:00:00Z')
        self.assertEqual(payload, {'exchange': 'FORTS_EVENING', 'from': '2026-11-05T00:00:00.000000Z',
                                  'to': '2026-11-12T00:00:00.000000Z'})
        short = schedule_request('FORTS_EVENING', TODAY, TODAY + timedelta(hours=1))
        self.assertEqual(instant(short['to']), TODAY + timedelta(hours=1))
        with self.assertRaises(DataError):
            schedule_request('FORTS_EVENING', END, START)

    def test_historical_day_export_uses_getcandles_without_calendar(self):
        calls = []
        def transport(request, timeout):
            method = request.full_url.split('contract.v1.')[1]
            calls.append(method)
            if method.endswith('/FindInstrument'):
                result = {'instruments': [dict(uid='fixture', ticker='GLDRUBF', instrumentType='futures')]}
            elif method.endswith('/FutureBy'):
                result = {'instrument': dict(uid='fixture', figi='figi', ticker='GLDRUBF', lot=1,
                    currency='rub', exchange='FORTS_EVENING', minPriceIncrement=q(1))}
            else:
                self.assertEqual(method, 'MarketDataService/GetCandles')
                result = {'candles': []}
            return FakeResponse(json.dumps(result).encode())
        client = make_client(transport=transport)
        with TemporaryDirectory() as directory, patch('src.gldrubf_bot.cli.ReadOnlyClient', return_value=client), \
             contextlib.redirect_stdout(io.StringIO()):
            main(['fetch', '--from', iso(START), '--to', iso(END), '--interval', 'day', '--output', directory])
            report = json.loads((Path(directory)/'validation.json').read_text())
            self.assertFalse(report['coverage_confirmed'])
            self.assertEqual(report['count'], 0)
        self.assertNotIn(METHOD, calls)

    def test_future_or_invalid_range_rejected_before_http(self):
        with patch('src.gldrubf_bot.cli.datetime', wraps=datetime) as clock, \
             patch('src.gldrubf_bot.cli.ReadOnlyClient') as client:
            clock.now.return_value = TODAY
            for start, end in [(START, TODAY + timedelta(days=1)), (END, START)]:
                with self.assertRaises(SystemExit):
                    main(['fetch', '--from', iso(start), '--to', iso(end), '--interval', 'day'])
        client.assert_not_called()

    def test_historical_minute_export_still_uses_candles_without_calendar(self):
        client = Mock()
        client.instrument.return_value = Instrument('fixture', 'figi', 'GLDRUBF', 'FORTS_EVENING', 1)
        client.candles.return_value = [dict(time=iso(START), open=q(100), high=q(100), low=q(100),
                                          close=q(100), volume='1', isComplete=True)]
        with TemporaryDirectory() as directory, \
             patch('src.gldrubf_bot.cli.datetime', wraps=datetime) as clock, \
             patch('src.gldrubf_bot.cli.ReadOnlyClient', return_value=client), \
             contextlib.redirect_stdout(io.StringIO()):
            clock.now.return_value = TODAY
            main(['fetch', '--from', iso(START), '--to', iso(END), '--interval', '1min', '--output', directory])
            self.assertTrue((Path(directory) / 'minute.csv').is_file())
        client.candles.assert_called_once_with('fixture', START, END, 'CANDLE_INTERVAL_1_MIN')
        client.schedules.assert_not_called()
        client.completed_daily.assert_not_called()

    def test_diagnostic_controls_do_not_turn_failed_history_into_success(self):
        client = Mock()
        listed = datetime(2025, 1, 1, tzinfo=UTC)
        client.instrument.return_value = Instrument('fixture', 'figi', 'GLDRUBF', 'FORTS_EVENING', 1, listed)
        failure = APIError(SECRET, method=METHOD, http_status=400, grpc_code=3, api_code=30003)
        client.call.side_effect = [failure, {'exchanges': [{'exchange': 'FORTS_EVENING'}]},
                                   {'exchanges': [{'exchange': 'FORTS_EVENING'}]}]
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = diag.run(client, START, END, TODAY)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(result, 1)
        self.assertEqual(client.call.call_count, 3)
        requests = [call.args[1] for call in client.call.call_args_list]
        self.assertEqual(instant(requests[0]['from']), listed)
        self.assertEqual(instant(requests[0]['to']), listed + timedelta(days=7))
        self.assertEqual(instant(requests[1]['from']), TODAY.replace(hour=0))
        self.assertEqual(instant(requests[1]['to']), TODAY.replace(hour=0) + timedelta(days=1))
        self.assertEqual(requests[2], {k: v for k, v in requests[1].items() if k != 'exchange'})
        self.assertEqual(rows[0]['exchange_source'], 'FutureBy')
        self.assertEqual((rows[1]['http_status'], rows[1]['api_code']), (400, 30003))
        self.assertTrue(rows[-1]['instrument_exchange_present'])
        self.assertNotIn(SECRET, output.getvalue())

    def test_discovery_never_maps_to_a_different_section(self):
        client = Mock()
        client.instrument.return_value = Instrument('fixture', 'figi', 'GLDRUBF', 'FORTS_EVENING', 1)
        client.call.return_value = {'exchanges': [{'exchange': 'FORTS'}]}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(diag.run(client, START, END, TODAY), 1)
        self.assertTrue(all(call.args[1].get('exchange', 'omitted') in ('FORTS_EVENING', 'omitted')
                            for call in client.call.call_args_list))
        self.assertEqual(output.getvalue().count('"instrument_exchange_present": false'), 3)

    def test_diagnostic_main_uses_hidden_token_restores_environment_and_sanitizes_failures(self):
        with patch('builtins.input', side_effect=['2024-11-05', '2026-11-05']), \
             patch.object(diag, 'read_token', return_value=SECRET), \
             patch.object(diag, 'ReadOnlyClient') as factory, \
             patch.dict(os.environ, {'T_INVEST_TOKEN': 'previous-binding'}), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            factory.return_value.instrument.side_effect = APIError(SECRET, method=METHOD, http_status=400,
                                                                   grpc_code=3, api_code=30003)
            self.assertEqual(diag.main(), 1)
            factory.assert_called_once_with(attempts=1)
            self.assertEqual(os.environ['T_INVEST_TOKEN'], 'previous-binding')
        self.assertNotIn(SECRET, output.getvalue())
        self.assertIn('"api_code": 30003', output.getvalue())
        self.assertNotIn('Authorization', output.getvalue())

    def test_diagnostic_launcher_has_crlf_and_preserves_exit_code(self):
        data = (Path(__file__).resolve().parents[1] / 'diagnose_trading_schedules_windows.cmd').read_bytes()
        self.assertEqual(data.count(b'\n'), data.count(b'\r\n'))
        self.assertIn(b'-m src.gldrubf_bot.diagnose_schedules\r\n', data)
        self.assertIn(b'exit /b %DIAG_EXIT%', data)
        self.assertNotIn(b'T_INVEST_TOKEN', data)
