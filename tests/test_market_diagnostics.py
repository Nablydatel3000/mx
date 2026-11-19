"""Calendar evidence and IOC arithmetic only; no trading entry point."""
from copy import deepcopy
from decimal import Decimal as D
from types import SimpleNamespace
import json
import unittest
from unittest.mock import Mock

from src.gldrubf_bot.market_diagnostics import inspect_calendar,price_bounds
from src.gldrubf_bot.models import instant,Quote
from tests.test_detailed_sessions import reported_day

CODE='forts_futures_weekend'
SECRET='secretcalendar'


class MarketDiagnosticsTests(unittest.TestCase):
    def inspect(self,days,now='2026-11-19T10:00:00Z',response=None):
        response=response if response is not None else dict(exchanges=[dict(exchange=CODE.upper(),days=days)])
        self.client=SimpleNamespace(call=Mock(return_value=response),read=SimpleNamespace(_token=SECRET))
        result=inspect_calendar(self.client,CODE,instant(now));self.client.call.assert_called_once()
        return result

    def test_three_reported_dates_use_same_parser_and_deduplicate_aliases(self):
        days=[reported_day('2026-11-'+n) for n in ('17','18','19')]
        calendar,r=self.inspect(days)
        self.assertTrue(r['calendar_validated']);self.assertTrue(r['inside_confirmed_window'])
        self.assertEqual([len(d.windows) for d in calendar.values()],[3,3,3])
        self.assertEqual([d['intervals_count'] for d in r['days']],[9,9,9])
        self.assertEqual(r['exchange_summary']['casefold_matches'],1)
        self.assertEqual(r['exchange_summary']['requested_exchange'],CODE)
        self.assertEqual(self.client.call.call_args.args[1]['exchange'],CODE)

    def test_auctions_clearing_gaps_and_second_boundaries_are_normal_waits(self):
        for clock,inside in [('03:50:00',False),('03:59:59',False),('04:00:00',True),
                ('05:59:58.999999',True),('05:59:59',False),('06:00:00',True),
                ('15:59:59',False),('16:00:00',True),('20:49:59.999999',True),('20:50:00',False),('20:50:01',False)]:
            with self.subTest(clock=clock):
                c,r=self.inspect([reported_day('2026-11-19')],'2026-11-19T'+clock+'Z')
                self.assertIsNotNone(c);self.assertIsNone(r['failure']);self.assertEqual(r['inside_confirmed_window'],inside)

    def test_moscow_date_rollover_does_not_shift_utc_request_or_open_future_session(self):
        c,r=self.inspect([reported_day('2026-11-20')],'2026-11-19T21:30:00Z')
        self.assertEqual(r['utc_date'],'2026-11-19');self.assertEqual(r['moscow_date'],'2026-11-20')
        self.assertEqual(r['request_from_utc'],'2026-11-19T00:00:00.000000Z')
        self.assertFalse(r['inside_confirmed_window']);self.assertFalse(r['has_current_utc_date_label'])

    def test_offset_timestamps_are_converted_to_utc_and_not_host_timezone(self):
        day=reported_day('2026-11-19');day['intervals']=[]
        day.update(startTime='2026-11-19T07:00:00+03:00',endTime='2026-11-19T08:00:00+03:00')
        day['intervals']=[dict(type='regular_trading_session',interval=dict(startTs=day['startTime'],endTs=day['endTime']))]
        c,r=self.inspect([day],'2026-11-19T04:30:00Z')
        self.assertTrue(r['inside_confirmed_window']);self.assertEqual(r['windows_utc'][0][0],'2026-11-19T04:00:00.000000Z')

    def test_each_invalid_day_has_index_reason_and_preserves_rejection(self):
        for change,reason in [
          (lambda d:d.pop('intervals'),'missing_detailed_intervals'),
          (lambda d:d['intervals'][0].update(type='new_phase'),'unknown_detailed_phase'),
          (lambda d:d.update(startTime='2026-11-19T04:01:00Z'),'detailed_legacy_contradiction'),
          (lambda d:d.update(endTime=None),'incomplete_legacy_pair'),
          (lambda d:d.update(startTime='2026-11-19T04:00:00'),'invalid_timestamp')]:
            day=reported_day('2026-11-19');change(day)
            with self.subTest(reason=reason):
                c,r=self.inspect([day]);self.assertIsNone(c)
                self.assertEqual(r['failure']['reason'],reason);self.assertEqual(r['days'][0]['reason'],reason)
                self.assertFalse(r['inside_confirmed_window']);self.assertEqual(r['days'][0]['index'],0)

    def test_bad_future_day_is_not_silently_ignored_to_allow_current_day(self):
        future=reported_day('2026-11-20');future['intervals'][0]['type']='unknown_phase'
        c,r=self.inspect([reported_day('2026-11-19'),future])
        self.assertIsNone(c);self.assertTrue(r['days'][0]['parse_ok']);self.assertFalse(r['days'][1]['parse_ok'])

    def test_nontrading_day_is_valid_and_never_authorizes_execution(self):
        c,r=self.inspect([dict(date='2026-11-19T00:00:00Z',isTradingDay=False)])
        self.assertIsNotNone(c);self.assertTrue(r['calendar_validated']);self.assertFalse(r['inside_confirmed_window'])

    def test_ambiguous_exchange_malformed_days_and_conflicts_remain_errors(self):
        day=reported_day('2026-11-19');other=deepcopy(day);other['isTradingDay']=False
        for response,reason in [
          (dict(exchanges=[dict(exchange=CODE,days=[day]),dict(exchange=CODE.upper(),days=[day])]),'missing_or_ambiguous_exchange'),
          (dict(exchanges=[dict(exchange=CODE,days=None)]),'invalid_days_array'),
          (dict(exchanges=[dict(exchange=CODE,days=[day,other])]),'conflicting_day_records')]:
            with self.subTest(reason=reason):
                c,r=self.inspect([],response=response);self.assertIsNone(c);self.assertEqual(r['failure']['reason'],reason)

    def test_safe_evidence_excludes_secrets_uids_and_arbitrary_api_fields(self):
        day=reported_day('2026-11-19');day.update(debug=SECRET,uid='private-uid',accountId='12345678')
        day['intervals'][0]['type']=SECRET
        c,r=self.inspect([day]);raw=json.dumps(r)
        for text in (SECRET,'private-uid','12345678','debug'):self.assertNotIn(text,raw)
        self.assertEqual(r['failure']['reason'],'unknown_detailed_phase')

    def bounds(self,reference='100',bid='99.9',ask='100.1',down='50',up='150'):
        return price_bounds(Quote(instant('2026-11-19T10:00:00Z'),D(reference)),SimpleNamespace(tick=D('.1')),
            dict(best_bid=bid,best_ask=ask,limit_down=down,limit_up=up),dict(max_slippage_ticks=2))

    def test_ioc_limits_equal_engine_last_plus_minus_two_ticks(self):
        r=self.bounds();self.assertEqual(r['bounded_limit_long_points'],'100.2')
        self.assertEqual(r['bounded_limit_short_points'],'99.8');self.assertTrue(r['book_within_proposed_limits'])

    def test_one_side_can_fail_without_expanding_two_tick_bound(self):
        r=self.bounds(ask='100.3');self.assertFalse(r['book_within_proposed_limits'])
        self.assertFalse(r['long_price_checks']['allowed']);self.assertTrue(r['short_price_checks']['allowed'])
        self.assertEqual(r['long_price_checks']['adverse_distance_ticks'],'3')
        self.assertEqual(r['bounded_limit_long_points'],'100.2')

    def test_off_grid_exchange_limits_and_empty_book_are_blocked(self):
        for kwargs,side in [(dict(reference='100.05'),'long'),(dict(up='100.1'),'long'),
                            (dict(down='99.9'),'short'),(dict(ask=None),'long'),(dict(bid=None),'short')]:
            with self.subTest(kwargs=kwargs):self.assertFalse(self.bounds(**kwargs)[side+'_price_checks']['allowed'])
