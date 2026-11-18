"""Frozen-trade accounting checks and MOCK-only contract capture tests."""
import contextlib
from decimal import Decimal as D
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from audits.gldrubf_economics.calculate import calculate,load_inputs,scenarios,Scenario
from audits.gldrubf_economics import capture_current
from src.gldrubf_bot.models import DataError


class EconomicsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.data=load_inputs()

    def test_gross_conversion_is_conditional_and_does_not_deduct_margin(self):
        r=calculate(self.data,scenarios()[0])['summary']
        self.assertEqual(r['total_gross_rub'],'5471.9')
        self.assertEqual(r['counterfactual_final_equity_rub'],'15471.9')
        self.assertTrue(r['all_economics_parameters_unverified'])
        self.assertFalse(r['actual_broker_liquidation_rule_or_outcome_verified'])
        larger=calculate(self.data,Scenario('higher margin',margin_long=D(4200),margin_short=D(4200)))['summary']
        self.assertEqual(larger['counterfactual_final_equity_rub'],r['counterfactual_final_equity_rub'])
        self.assertEqual(D(r['minimum_free_above_assumed_margin_rub'])-D(larger['minimum_free_above_assumed_margin_rub']),D(2800))

    def test_fees_cover_seventeen_legs_and_calendar_carry_covers_631_dates(self):
        r=calculate(self.data,scenarios()[2]);s=r['summary']
        self.assertEqual(s['closed_commission_rub'],'80')
        self.assertEqual(s['total_commission_rub'],'85')
        self.assertEqual(s['closed_adverse_carry_rub'],'579')
        self.assertEqual(s['total_adverse_carry_rub'],'631')
        self.assertEqual(r['opened']['carry_calendar_days'],52)
        self.assertEqual(s['closed_conditional_net_rub'],'3507.4')
        self.assertEqual(s['open_conditional_contribution_rub'],'1248.5')
        self.assertEqual(s['total_conditional_net_rub'],'4755.9')

    def test_daily_open_and_ohlc_minima_differ_and_path_is_checked(self):
        s=calculate(self.data,scenarios()[0])['summary']
        self.assertEqual(s['minimum_daily_close_equity_rub'],'8533.9')
        self.assertEqual(s['minimum_open_equity_rub'],'8531.9')
        self.assertEqual(s['minimum_equity_rub'],'8480.6')
        self.assertEqual(s['minimum_equity_provider_day'],'2025-07-29')
        self.assertEqual(s['minimum_equity_kind'],'ADVERSE_OHLC')
        self.assertEqual(s['daily_max_drawdown_rub'],'2097.2')
        stress=calculate(self.data,scenarios()[2])['summary']
        self.assertEqual(stress['minimum_equity_rub'],'8300.6')
        self.assertEqual(stress['minimum_free_above_assumed_margin_rub'],'6900.6')
        self.assertEqual(stress['daily_max_drawdown_rub'],'2138.2')

    def test_margin_failure_does_not_produce_a_feasible_final_result(self):
        c=Scenario('point10',rub_per_point=D(10),total_fee_per_side=D(5),adverse_carry_per_calendar_day=D(1))
        s=calculate(self.data,c)['summary']
        self.assertFalse(s['simplified_margin_requirement_met_on_all_samples'])
        self.assertEqual(s['first_failed_provider_day'],'2025-05-12')
        self.assertIsNone(s['feasible_scenario_final_equity_rub'])
        self.assertGreater(D(s['counterfactual_final_equity_rub']),D(10000))

    def test_adverse_extreme_uses_high_for_short_low_for_long(self):
        r=calculate(self.data,scenarios()[0])
        for mark in r['marks']:
            if mark['kind']=='ADVERSE_OHLC':
                field='low' if mark['position']==1 else 'high'
                self.assertEqual(mark['observed_or_model_price'],self.data['bars'][mark['provider_day']][field])


class LocalCaptureTests(unittest.TestCase):
    def client(self):
        c=Mock();c.instrument.return_value=SimpleNamespace(uid=capture_current.UID,tick=D('.1'))
        c.call.return_value=dict(minPriceIncrement=dict(units='0',nano=100000000),
            minPriceIncrementAmount=dict(units='0',nano=100000000),
            initialMarginOnBuy=dict(currency='rub',units='1400',nano=0),
            initialMarginOnSell=dict(currency='rub',units='1500',nano=0),
            Authorization='synthetic-credential',accountId='synthetic-account',arbitrary='excluded')
        return c

    def test_capture_projects_only_contract_fields_and_keeps_long_short_separate(self):
        c=self.client();r=capture_current.snapshot(c)
        self.assertEqual(r['rub_per_price_point'],'1')
        self.assertEqual(r['initial_margin_long_rub'],'1400')
        self.assertEqual(r['initial_margin_short_rub'],'1500')
        self.assertNotIn('synthetic',json.dumps(r))
        c.call.assert_called_once_with('InstrumentsService/GetFuturesMargin',{'instrumentId':capture_current.UID})

    def test_invalid_margin_currency_or_tick_blocks_capture(self):
        c=self.client();c.call.return_value['initialMarginOnBuy']['currency']='usd'
        with self.assertRaises(DataError):capture_current.snapshot(c)
        c=self.client();c.call.return_value['minPriceIncrement']['nano']=200000000
        with self.assertRaises(DataError):capture_current.snapshot(c)

    def test_cli_credential_never_written_and_previous_environment_restored(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)/'safe.json'
            with patch.object(capture_current,'read_token',return_value='synthetic-hidden-token'), \
                 patch.object(capture_current,'ReadOnlyClient',return_value=self.client()), \
                 patch.dict(os.environ,{'T_INVEST_TOKEN':'synthetic-prior'}), \
                 patch('sys.argv',['capture_current','--output',str(path)]), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(capture_current.main(),0)
                self.assertEqual(os.environ['T_INVEST_TOKEN'],'synthetic-prior')
            self.assertNotIn('synthetic',path.read_text()+output.getvalue())
            if os.name=='posix':self.assertEqual(path.stat().st_mode&0o777,0o600)


if __name__=='__main__':unittest.main()
