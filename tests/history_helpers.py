"""Synthetic daily API/evidence only, never a real GLDRUBF calendar."""
import csv
import json
from datetime import datetime, timedelta
from decimal import Decimal

from src.gldrubf_bot.api import Instrument
from src.gldrubf_bot.history import download_daily, digest, load_snapshot
from src.gldrubf_bot.models import UTC, iso
from tests.helpers import UP
from tests.test_api import FakeResponse, make_client, q


START = datetime(2026, 1, 1, tzinfo=UTC)
NOW = START + timedelta(days=53, hours=10)
INST = Instrument('history-test-uid', 'fixture-figi', 'GLDRUBF', 'FORTS_EVENING', Decimal(1))


def candles(prices=None):
    return [dict(time=iso(START+timedelta(days=i)), open=q(p), high=q(p), low=q(p), close=q(p),
                 volume='10', isComplete=True) for i, p in enumerate(prices if prices is not None else UP+[1,1])]


def client_for(rows, *, quote_time=NOW, trading=True):
    requests, bodies = [], []
    def transport(request, timeout):
        method, payload = request.full_url.split('contract.v1.')[1], json.loads(request.data)
        requests.append((method, payload))
        if method.endswith('/GetCandles'):
            result = {'candles': rows}
        elif method.endswith('/TradingSchedules'):
            label = payload['from'][:10]
            result = {'exchanges': [dict(exchange=INST.exchange, days=[dict(date=label+'T00:00:00Z',
                isTradingDay=True, startTime=label+'T08:00:00Z', endTime=label+'T18:00:00Z')])]}
        elif method.endswith('/GetFuturesMargin'):
            result = dict(minPriceIncrement=q(1), minPriceIncrementAmount=q(2))
        elif method.endswith('/GetTradingStatus'):
            result = dict(instrumentUid=INST.uid, tradingStatus='SECURITY_TRADING_STATUS_NORMAL_TRADING',
                          marketOrderAvailableFlag=trading, apiTradeAvailableFlag=trading)
        elif method.endswith('/GetLastPrices'):
            result = {'lastPrices': [dict(instrumentUid=INST.uid, time=iso(quote_time), price=q(100))]}
        else:
            raise AssertionError('Unexpected API method')
        raw = json.dumps(result, indent=2).encode()
        bodies.append(raw)
        return FakeResponse(raw)
    client = make_client(transport=transport)
    return client, requests, bodies


def snapshot(root, rows=None, name='snapshot', *, previous=None, clock=NOW):
    rows = candles() if rows is None else rows
    client, requests, bodies = client_for(rows)
    path = download_daily(client, INST, START, clock, root/name, previous=previous, clock=lambda:clock)
    return path, client, requests, bodies


def evidence_file(root, snapshot_path, *, kind='historical_assumption', name='timing.json'):
    manifest, rows = load_snapshot(snapshot_path)
    data = json.loads((snapshot_path/'evidence_template.json').read_text())
    data.update(kind=kind, source='SYNTHETIC TEST TIMING, NOT REAL CALENDAR',
                coverage_source='SYNTHETIC TEST COVERAGE', coverage_through=(NOW-timedelta(days=1)).date().isoformat())
    for item in data['rows']:
        t = datetime.fromisoformat(item['time'].replace('Z','+00:00'))
        item.update(trade_day=t.date().isoformat(), completed_at=iso(t+timedelta(hours=18)),
                    available_at=iso(t+timedelta(hours=18, seconds=2)))
    data['expected_trade_days'] = [r['trade_day'] for r in data['rows']]
    data['execution_windows'] = [[iso(START), iso(NOW+timedelta(days=1))]]
    quotes = root/(name+'.quotes.csv')
    with quotes.open('w', newline='') as stream:
        writer = csv.writer(stream); writer.writerow(['time','price'])
        for i, seconds in [(50,2),(50,3),(52,3)]:
            writer.writerow([iso(START+timedelta(days=i,hours=18,seconds=seconds)),100+i])
    data.update(quotes_sha256=digest(quotes.read_bytes()), quote_uid=INST.uid,
                quote_source='INDEPENDENT SYNTHETIC OBSERVATIONS')
    path = root/name
    path.write_text(json.dumps(data), encoding='utf-8')
    return path, quotes
