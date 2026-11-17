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

    def test_reported_gldrubf_codes_match_case_only_and_preserve_request(self):
        # Identifiers reported by the user; session data below remains synthetic.
        for requested, returned in [('forts_futures_weekend','FORTS_FUTURES_WEEKEND'),
                                    ('FORTS_FUTURES_WEEKEND','forts_futures_weekend'),
                                    ('FoRtS_Futures_Weekend','fOrTs_FUTURES_weekend')]:
            response = {'exchanges':[dict(exchange=returned,days=[day('2026-11-17')])]}
            for method in ('current_sessions','schedules'):
                with self.subTest(requested=requested,returned=returned,method=method):
                    client = self.client(response)
                    sessions = (client.current_sessions(requested,NOW) if method=='current_sessions' else
                                client.schedules(requested,instant('2026-11-17T00:00:00Z'),
                                                 instant('2026-11-18T00:00:00Z')))
                    self.assertTrue(next(iter(sessions.values())).contains(NOW))
                    self.assertEqual(json.loads(self.transport.call_args.args[0].data)['exchange'],requested)

    def test_casefold_collision_stops_even_with_one_literal_match(self):
        requested = 'forts_futures_weekend'
        client = self.client({'exchanges':[dict(exchange=code,days=[day('2026-11-17')])
                                          for code in [requested,requested.upper()]]})
        with self.assertRaises(DataError) as cm:client.current_sessions(requested,NOW)
        self.assertIn('"exact_matches": 1',str(cm.exception))
        self.assertIn('"casefold_matches": 2',str(cm.exception))
        self.assertIn('calendar_exchange_ambiguous',str(cm.exception))

    def test_casefold_has_no_aliases_whitespace_or_punctuation_heuristics(self):
        for returned in ('FORTS','MOEX','FORTS_EVENING','FORTS_FUTURES_WEEKEND ',
                         'FORTS-FUTURES-WEEKEND'):
            with self.subTest(returned=returned),self.assertRaises(DataError) as cm:
                self.client({'exchanges':[dict(exchange=returned,days=[day('2026-11-17')])]}).current_sessions(
                    'forts_futures_weekend',NOW)
            self.assertIn('"casefold_matches": 0',str(cm.exception))

    def test_selected_casefold_record_keeps_windows_clearing_auctions_and_detailed_intervals(self):
        raw = day('2026-11-17')
        raw.update(clearingStartTime='2026-11-17T12:00:00Z',clearingEndTime='2026-11-17T12:10:00Z',
                   eveningStartTime='2026-11-17T19:00:00Z',eveningEndTime='2026-11-17T21:00:00Z',
                   openingAuctionStartTime='2026-11-17T07:00:00Z',openingAuctionEndTime='2026-11-17T07:05:00Z',
                   closingAuctionStartTime='2026-11-17T17:50:00Z',closingAuctionEndTime='2026-11-17T18:00:00Z',
                   intervals=[dict(type='fixture',interval=dict(startTs='2026-11-17T07:00:00Z',
                                                               endTs='2026-11-17T18:00:00Z'))])
        response = {'exchanges':[dict(exchange='FORTS',days=None),
                                dict(exchange='FORTS_FUTURES_WEEKEND',days=[raw])]}
        session = next(iter(self.client(response).current_sessions('forts_futures_weekend',NOW).values()))
        for clock, allowed in [('07:02',False),('07:05',True),('12:05',False),('12:10',True),
                               ('17:55',False),('18:30',False),('20:00',True),('21:00',False)]:
            self.assertEqual(session.contains(instant('2026-11-17T'+clock+':00Z')),allowed)
        self.assertEqual(session.close,instant('2026-11-17T21:00:00Z'))

    def test_matched_real_codes_do_not_bypass_days_validation(self):
        unsupported = day('2026-11-17')
        unsupported['intervals'] = [dict(type='unknown',interval=dict(
            startTs='2026-11-17T22:00:00Z',endTs='2026-11-17T23:00:00Z'))]
        incomplete = day('2026-11-17');incomplete.pop('endTime')
        conflicting = [day('2026-11-17'),day('2026-11-17',trading=False)]
        for days in (None,[None],[unsupported],[incomplete],conflicting):
            with self.subTest(days=days),self.assertRaises(DataError):
                self.client({'exchanges':[dict(exchange='FORTS_FUTURES_WEEKEND',days=days)]}).current_sessions(
                    'forts_futures_weekend',NOW)

    def test_current_diagnostic_separates_literal_from_casefold_matches(self):
        client = self.diagnostic_client([{'exchanges':[{'exchange':'FORTS_FUTURES_WEEKEND'}]}]*2)
        client.instrument.return_value = Instrument('fixture','figi','GLDRUBF','forts_futures_weekend',1)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(diag.run_current(client,NOW),0)
        rows = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(all(r['exact_matches']==0 and r['casefold_matches']==1 for r in rows))
        self.assertTrue(all(r['category']=='calendar_casefold_match' for r in rows))

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
        self.assertEqual([r['category'] for r in rows],['calendar_exchange_missing','calendar_casefold_match'])
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
        self.assertEqual(output.getvalue().count('calendar_casefold_match'),2)
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
