"""Optional LOCAL read-only contract snapshot, never run by the economics calculation.

Hidden interactive token; only FutureBy and GetFuturesMargin; no accounts/orders.
No token, Authorization, arbitrary response body or traceback written to output.
"""
import argparse
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import sys

REPO=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO))
from src.gldrubf_bot.api import ReadOnlyClient,quotation
from src.gldrubf_bot.diagnose_api import read_token
from src.gldrubf_bot.models import DataError

UID='b347fe28-0d2a-45bf-b3bd-cda8a6ac64e6'


def snapshot(client):
    instrument=client.instrument(UID)  # FutureBy validates GLDRUBF, RUB, lot=1, UID.
    raw=client.call('InstrumentsService/GetFuturesMargin',{'instrumentId':UID})
    tick=quotation(raw.get('minPriceIncrement'));amount=quotation(raw.get('minPriceIncrementAmount'))
    if instrument.uid!=UID or tick!=instrument.tick or tick<=0 or amount<=0:
        raise DataError('Inconsistent current monetary specification')
    margins={}
    for source,name in [('initialMarginOnBuy','initial_margin_long_rub'),('initialMarginOnSell','initial_margin_short_rub')]:
        value=raw.get(source,{})
        if value.get('currency','').lower()!='rub':raise DataError('Margin currency not RUB')
        amount_margin=quotation(value)
        if amount_margin<=0:raise DataError('Invalid margin')
        margins[name]=str(amount_margin)
    # Strict projection, no response/header/body reflection.
    return dict(schema=1,uid=UID,ticker='GLDRUBF',observed_at_utc=datetime.now(timezone.utc).isoformat(),
        source_methods=['InstrumentsService/FutureBy','InstrumentsService/GetFuturesMargin'],
        tick_price_points=str(tick),tick_amount_rub=str(amount),rub_per_price_point=str(amount/tick),
        **margins,historical_specification_or_tariff_verified=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path)
    args=parser.parse_args();previous=os.environ.get('T_INVEST_TOKEN')
    try:
        os.environ.pop('SSLKEYLOGFILE',None)
        os.environ['T_INVEST_TOKEN']=read_token()
        try:client=ReadOnlyClient()
        finally:
            if previous is None:os.environ.pop('T_INVEST_TOKEN',None)
            else:os.environ['T_INVEST_TOKEN']=previous
        result=snapshot(client)
        path=args.output or Path('GLDRUBF_economics_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json')
        with path.open('x',encoding='utf-8') as stream:
            os.chmod(path,0o600);json.dump(result,stream,ensure_ascii=False,indent=2);stream.write('\n')
        print('Safe current contract snapshot saved; no account/order methods called.')
        return 0
    except (Exception,KeyboardInterrupt):
        print('STOP: current contract snapshot unavailable or inconsistent. No response body or credentials logged.')
        return 1
    finally:
        if previous is None:os.environ.pop('T_INVEST_TOKEN',None)
        else:os.environ['T_INVEST_TOKEN']=previous


if __name__=='__main__':raise SystemExit(main())
