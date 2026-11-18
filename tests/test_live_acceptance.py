"""Read-only acceptance tests. HTTP is mocked; no order-capable CLI is executed."""
import contextlib
from copy import deepcopy
from datetime import datetime,timedelta
from decimal import Decimal as D
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import Mock,patch

from src.gldrubf_bot import live_acceptance as acc
from src.gldrubf_bot.live_api import Broker,UID,quoted
from src.gldrubf_bot.live import load_config
from src.gldrubf_bot.models import DataError,UTC,iso
from tests.test_api import FakeResponse

TOKEN='synthetic-acceptance-token'
ACCOUNT='12345678'
REAL_CLASS=acc.ReadOnlyBroker


def money(value):return dict(currency='rub',**quoted(D(value)))


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory(prefix='mx read-only acceptance ');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.now=datetime(2026,11,18,12,tzinfo=UTC);self.clock=lambda:self.now
        self.c=deepcopy(acc.PROPOSED);self.c['account_id']=ACCOUNT
        self.cfg=self.root/'config.json';self.cfg.write_text(json.dumps(self.c))
        self.requests=[];self.balance=15000;self.position=0;self.go_short=1400;self.capacity=2
        self.bad_method=None;self.missing_margin=False;self.closed=False;self.no_complete=False
        self.quote_lag=0;self.inject_future=False;self.existing_orders=False
        self.clock_backwards=False
        self.wrong_status_uid=False;self.bad_orders_type=False
        self.secret_body=io.BytesIO(TOKEN.encode());self.responses_extra={'debug':TOKEN,'Authorization':TOKEN}
        self.env=self.enterContext(patch.dict(os.environ,{'T_INVEST_TOKEN':TOKEN}))

    def transport(self,request,timeout):
        method=request.full_url.split('contract.v1.')[1];body=json.loads(request.data)
        self.requests.append((method,body))
        if method==self.bad_method:
            raise urllib.error.HTTPError(request.full_url,502,TOKEN,{'Content-Type':'text/html'},self.secret_body)
        now=self.now
        if method.endswith('/GetAccounts'):
            r=dict(accounts=[dict(id=ACCOUNT,type='ACCOUNT_TYPE_TINKOFF',status='ACCOUNT_STATUS_OPEN',accessLevel='ACCOUNT_ACCESS_LEVEL_FULL_ACCESS',name=TOKEN)])
        elif method.endswith('/FutureBy'):
            r=dict(instrument=dict(uid=UID,figi='fixture',ticker='GLDRUBF',lot=1,currency='rub',exchange='forts_futures_weekend',minPriceIncrement=quoted('.1')))
        elif method.endswith('/GetPositions'):
            r=dict(accountId=ACCOUNT,money=[money(self.balance)],blocked=[],limitsLoadingInProgress=False,
                futures=[dict(instrumentUid=UID,balance=str(self.position),blocked='0')] if self.position else [])
        elif method.endswith('/GetPortfolio'):
            r=dict(accountId=ACCOUNT,totalAmountPortfolio=money(self.balance),positions=[dict(instrumentUid=UID,instrumentType='futures',quantity=quoted(self.position),varMargin=money(10))] if self.position else [])
        elif method.endswith('/GetWithdrawLimits'):r=dict(money=[money(self.balance)],blocked=[],blockedGuarantee=[])
        elif method.endswith('/GetMarginAttributes'):
            r=dict(liquidPortfolio=money(self.balance),startingMargin=money(0),minimalMargin=money(0),correctedMargin=money(0),amountOfMissingFunds=money(0))
            if self.missing_margin:r.pop('correctedMargin')
        elif method.endswith('/GetOrders'):
            r=dict(orders=[dict(instrumentUid=UID,orderRequestId='existing-request',orderId='existing-broker')] if self.existing_orders else [])
            if self.bad_orders_type:r['orders']={}
        elif method.endswith('/GetOrderState'):
            r=dict(instrumentUid=UID,orderRequestId='existing-request',orderId='existing-broker',executionReportStatus='EXECUTION_REPORT_STATUS_NEW',lotsRequested='2',lotsExecuted='0',stages=[])
        elif method.endswith('/GetFuturesMargin'):
            r=dict(minPriceIncrement=quoted('.1'),minPriceIncrementAmount=quoted('.1'),initialMarginOnBuy=money(1400),initialMarginOnSell=money(self.go_short))
        elif method.endswith('/GetTradingStatus'):
            r=dict(instrumentUid=UID,tradingStatus='SECURITY_TRADING_STATUS_NOT_AVAILABLE_FOR_TRADING' if self.closed else 'SECURITY_TRADING_STATUS_NORMAL_TRADING',apiTradeAvailableFlag=not self.closed,limitOrderAvailableFlag=not self.closed,marketOrderAvailableFlag=not self.closed)
            if self.wrong_status_uid:r['instrumentUid']='other-instrument'
        elif method.endswith('/GetLastPrices'):
            stamp=now-timedelta(seconds=self.quote_lag)+(timedelta(seconds=1) if self.inject_future else timedelta())
            r=dict(lastPrices=[dict(instrumentUid=UID,time=iso(stamp),price=quoted(100),lastPriceType='LAST_PRICE_EXCHANGE')])
        elif method.endswith('/GetOrderBook'):
            r=dict(instrumentUid=UID,orderbookTs=iso(now),bids=[dict(price=quoted('99.9'),quantity='2')],asks=[dict(price=quoted('100.1'),quantity='2')],limitDown=quoted(50),limitUp=quoted(150))
        elif method.endswith('/TradingSchedules'):
            label=now.date().isoformat()
            r=dict(exchanges=[dict(exchange='FORTS_FUTURES_WEEKEND',days=[dict(date=label+'T00:00:00Z',isTradingDay=True,
                startTime=label+'T07:00:00Z',endTime=label+'T21:00:00Z',
                intervals=[dict(type='regular_trading_session',interval=dict(startTs=label+'T07:00:00Z',endTs=label+'T21:00:00Z'))])])])
        elif method.endswith('/GetCandles'):
            r=dict(candles=[dict(time=iso(datetime.combine(now.date()-timedelta(days=n),datetime.min.time(),UTC)),
                open=quoted(100),high=quoted(101),low=quoted(99),close=quoted(100),volume='10',isComplete=n>0 and not self.no_complete,
                candleSource='CANDLE_SOURCE_EXCHANGE') for n in (2,1,0)])
        elif method.endswith('/GetMaxLots'):
            r=dict(currency='rub',buyMarginLimits=dict(buyMaxLots=str(self.capacity)),sellMarginLimits=dict(sellMaxLots=str(self.capacity)))
        elif method.endswith('/GetOrderPrice'):
            self.assertEqual(body['quantity'],'2')
            r=dict(executedCommissionRub=money(10),executedCommission=money(10),lotsRequested='2')
        elif method.endswith('/GetOperationsByCursor'):
            r=dict(items=[],hasNext=False)
            if self.clock_backwards:self.now-=timedelta(hours=1)
        else:raise AssertionError('Unexpected HTTP method '+method)
        return FakeResponse(json.dumps(dict(**r,**self.responses_extra)).encode())

    def broker(self):return REAL_CLASS(ACCOUNT,transport=self.transport,clock=self.clock)
    def collect(self):return acc.collect(self.c,self.broker(),self.clock)

    def test_success_checks_all_read_methods_but_never_accepts_live_or_ioc(self):
        r=self.collect();self.assertTrue(r['read_checks_ok'],r['blockers'])
        self.assertFalse(r['live_accepted']);self.assertFalse(r['ioc']['instrument_runtime_support_confirmed'])
        self.assertEqual(r['order_submission_count'],0);self.assertEqual(r['order_cancellation_count'],0)
        self.assertTrue(r['market']['tradable_now'])
        methods={m for m,p in self.requests};self.assertTrue(methods<=acc.READ_METHODS)
        self.assertIn('OrdersService/GetOrderPrice',methods);self.assertIn('OrdersService/GetMaxLots',methods)
        self.assertNotIn('OrdersService/GetOrderState',methods)
    def test_all_mutation_entry_points_permanently_denied_even_with_guard(self):
        b=self.broker();b.write_guard=Mock()
        for action in (lambda:b.post({}),lambda:b.cancel('x'),lambda:Broker.post(b,{}),
                       lambda:b.call('OrdersService/PostOrder',{}),lambda:b.call('OrdersService/CancelOrder',{}),
                       lambda:b.call('OrdersService/ReplaceOrder',{}),lambda:b.call('UsersService/CurrencyTransfer',{}),
                       lambda:b.read.call('OrdersService/PostOrder',{})):
            with self.assertRaises(DataError):action()
        self.assertFalse(self.requests);b.write_guard.assert_not_called()
    def test_reports_exclude_token_account_names_ids_and_arbitrary_response(self):
        raw=json.dumps(self.collect())
        for forbidden in (TOKEN,ACCOUNT,'Authorization','debug','existing-request','existing-broker'):
            self.assertNotIn(forbidden,raw)
    def test_non_json_502_closes_body_and_records_safe_method_status(self):
        self.bad_method='InstrumentsService/FutureBy';r=self.collect()
        self.assertFalse(r['read_checks_ok']);self.assertTrue(self.secret_body.closed)
        e=next(x for x in r['method_checks'] if x['method']==self.bad_method)
        self.assertEqual(e['http_status'],502);self.assertEqual(e['content_type'],'text/html')
        self.assertNotIn(TOKEN,json.dumps(r))
    def test_http_200_missing_margin_field_blocks_and_projects_presence(self):
        self.missing_margin=True;r=self.collect()
        self.assertIn('account_snapshot',r['blockers'])
        method=next(x for x in r['method_checks'] if x['method']=='UsersService/GetMarginAttributes')
        self.assertEqual(method['http_status'],200);self.assertFalse(method['fields_present']['correctedMargin'])
    def test_conservative_profile_refuses_insufficient_capital(self):
        self.balance=10000;r=self.collect()
        self.assertIn('capital_long',r['blockers']);self.assertIn('capital_short',r['blockers'])
        self.assertEqual(r['statistics']['capital_checks']['long']['required_rub'],'14604.4')
    def test_short_margin_is_checked_independently(self):
        self.go_short=1500;r=self.collect()
        self.assertTrue(r['statistics']['capital_checks']['long']['allowed'])
        self.assertFalse(r['statistics']['capital_checks']['short']['allowed'])
    def test_capacity_one_does_not_downsize_two_contract_estimate(self):
        self.capacity=1;r=self.collect()
        self.assertIn('capacity_two_long',r['blockers'])
        self.assertTrue(all(p['quantity']=='2' for m,p in self.requests if m=='OrdersService/GetOrderPrice'))
    def test_existing_position_blocks_flat_onboarding_without_closing(self):
        self.position=1;r=self.collect();self.assertIn('dedicated_flat_account',r['blockers'])
        self.assertEqual(r['statistics']['position'],1)
    def test_existing_order_only_read_no_probe_or_cancel_created(self):
        self.existing_orders=True;r=self.collect()
        self.assertIn('no_open_orders',r['blockers']);self.assertTrue(r['statistics']['existing_order_state_read'])
        self.assertTrue(all(m in acc.READ_METHODS for m,p in self.requests))
        self.assertNotIn('existing-request',json.dumps(r))
    def test_closed_market_is_waiting_not_ioc_proof(self):
        self.closed=True;r=self.collect();self.assertTrue(r['read_checks_ok']);self.assertFalse(r['market']['tradable_now'])
    def test_wrong_status_instrument_is_a_schema_blocker(self):
        self.wrong_status_uid=True;r=self.collect();self.assertIn('trading_status_schema',r['blockers'])
    def test_non_list_orders_cannot_masquerade_as_no_orders(self):
        self.bad_orders_type=True;r=self.collect();self.assertIn('open_orders',r['blockers'])
    def test_stale_quote_stops_market_gate_without_faking_new_time(self):
        self.quote_lag=16;r=self.collect();self.assertFalse(r['market']['quote_fresh']);self.assertFalse(r['market']['tradable_now'])
    def test_future_quote_is_validation_blocker(self):
        self.inject_future=True;r=self.collect();self.assertIn('last_price_schema',r['blockers'])
    def test_backwards_clock_never_marks_current_check_successful(self):
        self.clock_backwards=True;r=self.collect();self.assertIn('clock_backwards',r['blockers'])
        self.assertFalse(r['read_checks_ok'])
    def test_no_completed_candles_never_invents_history(self):
        self.no_complete=True;r=self.collect();self.assertIn('completed_history_missing',r['blockers'])
    def test_profile_covers_every_null_and_matches_example(self):
        nulls=json.loads(Path('deploy/live/config.example.json').read_text())
        proposed=json.loads(Path('deploy/live/config.acceptance.example.json').read_text())
        self.assertEqual(proposed,acc.PROPOSED)
        self.assertTrue(all(proposed[k] is not None for k,v in nulls.items() if v is None))
        self.assertEqual(load_config(self.cfg)['contracts'],2)

    def run_check(self):
        def factory(account):return REAL_CLASS(account,transport=self.transport,clock=self.clock)
        actual_collect=acc.collect
        with patch.object(acc,'ReadOnlyBroker',side_effect=factory), \
             patch.object(acc,'collect',side_effect=lambda c,b:actual_collect(c,b,self.clock)), \
             patch.object(acc.sys,'stdin',SimpleNamespace(buffer=io.BytesIO((TOKEN+'\n').encode()))), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            code=acc.main(['check','--credential-stdin','--config',str(self.cfg),'--state',str(self.root)])
        return code,output.getvalue()
    def test_cli_reads_protected_stdin_no_live_sqlite_or_permission(self):
        with patch.dict(os.environ,{'T_INVEST_TOKEN':'synthetic-prior'}):
            code,output=self.run_check();self.assertEqual(code,0)
            self.assertEqual(os.environ['T_INVEST_TOKEN'],'synthetic-prior')
        self.assertNotIn(TOKEN,output)
        for name in ('live.sqlite3','LIVE_PERMISSION.json','LEDGER_INITIALIZED.json','STOP'):
            self.assertFalse((self.root/name).exists())
        report=self.root/'report.json'
        self.assertNotIn(TOKEN,report.read_text())
        if os.name=='posix':self.assertEqual(report.stat().st_mode&0o777,0o600)
    def test_failed_restart_never_presents_old_green_report_as_current(self):
        self.assertEqual(self.run_check()[0],0)
        old=(self.root/'report.json').read_bytes()
        with patch.object(acc,'ReadOnlyBroker',side_effect=RuntimeError(TOKEN)), \
             patch.object(acc.sys,'stdin',SimpleNamespace(buffer=io.BytesIO((TOKEN+'\n').encode()))), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(acc.main(['check','--credential-stdin','--config',str(self.cfg),'--state',str(self.root)]),78)
        self.assertEqual(old,(self.root/'report.json').read_bytes())
        result=acc.status(self.root,self.clock);self.assertEqual(result['last_attempt']['status'],'FAILED')
        self.assertFalse(result['report_matches_last_attempt']);self.assertNotIn(TOKEN,output.getvalue())
        self.assertEqual(self.run_check()[0],0)
        self.assertTrue(acc.status(self.root,self.clock)['report_matches_last_attempt'])
    def test_api_failure_and_manual_retry_remain_read_only(self):
        self.bad_method='InstrumentsService/FutureBy'
        self.assertEqual(self.run_check()[0],2)
        self.assertFalse(acc.status(self.root,self.clock)['report']['read_checks_ok'])
        self.bad_method=None;self.assertEqual(self.run_check()[0],0)
        r=acc.status(self.root,self.clock)['report'];self.assertTrue(r['read_checks_ok']);self.assertFalse(r['live_accepted'])
        self.assertTrue(all(m in acc.READ_METHODS for m,p in self.requests))
    def test_status_does_not_make_api_calls_and_labels_old_cache(self):
        self.run_check();self.now+=timedelta(seconds=301)
        with patch.object(acc,'ReadOnlyBroker') as b:
            result=acc.status(self.root,self.clock);b.assert_not_called()
        self.assertFalse(result['cache_fresh'])
    def test_configure_and_token_install_do_not_overwrite_existing_files(self):
        path=self.root/'token'
        with patch.object(acc,'hidden',return_value=TOKEN),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(acc.main(['install-token','--credential-file',str(path),'--state',str(self.root)]),0)
            before=path.read_bytes();self.assertEqual(acc.main(['install-token','--credential-file',str(path),'--state',str(self.root)]),78)
            self.assertEqual(before,path.read_bytes())
        config_before=self.cfg.read_bytes()
        with patch.object(acc,'hidden') as h,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(acc.main(['configure','--config',str(self.cfg),'--state',str(self.root)]),78);h.assert_not_called()
        self.assertEqual(config_before,self.cfg.read_bytes())
    def test_protected_paper_or_live_directory_and_symlink_refused(self):
        protected=self.root/'paper';protected.mkdir();(protected/'paper.sqlite3').write_bytes(b'preserved')
        alias=self.root/'alias'
        try:alias.symlink_to(protected,target_is_directory=True)
        except OSError:alias=protected
        with patch.object(acc,'PROTECTED_ROOTS',(protected,)),patch.object(acc,'ReadOnlyBroker') as b,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(acc.main(['check','--state',str(alias),'--config',str(self.cfg)]),78);b.assert_not_called()
        self.assertEqual(list(protected.iterdir()),[protected/'paper.sqlite3'])
    def test_install_scripts_no_unit_activation_or_paper_paths(self):
        script=Path('deploy/live/install_readonly.sh').read_text()
        for forbidden in ('systemctl start','systemctl enable','daemon-reload','/var/lib/gldrubf/', 'LIVE_PERMISSION'):
            self.assertNotIn(forbidden,script)
        wrapper=Path('deploy/live/acceptance_check.sh').read_text()
        self.assertIn('--credential-stdin < /etc/gldrubf-live-acceptance/trading-token',wrapper)
        self.assertNotIn('$(cat',wrapper);self.assertNotIn('set -x',wrapper)
    def test_unknown_account_is_never_automatically_selected(self):
        target=self.root/'new_config.json'
        with patch.object(acc,'hidden',return_value=''),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(acc.main(['configure','--config',str(target),'--state',str(self.root)]),0)
        with self.assertRaises(DataError):load_config(target)
        with patch.object(acc,'hidden',return_value=ACCOUNT),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(acc.main(['account','--config',str(target),'--state',str(self.root)]),0)
        self.assertEqual(load_config(target)['account_id'],ACCOUNT)
    def test_account_list_is_local_read_only_and_does_not_create_report(self):
        def factory(account):return REAL_CLASS(account,transport=self.transport,clock=self.clock)
        with patch.object(acc,'ReadOnlyBroker',side_effect=factory), \
             patch.object(acc.sys,'stdin',SimpleNamespace(buffer=io.BytesIO((TOKEN+'\n').encode()))), \
             contextlib.redirect_stdout(io.StringIO()) as output,patch.object(output,'isatty',return_value=True):
            self.assertEqual(acc.main(['accounts','--credential-stdin','--state',str(self.root),'--config',str(self.cfg)]),0)
        self.assertNotIn(TOKEN,output.getvalue());self.assertIn(ACCOUNT,output.getvalue())
        self.assertEqual([m for m,p in self.requests],['UsersService/GetAccounts'])
        self.assertFalse((self.root/'report.json').exists())


if __name__=='__main__':unittest.main()
