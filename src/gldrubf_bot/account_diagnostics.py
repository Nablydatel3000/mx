"""Private read-only account evidence. Never persist arbitrary broker payloads.

The existing Broker.account validator is run against the four captured responses,
without repeating HTTP or substituting a deposit for broker collateral.
"""
from . import api
from .live_api import Broker, money_array, rub
from .models import DataError

METHODS=('OperationsService/GetPositions','OperationsService/GetPortfolio',
         'OperationsService/GetWithdrawLimits','UsersService/GetMarginAttributes')

# Only fixed, source-controlled error names may enter a report.
REASONS={
 'Broker account echo mismatch':'account_echo_mismatch',
 'Broker limits loading':'limits_loading',
 'Other account exposure':'other_account_exposure',
 'Other futures exposure':'other_futures_exposure',
 'Ambiguous futures position':'ambiguous_futures_position',
 'Ambiguous portfolio position':'ambiguous_portfolio_position',
 'Other portfolio instrument':'other_portfolio_exposure',
 'Position limit/quantity violated':'position_limit_or_quantity',
 'Position sources disagree':'position_sources_disagree',
 'Unsafe margin account':'margin_invariants',
 'Invalid broker integer':'invalid_integer',
 'Invalid API quotation':'invalid_quotation',
 'Missing/non-RUB broker amount':'missing_or_non_rub_amount',
 'Non-RUB exposure':'non_rub_exposure',
 'Duplicate currency balance':'duplicate_rub_balance'}


class SnapshotError(DataError):
    def __init__(self,reason,field=None):
        super().__init__('Read-only account snapshot blocked')
        self.reason=reason;self.field=field


def inspect_account(broker):
    raw={};failures={};methods={};findings=[]
    def issue(method,field,reason):
        findings.append(dict(method=method,field=field,reason=reason))
    for method in METHODS:
        try:raw[method]=broker.call(method,{'accountId':broker.account_id,**({'currency':'RUB'} if method.endswith('/GetPortfolio') else {})})
        except Exception as exc:failures[method]=exc

    for method,response in raw.items():
        view=methods[method]=dict(http_response_received=True)
        if not isinstance(response,dict):
            issue(method,'response','expected_object');continue
        if method in METHODS[:2]:
            view['account_echo_present']='accountId' in response
            view['account_echo_matches']=response.get('accountId')==broker.account_id
            if not view['account_echo_matches']:issue(method,'accountId','account_echo_missing_or_mismatch')
        array_money={'OperationsService/GetPositions':('money','blocked'),
                     'OperationsService/GetWithdrawLimits':('money','blocked','blockedGuarantee')}.get(method,())
        for field in array_money:
            rows=response.get(field,[])
            item=view[field]=dict(present=field in response)
            if not isinstance(rows,list):
                issue(method,field,'expected_array');continue
            item['row_count']=len(rows)
            # Do not echo a currency label supplied by the server.
            item['rub_row_count']=sum(isinstance(r,dict) and isinstance(r.get('currency'),str) and r['currency'].lower()=='rub' for r in rows)
            valid=True
            for row in rows:
                if not isinstance(row,dict) or not isinstance(row.get('currency'),str) or not row['currency']:
                    issue(method,field,'money_currency_or_object_invalid');valid=False;break
                try:api.quotation(row)
                except (DataError,ValueError,TypeError):
                    issue(method,field,'invalid_quotation');valid=False;break
            if valid:
                try:item['rub_total']=str(money_array(rows))
                except DataError as exc:issue(method,field,REASONS.get(str(exc),'money_array_invalid'))
        money_fields=('totalAmountPortfolio',) if method==METHODS[1] else (
            ('liquidPortfolio','startingMargin','minimalMargin','correctedMargin','amountOfMissingFunds') if method==METHODS[3] else ())
        for field in money_fields:
            item=view[field]=dict(present=field in response)
            if field not in response:
                issue(method,field,'required_money_missing');continue
            value=response[field]
            item['currency_is_rub']=isinstance(value,dict) and isinstance(value.get('currency'),str) and value['currency'].lower()=='rub'
            try:item['rub_value']=str(rub(value))
            except (DataError,ValueError,TypeError,AttributeError):issue(method,field,'required_money_invalid_or_non_rub')
        arrays=('securities','options','futures') if method==METHODS[0] else ('positions',) if method==METHODS[1] else ()
        for field in arrays:
            rows=response.get(field,[])
            view[field]=dict(present=field in response,row_count=len(rows) if isinstance(rows,list) else None)
            if not isinstance(rows,list) or any(not isinstance(r,dict) for r in rows):issue(method,field,'expected_array_of_objects')
        if method==METHODS[0]:
            value=response.get('limitsLoadingInProgress',False)
            view['limitsLoadingInProgress']=dict(present='limitsLoadingInProgress' in response,
                value=value if type(value) is bool else None)
            if type(value) is not bool:issue(method,'limitsLoadingInProgress','expected_boolean')
            elif value:issue(method,'limitsLoadingInProgress','limits_loading')

    # Reuse the actual production validator; no alternative capital calculation.
    cached=Broker.__new__(Broker);cached.account_id=broker.account_id
    def replay(method,payload):
        if method in failures:raise failures[method]
        return raw[method]
    cached.call=replay;snapshot=None;failure=None
    if failures:
        failure=dict(reason='account_endpoint_failed',method=next(m for m in METHODS if m in failures))
        exc=failures.get(METHODS[3])
        if isinstance(exc,api.APIError) and (exc.http_status,exc.grpc_code,exc.api_code)==(400,3,30051):
            failure=dict(reason='broker_margin_trading_disabled',method=METHODS[3],
                http_status=400,grpc_code=3,api_code=30051,
                next_step='verify_margin_trading_setting_for_selected_account_with_broker')
    elif findings:
        failure=dict(findings[0])
    else:
        try:snapshot=Broker.account(cached)
        except DataError as exc:failure=dict(reason=REASONS.get(str(exc),'unclassified_validation_error'))
        except (KeyError,TypeError,ValueError,AttributeError,OverflowError):failure=dict(reason='unclassified_schema_error')

    components={}
    if snapshot is not None:
        p,w,m=(raw[METHODS[i]] for i in (0,2,3))
        components={
          'positions_cash_minus_blocked_rub':str(money_array(p.get('money',[]))-money_array(p.get('blocked',[]))),
          'withdraw_money_minus_blocked_and_guarantee_rub':str(money_array(w.get('money',[]))-money_array(w.get('blocked',[]))-money_array(w.get('blockedGuarantee',[]))),
          'liquid_minus_starting_margin_rub':str(rub(m['liquidPortfolio'])-rub(m['startingMargin'])),
          'liquid_minus_corrected_margin_rub':str(rub(m['liquidPortfolio'])-rub(m['correctedMargin']))}
    invariant_reasons={'account_echo_missing_or_mismatch','account_echo_mismatch','limits_loading',
        'other_account_exposure','other_futures_exposure','ambiguous_futures_position',
        'ambiguous_portfolio_position','other_portfolio_exposure','position_limit_or_quantity',
        'position_sources_disagree','margin_invariants','non_rub_exposure','duplicate_rub_balance'}
    group=('broker_account_restriction' if failure and failure['reason']=='broker_margin_trading_disabled'
           else 'endpoint_error' if failures else 'account_state_invariant' if failure and failure['reason'] in invariant_reasons
           else 'schema_error' if failure else None)
    report=dict(schema=1,snapshot_validated=snapshot is not None,failure=failure,failure_group=group,findings=findings,
        methods=methods,endpoint_failures=[m for m in METHODS if m in failures],
        availability_components_rub=components,snapshot=snapshot,
        available_is_minimum_of_components=True,deposit_is_not_collateral_evidence=True)
    return snapshot,report
