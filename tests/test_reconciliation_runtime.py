"""AST-isolated live adapters: fresh evidence, cached journal and observable blocking."""
import ast
import copy
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ast_support import ROOT, parsed, load_definitions
from bot_core import legacy_reconciliation as legacy
from bot_core.execution_confirmation import apply_submission_evidence, apply_confirmation_fields, pending_blocks
from bot_core.postcheck_lifecycle import confirmed_full_close_result
from bot_core.settlement_commands import SettlementInbox, proposal
from test_postcheck_lifecycle import namespace, scan, request
from test_submission_margin import namespace as submit_namespace, order, Button
from test_legacy_reconciliation import record, row, snapshot


class FreshCoverageTests(unittest.TestCase):
    def test_complete_table_covers_old_symbols_outside_current_pool(self):
        s=snapshot(raw_positions={'VAR':{}},raw={'platform_position_coverage':{'VAR':{
            'complete':True,'source':'verified_positions_table','coins':['DASH'],'ts':1300}}})
        r=record()
        for i,t in enumerate([1300,1330,1360]):
            s['ts']=s['raw']['platform_position_coverage']['VAR']['ts']=t
            r,d=legacy.reconcile(r,s,now=t,scan_id=str(i))
        self.assertEqual(r['state'],'CONFIRMED_NOT_FILLED')

    def test_partial_unverified_stale_or_present_coverage_cannot_imply_empty(self):
        for extra in ({'complete':False},{'source':'cached'},{'ts':1000},{'coins':['TEST']}):
            coverage={'complete':True,'source':'verified_positions_table','coins':['DASH'],'ts':1300};coverage.update(extra)
            s=snapshot(raw_positions={'VAR':{}},raw={'platform_position_coverage':{'VAR':coverage}})
            r,d=legacy.reconcile(record(),s,now=1300,scan_id='one')
            self.assertEqual(r['state'],'PENDING_CONFIRM');self.assertIsNone(d.get('empty_scan_count'))

    def test_exact_later_coin_close_proves_resolved_flat(self):
        records={'VAR|TEST':record(),'VAR|TEST|CLOSE|LONG':record(kind='close_all',state='CONFIRMED_SUCCESS',
            pre_position=row(),resolved_ts=1200)}
        updated,_=legacy.replay(records,snapshot(position={'position_state':'CONFIRMED_EMPTY'}),now=1300,scan_id='one')
        self.assertEqual(updated['VAR|TEST']['state'],'RESOLVED_FLAT')

    def test_phantom_aggregate_close_only_resolves_on_fresh_whole_platform_empty(self):
        r=record(coin='ALL',kind='close_all',intent_usd=0,reason='postcheck_deferred_unconfirmed')
        s=snapshot(raw_positions={'VAR':{}},raw={'platform_empty_detail_map':{'VAR':{'confirmed_empty':True}}})
        updated,d=legacy.reconcile(r,s,now=1300,scan_id='one')
        self.assertEqual(updated['state'],'RESOLVED_FLAT');self.assertFalse(pending_blocks(updated))
        s['data_quality']='fail'
        self.assertEqual(legacy.reconcile(r,s,now=1300,scan_id='bad')[0]['state'],'PENDING_CONFIRM')

    def test_all_scope_is_not_a_missing_symbol_in_a_nonempty_table(self):
        r=record(coin='ALL',kind='close_all',intent_usd=0)
        s=snapshot(raw={'platform_position_coverage':{'VAR':{
            'complete':True,'source':'verified_positions_table','coins':['TEST'],'ts':1300}}})
        self.assertEqual(legacy.reconcile(r,s,now=1300,scan_id='one')[0]['state'],'PENDING_CONFIRM')
        s['raw']['platform_empty_detail_map']={'VAR':{'confirmed_empty':True}}
        self.assertEqual(legacy.reconcile(r,s,now=1300,scan_id='conflict')[0]['state'],'PENDING_CONFIRM')


class RuntimeReconciliationTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.directory=tmp.name;self.env,self.clock=namespace(tmp.name)
        self.clock.wall=1300

    def test_startup_settles_legacy_observed_by_m9_and_cleans_only_open_memory(self):
        r=record(platform='EXT',coin='TIA')
        self.env['_safety_submission_records']()['EXT|TIA']=r
        self.env['_EXEC_PENDING'][('EXT','TIA','LONG')]=1000
        self.env['_EXEC_PENDING'][('EXT','TIA','LONG','close')]=1000
        result=self.env['_postcheck_reconcile_unconfirmed_scan'](scan(1300,row=row()),scan_id='first')
        self.assertEqual(result,['EXT|TIA'])
        self.assertNotIn(('EXT','TIA','LONG'),self.env['_EXEC_PENDING'])
        self.assertIn(('EXT','TIA','LONG','close'),self.env['_EXEC_PENDING'])
        self.assertTrue(any('stage=STARTUP' in line and 'legacy_same_side' in line for line in self.env['logs']))
        after,_=namespace(self.directory)
        self.assertFalse(after['_safety_pending_scope']('EXT','TIA'))

    def test_manual_command_updates_running_cache_and_durable_record(self):
        r=record(platform='EXT',coin='TIA')
        self.env['_safety_submission_records']()['EXT|TIA']=r
        cmd=proposal('EXT|TIA',r,state='RESOLVED_FLAT',reason='reviewed',expected_fingerprint=legacy.fingerprint(r),now=1300)
        inbox=SettlementInbox(self.directory);inbox.enqueue(cmd)
        self.env['_postcheck_reconcile_unconfirmed_scan'](scan(1300),scan_id='first')
        self.assertEqual(inbox.status(cmd['id'])['status'],'APPLIED')
        self.assertFalse(self.env['_safety_pending_scope']('EXT','TIA'))

    def test_stale_startup_waits_for_fresh_scan(self):
        r=record(platform='EXT',coin='TIA');self.env['_safety_submission_records']()['EXT|TIA']=r
        self.env['_postcheck_reconcile_unconfirmed_scan'](scan(1100,row=row()),scan_id='old')
        self.assertFalse(self.env.get('_SAFETY_RECON_STARTUP_DONE',False))
        self.env['_postcheck_reconcile_unconfirmed_scan'](scan(1300,row=row()),scan_id='fresh')
        self.assertTrue(self.env['_SAFETY_RECON_STARTUP_DONE'])

    def test_blocked_avax_cannot_poison_dash_or_create_another_claim(self):
        records=self.env['_safety_submission_records']()
        records['VAR|ALL|CLOSE|ALL']=record(platform='VAR',coin='ALL',kind='close_all')
        records['EXT|DASH']=record(platform='EXT',coin='DASH',state='CONFIRMED_SUCCESS')
        before=copy.deepcopy(records)
        self.assertTrue(self.env['_postcheck_defer_to_scan'](request(coin='AVAX',platform='VAR'),reason='blocked_existing_unconfirmed_no_submit'))
        self.assertEqual(records,before);self.assertFalse(self.env.get('_SAFETY_SUBMISSION_FAULT'))
        self.assertFalse(self.env['_safety_pending_scope']('EXT','DASH'))

    def test_defer_does_not_create_fake_all_claim_after_confirmed_flat(self):
        self.env['_safety_submission_records']()['VAR|TIA|CLOSE|SHORT']=record(platform='VAR',coin='TIA',kind='close_all',state='CONFIRMED_SUCCESS')
        req=request(module='ANCHOR',coin='ALL',platform='PAIR',action_type='close_pair')
        self.assertTrue(self.env['_postcheck_defer_to_scan'](req,reason='deadline_exhausted'))
        self.assertNotIn('VAR|ALL|CLOSE|ALL',self.env['_safety_submission_records']())


class FullClosePostcheckTests(unittest.TestCase):
    def result(self):
        req={'module':'ANCHOR','coin':'ALL','platform':'PAIR','action_type':'close_pair','meta':{'req_id':'current'}}
        result={'req':req,'ok':True,'postcheck_ok':True,'close_platforms':{
            p:{'ok':True,'flat_confirmed':True,'remaining_coins':[],'unverified_coins':[]} for p in ('EXT','VAR')}}
        return req,result

    def test_current_dual_flat_result_cannot_be_downgraded_by_all_symbol_read(self):
        req,result=self.result()
        env={'_req_get_dict':lambda r:r,'_safety_verify_scoped_change':Mock(side_effect=AssertionError('ALL is not a symbol'))}
        load_definitions({'_postcheck_validate_effect_v2'},env)
        state,reason,_=env['_postcheck_validate_effect_v2'](req,{}, {},exec_res_list=[result])
        self.assertEqual(state,'success');self.assertEqual(reason,'both_platforms_live_flat_confirmed')

    def test_partial_or_different_request_has_no_success_shortcut(self):
        for field in ['flat_confirmed','ok']:
            req,result=self.result();result['close_platforms']['VAR'][field]=False
            self.assertIsNone(confirmed_full_close_result(req,[result]))
        req,result=self.result();req=copy.deepcopy(req);req['meta']['req_id']='other'
        self.assertIsNone(confirmed_full_close_result(req,[result]))


class ExplanationTests(unittest.TestCase):
    def test_observation_log_explains_no_submit_and_round_counts_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            env,_=submit_namespace(directory);o=order(module='M9')
            env['_safety_reserve_submission']('EXT','TIA','SHORT','open',None,170)
            env['_exe_open_position_safe'](SimpleNamespace(open_position=Mock()),o,plat='EXT')
            events=[]
            env.update(_observe_emit=lambda kind,text:events.append((kind,text)),
                _m9_runner_carry_display=lambda *a,**k:{},_safe_float=lambda x,*a:None,
                _observe_ts_text=lambda *a,**k:'time',_coin_pool_compact_text=lambda:'pool')
            load_definitions({'_observe_execution_result','_observe_round_finish'},env)
            req=SimpleNamespace(**o._exec_req_ref)
            env['_observe_execution_result'](req,skip_cnt=1)
            self.assertIn('未提交',events[0][1]);self.assertIn('EXT|TIA',events[0][1])
            self.assertIn('本次新增待确认=0 跳过=1',events[0][1])
            env['_observe_round_finish'](1,steps=2,attempts=1,skipped_attempts=1)
            summary=next(text for kind,text in events if '本轮执行汇总' in text)
            self.assertIn('失败=0',summary);self.assertIn('未提交/跳过=1',summary)

    def test_blocker_is_a_skipped_unsubmitted_request_with_scope_and_age(self):
        with tempfile.TemporaryDirectory() as directory:
            env,logs=submit_namespace(directory);o=order(module='M9')
            env['_safety_reserve_submission']('EXT','TIA','SHORT','open',None,170)
            exe=SimpleNamespace(open_position=Mock())
            self.assertFalse(env['_exe_open_position_safe'](exe,o,plat='EXT'))
            exe.open_position.assert_not_called()
            rr=env['_make_exec_result_row'](o._exec_req_ref,ok=False)
            self.assertTrue(rr['skipped']);self.assertFalse(rr['pending_confirm']);self.assertTrue(rr['no_submit'])
            self.assertIn('EXT|TIA',rr['message']);self.assertIn('170U',rr['message']);self.assertIn('没有点击',rr['message'])

    def test_journal_fault_is_not_mislabelled_as_this_coins_old_order(self):
        with tempfile.TemporaryDirectory() as directory:
            env,logs=submit_namespace(directory);o=order(module='M9')
            env['_SAFETY_SUBMISSION_FAULT']=True;env['_SAFETY_SUBMISSION_FAULT_DETAIL']='journal_save:OSError:disk unavailable'
            env['_exe_open_position_safe'](SimpleNamespace(open_position=Mock()),o,plat='EXT')
            rr=env['_make_exec_result_row'](o._exec_req_ref,ok=False)
            self.assertEqual(rr['reason'],'submission_journal_unavailable_no_submit')
            self.assertIn('disk unavailable',rr['message']);self.assertTrue(rr['skipped'])

    def test_quantity_verified_order_metadata_reaches_final_result(self):
        with tempfile.TemporaryDirectory() as directory:
            env,_=submit_namespace(directory);o=order();button=Button()
            def open_position(o):
                env['_submit_order_click'](button,o,'EXT','SHORT',{'position_state':'CONFIRMED_EMPTY'},170,500,'test')
                env['_safety_submission_records']()['EXT|TIA']['state']='CONFIRMED_SUCCESS'
                o.meta.update(_submit_verified_ok=True,_submit_verified_ev={'row_evidence':{
                    'effective':True,'confirm_basis':'position_quantity_delta'},'reason_code':'commit_success'})
                return True
            self.assertTrue(env['_exe_open_position_safe'](SimpleNamespace(open_position=open_position),o,plat='EXT'))
            rr=env['_make_exec_result_row'](o._exec_req_ref,ok=True)
            self.assertTrue(rr['postcheck_ok']);self.assertEqual(rr['confirmation_state'],'confirmed_success')


if __name__=='__main__':unittest.main()
