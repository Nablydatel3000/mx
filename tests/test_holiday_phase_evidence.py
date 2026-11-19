"""Reported 21 November fragment, NOT a full original broker response.

Only the two phase names and their 07:00--16:00 UTC boundaries were supplied.
date/isTradingDay/legacy envelope below are explicit synthetic test scaffolding.
No evidence here establishes the execution semantics of the holiday label.
"""
import json
import os
import unittest
from unittest.mock import Mock,patch

from src.gldrubf_bot.api import parse_day
from src.gldrubf_bot.live_acceptance import ReadOnlyBroker,safe_error
from src.gldrubf_bot.market_diagnostics import CalendarError
from src.gldrubf_bot.models import DataError,instant
from tests.test_api import FakeResponse,make_client,detail
from tests.test_detailed_sessions import reported_day

HOLIDAY='holiday_trading_session_morning'
CODE='forts_futures_weekend'
SECRET='synthetic-holiday-evidence-token'
NOW=instant('2026-11-19T10:00:00Z')


def reported_fragment_day():
    start,end='2026-11-21T07:00:00Z','2026-11-21T16:00:00Z'
    return dict(date='2026-11-21T00:00:00Z',isTradingDay=True,startTime=start,endTime=end,
        intervals=[detail(HOLIDAY,start,end),detail('regular_trading_session',start,end)])


def response():
    return dict(exchanges=[dict(exchange=CODE.upper(),days=[reported_day('2026-11-19'),
        reported_day('2026-11-20'),reported_fragment_day()])])


class HolidayPhaseEvidenceTests(unittest.TestCase):
    def test_exact_regular_duplicate_does_not_prove_unknown_holiday_phase(self):
        for reverse in (False,True):
            day=reported_fragment_day()
            if reverse:day['intervals'].reverse()
            with self.subTest(reverse=reverse),self.assertRaisesRegex(DataError,'Unknown detailed trading phase'):
                parse_day(day)

    def test_repeated_holiday_alias_never_creates_windows(self):
        day=reported_fragment_day();day['intervals']*=2
        with self.assertRaisesRegex(DataError,'Unknown detailed trading phase'):parse_day(day)

    def test_control_known_regular_duplicates_are_one_window_without_holiday(self):
        # Test control only; production never drops the unsupported record.
        day=reported_fragment_day();day['intervals']=[day['intervals'][1]]*2
        parsed=parse_day(day);self.assertEqual(len(parsed.windows),1)
        self.assertTrue(parsed.contains(instant('2026-11-21T07:00:00Z')))
        self.assertFalse(parsed.contains(instant('2026-11-21T16:00:00Z')))

    def test_paper_current_sessions_keeps_future_unsupported_day_blocking(self):
        transport=Mock(return_value=FakeResponse(json.dumps(response()).encode()))
        client=make_client(token=SECRET,transport=transport,attempts=1)
        with self.assertRaisesRegex(DataError,'Unknown detailed trading phase'):
            client.current_sessions(CODE,NOW)
        transport.assert_called_once()
        request=transport.call_args.args[0]
        self.assertTrue(request.full_url.endswith('InstrumentsService/TradingSchedules'))
        self.assertEqual(json.loads(request.data)['exchange'],CODE)

    def test_live_acceptance_excludes_future_but_blocks_current_holiday_without_order(self):
        transport=Mock(side_effect=lambda *a,**k:FakeResponse(json.dumps(response()).encode()))
        with patch.dict(os.environ,{'T_INVEST_TOKEN':SECRET}):
            broker=ReadOnlyBroker('synthetic-account',transport=transport)
        calendar=broker.current_calendar(CODE,NOW)
        self.assertTrue(any(day.contains(NOW) for day in calendar.values()))
        self.assertEqual(broker.calendar_diagnostic['selection']['excluded_record_indexes'],[1,2])
        with self.assertRaises(CalendarError) as cm:broker.current_calendar(CODE,instant('2026-11-21T10:00:00Z'))
        report=broker.calendar_diagnostic
        self.assertEqual(safe_error(cm.exception),dict(category='calendar_validation_error',reason='unknown_detailed_phase'))
        self.assertEqual([d['parse_ok'] for d in report['days']],[True,True,False])
        self.assertEqual(report['days'][2]['reason'],'unknown_detailed_phase')
        self.assertEqual(report['days'][2]['intervals'][0]['type'],HOLIDAY)
        self.assertFalse(report['calendar_validated']);self.assertFalse(report['inside_confirmed_window'])
        self.assertNotIn(SECRET,json.dumps(report));self.assertNotIn('synthetic-account',json.dumps(report))
        self.assertEqual(len(broker.calls),2)
        self.assertEqual(broker.calls[0]['method'],'InstrumentsService/TradingSchedules')
        self.assertEqual(transport.call_count,2)

    def test_holiday_alone_and_invented_prefixes_remain_unsupported(self):
        for kind in (HOLIDAY,'holiday_trading_session','holiday_trading_session_evening',
                     'regular_trading_session_holiday'):
            day=reported_fragment_day();day['intervals']=[detail(kind,day['startTime'],day['endTime'])]
            with self.subTest(kind=kind),self.assertRaisesRegex(DataError,'Unknown detailed trading phase'):
                parse_day(day)
