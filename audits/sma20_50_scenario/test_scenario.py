"""Accounting and timing controls using ONLY the supplied real-history observations."""
from decimal import Decimal as D
import unittest

from audits.sma20_50_scenario.run import load_real,run_scenario,reconcile


class RealScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows,cls.signals,_,_=load_real()
        cls.result=run_scenario(cls.rows,cls.signals)

    def test_trade_ledger_and_open_position_reconcile(self):
        r=self.result;s=r['summary']
        expected=[D(x) for x in ('-499.7','-595.4','-258','1943','-424','851','1675.2','1474.3')]
        self.assertEqual([D(t['gross_points']) for t in r['closed_trades']],expected)
        self.assertEqual(sum(expected),D('4166.4'))
        self.assertEqual(D(s['realized_gross_points'])+D(s['open_unrealized_points']),D('5471.9'))
        self.assertEqual(len(r['fills']),17)
        self.assertEqual(r['open_position'][0]['entry_provider_day'],'2026-09-26')
        self.assertEqual(r['open_position'][0]['entry_model_price'],'11574.1')
        self.assertEqual(r['open_position'][0]['unrealized_points'],'1305.5')

    def test_signal_cannot_fill_on_its_own_candle_and_waits_over_real_gap(self):
        daily={r['provider_day']:r for r in self.result['daily_marks']}
        self.assertEqual(daily['2025-02-21']['position_after_open'],0)
        self.assertEqual(daily['2025-02-21']['pending_for_next_open'],-1)
        first=self.result['fills'][0]
        self.assertEqual(first['provider_day'],'2025-02-24')
        self.assertEqual(first['modeled_day_open'],'8394.7')
        self.assertEqual(daily['2025-02-24']['cumulative_gross_points'],'111.5')
        for f in self.result['fills']:self.assertLess(f['signal_provider_day'],f['provider_day'])

    def test_every_fill_uses_the_raw_backed_next_observation_open(self):
        index={r['provider_day']:i for i,r in enumerate(self.rows)}
        for f in self.result['fills']:
            i=index[f['signal_provider_day']]+1
            self.assertEqual(self.rows[i]['provider_day'],f['provider_day'])
            self.assertEqual(self.rows[i]['open'],f['modeled_day_open'])
        for day in {f['provider_day'] for f in self.result['fills'][1:]}:
            fills=[f for f in self.result['fills'] if f['provider_day']==day]
            self.assertEqual([f['leg'] for f in fills],['CLOSE','OPEN'])
            self.assertEqual(fills[0]['modeled_day_open'],fills[1]['modeled_day_open'])

    def test_independent_gap_plus_intraday_mark_accounting(self):
        # Independent mark-to-market identity: old position owns the overnight gap,
        # new position owns today's open->close move. No entry/exit P&L algorithm.
        targets={s['provider_day']:int(s['exact_target']) for s in self.signals}
        position=0;pending=None;previous_close=None;total=D(0);peak=D(0);max_dd=D(0)
        for bar,actual in zip(self.rows,self.result['daily_marks']):
            o,c=D(bar['open']),D(bar['close'])
            if previous_close is not None:total+=position*(o-previous_close)
            if pending is not None:position=pending;pending=None
            total+=position*(c-o)
            self.assertEqual(total,D(actual['cumulative_gross_points']))
            peak=max(peak,total);max_dd=max(max_dd,peak-total)
            self.assertEqual(peak-total,D(actual['drawdown_points']))
            previous_close=c;pending=targets.get(bar['provider_day'])
        self.assertEqual(max_dd,D('2097.2'))
        self.assertEqual(total,D('5471.9'))

    def test_truncation_at_every_signal_keeps_past_marks_and_leaves_pending(self):
        index={r['provider_day']:i for i,r in enumerate(self.rows)}
        for signal in self.signals:
            length=index[signal['provider_day']]+1
            short=run_scenario(self.rows[:length],self.signals)
            self.assertEqual(short['daily_marks'],self.result['daily_marks'][:length])
            self.assertEqual(short['summary']['pending_unexecuted'],int(signal['exact_target']))
            self.assertTrue(all(f['provider_day']<=signal['provider_day'] for f in short['fills']))

    def test_unfinished_candle_excluded_no_final_liquidation_or_rub_conversion(self):
        self.assertEqual(len(self.result['daily_marks']),612)
        self.assertEqual(self.result['daily_marks'][-1]['provider_day'],'2026-11-17')
        self.assertEqual(self.result['fills'][-1]['leg'],'OPEN')
        self.assertEqual(self.result['daily_marks'][-1]['position_after_open'],-1)
        for key in ('profit_rub','return_percent','drawdown_percent','net_result'):
            self.assertIsNone(self.result['summary'][key])

    def test_reference_bridge_does_not_silently_reproduce_4220(self):
        table,report=reconcile(self.rows,self.signals,self.result['closed_trades'],self.result['summary'])
        self.assertEqual(sum(D(r['execution_model_difference_points']) for r in table),D('20.1'))
        self.assertEqual(report['unexplained_close_reference_minus_reported_points'],'-73.7')
        self.assertEqual(report['main_closed_minus_reported_points'],'-53.6')
        self.assertFalse(report['previous_ledger_available'])


if __name__=='__main__':unittest.main()
