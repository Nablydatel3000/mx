"""No exchange mapping or execution bypass; public-only calendar diagnostics."""
import contextlib
import io
import json
import os
from pathlib import Path
import traceback
import unittest
from unittest.mock import Mock, patch

from src.gldrubf_bot.api import APIError, Instrument, calendar_response_summary, current_schedule_request
from src.gldrubf_bot import diagnose_schedules as diag
from src.gldrubf_bot.models import DataError, instant
from tests.test_api import FakeResponse, day, make_client


NOW = instant('2026-11-17T10:00:00Z')
CODE = 'FORTS_EVENING'  # Documented section, NOT a verified real GLDRUBF response.
SECRET = 't.synthetic-calendar-secret'


class CurrentSessionsTests(unittest.TestCase):
    def client(self, response, token=SECRET):
        self.transport = Mock(return_value=FakeResponse(json.dumps(response).encode()))
        return make_client(token=token, transport=self.transport, attempts=1)

    def test_request_uses_exact_future_by_section_and_current_two_day_window(self):
        payload = current_schedule_request(CODE, NOW)
        self.assertEqual(payload, dict(exchange=CODE, **{'from':'2026-11-17T00:00:00.000000Z',
                                                       'to':'2026-11-19T00:00:00.000000Z'}))
        client = self.client({'exchanges':[dict(exchange=CODE, days=[day('2026-11-17')])]})
        sessions = client.current_sessions(CODE, NOW)
        self.assertTrue(next(iter(sessions.values())).contains(NOW))
        self.assertEqual(json.loads(self.transport.call_args.args[0].data), payload)
        self.transport.assert_called_once()

    def test_missing_exact_code_reports_codes_and_does_not_fallback(self):
        client = self.client({'exchanges':[dict(exchange='FORTS', days=[day('2026-11-17')])]})
        with self.assertRaises(DataError) as cm:
            client.current_sessions(CODE, NOW)
        text = str(cm.exception)
        self.assertIn('"requested_exchange": "FORTS_EVENING"', text)
        self.assertIn('"returned_exchanges": ["FORTS"]', text)
        self.assertIn('"exact_matches": 0', text)
        self.assertIn('calendar_exchange_missing', text)
        self.transport.assert_called_once()  # No unfiltered selection in PAPER.

    def test_duplicate_exact_records_are_ambiguous_even_when_days_are_identical(self):
        record = dict(exchange=CODE, days=[day('2026-11-17')])
        client = self.client({'exchanges':[record,record]})
        with self.assertRaises(DataError) as cm:
            client.current_sessions(CODE, NOW)
        self.assertIn('"exact_matches": 2', str(cm.exception))
        self.assertIn('calendar_exchange_ambiguous', str(cm.exception))

    def test_omitted_and_empty_exchanges_are_valid_empty_lists_but_not_permission(self):
        for response in ({}, {'exchanges':[]}):
            with self.subTest(response=response), self.assertRaises(DataError) as cm:
                self.client(response).current_sessions(CODE, NOW)
            self.assertIn('"response_shape_valid": true', str(cm.exception))
            self.assertIn('"returned_count": 0', str(cm.exception))

    def test_malformed_success_envelopes_and_days_fail_closed(self):
        for exchanges in (None, {}, 'not-a-list', [None], [{}], [{'exchange':[]}],
                          [dict(exchange=CODE,days=None)], [dict(exchange=CODE,days=[None])]):
            with self.subTest(exchanges=exchanges), self.assertRaises(DataError):
                self.client({'exchanges':exchanges}).current_sessions(CODE, NOW)
        for exchange in ('', ' ', None, 123):
            client = self.client({})
            with self.assertRaises(DataError):client.current_sessions(exchange,NOW)
            self.transport.assert_not_called()

    def test_unknown_exchange_redaction_never_changes_internal_matching(self):
        client = self.client({'exchanges':[dict(exchange='fixture_unknown_section',days=[day('2026-11-17')])]})
        self.assertTrue(client.current_sessions('fixture_unknown_section',NOW))
        self.assertEqual(json.loads(self.transport.call_args.args[0].data)['exchange'],'fixture_unknown_section')

    def test_response_secrets_and_reflected_public_token_cannot_escape_diagnostics(self):
        for token in (SECRET,'FORTS','MOEX'):
            response = {'exchanges':[{'exchange':token,'days':SECRET,'Authorization':token},
                                     {'exchange':'FORTS_EVENING','body':token}], 'private':token}
            requested = token if token == SECRET else 'MOEX_PLUS'
            client = self.client(response,token)
            with self.assertRaises(DataError) as cm:client.current_sessions(requested,NOW)
            text = ''.join(traceback.format_exception(cm.exception))
            self.assertNotIn(token,text)
            self.assertNotIn('Authorization',text)
            self.assertNotIn(SECRET,text)
        large = {'exchanges':[{'exchange':SECRET+str(n)} for n in range(1000)]}
        summary = calendar_response_summary(large,SECRET,token=SECRET)
        self.assertEqual(summary['returned_exchanges'],['other'])
        self.assertLess(len(json.dumps(summary)),250)

    def diagnostic_client(self, responses):
        client = self.client({})
        client.instrument = Mock(return_value=Instrument('fixture','figi','GLDRUBF',CODE,1))
        self.transport.side_effect = [FakeResponse(json.dumps(r).encode()) if isinstance(r,dict) else r
                                     for r in responses]
        return client

    def test_current_control_omits_filter_and_never_turns_missing_filtered_match_into_success(self):
        client = self.diagnostic_client([{'exchanges':[]},{'exchanges':[{'exchange':CODE}]}])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(diag.run_current(client,NOW),1)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([r['exact_matches'] for r in rows],[0,1])
        self.assertEqual([r['category'] for r in rows],['calendar_exchange_missing','calendar_exact_match'])
        payloads = [json.loads(c.args[0].data) for c in self.transport.call_args_list]
        self.assertEqual(payloads[0],current_schedule_request(CODE,NOW))
        self.assertEqual(payloads[1],{k:v for k,v in payloads[0].items() if k!='exchange'})
        self.assertNotIn(SECRET,output.getvalue())
        self.assertEqual(self.transport.call_count,2)

    def test_unfiltered_response_with_different_section_never_establishes_mapping(self):
        client = self.diagnostic_client([{}, {'exchanges':[{'exchange':'FORTS'}]}])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(diag.run_current(client,NOW),1)
        self.assertTrue(all(json.loads(line)['exact_matches']==0 for line in output.getvalue().splitlines()))

    def test_control_success_requires_one_exact_match_in_both_responses(self):
        client = self.diagnostic_client([{'exchanges':[{'exchange':CODE}]},
                                        {'exchanges':[{'exchange':'FORTS'},{'exchange':CODE}]}])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(diag.run_current(client,NOW),0)
        self.assertEqual(output.getvalue().count('calendar_exact_match'),2)
        # This probes only codes; it deliberately does not certify trading windows.

    def test_current_control_continues_after_api_error_without_logging_exception_text(self):
        error = APIError(SECRET,method=diag.METHOD,http_status=400,grpc_code=3,api_code=50001)
        client = self.diagnostic_client([error,{'exchanges':[{'exchange':CODE}]}])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(diag.run_current(client,NOW),1)
        self.assertIn('"api_code": 50001',output.getvalue())
        self.assertNotIn(SECRET,output.getvalue())
        self.assertEqual(self.transport.call_count,2)

    def test_windows_current_mode_uses_hidden_token_no_dates_and_restores_environment(self):
        with patch('builtins.input') as prompt, patch.object(diag,'read_token',return_value=SECRET) as hidden, \
             patch.object(diag,'ReadOnlyClient') as factory, patch.object(diag,'run_current',return_value=1) as run, \
             patch.dict(os.environ,{'T_INVEST_TOKEN':'previous-binding'}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(diag.main(['--current']),1)
            prompt.assert_not_called();hidden.assert_called_once()
            factory.assert_called_once_with(attempts=1)
            run.assert_called_once()
            self.assertEqual(os.environ['T_INVEST_TOKEN'],'previous-binding')
        data = (Path(__file__).resolve().parents[1]/'diagnose_current_sessions_windows.cmd').read_bytes()
        self.assertEqual(data.count(b'\n'),data.count(b'\r\n'))
        self.assertIn(b'diagnose_schedules --current\r\n',data)
        self.assertIn(b'exit /b %DIAG_EXIT%',data)
        self.assertNotIn(b'T_INVEST_TOKEN',data)
