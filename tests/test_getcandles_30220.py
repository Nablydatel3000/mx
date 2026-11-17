"""GetCandles optional-limit conflict and credential-safe request diagnostics."""
import io
import json
import traceback
import unittest
import urllib.error
from datetime import datetime, timedelta
from unittest.mock import Mock

from src.gldrubf_bot.api import APIError
from src.gldrubf_bot.models import UTC, iso, instant
from tests.test_api import FakeResponse, make_client, q


METHOD = 'MarketDataService/GetCandles'
UID = '11111111-1111-4111-8111-111111111111'  # Synthetic public identifier, not GLDRUBF's UID.
SECRET = 't.synthetic-private-do-not-log'
START = datetime(2025, 1, 1, tzinfo=UTC)


class GetCandles30220Tests(unittest.TestCase):
    def test_exchange_daily_candles_avoid_limit_conflict(self):
        requests = []
        def transport(request, timeout):
            payload = json.loads(request.data)
            requests.append(payload)
            if 'limit' in payload:
                raise urllib.error.HTTPError(request.full_url, 400, 'bad request',
                    {'Content-Type': 'application/json'}, io.BytesIO(b'{"code":3,"description":"30220"}'))
            return FakeResponse(json.dumps({'candles': [dict(time=iso(START), open=q(100), high=q(101),
                low=q(99), close=q(100), volume='1', isComplete=True, candleSource='CANDLE_SOURCE_EXCHANGE')]}).encode())
        client = make_client(transport=transport)
        rows = client.candles(UID, START, START+timedelta(days=1))
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(requests), 1)
        self.assertNotIn('limit', requests[0])
        self.assertEqual(requests[0]['candleSourceType'], 'CANDLE_SOURCE_EXCHANGE')
        self.assertEqual(requests[0]['interval'], 'CANDLE_INTERVAL_DAY')

    def test_day_and_minute_requests_preserve_contract_and_range(self):
        for interval, days, count in [('CANDLE_INTERVAL_DAY', 181, 3), ('CANDLE_INTERVAL_1_MIN', 2, 2)]:
            with self.subTest(interval=interval):
                requests = []
                def transport(request, timeout):
                    requests.append(json.loads(request.data))
                    return FakeResponse(b'{"candles":[]}')
                self.assertEqual(make_client(transport=transport).candles(UID,START,START+timedelta(days=days),interval), [])
                self.assertEqual(len(requests), count)
                self.assertEqual(instant(requests[0]['from']), START)
                self.assertEqual(instant(requests[-1]['to']), START+timedelta(days=days))
                for payload in requests:
                    self.assertEqual(set(payload), {'instrumentId','from','to','interval','candleSourceType'})
                    self.assertEqual(payload['instrumentId'], UID)
                    self.assertEqual(payload['interval'], interval)
                    self.assertEqual(payload['candleSourceType'], 'CANDLE_SOURCE_EXCHANGE')
                    self.assertLessEqual(instant(payload['to'])-instant(payload['from']),
                                         timedelta(days=90 if interval=='CANDLE_INTERVAL_DAY' else 1))
                for left,right in zip(requests,requests[1:]):
                    self.assertEqual(left['to'],right['from'])

    def test_30220_error_contains_safe_parameters_without_retry(self):
        body = io.BytesIO(json.dumps({'code':3,'description':'30220','message':SECRET}).encode())
        error = urllib.error.HTTPError('https://example.invalid/'+SECRET,400,SECRET,
                                       {'Content-Type':'application/json'},body)
        transport = Mock(side_effect=error)
        recorder = Mock()
        client = make_client(token=SECRET, transport=transport, on_candles=recorder)
        payload = dict(instrumentId=UID, **{'from':iso(START),'to':iso(START+timedelta(days=90))},
                       interval='CANDLE_INTERVAL_DAY',candleSourceType='CANDLE_SOURCE_EXCHANGE',limit=2400)
        with self.assertRaises(APIError) as cm:
            client.call(METHOD,payload)
        failure=cm.exception
        self.assertEqual((failure.grpc_code,failure.api_code,failure.http_status),(3,30220,400))
        self.assertEqual(failure.request_parameters,payload)
        for key in ['instrumentId','interval','candleSourceType','limit']:
            self.assertIn(key+'='+str(payload[key]),str(failure))
        self.assertIn('from_utc='+payload['from'],str(failure))
        self.assertIn('to_utc='+payload['to'],str(failure))
        self.assertNotIn(SECRET,''.join(traceback.format_exception(failure)))
        self.assertNotIn('Authorization',str(failure))
        self.assertFalse(failure.retryable)
        self.assertTrue(body.closed)
        transport.assert_called_once()
        recorder.assert_not_called()

    def test_invalid_parameters_are_redacted(self):
        response=FakeResponse(b'{"code":3,"description":"30220"}',status=400)
        client=make_client(token=SECRET,transport=Mock(return_value=response))
        payload={key:SECRET for key in ['instrumentId','from','to','interval','candleSourceType','limit']}
        with self.assertRaises(APIError) as cm:
            client.call(METHOD,payload)
        self.assertEqual(cm.exception.request_parameters,
            dict(instrumentId='redacted', **{'from':'invalid','to':'invalid'},
                 interval='invalid',candleSourceType='invalid',limit='invalid'))
        self.assertNotIn(SECRET,str(cm.exception))
        # Even a credential with UUID syntax must never be echoed as a public UID.
        client=make_client(token=UID,transport=Mock(return_value=FakeResponse(b'{"code":3,"description":"30220"}',status=400)))
        with self.assertRaises(APIError) as cm:
            client.call(METHOD,dict(instrumentId=UID))
        self.assertNotIn(UID,str(cm.exception))
        self.assertEqual(cm.exception.request_parameters['instrumentId'],'redacted')

    def test_omitted_limit_is_visible_in_failure_diagnostics(self):
        client=make_client(transport=Mock(return_value=FakeResponse(b'{"code":3,"description":"30220"}',status=400)))
        with self.assertRaises(APIError) as cm:
            client.candles(UID,START,START+timedelta(days=1))
        self.assertEqual(cm.exception.request_parameters['limit'],'omitted')
        self.assertIn('limit=omitted',str(cm.exception))
        self.assertEqual(cm.exception.request_parameters['instrumentId'],UID)

    def test_noncanonical_uid_is_not_echoed_and_offset_dates_are_utc(self):
        client=make_client(transport=Mock(return_value=FakeResponse(b'{"code":3,"description":"30220"}',status=400)))
        with self.assertRaises(APIError) as cm:
            client.call(METHOD,dict(instrumentId=SECRET,**{'from':'2025-01-01T03:00:00+03:00',
                                                        'to':'2025-01-02T03:00:00+03:00'},limit=True))
        parameters=cm.exception.request_parameters
        self.assertEqual(parameters['instrumentId'],'redacted')
        self.assertEqual(parameters['from'],iso(START))
        self.assertEqual(parameters['to'],iso(START+timedelta(days=1)))
        self.assertEqual(parameters['interval'],'omitted')
        self.assertEqual(parameters['limit'],'invalid')
        self.assertNotIn(SECRET,str(cm.exception))
