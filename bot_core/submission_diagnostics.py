"""Human explanations for pre-submit blocking. Does not change order eligibility."""
from datetime import datetime
from .submission_scope import key


def describe(record, *, now):
    r = record or {}
    if r.get('reason') == 'submission_journal_unavailable':
        cause = str(r.get('fault_detail') or '账本读取或保存未通过核对')
        return {'code':'submission_journal_unavailable_no_submit','scope':'journal',
                'state':'UNAVAILABLE','cause':cause,'current_submit_attempted':False,
                'message':f'未确认订单账本不可用，已跳过提交；原因：{cause}'}
    ts = float(r.get('ts') or 0)
    scope = key(r.get('platform'),r.get('coin'),r.get('kind'),r.get('side'))
    last = r.get('reconciliation_last') or {}
    age = max(0, now-ts) if ts else None
    created = datetime.fromtimestamp(ts).strftime('%m-%d %H:%M:%S') if ts else '时间缺失'
    amount = float(r.get('intent_usd') or 0)
    message = (f"被旧记录 {scope} 拦截：{r.get('state','未知')}，原单 {r.get('module') or '旧版'} "
               f"{r.get('side') or '-'} {amount:g}U，提交于 {created}")
    if age is not None:message += f'，已等待 {age/60:.1f} 分钟'
    if last.get('reason_text'):message += '；最近核对：'+last['reason_text']
    message += '；本次没有点击下单，等待持仓对账'
    return {'code':'blocked_existing_unconfirmed_no_submit','scope':scope,'state':r.get('state'),
            'module':r.get('module'),'side':r.get('side'),'intent_usd':amount,'submitted_ts':ts,
            'age_sec':age,'record_reason':r.get('reason'),'last_reconciliation':last,
            'current_submit_attempted':False,'historical_pending_count':1,'message':message}
