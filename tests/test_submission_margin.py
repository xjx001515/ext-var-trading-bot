"""Request-scoped submit evidence and available-margin gates; all UI is fake."""
import copy
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from ast_support import ROOT, load_definitions, parsed
from bot_core import margin_guard
from bot_core.execution_confirmation import (
    apply_confirmation_fields, apply_submission_evidence, pending_blocks,
    verify_pending_settlement,
)


def req():
    return {'module':'B1', 'coin':'TIA', 'platform':'EXT', 'side':'SHORT',
            'action_type':'open', 'amount_usd':170, 'params':{}, 'meta':{'req_id':'new-request'}}


def order(request=None, module='B1', platform='EXT', reduce_only=False):
    request = request if request is not None else req()
    request.update(module=module, platform=platform)
    return SimpleNamespace(module=module, platform=platform, coin='TIA', side='sell',
                           action_type='decrease' if reduce_only else 'open', usd=170,
                           reduce_only=reduce_only, _exec_req_ref=request, meta={})


def quote(available='30 USD', cost='17.82 USD', bound=True):
    return {'available_text':'Available To Trade '+available,
            'cost_text':'Cost: '+cost, 'side_cost_bound':bound}


class Button:
    def __init__(self, evidence=None, enabled=True, visible=True, click_error=False):
        self.evidence = quote() if evidence is None else evidence
        self.enabled, self.visible, self.click_error = enabled, visible, click_error
        self.clicks = 0
        self.reads = 0
    def evaluate(self, script, **kwargs):
        self.reads += 1
        if isinstance(self.evidence, Exception):
            raise self.evidence
        return self.evidence
    def wait_for(self, **kwargs):
        if not self.visible:
            raise TimeoutError('not visible; no click')
    def is_enabled(self, **kwargs):
        return self.enabled
    def click(self, **kwargs):
        self.clicks += 1
        if self.click_error:
            raise TimeoutError('dispatch happened; delivery uncertain')


def namespace(directory):
    logs = []
    def write(path, value, **kwargs):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(value), encoding='utf-8')
        return True
    def call(label, fn, *args, **kwargs):
        kwargs.pop('_operation_timeout_ms', None)
        return fn(*args, **kwargs)
    env = {'time':time, 'threading':threading, 'copy':copy, 'os':os, '_margin_guard':margin_guard,
           'OUT_DIR':directory, '_SYNC_PW_WORKER_LOCK':threading.RLock(),
           '_FUNDING_MARGIN_LOCK':threading.RLock(), '_FUNDING_MARGIN_ALERTS':{},
           '_EXEC_PENDING':{}, '_EXEC_PENDING_INFO':{}, 'safe_write_json':write,
           '_safety_pending_blocks':pending_blocks, '_safety_verify_pending_settlement':verify_pending_settlement,
           '_pw_call':call, 'safe_log':lambda f:logs.append(f()), 'now_ts':time.time,
           '_record_swallowed_exception':Mock(), '_set_last_exec_err':Mock(),
           '_pw_guard':lambda **k:True, '_runtime_recovering_gate':lambda *a, **k:(True,'',{}),
           '_a1_norm_side':lambda s: {'BUY':'LONG','SELL':'SHORT'}.get(str(s).upper(),str(s).upper()),
           '_failed_close_pending_should_block_new_open':lambda *a:(False,{}),
           '_req_get_dict':lambda r:r if isinstance(r,dict) else vars(r),
           '_extract_profit_u_from_req':lambda r:None,
           '_executor_get_layer_info':lambda r:('',None,None), '_grid_layer_tag':lambda x:'',
           '_grid_layer_no':lambda x:None, '_executor_contract_detail_from_req':lambda r:{},
           '_exec_fill_cache_lookup':lambda r:{},
           'safe_float':lambda value,default=0:float(value) if value is not None else default,
           '_apply_submission_evidence':apply_submission_evidence,
           '_result_apply_confirmation_fields':apply_confirmation_fields}
    names = {'_safety_submission_records','_safety_submission_save','_safety_pending_scope',
             '_submission_blocker_detail',
             '_safety_reserve_submission','_safety_click_submit_once','_req_ensure_meta',
             '_submit_attempt_begin','_submit_attempt_set','_submit_margin_precheck',
             '_submit_order_click','_funding_margin_status','_exe_open_position_safe',
             '_make_exec_result_row','_submit_close_click'}
    load_definitions(names,env)
    return env, logs


class MarginParserTests(unittest.TestCase):
    def test_tia_screenshot_shortfall(self):
        allow, detail = margin_guard.decide(quote('2.21 USD','17.82 USD'))
        self.assertFalse(allow)
        self.assertEqual(detail['reason'],'insufficient_available_margin')
        self.assertAlmostEqual(detail['shortfall_u'],15.61)

    def test_zero_available_is_a_real_zero(self):
        self.assertFalse(margin_guard.decide(quote('0.00 USD','1 USD'))[0])

    def test_exact_required_amount_is_allowed(self):
        self.assertTrue(margin_guard.decide(quote('$17.82','$17.82'))[0])

    def test_thousands_and_dollar_labels(self):
        allow, detail = margin_guard.decide(quote('$1,234.56','$1,000.00'))
        self.assertTrue(allow); self.assertEqual(detail['available_u'],1234.56)

    def test_unknown_invalid_currency_or_ambiguous_fields_do_not_block(self):
        cases = [quote('-', '17 USD'), quote('NaN','17 USD'), quote('1 EUR','17 USD'),
                 quote('30 USD','17 USD',False), quote('1 USD','—'),
                 {'available_text':'Equity 2 USD','cost_text':'Cost 17 USD','side_cost_bound':True},
                 {'available_text':'Available To Trade 2 USD\nAvailable Margin 30 USD',
                  'cost_text':'Cost 17 USD','side_cost_bound':True},
                 {'available_text':'Available To Trade 2 USD',
                  'cost_text':'Cost 17.93 USD\nCost 17.82 USD','side_cost_bound':True}]
        for value in cases:
            self.assertTrue(margin_guard.decide(value)[0],value)

    def test_var_required_margin_label(self):
        self.assertFalse(margin_guard.decide({'available_text':'Available Margin\n$2.21',
            'cost_text':'Required Margin\n$17.82','side_cost_bound':True})[0])


class SubmissionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.env, self.logs = namespace(self.temp.name)
        self.request = req(); self.order = order(self.request)
        self.env['_submit_attempt_begin'](self.order)

    @property
    def journal(self):
        return Path(self.temp.name)/'state/execution_unconfirmed.json'

    def submit(self, button):
        return self.env['_submit_order_click'](button,self.order,self.order.platform,'SHORT',
            {'position_state':'CONFIRMED_EMPTY'},170,100,'fake submit')

    def result(self, reason='open_leg_fail'):
        return self.env['_make_exec_result_row'](self.request,ok=False,reason=reason)

    def test_insufficient_margin_skips_without_click_or_journal_and_alerts(self):
        b = Button(quote('2.21 USD','17.82 USD'))
        self.assertFalse(self.submit(b)); self.assertEqual(b.clicks,0)
        self.assertFalse(self.journal.exists())
        r = self.result()
        self.assertTrue(r['confirmed_fail']); self.assertFalse(r['pending_confirm'])
        self.assertFalse(r['submit_ok']); self.assertEqual(r['reason'],'insufficient_available_margin')
        self.assertTrue(any('FUNDING_IMBALANCE' in line for line in self.logs))
        status = self.env['_funding_margin_status']()
        self.assertIn('2.21U',status['message']); self.assertIn('17.82U',status['message'])
        self.assertEqual(self.order.usd,170); self.assertEqual(self.order.side,'sell')

    def test_disabled_button_is_failure_not_pending(self):
        b = Button(enabled=False)
        self.assertFalse(self.submit(b)); self.assertEqual(b.clicks,0)
        self.assertFalse(self.journal.exists())
        r = self.result('submit_click_fail')
        self.assertTrue(r['confirmed_fail']); self.assertFalse(r['pending_confirm'])
        self.assertEqual(r['reason'],'submit_click_fail')

    def test_invisible_button_does_not_reserve(self):
        b = Button(visible=False)
        self.assertFalse(self.submit(b)); self.assertFalse(self.journal.exists())
        self.assertFalse(self.result()['pending_confirm'])

    def test_actual_click_with_receipt_missing_is_pending(self):
        b = Button()
        self.assertTrue(self.submit(b)); self.assertEqual(b.clicks,1)
        r = self.result('receipt_timeout')
        self.assertTrue(r['pending_confirm']); self.assertTrue(r['submit_ok'])
        self.assertTrue(r['submit_attempted']); self.assertFalse(r['confirmed_success'])
        self.assertEqual(r['reason'],'submitted_pending_confirm')
        data = json.loads(self.journal.read_text())['records']['EXT|TIA']
        self.assertEqual(data['submission_attempt_id'],self.order._submission_attempt['id'])

    def test_click_dispatch_timeout_is_ambiguous_and_never_reclicked(self):
        b = Button(click_error=True)
        self.assertFalse(self.submit(b)); self.assertEqual(b.clicks,1)
        r = self.result()
        self.assertTrue(r['pending_confirm']); self.assertFalse(r['submit_ok'])
        self.assertEqual(r['reason'],'submit_click_outcome_unknown')
        self.assertTrue(self.journal.exists())

    def test_worker_guard_before_click_never_creates_pending(self):
        original = self.env['_pw_call']
        def guarded(label, fn, *args, **kwargs):
            if 'click_once' in label:raise RuntimeError('retired before dispatch')
            return original(label,fn,*args,**kwargs)
        self.env['_pw_call'] = guarded
        b = Button()
        self.assertFalse(self.submit(b)); self.assertEqual(b.clicks,0)
        self.assertFalse(self.journal.exists()); self.assertFalse(self.result()['pending_confirm'])

    def test_old_pending_blocks_new_request_without_changing_old_record(self):
        self.env['_safety_reserve_submission']('EXT','TIA','SHORT','open',None,170,
                                               submission_attempt_id='old-attempt')
        before = self.journal.read_bytes()
        exe = SimpleNamespace(open_position=Mock())
        self.assertFalse(self.env['_exe_open_position_safe'](exe,self.order,plat='EXT',act='open'))
        exe.open_position.assert_not_called()
        r = self.result()
        self.assertFalse(r['submit_ok']); self.assertFalse(r['pending_confirm'])
        self.assertTrue(r['confirmed_fail'])
        self.assertEqual(r['reason'],'blocked_existing_unconfirmed_no_submit')
        self.assertEqual(self.journal.read_bytes(),before)

    def test_raw_failure_cannot_be_promoted_by_unrelated_same_coin_pending(self):
        self.env['_safety_reserve_submission']('EXT','TIA','SHORT','open',None,170)
        self.request['meta'].pop('_submission_attempts')
        r = self.result('submit_click_fail')
        self.assertFalse(r['pending_confirm']); self.assertTrue(r['confirmed_fail'])

    def test_concurrent_claim_winner_is_preserved_without_click(self):
        self.env['_safety_reserve_submission']('EXT','TIA','SHORT','open',None,120,
                                               submission_attempt_id='another-winner')
        b = Button()
        self.assertFalse(self.submit(b)); self.assertEqual(b.clicks,0)
        r = self.result(); self.assertFalse(r['pending_confirm'])
        self.assertEqual(r['reason'],'submission_claim_blocked_no_submit')
        self.assertEqual(json.loads(self.journal.read_text())['records']['EXT|TIA']['intent_usd'],120)

    def test_failed_journal_write_prevents_click(self):
        self.env['safe_write_json'] = lambda *a,**k:False
        b = Button()
        self.assertFalse(self.submit(b)); self.assertEqual(b.clicks,0)
        self.assertFalse(self.result()['pending_confirm'])

    def test_unreadable_margin_keeps_existing_behavior(self):
        for evidence in ({},RuntimeError('UI unreadable')):
            with tempfile.TemporaryDirectory() as directory:
                env, _ = namespace(directory); o = order()
                env['_submit_attempt_begin'](o); b = Button(evidence)
                self.assertTrue(env['_submit_order_click'](b,o,'EXT','SHORT',None,170,100,'fake'))
                self.assertEqual(b.clicks,1); self.assertEqual(o.usd,170)

    def test_m9_increase_is_checked_but_m9_reduce_and_m10_are_not(self):
        for module,reducing,blocked in [('M9',False,True),('M9',True,False),('M10',False,False)]:
            with tempfile.TemporaryDirectory() as directory:
                env, _ = namespace(directory); o = order(module=module,reduce_only=reducing)
                env['_submit_attempt_begin'](o); b = Button(quote('0 USD','17 USD'))
                ok = env['_submit_order_click'](b,o,'EXT','SHORT',None,170,100,'fake')
                self.assertEqual(ok,not blocked)
                self.assertEqual(b.reads,1 if blocked else 0)

    def test_var_margin_failure_has_same_no_click_semantics(self):
        self.order.platform='VAR'; self.request['platform']='VAR'
        b = Button(quote('2 USD','17 USD'))
        self.assertFalse(self.submit(b)); self.assertEqual(b.clicks,0)
        self.assertFalse(self.journal.exists())
        self.assertIn('VAR',self.env['_funding_margin_status']()['message'])

    def test_disabled_close_button_does_not_create_unconfirmed_record(self):
        self.env['_ACTIVE_EXEC_CTX'] = SimpleNamespace(req_ref=self.request)
        b = Button(enabled=False)
        self.assertFalse(self.env['_submit_close_click'](b,'EXT','TIA','SHORT',None,170,100,'close fake'))
        self.assertFalse(self.journal.exists()); self.assertEqual(b.clicks,0)
        self.assertEqual(b.reads,0)
        self.assertFalse(self.result()['pending_confirm'])

    def test_close_click_is_bound_to_current_thread_request_and_keeps_unknown_pending(self):
        self.env['_ACTIVE_EXEC_CTX'] = SimpleNamespace(req_ref=self.request)
        b = Button(click_error=True)
        self.assertFalse(self.env['_submit_close_click'](b,'EXT','TIA','SHORT',None,170,100,'close fake'))
        self.assertTrue(self.result()['pending_confirm'])
        self.assertEqual(json.loads(self.journal.read_text())['records']['EXT|TIA|CLOSE|SHORT']['kind'],'close_all')
        self.assertEqual(b.reads,0)

    def test_funds_alert_clears_only_when_available_covers_previous_shortfall(self):
        self.env['_submit_margin_precheck'](Button(quote('2 USD','17 USD')),self.order,'EXT')
        self.env['_submit_margin_precheck'](Button(quote('3 USD','1 USD')),self.order,'EXT')
        self.assertTrue(self.env['_funding_margin_status']()['message'])
        self.env['_submit_margin_precheck'](Button(quote('30 USD','17 USD')),self.order,'EXT')
        self.assertEqual(self.env['_funding_margin_status']()['message'],'')

    def test_actual_ext_ready_failure_no_longer_reserves_first(self):
        tree = parsed(str(ROOT/'BOT5-2-414 .py'))
        cls = next(n for n in tree.body if getattr(n,'name','')=='_ExtExecutor')
        method = next(n for n in cls.body if getattr(n,'name','')=='_click_submit_button_direct')
        import ast
        code = ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),method],type_ignores=[])
        self.env.update(_ext_market_context_matches_request=lambda *a,**k:(True,{}),
                        _exec_row_side_notional=lambda row:('NONE',0))
        exec(compile(ast.fix_missing_locations(code),'<actual EXT ready/click>', 'exec'),self.env)
        fake = SimpleNamespace(page=object(),_submit_click_guard_acquire=lambda *a,**k:(True,{}),
            _submit_click_guard_release=Mock(),_wait_ext_submit_button_ready=lambda *a,**k:(False,{}))
        b = Button()
        self.assertFalse(self.env['_click_submit_button_direct'](fake,b,'sell',coin='TIA',order=self.order,usd=170))
        self.assertEqual(b.clicks,0); self.assertFalse(self.journal.exists())
        self.assertEqual(self.result()['reason'],'submit_btn_disabled')


if __name__ == '__main__':
    unittest.main()
