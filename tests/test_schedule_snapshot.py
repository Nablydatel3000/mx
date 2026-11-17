"""Sanitized schedule evidence only: synthetic API, no execution/calendar repair."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from src.gldrubf_bot import diagnose_schedule_snapshot as diag
from src.gldrubf_bot.api import Instrument, current_schedule_request
from src.gldrubf_bot.models import DataError, instant
from tests.test_api import FakeResponse, make_client, q


NOW = instant('2026-11-17T10:00:00Z')
CODE = 'forts_futures_weekend'
SECRET = 't.synthetic-snapshot-credential'


def raw_day():
    return dict(date='2026-11-18T00:00:00Z',isTradingDay=True,
        startTime='2026-11-17T21:00:00Z',endTime='2026-11-18T01:00:00Z',
        intervals=[dict(type='Новый тип интервала',interval=dict(
            startTs='2026-11-17T21:00:00.000000001Z',endTs='2026-11-18T00:00:00.000000002Z'))])


def project(days=None):
    return diag.project({'exchanges':[dict(exchange=CODE.upper(),days=[raw_day()] if days is None else days)]},
                        CODE,current_schedule_request(CODE,NOW),token=SECRET)


class ScheduleSnapshotTests(unittest.TestCase):
    def test_pipeline_saves_only_projected_fields_and_current_read_only_requests(self):
        requests = []
        def transport(request, timeout):
            method = request.full_url.split('contract.v1.')[1]
            payload = json.loads(request.data);requests.append((method,payload))
            if method.endswith('/FindInstrument'):
                result = {'instruments':[dict(uid='private-uid',ticker='GLDRUBF',instrumentType='futures')]}
            elif method.endswith('/FutureBy'):
                result = {'instrument':dict(uid='private-uid',figi='private-figi',ticker='GLDRUBF',
                    lot=1,currency='rub',exchange=CODE,minPriceIncrement=q(1))}
            else:
                self.assertEqual(method,diag.METHOD)
                day = raw_day();day.update(uid='private-uid',accountId='private-account')
                day['intervals'][0].update(Authorization=SECRET)
                day['intervals'][0]['interval']['secret'] = SECRET
                result = {'exchanges':[dict(exchange=CODE.upper(),days=[day],uid='private-uid')],
                          'accountId':'private-account','Authorization':SECRET}
            return FakeResponse(json.dumps(result).encode())
        client = make_client(token=SECRET,transport=transport,attempts=1)
        with TemporaryDirectory(prefix='mx snapshot пробел ') as temporary:
            root = Path(temporary)/'snapshot'
            report = diag.capture(client,NOW,root,token=SECRET)
            self.assertEqual(report['status'],'valid_for_diagnostics')
            self.assertFalse(report['execution_permission'])
            self.assertFalse(report['interval_type_semantics_verified'])
            data = json.loads((root/'schedule.json').read_text())
            self.assertEqual(data['exchange'],CODE.upper())
            self.assertEqual(data['requestedExchange'],CODE)
            self.assertEqual(data['days'][0]['date'],'2026-11-18T00:00:00Z')
            span = data['days'][0]['intervals'][0]
            self.assertEqual(span['type'],'Новый тип интервала')
            self.assertEqual(span['interval']['startTs'],'2026-11-17T21:00:00.000000001Z')
            self.assertEqual(len(data['days'][0]),16)  # date, flag, intervals, 13 legacy fields.
            for path in root.iterdir():
                for secret in (SECRET,'Authorization','private-uid','private-figi','private-account','accountId'):
                    self.assertNotIn(secret,path.read_text())
            self.assertEqual({p.name for p in root.iterdir()},{'schedule.json','validation.json'})
            self.assertEqual(diag.validate(data),report)
        self.assertEqual(len(requests),3)
        self.assertEqual(requests[-1],(diag.METHOD,current_schedule_request(CODE,NOW)))

    def test_incomplete_and_reversed_intervals_are_retained_and_flagged_not_repaired(self):
        raw = raw_day();raw.pop('isTradingDay');raw.pop('endTime')
        raw['intervals'].append(dict(interval=dict(startTs='2026-11-18T01:00:00Z',endTs='2026-11-17T21:00:00Z')))
        data = project([raw])
        report = diag.validate(data)
        self.assertEqual(report['status'],'invalid')
        self.assertTrue(report['errors'])
        self.assertTrue(any('missing_boundary' in warning for warning in report['warnings']))
        self.assertIsNone(data['days'][0]['isTradingDay'])
        self.assertIsNone(data['days'][0]['endTime'])
        self.assertIsNone(data['days'][0]['intervals'][1]['type'])
        self.assertEqual(report['intervals_count'],2)
        for days in ([],[{'date':'2026-11-18T00:00:00Z'}]):
            self.assertEqual(diag.validate(project(days))['status'],'needs_review')
        raw['intervals'] = [dict(type='interval_not_supplied',interval=None)]
        missing = project([raw])
        self.assertEqual(missing['days'][0]['intervals'][0]['interval'],dict(startTs=None,endTs=None))
        self.assertEqual(diag.validate(missing)['status'],'needs_review')

    def test_missing_and_unknown_fields_fail_offline_validation(self):
        for mutate in (lambda d:d.update(accountId='private'),
                       lambda d:d['days'][0].update(uid='private'),
                       lambda d:d['days'][0].pop('startTime'),
                       lambda d:d['days'][0]['intervals'][0]['interval'].update(extra='private'),
                       lambda d:d['days'][0].update(isTradingDay='true'),
                       lambda d:d['days'][0].update(date='2026-11-18T00:00:00')):
            data = project();mutate(data)
            with self.assertRaises(DataError):diag.validate(data)

    def test_sensitive_or_malformed_allowed_values_never_create_a_snapshot(self):
        for value in (SECRET,'main '+SECRET,'Bearer '+SECRET,'bad\ncontrol',
                      'user-123e4567-e89b-12d3-a456-426614174000'):
            client = Mock()
            client.instrument.return_value = Instrument('uid','figi','GLDRUBF',CODE,1)
            raw = raw_day();raw['intervals'][0]['type'] = value
            client.call.return_value = {'exchanges':[dict(exchange=CODE.upper(),days=[raw])]}
            with TemporaryDirectory() as temporary:
                output = Path(temporary)/'snapshot'
                with self.subTest(value=value),self.assertRaises(DataError):
                    diag.capture(client,NOW,output,token=SECRET)
                self.assertFalse(output.exists())
        with self.assertRaises(DataError):diag.utc_stamp('2026-11-17T12:00:00.'+SECRET+'Z')

    def test_utc_conversion_nanoseconds_duplicates_and_order_are_preserved(self):
        self.assertEqual(diag.utc_stamp('2026-11-18T00:00:00.123456789+03:00'),
                         '2026-11-17T21:00:00.123456789Z')
        raw = raw_day()
        raw['intervals'].append(dict(type='Новая фаза = X|Y',interval=dict(startTs='2026-11-17T20:00:00Z',
                                                                endTs='2026-11-17T22:00:00Z')))
        data = project([raw,copy.deepcopy(raw)])
        report = diag.validate(data)
        self.assertEqual(report['days_count'],2)
        self.assertEqual(report['intervals_count'],4)
        self.assertIn('duplicate_dates_preserved',report['warnings'])
        self.assertTrue(any('not_chronological' in warning for warning in report['warnings']))
        self.assertEqual(data['days'][0]['intervals'][1]['type'],'Новая фаза = X|Y')
        self.assertFalse(report['coverage_confirmed'])
        self.assertLess(diag.time_key('2026-11-17T00:00:00.000000001Z'),
                        diag.time_key('2026-11-17T00:00:00.000000002Z'))

    def test_selection_is_unique_and_existing_directory_is_not_overwritten(self):
        for records in ([],[dict(exchange='FORTS',days=[])],
                        [dict(exchange=CODE,days=[]),dict(exchange=CODE.upper(),days=[])]):
            with self.assertRaises(DataError):
                diag.project({'exchanges':records},CODE,current_schedule_request(CODE,NOW),token=SECRET)
        client = Mock();client.instrument.return_value = Instrument('uid','figi','GLDRUBF',CODE,1)
        client.call.return_value = {'exchanges':[dict(exchange=CODE.upper(),days=[raw_day()])]}
        with TemporaryDirectory() as temporary:
            diag.capture(client,NOW,Path(temporary)/'snapshot',token=SECRET)
            before = (Path(temporary)/'snapshot/schedule.json').read_bytes()
            with self.assertRaises(FileExistsError):diag.capture(client,NOW,Path(temporary)/'snapshot',token=SECRET)
            self.assertEqual((Path(temporary)/'snapshot/schedule.json').read_bytes(),before)

    def test_offline_cli_needs_no_token_or_client_and_main_sanitizes_errors(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary)/'schedule.json';path.write_text(json.dumps(project()))
            with patch.object(diag,'read_token') as token,patch.object(diag,'ReadOnlyClient') as factory, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(diag.main(['--validate',str(path)]),0)
            token.assert_not_called();factory.assert_not_called()
            self.assertIn('valid_for_diagnostics',output.getvalue())
        with patch.object(diag,'read_token',return_value=SECRET) as token, \
             patch.object(diag,'ReadOnlyClient') as factory,patch.dict(os.environ,{'T_INVEST_TOKEN':'prior'}), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            factory.side_effect = RuntimeError(SECRET)
            self.assertEqual(diag.main([]),1)
            self.assertEqual(os.environ['T_INVEST_TOKEN'],'prior')
            token.assert_called_once();factory.assert_called_once_with(attempts=1)
        self.assertNotIn(SECRET,output.getvalue())
        self.assertNotIn('Traceback',output.getvalue())

    def test_windows_launcher_is_crlf_and_preserves_error_status(self):
        data = (Path(__file__).resolve().parents[1]/'diagnose_schedule_snapshot_windows.cmd').read_bytes()
        self.assertEqual(data.count(b'\n'),data.count(b'\r\n'))
        self.assertIn(b'-m src.gldrubf_bot.diagnose_schedule_snapshot\r\n',data)
        self.assertIn(b'exit /b %DIAG_EXIT%',data)
        self.assertNotIn(b'T_INVEST_TOKEN',data)
