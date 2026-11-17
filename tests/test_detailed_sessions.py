"""Reconstructed from user observations, NOT the unavailable original JSON files."""
import copy
import json
import unittest
from unittest.mock import Mock

from src.gldrubf_bot.api import parse_day
from src.gldrubf_bot.models import DataError, instant
from tests.test_api import FakeResponse, make_client, detail


DATES = ('2026-11-17','2026-11-18','2026-11-19')


def reported_day(label):
    """Nine reported records plus the reported envelope; no invented legacy phases."""
    def span(kind,a,b):return detail(kind,label+'T'+a+'Z',label+'T'+b+'Z')
    intervals = []
    for names,a,b in [
        (('opening_auction_morning','opening_auction'),'03:50:00','03:59:59'),
        (('regular_trading_session_morning','regular_trading_session'),'04:00:00','05:59:59'),
        (('regular_trading_session_main','regular_trading_session'),'06:00:00','15:59:59'),
        (('regular_trading_session_evening','regular_trading_session'),'16:00:00','20:50:00'),
        (('clearing',),'20:50:01','21:30:00')]:
        intervals.extend(span(name,a,b) for name in names)
    return dict(date=label+'T00:00:00Z',isTradingDay=True,startTime=label+'T04:00:00Z',
                endTime=label+'T20:50:00Z',intervals=intervals)


class DetailedSessionsTests(unittest.TestCase):
    def test_all_three_reported_dates_nine_records_make_three_distinct_windows(self):
        for label in DATES:
            with self.subTest(label=label):
                raw = reported_day(label);self.assertEqual(len(raw['intervals']),9)
                result = parse_day(raw)
                expected = tuple((instant(label+'T'+a+'Z'),instant(label+'T'+b+'Z')) for a,b in
                                 [('04:00:00','05:59:59'),('06:00:00','15:59:59'),('16:00:00','20:50:00')])
                self.assertEqual(result.windows,expected)
                self.assertEqual(result.close,instant(label+'T20:50:00Z'))
                self.assertEqual(result.day.isoformat(),label)

    def test_second_and_microsecond_edges_and_actual_gaps_on_all_three_dates(self):
        observations = [('03:49:59',False),('03:50:00',False),('03:59:58',False),('03:59:59',False),
            ('03:59:59.999999',False),('04:00:00',True),('05:59:58.999999',True),
            ('05:59:59',False),('05:59:59.999999',False),('06:00:00',True),
            ('15:59:58.999999',True),('15:59:59',False),('15:59:59.999999',False),
            ('16:00:00',True),('20:49:59.999999',True),('20:50:00',False),
            ('20:50:00.999999',False),('20:50:01',False),('21:29:59',False),('21:30:00',False)]
        for label in DATES:
            result = parse_day(reported_day(label))
            for clock,allowed in observations:
                with self.subTest(label=label,clock=clock):
                    self.assertEqual(result.contains(instant(label+'T'+clock+'Z')),allowed)

    def test_missing_detail_never_falls_back_to_envelope_and_unknown_alias_never_grants(self):
        for intervals in (None,[],[detail('unknown',DATES[0]+'T04:00:00Z',DATES[0]+'T20:50:00Z')],
                          [detail('regular_trading_session_unconfirmed',DATES[0]+'T04:00:00Z',DATES[0]+'T20:50:00Z')],
                          [detail('opening_auction',DATES[0]+'T03:50:00Z',DATES[0]+'T03:59:59Z')],
                          [detail('clearing',DATES[0]+'T20:50:01Z',DATES[0]+'T21:30:00Z')]):
            raw = reported_day(DATES[0]);raw['intervals']=intervals
            with self.subTest(intervals=intervals),self.assertRaises(DataError):parse_day(raw)
        raw.pop('intervals')
        with self.assertRaises(DataError):parse_day(raw)

    def test_missing_main_phase_leaves_a_gap_and_missing_duplicate_does_not_expand(self):
        raw = reported_day(DATES[0]);del raw['intervals'][4:6]
        result = parse_day(raw)
        self.assertEqual(len(result.windows),2)
        self.assertFalse(result.contains(instant(DATES[0]+'T12:00:00Z')))
        raw = reported_day(DATES[0]);raw['intervals'].pop(4)
        self.assertEqual(parse_day(raw).windows,parse_day(reported_day(DATES[0])).windows)

    def test_duplicates_order_and_aliases_do_not_double_or_bridge_windows(self):
        raw = reported_day(DATES[0])
        raw['intervals'] += copy.deepcopy(raw['intervals'])
        raw['intervals'].reverse()
        self.assertEqual(parse_day(raw),parse_day(reported_day(DATES[0])))

    def test_non_identical_overlap_and_contradictory_phase_labels_stop(self):
        for change in ('regular_overlap','auction_overlap','conflicting_family','different_specific_names'):
            raw = reported_day(DATES[0])
            if change=='regular_overlap':
                for item in raw['intervals'][4:6]:item['interval']['startTs']=DATES[0]+'T05:59:58Z'
            elif change=='auction_overlap':
                for item in raw['intervals'][:2]:
                    item['interval'].update(startTs=DATES[0]+'T04:30:00Z',endTs=DATES[0]+'T04:40:00Z')
            elif change=='conflicting_family':
                raw['intervals'].append(detail('clearing',DATES[0]+'T04:00:00Z',DATES[0]+'T05:59:59Z'))
            else:
                raw['intervals'][3]['type']='regular_trading_session_main'
            with self.subTest(change=change),self.assertRaises(DataError):parse_day(raw)

    def test_legacy_contradictions_and_incomplete_pairs_stop(self):
        for fields in [dict(startTime=DATES[0]+'T04:01:00Z'),dict(endTime=DATES[0]+'T20:49:00Z'),
                       dict(clearingStartTime=DATES[0]+'T20:50:01Z',clearingEndTime=DATES[0]+'T21:00:00Z'),
                       dict(openingAuctionStartTime=DATES[0]+'T03:50:00Z',openingAuctionEndTime=DATES[0]+'T03:59:58Z'),
                       dict(openingAuctionStartTime=DATES[0]+'T03:50:00Z')]:
            raw = reported_day(DATES[0]);raw.update(fields)
            with self.subTest(fields=fields),self.assertRaises(DataError):parse_day(raw)

    def test_legacy_auction_and_clearing_only_restrict_do_not_extend_close(self):
        raw = reported_day(DATES[0]);raw.update(
            openingAuctionStartTime=DATES[0]+'T03:50:00Z',openingAuctionEndTime=DATES[0]+'T04:00:00Z',
            clearingStartTime=DATES[0]+'T20:50:00Z',clearingEndTime=DATES[0]+'T21:30:00Z')
        self.assertEqual(parse_day(raw),parse_day(reported_day(DATES[0])))

    def test_malformed_unknown_and_nonpositive_details_stop_without_echoing_type(self):
        for item in (None,{},dict(type='secret-unknown',interval={}),
                     detail('regular_trading_session',DATES[0]+'T04:00:00Z',DATES[0]+'T04:00:00Z'),
                     dict(type='regular_trading_session',interval=dict(startTs=DATES[0]+'T04:00:00Z'))):
            raw = reported_day(DATES[0]);raw['intervals'].append(item)
            with self.subTest(item=item),self.assertRaises(DataError) as cm:parse_day(raw)
            self.assertNotIn('secret-unknown',str(cm.exception))

    def test_submicrosecond_boundaries_are_not_rounded_into_permission(self):
        raw = reported_day(DATES[0])
        for item in raw['intervals'][2:4]:item['interval']['startTs']=DATES[0]+'T04:00:00.000000001Z'
        with self.assertRaises(DataError):parse_day(raw)
        for item in raw['intervals'][2:4]:item['interval']['startTs']=DATES[0]+'T04:00:00.000000000Z'
        self.assertEqual(parse_day(raw),parse_day(reported_day(DATES[0])))

    def test_current_calendar_selection_keeps_real_casefold_and_processes_all_three_labels(self):
        response = {'exchanges':[dict(exchange='FORTS_FUTURES_WEEKEND',days=[reported_day(d) for d in DATES])]}
        transport = Mock(return_value=FakeResponse(json.dumps(response).encode()))
        client = make_client(transport=transport)
        calendar = client.current_sessions('forts_futures_weekend',instant(DATES[0]+'T10:00:00Z'))
        self.assertEqual([d.isoformat() for d in calendar],list(DATES))
        self.assertTrue(all(len(day.windows)==3 for day in calendar.values()))
        self.assertEqual(json.loads(transport.call_args.args[0].data)['exchange'],'forts_futures_weekend')

    def test_nontrading_day_does_not_gain_permission_from_regular_detail(self):
        raw = reported_day(DATES[0]);raw['isTradingDay']=False
        result = parse_day(raw)
        self.assertFalse(result.trading)
        self.assertEqual(result.windows,())
