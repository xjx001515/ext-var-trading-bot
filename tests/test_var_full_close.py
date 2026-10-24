"""Full-close replays with fake pages and temporary journals; never import the bot."""
import ast
from contextlib import contextmanager
import copy
import os
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ast_support import ROOT, load_definitions, parsed
from bot_core import close_flow
from bot_core.browser_runtime import SessionLiveness
from bot_core.worker_state import WorkerState, WorkerRetiredError
from test_submission_margin import namespace, Button


class Clock:
    def __init__(self): self.now=0.0
    def time(self): return self.now
    def sleep(self,n): self.now+=n


def request():
    return SimpleNamespace(module='ANCHOR',coin='ALL',platform='PAIR',action_type='close_pair',meta={})


class BudgetEvidenceTests(unittest.TestCase):
    def test_only_anchor_s1_all_receive_full_close_budget(self):
        self.assertTrue(close_flow.is_full_close_batch([request()]))
        for fields in ({'module':'M10'},{'coin':'TIA'},{'action_type':'open'}):
            r=request();r.__dict__.update(fields)
            self.assertFalse(close_flow.is_full_close_batch([r]))
        self.assertFalse(close_flow.is_full_close_batch([]))

    def test_submit_started_clicked_unknown_all_forbid_retry(self):
        for state in ('CLICK_STARTED','CLICKED','CLICK_UNKNOWN'):
            flow=close_flow.CloseFlow();flow.begin('VAR','TIA')
            flow.record_attempt(dict(id='one',platform='VAR',coin='TIA',state=state))
            recovery=Mock(return_value=True)
            self.assertFalse(flow.recover_once('VAR','TIA',recovery))
            flow.begin('VAR','ALL');self.assertFalse(flow.can_resubmit('VAR','ALL'))
            recovery.assert_not_called()

    def test_failure_cannot_downgrade_a_dispatched_attempt(self):
        flow=close_flow.CloseFlow();a=dict(id='one',platform='VAR',coin='TIA',state='CLICK_STARTED')
        flow.record_attempt(a);a['state']='BLOCKED';flow.record_attempt(a)
        self.assertTrue(flow.submitted('VAR','TIA'));self.assertFalse(flow.can_resubmit('VAR','TIA'))

    def test_recovery_is_at_most_once_per_platform(self):
        flow=close_flow.CloseFlow();flow.begin('VAR','TIA');flow.begin('VAR','LTC')
        recover=Mock(return_value=True)
        self.assertTrue(flow.recover_once('VAR','TIA',recover))
        self.assertFalse(flow.recover_once('VAR','LTC',recover));recover.assert_called_once()

    def test_deadline_reserves_final_reads_and_cannot_extend(self):
        clock=Clock();flow=close_flow.CloseFlow(clock=clock.time)
        self.assertEqual(flow.remaining(),115)
        clock.sleep(116)
        with self.assertRaises(close_flow.CloseDeadlineError):flow.begin('VAR','TIA')
        flow.final_verification=True;self.assertEqual(flow.timeout_ms(10000),4000)
        clock.sleep(5)
        with self.assertRaises(close_flow.CloseDeadlineError):flow.check()

    def test_retired_owner_cannot_recover_or_confirm(self):
        state=WorkerState();state.begin('W',0,{},{});flow=close_flow.CloseFlow(assert_live=state.assert_live)
        flow.begin('VAR','TIA');state.invalidate('W',{},{},{},{})
        recover=Mock()
        with self.assertRaises(WorkerRetiredError):flow.recover_once('VAR','TIA',recover)
        with self.assertRaises(WorkerRetiredError):flow.mark_result('VAR','TIA',True)
        recover.assert_not_called()

    def test_unknown_current_submit_remains_pending_but_existing_claim_is_not_new_submit(self):
        reports={'VAR':{'flat_confirmed':False,'close_attempts':[{'submitted':True,'attempts':[
            {'id':'one','state':'CLICK_UNKNOWN'}]}]}}
        self.assertTrue(close_flow.submission_summary(reports)['pending_confirm'])
        reports['VAR']['flat_confirmed']=True
        self.assertFalse(close_flow.submission_summary(reports)['pending_confirm'])
        reports['VAR'].update(flat_confirmed=False,close_attempts=[{'submitted':True,'attempts':[]}])
        self.assertEqual(close_flow.submission_summary(reports)['submission_state'],'NOT_SUBMITTED')


class ButtonReadinessTests(unittest.TestCase):
    def test_waits_for_enabled_without_clicking_or_reserving(self):
        clock=Clock();button=SimpleNamespace(is_enabled=Mock(side_effect=[False,False,True]),click=Mock())
        ready,detail=close_flow.wait_ready(button,lambda label,fn,**k:fn(),timeout_ms=500,
            clock=clock.time,sleep=clock.sleep)
        self.assertTrue(ready);self.assertEqual(detail['samples'],3)
        self.assertAlmostEqual(clock.now,.2);button.click.assert_not_called()

    def test_disabled_timeout_records_inputs_and_never_clicks(self):
        clock=Clock();button=SimpleNamespace(is_enabled=lambda:False,click=Mock())
        ready,detail=close_flow.wait_ready(button,lambda label,fn,**k:fn(),timeout_ms=300,
            clock=clock.time,sleep=clock.sleep,sample=lambda:{'inputs':[{'value':'0'}]})
        self.assertFalse(ready);self.assertEqual(detail['reason'],'close_button_disabled_timeout')
        self.assertEqual(detail['ui']['inputs'][0]['value'],'0');button.click.assert_not_called()
        self.assertLessEqual(clock.now,.301)


class FullClosePretradeTests(unittest.TestCase):
    def snapshot(self):
        return {'ts':995,'data_quality':'ok','dq_reason':'positions_verified','positions':{
            'EXT':{'TIA':{'position_state':'CONFIRMED_EMPTY','confirmed_empty':True,'side':'NONE'}},
            'VAR':{'TIA':{'position_state':'FOUND','side':'SHORT','qty_abs':343.4,'notional_u':188.6}}}}

    def test_positions_verified_text_without_counts_uses_real_rows(self):
        ok,detail=close_flow.pretrade_evidence(request(),self.snapshot(),now=1000)
        self.assertTrue(ok);self.assertEqual(detail['counts']['EXT']['found'],0)
        self.assertEqual(detail['counts']['VAR']['found'],1)
        self.assertTrue(detail['executor_live_confirmation_required'])

    def test_unknown_missing_or_stale_rows_never_claim_valid_empty(self):
        for row in ({'side':'NONE'},{'position_state':'UNAVAILABLE'},
                    {'position_state':'FOUND','side':'SHORT','qty_abs':2,'ts':900}):
            s=self.snapshot();s['positions']['VAR']['TIA']=row
            self.assertFalse(close_flow.pretrade_evidence(request(),s,now=1000)[0])
        s=self.snapshot();s['positions']['VAR']={}
        self.assertFalse(close_flow.pretrade_evidence(request(),s,now=1000)[0])

    def test_stale_partial_or_other_module_cannot_use_full_close_entry(self):
        for fields in ({'ts':900},{'ts':1001},{'data_quality':'fail'},{'dq_fallback':True},
                       {'scan_scope':'local'},{'recovered_from_last_good':True}):
            s=self.snapshot();s.update(fields)
            self.assertFalse(close_flow.pretrade_evidence(request(),s,now=1000)[0])
        for module in ('B1','M9','M10'):
            r=request();r.module=module
            self.assertFalse(close_flow.pretrade_evidence(r,self.snapshot(),now=1000)[0])

    def test_actual_pretrade_gate_skips_string_count_rescan_only_with_evidence(self):
        tree=parsed(str(ROOT/'BOT5-2-414 .py'))
        gate=next(n for n in ast.walk(tree) if isinstance(n,ast.If)
                  and ast.unparse(n.test)=='(int(_ext_found) <= 0 or int(_var_found) <= 0) and (not _pretrade_found_low_confirmed)')
        code=compile(ast.fix_missing_locations(ast.Expression(body=gate.test)),'<actual full-close pretrade gate>','eval')
        env={'_ext_found':0,'_var_found':0,'_pretrade_found_low_confirmed':True}
        self.assertFalse(eval(code,env))
        env['_pretrade_found_low_confirmed']=False;self.assertTrue(eval(code,env))


def environment(directory):
    env,logs=namespace(directory)
    env.update(_close_flow=close_flow,_WORKER_STATE=SimpleNamespace(assert_live=lambda:None),
        _ACTIVE_EXEC_CTX=threading.local(),_pin=Mock(),EXT={'name':'EXT','rows':'ext-rows'},VAR={'name':'VAR','rows':'var-rows'},
        _exec_log=Mock(),_pin_exec_reduce_phase=Mock(),_exec_ensure_active_page=Mock(),
        _var_prepare_positions_view=Mock(),re=__import__('re'))
    def bind(req,**kwargs):env['_ACTIVE_EXEC_CTX'].req_ref=req
    env['_active_req_bind']=bind
    env['_exec_positions_confirmed_empty']=lambda page,cfg:bool(page.flat)
    env['_exec_read_portfolio_snapshot']=lambda page,cfg,**k:{'available':True,'positions':dict(page.positions)}
    names={'_var_full_close_guarded','_run_full_close_transaction','_full_close_read_var_coins',
           '_full_close_confirm_var_absence',
           '_full_close_dismiss_dialog','_full_close_recover_page','_exec_global_close_report',
           '_safety_confirm_flat_scope','_safety_resolve_submission'}
    load_definitions(names,env)
    env['_full_close_dismiss_dialog']=Mock(return_value=True)
    env['_full_close_recover_page']=Mock(return_value=True)
    return env,logs


class FullCloseReplayTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.env,self.logs=environment(tmp.name);self.directory=tmp.name
        self.varpage=SimpleNamespace(flat=False,positions={'TIA':{'side':'SHORT','qty_abs':10,'notional_u':100}})
        self.extpage=SimpleNamespace(flat=True,positions={})
        self.ext=SimpleNamespace(page=self.extpage,close_all_global=Mock(return_value=True))
        self.var=SimpleNamespace(page=self.varpage)
        self.var.close_all_global=lambda:self.env['_var_full_close_guarded'](self.var)
        self.env['_full_close_read_var_coins']=Mock(side_effect=lambda exe:list(exe.page.positions))
        self.clock=Clock();self.flow=close_flow.CloseFlow(clock=self.clock.time)

    def run_close(self):
        with close_flow.scope(self.flow):
            return self.env['_run_full_close_transaction'](self.ext,self.var,request(),['TIA'])

    def confirm(self,coin):
        self.varpage.positions.pop(coin,None);self.varpage.flat=not self.varpage.positions
        return True

    def test_read_table_failure_recovers_once_then_closes(self):
        self.env['_full_close_read_var_coins']=Mock(side_effect=[TimeoutError('table temporarily unavailable'),['TIA']])
        self.var.close_all=Mock(side_effect=self.confirm)
        reports=self.run_close()
        self.env['_full_close_recover_page'].assert_called_once()
        self.var.close_all.assert_called_once_with('TIA')
        self.assertTrue(reports['EXT']['ok'] and reports['VAR']['ok'])

    def test_unsubmitted_close_failure_recovers_once_not_three_times(self):
        self.var.close_all=Mock(return_value=False)
        reports=self.run_close()
        self.assertEqual(self.var.close_all.call_count,2)
        self.env['_full_close_recover_page'].assert_called_once()
        self.assertFalse(reports['VAR']['ok']);self.assertTrue(reports['EXT']['ok'])
        self.assertIn('TIA',reports['VAR']['remaining_coins'])

    def test_submitted_but_unconfirmed_is_never_retried_or_dismissed(self):
        def attempt(coin):
            self.flow.record_attempt(dict(id='one',platform='VAR',coin=coin,state='CLICK_UNKNOWN'))
            return False
        self.var.close_all=Mock(side_effect=attempt)
        reports=self.run_close()
        self.var.close_all.assert_called_once();self.env['_full_close_recover_page'].assert_not_called()
        self.env['_full_close_dismiss_dialog'].assert_not_called()
        self.assertTrue(close_flow.submission_summary(reports)['pending_confirm'])
        self.assertFalse(reports['VAR']['ok'])

    def test_external_close_pending_blocks_without_a_new_submission(self):
        self.env['_safety_reserve_submission']('VAR','TIA','SHORT','close_all',self.varpage.positions['TIA'],100)
        self.var.close_all=Mock(side_effect=AssertionError('must not resubmit'))
        reports=self.run_close();self.var.close_all.assert_not_called()
        self.env['_full_close_recover_page'].assert_not_called()
        self.assertTrue(close_flow.submission_summary(reports)['no_submit'])

    def test_old_open_pending_does_not_block_real_close_submission(self):
        self.env['_safety_reserve_submission']('VAR','TIA','SHORT','open',None,100)
        button=Button();old_click=button.click
        def click(**kwargs):old_click(**kwargs);self.confirm('TIA')
        button.click=click
        self.var.close_all=lambda coin:self.env['_submit_close_click'](button,'VAR',coin,'SHORT',
            {'side':'SHORT','qty_abs':10,'notional_u':100},100,500,'fake close')
        reports=self.run_close();self.assertTrue(reports['VAR']['ok']);self.assertEqual(button.clicks,1)
        rec=self.env['_safety_submission_records']()
        self.assertEqual(rec['VAR|TIA']['kind'],'open')
        self.assertEqual(rec['VAR|TIA|CLOSE|SHORT']['module'],'ANCHOR')
        self.assertEqual(reports['VAR']['close_attempts'][-1]['attempts'][0]['state'],'CLICKED')

    def test_click_timeout_retains_durable_unknown_and_one_dispatch(self):
        button=Button(click_error=True)
        self.var.close_all=Mock(side_effect=lambda coin:self.env['_submit_close_click'](button,'VAR',coin,'SHORT',
            {'side':'SHORT','qty_abs':10,'notional_u':100},100,500,'fake close'))
        reports=self.run_close();self.assertEqual(button.clicks,1);self.var.close_all.assert_called_once()
        self.assertTrue(close_flow.submission_summary(reports)['pending'])
        self.assertEqual(self.env['_safety_submission_records']()['VAR|TIA|CLOSE|SHORT']['state'],'PENDING_CONFIRM')
        self.env['_full_close_recover_page'].assert_not_called()

    def test_submitted_coin_disappears_while_other_positions_remain(self):
        self.varpage.positions['LTC']={'side':'SHORT','qty_abs':5,'notional_u':300}
        calls=[]
        def close(coin):
            calls.append(coin)
            self.env['_safety_reserve_submission']('VAR',coin,'SHORT','close_all',self.varpage.positions[coin],100)
            self.flow.record_attempt(dict(id=coin,platform='VAR',coin=coin,state='CLICKED'))
            self.confirm(coin)
            return False  # Old per-coin reader cannot confirm absence with another row present.
        self.var.close_all=close
        reports=self.run_close()
        self.assertEqual(calls,['TIA','LTC']);self.assertTrue(reports['VAR']['ok'])
        self.assertEqual(self.env['_safety_submission_records']()['VAR|TIA|CLOSE|SHORT']['state'],'CONFIRMED_SUCCESS')
        self.env['_full_close_recover_page'].assert_not_called()

    def test_prior_coin_close_is_settled_without_resubmission_then_other_coins_close(self):
        self.env['_safety_reserve_submission']('VAR','LTC','SHORT','close_all',
            {'side':'SHORT','qty_abs':5,'notional_u':300},300)
        self.var.close_all=Mock(side_effect=self.confirm)
        reports=self.run_close()
        self.var.close_all.assert_called_once_with('TIA')
        self.assertTrue(reports['VAR']['ok'])
        self.assertEqual(self.env['_safety_submission_records']()['VAR|LTC|CLOSE|SHORT']['state'],'CONFIRMED_SUCCESS')

    def test_pending_global_close_still_prevents_any_duplicate_coin_order(self):
        self.env['_safety_reserve_submission']('VAR','ALL','','close_all',None,0)
        self.var.close_all=Mock()
        reports=self.run_close()
        self.var.close_all.assert_not_called();self.assertFalse(reports['VAR']['ok'])

    def test_absence_confirmation_rejects_transient_or_unreadable_table(self):
        self.env['_safety_reserve_submission']('VAR','LTC','SHORT','close_all',
            {'side':'SHORT','qty_abs':5,'notional_u':300},300)
        self.flow.mark_unknown('VAR','LTC','submitted')
        for values in ([['TIA'],['TIA','LTC']],[['TIA'],['GRASS']],
                       [['TIA'],TimeoutError('table unavailable')]):
            self.env['_full_close_read_var_coins']=Mock(side_effect=values)
            with close_flow.scope(self.flow):
                try:result=self.env['_full_close_confirm_var_absence'](self.var,'LTC',self.flow)
                except TimeoutError:result=False
            self.assertFalse(result)
            self.assertEqual(self.env['_safety_submission_records']()['VAR|LTC|CLOSE|SHORT']['state'],'PENDING_CONFIRM')

    def test_global_failure_logs_exact_platform_and_real_coins(self):
        self.var.close_all=Mock(return_value=False)
        self.run_close()
        text='\n'.join(self.logs)
        self.assertIn('platform=VAR',text);self.assertIn("remaining_coins=['TIA']",text)
        self.assertIn('anchor_unchanged=1',text)

    def test_deadline_cannot_be_reset_by_recovery(self):
        self.clock.now=114
        self.var.close_all=Mock(side_effect=lambda coin:(self.clock.sleep(10),False)[1])
        reports=self.run_close()
        self.assertFalse(any(r['ok'] for r in reports.values()))
        self.assertTrue(all(r['reason']=='full_close_deadline_exhausted' for r in reports.values()))
        self.env['_full_close_recover_page'].assert_not_called()

    def test_unclosed_unsubmitted_modal_aborts_instead_of_clicking_next_market(self):
        self.varpage.positions['LTC']={'side':'SHORT','qty_abs':5,'notional_u':300}
        self.var.close_all=Mock(return_value=False)
        self.env['_full_close_dismiss_dialog'].return_value=False
        self.env['_full_close_recover_page'].return_value=False
        reports=self.run_close()
        self.var.close_all.assert_called_once_with('TIA')
        self.assertEqual(set(reports['VAR']['remaining_coins']),{'TIA','LTC'})


class SessionAndLiveEntryTests(unittest.TestCase):
    def test_closed_page_in_full_close_does_not_destroy_owner_runtime(self):
        env={'_pw_guard':lambda **k:True,'_pw_is_closed':lambda p:True,'_close_flow':close_flow,
             '_pw_runtime_hard_reset':Mock(),'_UiDeadError':lambda where,reason:RuntimeError(reason)}
        load_definitions({'_pw_require_page'},env)
        with close_flow.scope(close_flow.CloseFlow()):
            with self.assertRaisesRegex(RuntimeError,'page_is_closed_or_none'):
                env['_pw_require_page'](object(),'VAR full close')
        env['_pw_runtime_hard_reset'].assert_not_called()

    def test_actual_worker_profile_and_handoff_use_bounded_owner(self):
        state=WorkerState()
        env={'_WORKER_STATE':state,'_has_running_asyncio_loop':lambda:False,
             '_executor_batch_timeout_for_requests':lambda *a,**k:42}
        load_definitions({'_executor_coin_worker_profile','_executor_worker_handoff_needed'},env)
        profile=env['_executor_coin_worker_profile']([request()],batch_name='EXECUTOR_QUANBU')
        self.assertEqual(profile['timeout_sec'],125)
        handoff=lambda r:env['_executor_worker_handoff_needed']([r],batch_name='EXECUTOR_QUANBU',default_worker='unused')
        self.assertTrue(handoff(request()))
        state.begin(profile['worker_name'],0,{},{});self.assertFalse(handoff(request()))
        r=request();r.module='M10'
        self.assertEqual(env['_executor_coin_worker_profile']([r])['timeout_sec'],50)
        self.assertFalse(handoff(r))

    def test_only_exact_unsubmitted_close_dialog_can_be_cancelled(self):
        with tempfile.TemporaryDirectory() as directory:
            env,_=environment(directory);load_definitions({'_full_close_dismiss_dialog'},env)
            cancel=SimpleNamespace(count=lambda:1,click=Mock())
            dialog=SimpleNamespace(get_by_role=Mock(return_value=cancel),wait_for=Mock())
            owned=SimpleNamespace(count=lambda:1,first=dialog)
            dialogs=SimpleNamespace(count=lambda:1,filter=Mock(return_value=owned))
            page=SimpleNamespace(locator=Mock(return_value=dialogs))
            flow=close_flow.CloseFlow();flow.begin('VAR','TIA')
            self.assertTrue(env['_full_close_dismiss_dialog'](page,'TIA',flow))
            cancel.click.assert_called_once();dialog.wait_for.assert_called_once()
            pattern=dialog.get_by_role.call_args.kwargs['name']
            self.assertIsNotNone(pattern.fullmatch('Cancel'));self.assertIsNone(pattern.fullmatch('Confirm'))
            flow.record_attempt(dict(id='sent',platform='VAR',coin='TIA',state='CLICK_UNKNOWN'))
            self.assertFalse(env['_full_close_dismiss_dialog'](page,'TIA',flow))
            cancel.click.assert_called_once()

    def test_recovery_rebinds_only_live_owner_and_reloads_same_page(self):
        with tempfile.TemporaryDirectory() as directory:
            env,_=environment(directory)
            sessions=SessionLiveness();browser=SimpleNamespace(is_connected=lambda:True)
            page=SimpleNamespace(is_closed=lambda:False,reload=Mock())
            extpage=object();exe=SimpleNamespace(page=page)
            rt={'browser':browser,'context':object(),'ext_page':extpage,'var_page':page}
            sessions.begin(browser);sessions.set_ready(False)
            env.update(_PW_SESSIONS=sessions,_exec_runtime_cache_get=lambda:rt,
                       _exec_runtime_connection_alive=lambda r:True,_exec_runtime_cache_set=Mock(),
                       _rebuild_single_page_in_context=Mock())
            load_definitions({'_full_close_recover_page'},env)
            flow=close_flow.CloseFlow();flow.begin('VAR','TIA')
            with close_flow.scope(flow):self.assertTrue(env['_full_close_recover_page'](exe,'VAR','TIA',flow))
            page.reload.assert_called_once();self.assertIs(exe.page,page);self.assertIs(rt['ext_page'],extpage)
            self.assertTrue(sessions.current_alive());env['_rebuild_single_page_in_context'].assert_not_called()

    def test_recovery_rejects_dead_owner_or_any_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            env,_=environment(directory);page=SimpleNamespace(reload=Mock())
            env.update(_exec_runtime_cache_get=lambda:{},_exec_runtime_connection_alive=lambda rt:False)
            load_definitions({'_full_close_recover_page'},env)
            flow=close_flow.CloseFlow();flow.begin('VAR','TIA');exe=SimpleNamespace(page=page)
            self.assertFalse(env['_full_close_recover_page'](exe,'VAR','TIA',flow))
            flow.record_attempt(dict(id='sent',platform='VAR',coin='TIA',state='CLICK_UNKNOWN'))
            self.assertFalse(env['_full_close_recover_page'](exe,'VAR','TIA',flow));page.reload.assert_not_called()

    def test_m2_driver_exit_preserves_executor_var_full_close(self):
        with tempfile.TemporaryDirectory() as directory:
            env,_=environment(directory)
            sessions=SessionLiveness();browser=SimpleNamespace(is_connected=lambda:True)
            sessions.begin(browser);stopped=[]
            @contextmanager
            def scan_driver():
                try:yield object()
                finally:stopped.append('M2 released')
            env.update(_PW_SESSIONS=sessions,_worker_runtime_update_allowed=lambda:True,
                _pw_runtime_is_recovering=lambda *a:False,sync_playwright=scan_driver,
                contextmanager=contextmanager,_EXEC_WORKER_LOCAL=threading.local())
            load_definitions({'_owned_playwright_session','_pw_set_alive','_pw_guard'},env)
            def scanner():
                with env['_owned_playwright_session']():
                    sessions.begin(SimpleNamespace(is_connected=lambda:True));env['_pw_set_alive'](False)
            t=threading.Thread(target=scanner);t.start();t.join(1)
            self.assertEqual(stopped,['M2 released']);self.assertTrue(env['_pw_guard'](plat='VAR'))
            env['_exec_ensure_active_page']=lambda *a:self.assertTrue(env['_pw_guard'](plat='VAR'))
            def call(label,fn,*args,**kwargs):
                self.assertTrue(env['_pw_guard'](plat='VAR'))
                kwargs.pop('_operation_timeout_ms',None)
                return fn(*args,**kwargs)
            env['_pw_call']=call
            env['_full_close_read_var_coins']=lambda exe:['TIA']
            page=SimpleNamespace(flat=False,positions={'TIA':{'side':'SHORT','qty_abs':10,'notional_u':100}})
            button=Button();click=button.click
            def close_click(**kwargs):click(**kwargs);page.flat=True;page.positions.clear()
            button.click=close_click
            exe=SimpleNamespace(page=page,close_all=lambda coin:env['_submit_close_click'](
                button,'VAR',coin,'SHORT',{'side':'SHORT','qty_abs':10,'notional_u':100},100,500,'VAR full close'))
            self.assertTrue(env['_var_full_close_guarded'](exe));self.assertEqual(button.clicks,1)

    def test_retired_reason_is_preserved_at_page_guard(self):
        class Dead(RuntimeError):pass
        state=WorkerState();state.begin('W',0,{},{});state.invalidate('W',{},{},{},{})
        env={'_WORKER_STATE':state,'_EXEC_WORKER_LOCAL':threading.local(),
             '_PW_SESSIONS':SessionLiveness(),'_worker_runtime_update_allowed':lambda:not state.retired_current(),
             '_pw_runtime_is_recovering':lambda *a:False,'_pin':Mock(),'_record_swallowed_exception':Mock(),
             '_UiDeadError':lambda where,reason:Dead(reason)}
        load_definitions({'_pw_guard','_pw_require_page'},env)
        with self.assertRaisesRegex(Dead,'worker_generation_retired'):
            env['_pw_require_page'](object(),'VAR full close')

    def test_real_var_count_exception_is_not_returned_as_zero_in_full_close(self):
        tree=parsed(str(ROOT/'BOT5-2-414 .py'))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='_VarExecutor')
        close=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='close_all')
        find=copy.deepcopy(next(n for n in ast.walk(close) if isinstance(n,ast.FunctionDef) and n.name=='_find_target_row_once'))
        code=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),find],type_ignores=[])
        env={'self':SimpleNamespace(page=SimpleNamespace(locator=lambda *a:SimpleNamespace(count=Mock(side_effect=TimeoutError('read failed'))))),
             'VAR':{'rows':'rows'},'_close_flow':close_flow}
        exec(compile(ast.fix_missing_locations(code),'<real VAR target-row reader>','exec'),env)
        with close_flow.scope(close_flow.CloseFlow()):
            with self.assertRaisesRegex(TimeoutError,'read failed'):env['_find_target_row_once']()

    def test_full_close_uses_positions_rows_without_market_navigation(self):
        tree=parsed(str(ROOT/'BOT5-2-414 .py'))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='_VarExecutor')
        close=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='close_all')
        gate=next(n for n in close.body if isinstance(n,ast.If) and '_exec_goto_coin_var' in ast.unparse(n))
        code=compile(ast.fix_missing_locations(ast.Expression(body=gate.test)),'<actual VAR navigation predicate>','eval')
        env={'coin':'TIA','_close_flow':close_flow}
        self.assertTrue(eval(code,env))
        with close_flow.scope(close_flow.CloseFlow()):self.assertFalse(eval(code,env))

    def test_controlled_restart_preserves_anchor_without_resetting_grid_records(self):
        env={'os':os,'_load_anchor_state':lambda:{'anchor_u':318.83},
             '_get_anchor_u':Mock(),'_anchor_reset_other_module_records_on_start':Mock()}
        load_definitions({'_anchor_bootstrap_runtime'},env)
        with patch.dict(os.environ,{'BOT_CONTROLLED_RESTART_RESUME':'1'}):
            self.assertEqual(env['_anchor_bootstrap_runtime'](380),318.83)
        env['_anchor_reset_other_module_records_on_start'].assert_not_called()
        env['_get_anchor_u'].assert_not_called()

    def test_controlled_restart_with_invalid_anchor_fails_closed(self):
        env={'os':os,'_load_anchor_state':lambda:{'anchor_u':0}}
        load_definitions({'_anchor_bootstrap_runtime'},env)
        with patch.dict(os.environ,{'BOT_CONTROLLED_RESTART_RESUME':'1'}):
            with self.assertRaisesRegex(RuntimeError,'anchor_missing'):env['_anchor_bootstrap_runtime'](380)


if __name__=='__main__':unittest.main()
