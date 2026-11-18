"""Fixed SMA20/50 NEXT_DAY_OPEN gross-points scenario on the audited real archive.

No RUB conversion, inferred exchange timestamps, broker/API calls or SQLite.
Run from repo root: python audits/sma20_50_scenario/run.py
"""
import argparse
import csv
from datetime import date, timedelta
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

REPO=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO))
from audits.gldrubf_real_history.audit import unpack,quality,calculate,require

HERE=Path(__file__).resolve().parent
ARCHIVE=HERE.parent/'gldrubf_real_history/source/GLDRUBF_history.tar.gz'
ARCHIVE_SHA='498e4aacb2da1d041a93efa34de807945f171fd44791541a14735a279e05ee7d'
REFERENCE=D('4220')  # User-reported previous number, not a verified trade ledger.


def text(value):
    if isinstance(value,D):return format(value,'f')
    return value


def load_real():
    require(hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()==ARCHIVE_SHA,'Wrong archive')
    with TemporaryDirectory(prefix='gldrubf points scenario ') as folder:
        root,_=unpack(ARCHIVE,Path(folder))
        rows,checks,_=quality(root)
        _,signals,comparison=calculate(rows)
    complete=[r for r in rows if r['is_complete']]
    require(all(r['is_complete'] for r in rows[:len(complete)]),'Incomplete candles inside historical prefix')
    return complete,signals,checks,comparison


def run_scenario(rows,signals):
    """At each row: pending at OPEN, mark at CLOSE, new signal only AFTER CLOSE."""
    targets={s['provider_day']:s for s in signals}
    position=0;entry=None;pending=None;realized=D(0);peak=D(0)
    peak_day='INITIAL_BASELINE';max_dd=D(0);max_peak=D(0);trough_pnl=D(0)
    dd_peak_day=dd_trough_day=None
    closed=[];fills=[];daily=[];trade_id=0
    for bar in rows:
        day=bar['provider_day'];o,c=D(bar['open']),D(bar['close']);before=position
        require(bar['is_complete'],'Scenario evaluation horizon excludes incomplete rows')
        if pending is not None:
            target=int(pending['exact_target'])
            require(pending['provider_day']<day,'Same-candle execution prohibited')
            if target!=position:
                if position:
                    pnl=(o-D(entry['entry_model_price']))*position
                    realized+=pnl
                    closed.append(dict(**entry,exit_signal_provider_day=pending['provider_day'],
                        exit_provider_day=day,exit_model_price=str(o),gross_points=str(pnl)))
                    fills.append(dict(fill_id=len(fills)+1,trade_id=trade_id,provider_day=day,
                        signal_provider_day=pending['provider_day'],leg='CLOSE',side=-position,
                        modeled_day_open=str(o),position_after=0))
                trade_id+=1;position=target
                entry=dict(trade_id=trade_id,contracts=1,direction='LONG' if position==1 else 'SHORT',
                    entry_signal_provider_day=pending['provider_day'],entry_provider_day=day,entry_model_price=str(o))
                fills.append(dict(fill_id=len(fills)+1,trade_id=trade_id,provider_day=day,
                    signal_provider_day=pending['provider_day'],leg='OPEN',side=position,
                    modeled_day_open=str(o),position_after=position))
            pending=None
        unreal=(c-D(entry['entry_model_price']))*position if position else D(0)
        pnl=realized+unreal
        if pnl>peak:peak=pnl;peak_day=day
        drawdown=peak-pnl
        if drawdown>max_dd:
            max_dd=drawdown;dd_peak_day=peak_day;dd_trough_day=day;max_peak=peak;trough_pnl=pnl
        # Critically, today's signal is not eligible at today's OPEN or CLOSE.
        signal=targets.get(day)
        if signal is not None:pending=signal
        daily.append(dict(provider_day=day,provider_time_utc=bar['time'],open=bar['open'],close=bar['close'],
            position_before_open=before,position_after_open=position,
            signal_after_close=signal['signal'] if signal else '',
            pending_for_next_open=int(pending['exact_target']) if pending else 0,
            realized_gross_points=str(realized),unrealized_points=str(unreal),
            cumulative_gross_points=str(pnl),running_peak_points=str(peak),drawdown_points=str(drawdown)))
    last=rows[-1]
    opened=dict(**entry,mark_provider_day=last['provider_day'],mark_day_close=last['close'],
                unrealized_points=str(unreal)) if position else None
    recovery=next((r['provider_day'] for r in daily if dd_trough_day and r['provider_day']>dd_trough_day
                   and D(r['cumulative_gross_points'])>=max_peak),None)
    summary=dict(model='NEXT_AVAILABLE_DAY_OPEN',units='price_points_per_one_contract',
        first_complete=rows[0]['provider_day'],last_complete=last['provider_day'],completed_candles=len(rows),
        signal_count=sum(r['provider_day'] in targets for r in rows),closed_trades=len(closed),fill_legs=len(fills),
        realized_gross_points=str(realized),open_unrealized_points=str(unreal),total_gross_points=str(pnl),
        max_daily_mark_drawdown_points=str(max_dd),drawdown_peak_provider_day=dd_peak_day,
        drawdown_trough_provider_day=dd_trough_day,drawdown_peak_cumulative_points=str(max_peak),
        drawdown_trough_cumulative_points=str(trough_pnl),drawdown_recovery_provider_day=recovery,
        pending_unexecuted=int(pending['exact_target']) if pending else 0,
        profit_rub=None,return_percent=None,drawdown_percent=None,net_result=None,
        costs='EXCLUDED: commissions, spread, slippage, funding; gross only',
        historical_publication_and_execution_verified=False)
    return dict(summary=summary,closed_trades=closed,fills=fills,daily_marks=daily,
                open_position=[opened] if opened else [])


def reconcile(rows,signals,closed,summary):
    # This ZERO-LAG CLOSE reference is only for the requested +4220 reconciliation.
    # It is NOT an executable baseline and does not choose the main model.
    mapping={r['provider_day']:r for r in rows};table=[]
    for i,trade in enumerate(closed):
        a,b=signals[i],signals[i+1]
        old=(D(mapping[b['provider_day']]['close'])-D(mapping[a['provider_day']]['close']))*int(a['exact_target'])
        table.append(dict(trade_id=i+1,direction=a['signal'],signal_entry_day=a['provider_day'],
            signal_exit_day=b['provider_day'],zero_lag_signal_close_reference_points=str(old),
            main_next_open_points=trade['gross_points'],execution_model_difference_points=str(D(trade['gross_points'])-old)))
    ref=sum((D(r['zero_lag_signal_close_reference_points']) for r in table),D(0))
    real=D(summary['realized_gross_points']);total=D(summary['total_gross_points'])
    return table,dict(user_reported_previous_points=str(REFERENCE),previous_ledger_available=False,
        zero_lag_close_reference_closed_points=str(ref),
        unexplained_close_reference_minus_reported_points=str(ref-REFERENCE),
        next_open_minus_close_reference_closed_points=str(real-ref),
        main_closed_minus_reported_points=str(real-REFERENCE),
        open_position_added_points=summary['open_unrealized_points'],
        main_total_minus_reported_points=str(total-REFERENCE),
        note='Previous +4220 period/prices/costs/open-position convention unknown; residual not assigned a fabricated cause.')


def data_requests(rows,signals):
    index={r['provider_day']:i for i,r in enumerate(rows)};result=[]
    for signal in signals:
        i=index[signal['provider_day']]
        if i+1==len(rows):continue
        next_day=rows[i+1]['provider_day']
        # Technical UTC request buffer, not a claim that these are full exchange sessions.
        end=(date.fromisoformat(next_day)+timedelta(days=2)).isoformat()
        result.append(dict(signal_provider_day=signal['provider_day'],direction=signal['signal'],
            modeled_entry_provider_day=next_day,uid='b347fe28-0d2a-45bf-b3bd-cda8a6ac64e6',
            request_from_utc=signal['provider_day']+'T00:00:00Z',request_to_utc_exclusive=end+'T00:00:00Z',
            interval='CANDLE_INTERVAL_1_MIN',scope='initial_candidate_window_not_verified_session_mapping'))
    return result


def write_csv(path,rows):
    require(bool(rows),'Unexpected empty result')
    with path.open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]),lineterminator='\n')
        writer.writeheader();writer.writerows(rows)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=HERE/'results')
    args=parser.parse_args();rows,signals,quality_report,comparison=load_real()
    result=run_scenario(rows,signals)
    table,reconciliation=reconcile(rows,signals,result['closed_trades'],result['summary'])
    args.output.mkdir(parents=True,exist_ok=True)
    for key in ('closed_trades','fills','daily_marks','open_position'):write_csv(args.output/(key+'.csv'),result[key])
    write_csv(args.output/'reconciliation.csv',table)
    write_csv(args.output/'m1_requests.csv',data_requests(rows,signals))
    report=dict(source_archive_sha256=ARCHIVE_SHA,signal_audit_commit='d68f4cc',
        source_script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        assumptions=['Initial position flat; first 50 completed observations warm up unchanged SMA20/50.',
         'After each completed candle, signal becomes known before the next available DAY open (UNVERIFIED assumption).',
         'Execute both reversal legs at that next DAY open with no costs, spread or slippage.',
         'DAY labels identify row order, not verified publication/open/fill timestamps.',
         'Mark daily on complete DAY closes; exclude unfinished 2026-11-18 candle.',
         'No forward filling missing dates; no forced liquidation at the last mark.',
         'Point size is one quoted price unit; no historical RUB point value assumed.'],
        quality=quality_report,signal_crosscheck=comparison,summary=result['summary'],reconciliation=reconciliation)
    (args.output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(dict(summary=result['summary'],reconciliation=reconciliation),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
