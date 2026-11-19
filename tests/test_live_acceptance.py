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
from src.gldrubf_bot.account_diagnostics import METHODS
from src.gldrubf_bot.live import load_config
from src.gldrubf_bot.models import DataError,UTC,iso
from tests.test_api import FakeResponse
from tests.test_detailed_sessions import reported_day

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
        self.requests=[];self.balance=15000;self.position=0;self.go_long=1400;self.go_short=1400;self.capacity=2
        self.fee=D(10);self.account_changes={}
        self.bad_method=None;self.missing_margin=False;self.closed=False;self.no_complete=False
        self.margin_api_error=None
        self.quote_lag=0;self.inject_future=False;self.existing_orders=False
        self.clock_backwards=False
        self.wrong_status_uid=False;self.bad_orders_type=False
        self.secret_body=io.BytesIO(TOKEN.encode());self.responses_extra={'debug':TOKEN,'Authorization':TOKEN}
        self.env=self.enterContext(patch.dict(os.environ,{'T_INVEST_TOKEN':TOKEN}))

    def transport(self,request,timeout):
        method=request.full_url.split('contract.v1.')[1];body=json.loads(request.data)
        self.requests.append((method,body))
        if method=='UsersService/GetMarginAttributes' and self.margin_api_error is not None:
            code,grpc=self.margin_api_error
            payload=json.dumps(dict(code=grpc,description=code,message=TOKEN+' '+ACCOUNT)).encode()
            raise urllib.error.HTTPError(request.full_url,400,TOKEN,{'Content-Type':'application/json'},io.BytesIO(payload))
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
            r=dict(minPriceIncrement=quoted('.1'),minPriceIncrementAmount=quoted('.1'),initialMarginOnBuy=money(self.go_long),initialMarginOnSell=money(self.go_short))
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
            r=dict(executedCommissionRub=money(self.fee),executedCommission=money(self.fee),lotsRequested='2')
        elif method.endswith('/GetOperationsByCursor'):
            r=dict(items=[],hasNext=False)
            if self.clock_backwards:self.now-=timedelta(hours=1)
        else:raise AssertionError('Unexpected HTTP method '+method)
        if method in self.account_changes:self.account_changes[method](r)
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

    def test_missing_margin_is_not_reported_as_insufficient_cash(self):
        self.missing_margin=True;r=self.collect()
        d=r['statistics']['account_diagnostic']
        self.assertEqual(d['failure'],dict(method='UsersService/GetMarginAttributes',field='correctedMargin',reason='required_money_missing'))
        self.assertEqual(d['failure_group'],'schema_error')
        for side in ('long','short'):
            c=r['statistics']['capital_checks'][side]
            self.assertFalse(c['allowed']);self.assertEqual(c['evaluation'],'not_evaluated')
            self.assertEqual(c['reason'],'account_snapshot_unavailable');self.assertNotIn('available_rub',c)
        self.assertNotIn(TOKEN,json.dumps(d));self.assertNotIn(ACCOUNT,json.dumps(d))

    def test_real_observed_go_fee_profile_and_capital_arithmetic(self):
        self.go_long=D('1035.82');self.go_short=D('1036.67');self.fee=D('20.28');self.capacity=10
        r=self.collect();self.assertTrue(r['read_checks_ok'],r['blockers'])
        checks=r['statistics']['capital_checks']
        self.assertEqual(D(checks['long']['required_rub']),D('12429.60'))
        self.assertEqual(D(checks['short']['required_rub']),D('12434.70'))
        self.c['max_order_fee_rub']='20';r=self.collect()
        self.assertIn('fee_ceiling_long',r['blockers']);self.assertIn('fee_ceiling_short',r['blockers'])
        self.assertTrue(r['statistics']['capital_checks']['long']['allowed'])
        self.assertFalse(r['read_checks_ok'])

    def test_liquid_and_withdraw_limits_are_not_replaced_by_claimed_deposit(self):
        self.account_changes={
          'OperationsService/GetPositions':lambda r:r.update(blocked=[money(200)]),
          'OperationsService/GetWithdrawLimits':lambda r:r.update(money=[money(12000)],blocked=[money(500)],blockedGuarantee=[money(2000)]),
          'UsersService/GetMarginAttributes':lambda r:r.update(liquidPortfolio=money(14500),startingMargin=money(1000),correctedMargin=money(1500))}
        r=self.collect();s=r['statistics'];d=s['account_diagnostic']
        self.assertTrue(d['snapshot_validated']);self.assertEqual(s['balance_rub'],'15000')
        self.assertEqual(s['available_conservative_rub'],'9500')
        self.assertEqual(d['availability_components_rub'],dict(positions_cash_minus_blocked_rub='14800',
            withdraw_money_minus_blocked_and_guarantee_rub='9500',liquid_minus_starting_margin_rub='13500',liquid_minus_corrected_margin_rub='13000'))
        self.assertEqual(s['capital_checks']['long']['evaluation'],'evaluated')
        self.assertIn('capital_long',r['blockers']);self.assertNotIn('account_snapshot',r['blockers'])

    def test_account_echo_missing_or_wrong_is_not_printed(self):
        for method in ('OperationsService/GetPositions','OperationsService/GetPortfolio'):
            for change in (lambda r:r.pop('accountId'),lambda r:r.update(accountId=TOKEN)):
                with self.subTest(method=method):
                    self.account_changes={method:change};r=self.collect();d=r['statistics']['account_diagnostic']
                    self.assertEqual(d['failure']['reason'],'account_echo_missing_or_mismatch')
                    self.assertNotIn(TOKEN,json.dumps(r));self.assertNotIn(ACCOUNT,json.dumps(r))

    def test_invalid_required_money_fields_are_identified_by_path(self):
        for field in ('liquidPortfolio','startingMargin','minimalMargin','correctedMargin','amountOfMissingFunds'):
            for invalid in (None,dict(currency='usd',units='15000'),dict(currency='rub',units=TOKEN)):
                with self.subTest(field=field,invalid=type(invalid).__name__):
                    self.account_changes={'UsersService/GetMarginAttributes':lambda r,f=field,v=invalid:r.update({f:v})}
                    r=self.collect();failure=r['statistics']['account_diagnostic']['failure']
                    self.assertEqual(failure['field'],field);self.assertEqual(failure['reason'],'required_money_invalid_or_non_rub')
                    self.assertNotIn(TOKEN,json.dumps(r))

    def test_empty_arrays_and_omitted_proto_zero_parts_are_valid_not_guessed_funds(self):
        self.account_changes={'OperationsService/GetPositions':lambda r:r.pop('blocked'),
            'OperationsService/GetWithdrawLimits':lambda r:(r.pop('blocked'),r.pop('blockedGuarantee')),
            'UsersService/GetMarginAttributes':lambda r:r.update(startingMargin=dict(currency='rub'),minimalMargin=dict(currency='rub'),correctedMargin=dict(currency='rub'),amountOfMissingFunds=dict(currency='rub'))}
        self.assertTrue(self.collect()['read_checks_ok'])
        self.account_changes={'OperationsService/GetPositions':lambda r:r.pop('money')}
        r=self.collect();self.assertTrue(r['statistics']['account_diagnostic']['snapshot_validated'])
        self.assertEqual(r['statistics']['available_conservative_rub'],'0')
        self.assertIn('capital_long',r['blockers'])

    def test_malformed_money_arrays_duplicate_rub_and_non_rub_fail_closed(self):
        cases=[(None,'expected_array'),([None],'money_currency_or_object_invalid'),
               ([money(1),money(2)],'duplicate_rub_balance'),
               ([dict(currency='usd',units='1')],'non_rub_exposure')]
        for method,fields in (('OperationsService/GetPositions',('money','blocked')),
                              ('OperationsService/GetWithdrawLimits',('money','blocked','blockedGuarantee'))):
            for field in fields:
                for value,reason in cases:
                    with self.subTest(method=method,field=field,reason=reason):
                        self.account_changes={method:lambda r,f=field,v=value:r.update({f:v})}
                        d=self.collect()['statistics']['account_diagnostic'];self.assertEqual(d['failure']['reason'],reason)
                        self.assertEqual(d['failure']['field'],field)

    def test_loading_exposure_and_margin_invariants_are_still_blocked(self):
        cases=[('OperationsService/GetPositions',lambda r:r.update(limitsLoadingInProgress=True),'limits_loading'),
          ('OperationsService/GetPositions',lambda r:r.update(limitsLoadingInProgress='false'),'expected_boolean'),
          ('OperationsService/GetPositions',lambda r:r.update(securities=[dict(balance='1')]),'other_account_exposure'),
          ('OperationsService/GetPositions',lambda r:r.update(futures=[dict(instrumentUid='foreign',balance='1')]),'other_futures_exposure'),
          ('UsersService/GetMarginAttributes',lambda r:r.update(startingMargin=money(1),minimalMargin=money(2)),'margin_invariants')]
        for method,change,reason in cases:
            with self.subTest(reason=reason):
                self.account_changes={method:change};r=self.collect()
                self.assertEqual(r['statistics']['account_diagnostic']['failure']['reason'],reason)
                self.assertIn('account_snapshot',r['blockers'])

    def test_margin_deficit_is_a_valid_snapshot_with_blocked_capital(self):
        self.account_changes={'UsersService/GetMarginAttributes':lambda r:r.update(amountOfMissingFunds=money(100))}
        r=self.collect();self.assertTrue(r['statistics']['account_diagnostic']['snapshot_validated'])
        self.assertTrue(r['statistics']['margin_deficit']);self.assertIn('capital_long',r['blockers'])

    def test_account_http_failure_does_not_invent_zero_or_persist_body(self):
        self.bad_method='OperationsService/GetPositions';r=self.collect()
        d=r['statistics']['account_diagnostic'];self.assertFalse(d['snapshot_validated'])
        self.assertEqual(d['failure'],dict(reason='account_endpoint_failed',method=self.bad_method))
        self.assertIsNone(d['snapshot']);self.assertNotIn(TOKEN,json.dumps(r))
        self.assertEqual(r['statistics']['capital_checks']['long']['evaluation'],'not_evaluated')

    def test_standalone_account_command_uses_only_four_reads_preserves_full_report(self):
        self.assertEqual(self.run_check()[0],0);previous=(self.root/'report.json').read_bytes();self.requests=[]
        def factory(account):return REAL_CLASS(account,transport=self.transport,clock=self.clock)
        with patch.object(acc,'ReadOnlyBroker',side_effect=factory), \
             patch.object(acc.sys,'stdin',SimpleNamespace(buffer=io.BytesIO((TOKEN+'\n').encode()))),contextlib.redirect_stdout(io.StringIO()) as output:
            code=acc.main(['account-diagnostics','--credential-stdin','--config',str(self.cfg),'--state',str(self.root)])
        self.assertEqual(code,0);self.assertEqual([m for m,p in self.requests],list(METHODS))
        self.assertEqual(previous,(self.root/'report.json').read_bytes())
        self.assertFalse(acc.status(self.root,self.clock)['report_matches_last_attempt'])
        self.assertNotIn(TOKEN,output.getvalue());self.assertNotIn(ACCOUNT,output.getvalue())
        self.assertTrue(json.loads((self.root/'account_diagnostic.json').read_text())['snapshot_validated'])
        if os.name=='posix':self.assertEqual((self.root/'account_diagnostic.json').stat().st_mode&0o777,0o600)

    def test_fee_policy_update_is_explicit_acceptance_only_and_preserves_other_settings(self):
        before=deepcopy(self.c);before['max_order_fee_rub']='20';self.cfg.write_text(json.dumps(before));os.chmod(self.cfg,0o640)
        with patch.object(acc,'ReadOnlyBroker') as broker,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(acc.main(['fee-limit','--config',str(self.cfg),'--state',str(self.root)]),0);broker.assert_not_called()
        after=json.loads(self.cfg.read_text());self.assertEqual(after.pop('max_order_fee_rub'),'25');before.pop('max_order_fee_rub')
        self.assertEqual(before,after)
        if os.name=='posix':self.assertEqual(self.cfg.stat().st_mode&0o777,0o640)

    def test_readonly_validation_reuses_original_snapshot_without_duplicate_http(self):
        for position in (0,1,-2):
            with self.subTest(position=position):
                self.position=position;original=Broker.account(self.broker());self.requests=[]
                broker=self.broker();self.assertEqual(broker.account(),original)
                self.assertEqual([m for m,p in self.requests],list(METHODS))

    def test_unknown_validation_exception_text_is_never_reported(self):
        with patch.object(Broker,'account',side_effect=DataError(TOKEN+' '+ACCOUNT)):
            r=self.collect()
        self.assertIn('account_snapshot',r['blockers'])
        self.assertEqual(r['statistics']['account_diagnostic']['failure']['reason'],'unclassified_validation_error')
        self.assertNotIn(TOKEN,json.dumps(r));self.assertNotIn(ACCOUNT,json.dumps(r))

    def test_real_margin_disabled_code_blocks_even_with_15100_cash(self):
        self.balance=15100;self.margin_api_error=('30051',3);self.capacity=11
        r=self.collect();d=r['statistics']['account_diagnostic']
        self.assertEqual(d['failure']['reason'],'broker_margin_trading_disabled')
        self.assertEqual(d['failure_group'],'broker_account_restriction')
        self.assertEqual(d['failure']['api_code'],30051)
        self.assertEqual(d['methods']['OperationsService/GetPositions']['money']['rub_total'],'15100')
        self.assertIsNone(d['snapshot']);self.assertEqual(d['availability_components_rub'],{})
        self.assertIn('account_snapshot',r['blockers'])
        for side in ('long','short'):
            self.assertIn('capital_'+side,r['blockers'])
            self.assertEqual(r['statistics']['capital_checks'][side]['evaluation'],'not_evaluated')
        self.assertFalse(r['live_accepted']);self.assertEqual(r['order_submission_count'],0)
        self.assertNotIn(TOKEN,json.dumps(r));self.assertNotIn(ACCOUNT,json.dumps(r))

    def test_margin_request_uses_only_explicit_account_id_and_reads_once(self):
        self.margin_api_error=('30051',3);r=self.collect()
        requests=[p for m,p in self.requests if m=='UsersService/GetMarginAttributes']
        self.assertEqual(requests,[dict(accountId=ACCOUNT)])
        method=next(v for v in r['method_checks'] if v['method']=='UsersService/GetMarginAttributes')
        self.assertEqual((method['http_status'],method['grpc_code'],method['api_code']),(400,3,30051))

    def test_other_or_incomplete_codes_cannot_be_mislabelled_margin_disabled(self):
        for error in (('30042',3),('30051',7),('unknown',3)):
            with self.subTest(error=error):
                self.margin_api_error=error;r=self.collect();d=r['statistics']['account_diagnostic']
                self.assertEqual(d['failure']['reason'],'account_endpoint_failed')
                self.assertFalse(d['snapshot_validated']);self.assertIn('capital_long',r['blockers'])

    def test_manual_recheck_after_broker_returns_attributes_does_not_reuse_disabled_state(self):
        self.margin_api_error=('30051',3);self.assertEqual(self.run_check()[0],2)
        self.margin_api_error=None;self.assertEqual(self.run_check()[0],0)
        r=acc.status(self.root,self.clock)['report'];self.assertTrue(r['statistics']['account_diagnostic']['snapshot_validated'])
        self.assertIsNone(r['statistics']['account_diagnostic']['failure'])
        self.assertFalse(r['live_accepted']);self.assertTrue(all(m in acc.READ_METHODS for m,p in self.requests))

    def use_reported_calendar(self):
        self.account_changes['InstrumentsService/TradingSchedules']=lambda r:r['exchanges'][0].update(days=[reported_day(self.now.date().isoformat())])

    def test_calendar_schema_failure_is_not_a_normal_closed_market(self):
        def change(r):r['exchanges'][0]['days'][0].pop('intervals')
        self.account_changes['InstrumentsService/TradingSchedules']=change
        r=self.collect();self.assertIn('current_calendar',r['blockers'])
        entry=next(v for v in r['checks'] if v['check']=='current_calendar')
        self.assertEqual(entry['category'],'calendar_validation_error');self.assertEqual(entry['reason'],'missing_detailed_intervals')
        self.assertEqual(r['market']['calendar_state'],'validation_error')
        self.assertEqual(r['statistics']['calendar_diagnostic']['days'][0]['reason'],'missing_detailed_intervals')

    def test_outside_confirmed_window_is_waiting_without_calendar_schema_blocker(self):
        self.now=self.now.replace(hour=3,minute=55);self.use_reported_calendar()
        r=self.collect();self.assertNotIn('current_calendar',r['blockers'])
        self.assertTrue(r['read_checks_ok']);self.assertFalse(r['market']['tradable_now'])
        self.assertEqual(r['market']['calendar_state'],'outside_confirmed_windows')

    def test_session_buffer_is_measured_to_actual_exclusive_end(self):
        for seconds,allowed in ((0,True),(1,False)):
            self.now=self.now.replace(hour=20,minute=49,second=seconds);self.use_reported_calendar()
            r=self.collect();self.assertNotIn('current_calendar',r['blockers'])
            self.assertEqual(r['market']['remaining_session_seconds'],60-seconds)
            self.assertEqual(r['market']['session_buffer_ok'],allowed)
            self.assertEqual(r['market']['tradable_now'],allowed)

    def test_fresh_quote_in_previous_phase_does_not_authorize_new_phase(self):
        self.now=self.now.replace(hour=16,minute=0);self.quote_lag=1;self.use_reported_calendar()
        r=self.collect();self.assertTrue(r['market']['quote_fresh'])
        self.assertFalse(r['market']['same_continuous_interval']);self.assertFalse(r['market']['tradable_now'])

    def test_readonly_explains_wide_book_by_side_without_changing_price_bound(self):
        self.account_changes['MarketDataService/GetOrderBook']=lambda r:r['asks'][0].update(price=quoted('100.3'))
        r=self.collect();m=r['market'];self.assertFalse(m['book_within_proposed_limits'])
        self.assertFalse(m['long_price_checks']['allowed']);self.assertTrue(m['short_price_checks']['allowed'])
        self.assertEqual(m['bounded_limit_long_points'],'100.2');self.assertFalse(m['tradable_now'])

    def test_standalone_calendar_command_only_reads_instrument_and_schedules(self):
        self.assertEqual(self.run_check()[0],0);old=(self.root/'report.json').read_bytes();self.requests=[]
        def factory(account):return REAL_CLASS(account,transport=self.transport,clock=self.clock)
        with patch.object(acc,'ReadOnlyBroker',side_effect=factory), \
             patch.object(acc.sys,'stdin',SimpleNamespace(buffer=io.BytesIO((TOKEN+'\n').encode()))),contextlib.redirect_stdout(io.StringIO()) as output:
            code=acc.main(['calendar-diagnostics','--credential-stdin','--config',str(self.cfg),'--state',str(self.root)])
        self.assertEqual(code,0)
        self.assertEqual([m for m,p in self.requests],['InstrumentsService/FutureBy','InstrumentsService/TradingSchedules'])
        self.assertEqual((self.root/'report.json').read_bytes(),old)
        self.assertFalse(acc.status(self.root,self.clock)['report_matches_last_attempt'])
        self.assertNotIn(TOKEN,output.getvalue());self.assertNotIn(ACCOUNT,output.getvalue())
        if os.name=='posix':self.assertEqual((self.root/'calendar_diagnostic.json').stat().st_mode&0o777,0o600)


if __name__=='__main__':unittest.main()
