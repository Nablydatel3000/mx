import io
import os
from unittest.mock import patch
import json
import unittest
import urllib.error
from datetime import datetime, timedelta
from decimal import Decimal as D
from email.message import Message
from src.gldrubf_bot.api import ReadOnlyClient, Instrument, APIError, parse_day, quotation
from src.gldrubf_bot.models import UTC, DataError


UID = 'verified-fixture-uid'
INST = Instrument(UID, 'fixture-figi', 'GLDRUBF', 'MOEX_FUT', D(1))
NOW = datetime(2026, 1, 2, 19, tzinfo=UTC)


class FakeResponse(io.BytesIO):
    """HTTP response metadata as returned by urllib, with a closeable body."""

    def __init__(self, body, status=200, content_type='application/json'):
        super().__init__(body)
        self.status = status
        self.headers = Message()
        if content_type is not None:
            self.headers['Content-Type'] = content_type


def q(n):return {'units': str(n), 'nano': 0}
def day(d='2026-01-02', trading=True):
    if not trading:return {'date': d+'T00:00:00Z', 'isTradingDay': False}
    return dict(date=d+'T00:00:00Z', isTradingDay=True, startTime=d+'T07:00:00Z', endTime=d+'T18:00:00Z')
def candle(d='2026-01-02', complete=True):
    return dict(time=d+'T00:00:00Z', open=q(100), high=q(101), low=q(99), close=q(100), volume='10', isComplete=complete)


def make_client(*, token='fake-test-token', **kwargs):
    # Test-only injection via environment; production client accepts no token argument.
    with patch.dict(os.environ, {'T_INVEST_TOKEN': token}):
        return ReadOnlyClient(**kwargs)


class APITests(unittest.TestCase):
    def client(self, responder):
        self.requests = []
        def transport(request, timeout):
            self.requests.append((request.full_url, json.loads(request.data)))
            return FakeResponse(json.dumps(responder(request.full_url.split('.')[-1], json.loads(request.data))).encode())
        return make_client(token='fake-test-token', transport=transport, sleep=lambda _: None)

    def test_allowlist_and_no_token_echo(self):
        with patch.dict(os.environ, {'T_INVEST_TOKEN': ''}):
            with self.assertRaises(APIError):ReadOnlyClient()
        c = self.client(lambda *_: {})
        with self.assertRaises(APIError):c.call('OrdersService/PostOrder', {})
        self.assertFalse(self.requests)
        def fail(*_, **__):raise urllib.error.URLError('fake-test-token')
        c = make_client(token='fake-test-token', transport=fail, sleep=lambda _: None, attempts=2)
        with self.assertRaises(APIError) as cm:c.call('MarketDataService/GetCandles', {})
        self.assertNotIn('fake-test-token', str(cm.exception))

    def test_retry_429_then_success_and_401_no_retry(self):
        attempts = []
        def transport(request, timeout):
            attempts.append(1)
            if len(attempts) == 1:raise urllib.error.HTTPError(request.full_url, 429, 'rate limit', {}, None)
            return FakeResponse(b'{"candles":[]}')
        c = make_client(token='fake', transport=transport, sleep=lambda _: None)
        self.assertEqual(c.call('MarketDataService/GetCandles', {}), {'candles': []})
        self.assertEqual(len(attempts), 2)
        def unauth(request, timeout):
            attempts.append(1)
            raise urllib.error.HTTPError(request.full_url, 401, 'secret body', {}, None)
        attempts.clear()
        with self.assertRaises(APIError):make_client(token='fake', transport=unauth).call('MarketDataService/GetCandles', {})
        self.assertEqual(len(attempts), 1)

    def test_uid_resolution_and_mismatch(self):
        def respond(method, payload):
            if method.endswith('FindInstrument'):return {'instruments': [{'uid': UID, 'ticker':'GLDRUBF','instrumentType':'futures'}]}
            return {'instrument': dict(uid=UID,ticker='GLDRUBF',lot=1,currency='rub',figi='fixture',exchange='MOEX_FUT',minPriceIncrement=q(1))}
        c = self.client(respond)
        self.assertEqual(c.instrument().uid, UID)
        self.assertEqual(self.requests[1][1]['idType'], 'INSTRUMENT_ID_TYPE_UID')
        with self.assertRaises(DataError):c.instrument('wrong')

    def test_calendar_completed_and_incomplete(self):
        def respond(method, payload):
            if method.endswith('TradingSchedules'):return {'exchanges':[{'exchange':'MOEX_FUT','days':[day()]}]}
            return {'candles':[candle()]}
        c = self.client(respond)
        b, _ = c.completed_daily(INST, datetime(2026,1,2,tzinfo=UTC), NOW)
        self.assertEqual(len(b), 1)
        self.assertEqual(b[0].closed_at.hour, 18)
        self.assertEqual(self.requests[-1][1]['instrumentId'], UID)
        c = self.client(lambda method,p: {'exchanges':[{'exchange':'MOEX_FUT','days':[day()]}]} if method.endswith('TradingSchedules') else {'candles':[candle(complete=False)]})
        self.assertEqual(c.completed_daily(INST, datetime(2026,1,2,tzinfo=UTC), NOW)[0], [])

    def test_missing_completed_data_and_calendar(self):
        c = self.client(lambda method,p: {'exchanges':[{'exchange':'MOEX_FUT','days':[day()]}]} if method.endswith('TradingSchedules') else {'candles':[]})
        with self.assertRaises(DataError):c.completed_daily(INST, datetime(2026,1,2,tzinfo=UTC), NOW)
        c = self.client(lambda *_: {'exchanges':[{'exchange':'MOEX_FUT','days':[]}]})
        with self.assertRaises(DataError):c.schedules('MOEX_FUT', datetime(2026,1,2,tzinfo=UTC), NOW)

    def test_sessions_clearing_and_weekend_not_guessed(self):
        raw = day()
        raw.update(clearingStartTime='2026-01-02T12:00:00Z', clearingEndTime='2026-01-02T12:10:00Z',
                   eveningStartTime='2026-01-02T19:00:00Z',eveningEndTime='2026-01-02T21:00:00Z')
        s = parse_day(raw)
        self.assertFalse(s.contains(datetime(2026,1,2,12,5,tzinfo=UTC)))
        self.assertTrue(s.contains(datetime(2026,1,2,20,tzinfo=UTC)))
        self.assertEqual(s.close.hour, 21)
        self.assertFalse(parse_day(day(trading=False)).trading)

    def test_candle_pagination_and_conflict(self):
        c = self.client(lambda *_:{'candles':[]})
        start = datetime(2026,1,1,tzinfo=UTC)
        c.candles(UID,start,start+timedelta(days=3),'CANDLE_INTERVAL_1_MIN')
        self.assertEqual(len(self.requests),3)
        broken=candle('2026-01-01');changed=dict(broken,volume='12')
        c=self.client(lambda *_:{'candles':[broken,changed]})
        with self.assertRaises(DataError):c.candles(UID,start,start+timedelta(days=1))

    def test_quote_status_uid_time_and_quotation(self):
        status=dict(instrumentUid=UID,tradingStatus='SECURITY_TRADING_STATUS_NORMAL_TRADING',marketOrderAvailableFlag=True,apiTradeAvailableFlag=True)
        c=self.client(lambda method,p:status if method.endswith('GetTradingStatus') else {'lastPrices':[dict(instrumentUid=UID,time='2026-01-02T18:59:59Z',price=q(100))]})
        self.assertEqual(c.quote(INST,lambda:NOW).price,D(100))
        c=self.client(lambda method,p:status if method.endswith('GetTradingStatus') else {'lastPrices':[dict(instrumentUid=UID,time='2026-01-03T18:59:59Z',price=q(100))]})
        with self.assertRaises(DataError):c.quote(INST,NOW)
        self.assertEqual(quotation({'units':'5'}),D(5))
        for invalid in [None,{'units':'1.5'}, {'units':'1','nano':-1}]:
            with self.assertRaises(DataError):quotation(invalid)

class APIContractTests(unittest.TestCase):
    client = APITests.client
    def test_current_margin_contract_uid(self):
        c=self.client(lambda *_:{'minPriceIncrement':q(2),'minPriceIncrementAmount':q(10)})
        inst=Instrument(UID,'fixture','GLDRUBF','MOEX_FUT',D(2))
        self.assertEqual(c.point_value(inst),D(5))
        self.assertEqual(self.requests[0][1],{'instrumentId':UID})

    def test_daily_chunk_endpoint_ignores_time_of_day(self):
        start=datetime(2026,1,1,tzinfo=UTC);end=start+timedelta(days=91)
        endpoint=candle('2026-04-01');endpoint['time']='2026-04-01T07:00:00Z'
        c=self.client(lambda *_:{'candles':[endpoint]})
        self.assertEqual(len(c.candles(UID,start,end)),1)
        self.assertEqual(len(self.requests),2)

    def test_unknown_detailed_interval_and_proto_false_default(self):
        self.assertFalse(parse_day({'date':'2026-01-03T00:00:00Z'}).trading)
        raw=day();raw['intervals']=[{'type':'unverified','interval':{'startTs':'2026-01-02T08:00:00Z','endTs':'2026-01-02T09:00:00Z'}}]
        with self.assertRaises(DataError):parse_day(raw)
        raw['intervals'][0]['interval']={'startTs':raw['startTime'],'endTs':raw['endTime']}
        self.assertTrue(parse_day(raw).trading)

    def test_bad_json_is_sanitized(self):
        def transport(*_,**__):return FakeResponse(b'not-json-fake-secret')
        c=make_client(token='fake-secret',transport=transport)
        with self.assertRaises(APIError) as cm:c.call('MarketDataService/GetCandles',{})
        self.assertNotIn('fake-secret',str(cm.exception))

    def test_historical_incomplete_day_not_silently_dropped(self):
        c=self.client(lambda method,p: {'exchanges':[{'exchange':'MOEX_FUT','days':[day('2026-01-01'),day()]}]}
                      if method.endswith('TradingSchedules') else {'candles':[candle('2026-01-01',False),candle()]})
        with self.assertRaises(DataError):c.completed_daily(INST,datetime(2026,1,1,tzinfo=UTC),NOW)

    def test_history_starts_at_documented_first_daily_candle(self):
        inst=Instrument(UID,'fixture','GLDRUBF','MOEX_FUT',D(1),datetime(2026,1,2,tzinfo=UTC))
        c=self.client(lambda method,p: {'exchanges':[{'exchange':'MOEX_FUT','days':[day()]}]}
                      if method.endswith('TradingSchedules') else {'candles':[candle()]})
        got,_=c.completed_daily(inst,datetime(2026,1,1,tzinfo=UTC),NOW)
        self.assertEqual(len(got),1)
        self.assertTrue(self.requests[0][1]['from'].startswith('2026-01-02'))
