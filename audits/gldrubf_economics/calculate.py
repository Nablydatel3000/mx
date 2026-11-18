"""Conditional RUB economics from frozen scenario outputs; no SMA or candle audit.

All numerical economics below are explicit scenarios, NOT verified contract/tariffs.
No broker API, token, SQLite or production service access.
"""
import argparse
from collections import defaultdict
import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import tarfile

HERE=Path(__file__).resolve().parent
INPUT=HERE.parent/'sma20_50_scenario/results'
ARCHIVE=HERE.parent/'gldrubf_real_history/source/GLDRUBF_history.tar.gz'
CAPITAL=D('10000')


@dataclass(frozen=True)
class Scenario:
    name:str
    rub_per_point:D=D('1')
    margin_long:D=D('1400')
    margin_short:D=D('1400')
    total_fee_per_side:D=D('0')
    adverse_carry_per_calendar_day:D=D('0')


def scenarios():
    return [Scenario('CONDITIONAL_GROSS_Q1_GO1400'),
        Scenario('ILLUSTRATIVE_TOTAL_FEE5',total_fee_per_side=D('5')),
        Scenario('ILLUSTRATIVE_FEE5_CARRY1',total_fee_per_side=D('5'),adverse_carry_per_calendar_day=D('1'))]


def read_csv(path):
    with path.open(encoding='utf-8',newline='') as stream:return list(csv.DictReader(stream))


def load_inputs():
    if hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()!='498e4aacb2da1d041a93efa34de807945f171fd44791541a14735a279e05ee7d':
        raise ValueError('Unexpected archive')
    # Read already-audited OHLC only for adverse intraday balance checks. No new SMA.
    with tarfile.open(ARCHIVE,'r:gz') as stream:
        bars={r['provider_day']:r for r in json.load(stream.extractfile('20261118T085824_435878Z/candles.json'))}
    return dict(daily=read_csv(INPUT/'daily_marks.csv'),fills=read_csv(INPUT/'fills.csv'),
        trades=read_csv(INPUT/'closed_trades.csv'),opened=read_csv(INPUT/'open_position.csv')[0],bars=bars)


def calculate(data,scenario):
    fills=defaultdict(list)
    for fill in data['fills']:fills[fill['provider_day']].append(fill)
    first=date.fromisoformat(data['fills'][0]['provider_day'])
    accumulated_fee=D(0);entry=None;all_marks=[];daily=[];peak=CAPITAL;max_dd=D(0);max_dd_pct=D(0)
    for row in data['daily']:
        day=row['provider_day'];position=int(row['position_after_open']);bar=data['bars'][day]
        accumulated_fee+=len(fills[day])*scenario.total_fee_per_side
        for fill in fills[day]:
            if fill['leg']=='OPEN':entry=D(fill['modeled_day_open'])
        elapsed=max(0,(date.fromisoformat(day)-first).days)
        carry=D(elapsed)*scenario.adverse_carry_per_calendar_day
        margin=scenario.margin_long if position==1 else scenario.margin_short if position==-1 else D(0)
        samples={}
        for kind,price in [('OPEN',row['open']),('CLOSE',row['close'])]+(
                [('ADVERSE_OHLC',bar['low'] if position==1 else bar['high'])] if position else []):
            gross_points=D(row['realized_gross_points'])+((D(price)-entry)*position if position else D(0))
            gross_rub=gross_points*scenario.rub_per_point
            equity=CAPITAL+gross_rub-accumulated_fee-carry
            mark=dict(scenario=scenario.name,provider_day=day,kind=kind,position=position,
                observed_or_model_price=str(price),gross_points=str(gross_points),gross_rub=str(gross_rub),
                cumulative_modeled_commission_rub=str(accumulated_fee),cumulative_adverse_carry_rub=str(carry),
                equity_rub=str(equity),assumed_initial_margin_rub=str(margin),free_above_assumed_margin_rub=str(equity-margin),
                simplified_margin_requirement_met=equity>=margin)
            all_marks.append(mark);samples[kind]=mark
        close=samples['CLOSE'];equity=D(close['equity_rub']);peak=max(peak,equity)
        drawdown=peak-equity;pct=drawdown/peak*100
        max_dd=max(max_dd,drawdown);max_dd_pct=max(max_dd_pct,pct)
        daily.append(dict(**close,daily_peak_equity_rub=str(peak),daily_drawdown_rub=str(drawdown),
                          daily_drawdown_percent=str(pct)))
    held=[r for r in all_marks if r['position']]
    lowest=min(held,key=lambda r:D(r['equity_rub']))
    lowest_daily=min((r for r in daily if r['position']),key=lambda r:D(r['equity_rub']))
    lowest_open=min((r for r in held if r['kind']=='OPEN'),key=lambda r:D(r['equity_rub']))
    free=min(held,key=lambda r:D(r['free_above_assumed_margin_rub']))
    failed=[r for r in held if not r['simplified_margin_requirement_met']]
    final=daily[-1]
    trades=[]
    for row in data['trades']:
        days=(date.fromisoformat(row['exit_provider_day'])-date.fromisoformat(row['entry_provider_day'])).days
        gross=D(row['gross_points'])*scenario.rub_per_point
        fee=2*scenario.total_fee_per_side;cost=D(days)*scenario.adverse_carry_per_calendar_day
        trades.append(dict(scenario=scenario.name,trade_id=row['trade_id'],direction=row['direction'],
            entry_provider_day=row['entry_provider_day'],exit_provider_day=row['exit_provider_day'],
            gross_points=row['gross_points'],conditional_gross_rub=str(gross),modeled_total_commission_rub=str(fee),
            carry_calendar_days=days,modeled_adverse_carry_rub=str(cost),conditional_net_rub=str(gross-fee-cost)))
    opened=data['opened'];days=(date.fromisoformat(opened['mark_provider_day'])-date.fromisoformat(opened['entry_provider_day'])).days
    gross_open=D(opened['unrealized_points'])*scenario.rub_per_point
    open_fee=scenario.total_fee_per_side;open_carry=D(days)*scenario.adverse_carry_per_calendar_day
    open_result=dict(scenario=scenario.name,direction=opened['direction'],entry_provider_day=opened['entry_provider_day'],
        mark_provider_day=opened['mark_provider_day'],conditional_unrealized_gross_rub=str(gross_open),
        entry_commission_rub=str(open_fee),carry_calendar_days=days,modeled_adverse_carry_rub=str(open_carry),
        conditional_open_contribution_rub=str(gross_open-open_fee-open_carry))
    closed_gross=sum(D(r['conditional_gross_rub']) for r in trades)
    closed_net=sum(D(r['conditional_net_rub']) for r in trades)
    total_net=D(final['equity_rub'])-CAPITAL
    assert closed_net+D(open_result['conditional_open_contribution_rub'])==total_net
    required_initial=max(D(r['assumed_initial_margin_rub'])-(D(r['equity_rub'])-CAPITAL) for r in held)
    summary=dict(scenario=scenario.name,all_economics_parameters_unverified=True,initial_capital_rub=str(CAPITAL),
        rub_per_point=str(scenario.rub_per_point),conditional_rub_per_tick=str(D('.1')*scenario.rub_per_point),
        assumed_long_initial_margin_rub=str(scenario.margin_long),assumed_short_initial_margin_rub=str(scenario.margin_short),
        assumed_total_commission_per_side_rub=str(scenario.total_fee_per_side),
        assumed_adverse_carry_per_calendar_day_rub=str(scenario.adverse_carry_per_calendar_day),
        closed_gross_rub=str(closed_gross),closed_commission_rub=str(16*scenario.total_fee_per_side),
        closed_adverse_carry_rub=str(sum(D(r['modeled_adverse_carry_rub']) for r in trades)),
        closed_conditional_net_rub=str(closed_net),unrealized_gross_rub=str(gross_open),
        open_conditional_contribution_rub=open_result['conditional_open_contribution_rub'],
        total_gross_rub=str(closed_gross+gross_open),total_commission_rub=final['cumulative_modeled_commission_rub'],
        total_adverse_carry_rub=final['cumulative_adverse_carry_rub'],total_conditional_net_rub=str(total_net),
        counterfactual_final_equity_rub=final['equity_rub'],feasible_scenario_final_equity_rub=final['equity_rub'] if not failed else None,
        daily_max_drawdown_rub=str(max_dd),daily_max_drawdown_percent=str(max_dd_pct),
        minimum_equity_rub=lowest['equity_rub'],minimum_equity_provider_day=lowest['provider_day'],minimum_equity_kind=lowest['kind'],
        minimum_daily_close_equity_rub=lowest_daily['equity_rub'],minimum_open_equity_rub=lowest_open['equity_rub'],
        minimum_free_above_assumed_margin_rub=free['free_above_assumed_margin_rub'],
        simplified_margin_requirement_met_on_all_samples=not failed,
        first_failed_provider_day=failed[0]['provider_day'] if failed else None,
        first_failed_sample=failed[0]['kind'] if failed else None,
        minimum_initial_capital_for_this_fixed_path_rub=str(required_initial),
        actual_broker_liquidation_rule_or_outcome_verified=False)
    return dict(summary=summary,trades=trades,opened=open_result,daily=daily,marks=all_marks)


def write_csv(path,rows):
    with path.open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]),lineterminator='\n');writer.writeheader();writer.writerows(rows)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=HERE/'results')
    args=parser.parse_args();args.output.mkdir(exist_ok=True,parents=True);data=load_inputs()
    cases=scenarios()
    runs=[calculate(data,c) for c in cases]
    write_csv(args.output/'scenario_summary.csv',[r['summary'] for r in runs])
    write_csv(args.output/'trades_rub_conditional.csv',[t for r in runs for t in r['trades']])
    write_csv(args.output/'open_position_conditional.csv',[r['opened'] for r in runs])
    write_csv(args.output/'daily_equity_conditional.csv',[m for r in runs for m in r['daily']])
    write_csv(args.output/'balance_checks_conditional.csv',[m for r in runs for m in r['marks']])
    stress=[]
    for go in ('1400','2800','4200'):
        c=Scenario('ILLUSTRATIVE_GO_'+go,margin_long=D(go),margin_short=D(go),total_fee_per_side=D('5'),adverse_carry_per_calendar_day=D('1'))
        stress.append(calculate(data,c)['summary'])
    write_csv(args.output/'margin_sensitivity.csv',stress)
    values=[]
    for point in ('.1','1','5','10'):
        c=Scenario('UNVERIFIED_POINT_VALUE_'+point,rub_per_point=D(point),total_fee_per_side=D('5'),adverse_carry_per_calendar_day=D('1'))
        values.append(calculate(data,c)['summary'])
    write_csv(args.output/'point_value_sensitivity.csv',values)
    report=dict(base_scenario_commit='531f527',verified_numeric_broker_economics=False,
        actual_long_margin=None,actual_short_margin=None,actual_rub_per_point=None,actual_fees=None,actual_funding=None,
        scenario_note='Q=1 RUB/point and GO=1400 are hypotheses, NOT official confirmed contract values.',
        carry_note='Adverse 1 RUB per elapsed calendar date to either side is generic cost stress, NOT GLDRUBF funding rules.',
        fees_note='5 RUB total per leg is a hypothetical all-in fee, NOT a T-bank tariff or separate exchange/broker schedule.',
        minimum_equity_note='OPEN, CLOSE and adverse OHLC checks under fixed DAY-open positions. Actual timestamps/GO/costs unknown.',
        input_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(INPUT.glob('*.csv'))},
        source_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),results=[r['summary'] for r in runs])
    (args.output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps([r['summary'] for r in runs],ensure_ascii=False,indent=2))


if __name__=='__main__':main()
