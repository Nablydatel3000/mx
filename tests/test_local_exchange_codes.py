"""Synthetic terminal tests; never use a real API token or save raw identifiers."""
import contextlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from src.gldrubf_bot import diagnose_exchange_codes as diag
from src.gldrubf_bot.api import APIError, Instrument, current_schedule_request
from src.gldrubf_bot.models import instant
from tests.test_api import FakeResponse, make_client, q


SECRET = 't.synthetic-console-credential'
NOW = instant('2026-11-17T10:00:00Z')
CODE = 'fixture_Future_section'  # Deliberately outside the public logging allowlist.
OTHER = 'fixture_Calendar_section'


class Console(io.StringIO):
    def isatty(self):
        return True


class LocalExchangeCodeTests(unittest.TestCase):
    def test_actual_identifiers_and_production_discovery_request_contract(self):
        requests = []
        def transport(request, timeout):
            method = request.full_url.split('contract.v1.')[1]
            payload = json.loads(request.data)
            requests.append((method,payload))
            if method.endswith('/FindInstrument'):
                result = {'instruments':[dict(uid='fixture-uid',ticker='GLDRUBF',instrumentType='futures')]}
            elif method.endswith('/FutureBy'):
                result = {'instrument':dict(uid='fixture-uid',figi='fixture-figi',ticker='GLDRUBF',
                    lot=1,currency='rub',exchange=CODE,minPriceIncrement=q(1))}
            else:
                self.assertEqual(method,diag.METHOD)
                codes = [OTHER] if 'exchange' in payload else [CODE,OTHER,CODE]
                result = {'exchanges':[dict(exchange=code,private='account-sensitive',days=[]) for code in codes],
                          'Authorization':SECRET,'accountId':'account-sensitive'}
            return FakeResponse(json.dumps(result).encode())
        client = make_client(token=SECRET,transport=transport,attempts=1)
        with patch.object(diag.sys,'stdin',Console()),contextlib.redirect_stdout(Console()) as output:
            self.assertEqual(diag.run(client,NOW,token=SECRET),1)
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(lines[0]['instrument_exchange'],CODE)
        self.assertEqual(lines[1]['returned_exchanges'],[OTHER])
        self.assertEqual(lines[2]['returned_exchanges'],[CODE,OTHER,CODE])
        self.assertEqual([row['exact_matches'] for row in lines[1:]],[0,2])
        self.assertEqual(len(requests),4)
        expected = current_schedule_request(CODE,NOW)
        self.assertEqual(requests[-2][1],expected)
        self.assertEqual(requests[-1][1],{k:v for k,v in expected.items() if k!='exchange'})
        self.assertEqual(requests[1][1],{'idType':'INSTRUMENT_ID_TYPE_UID','id':'fixture-uid'})
        for private in (SECRET,'Authorization','account-sensitive','fixture-uid','fixture-figi'):
            self.assertNotIn(private,output.getvalue())

    def test_reflected_token_malformed_data_and_exception_text_are_not_printed(self):
        for first in ({'exchanges':[{'exchange':SECRET},{'exchange':'MOEX_'+SECRET}]},
                      {'exchanges':[{'exchange':'bad\nAuthorization '+SECRET}]},
                      {'exchanges':[None]}, {'exchanges':None},
                      APIError(SECRET,http_status=502,method=diag.METHOD)):
            client = Mock()
            client.instrument.return_value = Instrument('uid','figi','GLDRUBF',CODE,1)
            client.call.side_effect = [first,{'exchanges':[{'exchange':CODE}]}]
            with self.subTest(response_type=type(first).__name__), \
                 patch.object(diag.sys,'stdin',Console()),contextlib.redirect_stdout(Console()) as output:
                self.assertEqual(diag.run(client,NOW,token=SECRET),1)
            self.assertNotIn(SECRET,output.getvalue())
            self.assertNotIn('Authorization',output.getvalue())
            self.assertEqual(client.call.call_count,2)
        self.assertEqual(diag.local_code('x'*129,SECRET),'[redacted]')

    def test_redirected_console_refused_before_hidden_input_or_any_api(self):
        for stdin,stdout in [(Console(),io.StringIO()),(io.StringIO(),Console())]:
            with patch.object(diag.sys,'stdin',stdin),patch.object(diag.sys,'stdout',stdout), \
                 patch.object(diag,'read_token') as token,patch.object(diag,'ReadOnlyClient') as factory:
                self.assertEqual(diag.main(),1)
            token.assert_not_called();factory.assert_not_called()
            self.assertNotIn(CODE,stdout.getvalue())

    def test_main_hidden_input_restores_environment_and_emits_no_traceback(self):
        with patch.object(diag.sys,'stdin',Console()),contextlib.redirect_stdout(Console()) as output, \
             patch.object(diag,'read_token',return_value=SECRET) as hidden, \
             patch.object(diag,'ReadOnlyClient') as factory, \
             patch.dict(os.environ,{'T_INVEST_TOKEN':'prior-binding'}):
            def construct(**kwargs):
                self.assertEqual(kwargs,dict(attempts=1))
                self.assertEqual(os.environ['T_INVEST_TOKEN'],SECRET)
                raise RuntimeError(SECRET)
            factory.side_effect = construct
            self.assertEqual(diag.main(),1)
            hidden.assert_called_once()
            self.assertEqual(os.environ['T_INVEST_TOKEN'],'prior-binding')
        self.assertNotIn(SECRET,output.getvalue())
        self.assertNotIn('Traceback',output.getvalue())

    def test_local_launcher_crlf_no_token_or_output_redirection(self):
        data = (Path(__file__).resolve().parents[1]/'diagnose_exchange_codes_local_windows.cmd').read_bytes()
        self.assertEqual(data.count(b'\n'),data.count(b'\r\n'))
        self.assertIn(b'-m src.gldrubf_bot.diagnose_exchange_codes\r\n',data)
        self.assertIn(b'exit /b %DIAG_EXIT%',data)
        self.assertNotIn(b'T_INVEST_TOKEN',data)
        self.assertNotIn(b'.log',data)
        # Only pause's fixed output is redirected, never the diagnostic process.
        command = next(line for line in data.splitlines() if b'-m src.' in line)
        self.assertNotIn(b'>',command)
