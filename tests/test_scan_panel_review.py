"""Recorded current-page navigation and DQ/display regressions; no live actions."""
import ast
import copy
from datetime import datetime
import time
import types
import unittest

from ast_support import ROOT, parsed, load_definitions


class ScanCompletionTests(unittest.TestCase):
    def env(self, snapshot, worker, accept=None):
        calls=[]
        env=load_definitions({'m2_scan'}, {
            '_env_flag':lambda *a:worker, '_m2_scan_timeout_sec':lambda d:90,
            'time':types.SimpleNamespace(time=lambda:2000),
            '_m2_scan_runtime_gen_capture':lambda:3,
            '_m2_scan_watch_update':lambda *a,**k:None,
            '_m2_scan_watch_finish':lambda state,**k:calls.append((state,k.get('reason'))),
            '_m2_scan_impl':lambda *a,**k:snapshot,
            '_m2_accept_snapshot':accept or (lambda value,generation:value),
            '_run_call_in_worker_thread':lambda fn,**k:fn(),
            'write_jsonl':lambda *a,**k:True,'now_ts':lambda:2000,
        })
        return env,calls

    def test_failed_snapshot_is_error_in_direct_and_worker_paths(self):
        snapshot={'data_quality':'fail','data_quality_reason':'EXT:IRYS:row_not_found'}
        for worker in (False,True):
            with self.subTest(worker=worker):
                env,calls=self.env(snapshot,worker)
                self.assertIs(env['m2_scan']('unused',['MAGMA','IRYS'],'unused'),snapshot)
                self.assertEqual(calls,[('ERROR','EXT:IRYS:row_not_found')])

    def test_success_is_reported_only_after_snapshot_acceptance(self):
        snapshot={'data_quality':'ok'}
        for worker in (False,True):
            env,calls=self.env(snapshot,worker)
            self.assertIs(env['m2_scan']('unused',[],'unused'),snapshot)
            self.assertEqual(calls,[('DONE','scan_ok')])
            rejected={'data_quality':'fail','data_quality_reason':'runtime_generation_changed_before_commit'}
            env,calls=self.env(snapshot,worker,accept=lambda *a:rejected)
            self.assertIs(env['m2_scan']('unused',[],'unused'),rejected)
            self.assertEqual(calls,[('ERROR','runtime_generation_changed_before_commit')])


class RetryDisplayTests(unittest.TestCase):
    def env(self):
        names={'_scan_sleep_plan_set','_panel_scan_timing_set','_scan_sleep_plan_snapshot',
               '_scan_sleep_plan_status','_scan_runtime_panel_brief','_scan_sleep_plan_human_view'}
        return load_definitions(names,{
            'time':types.SimpleNamespace(time=lambda:2000,strftime=time.strftime,localtime=time.localtime),
            'datetime':datetime,'_scan_sleep_wake_tolerance_sec':lambda:5,
            '_reason_with_zh':lambda s:s,'_reason_human_text':lambda s:s,
            '_m2_scan_watch_snapshot':lambda:{'active':False,'state':'ERROR','stage':'done','reason':'scan_dq_fail'},
            '_MAIN_ROUND_WATCH':{'active':False,'state':'ERROR','stage':'SCAN_M2'},
            '_SCAN_SLEEP_PLAN':{'active':True,'deadline_ts':1000,'deadline_text':'OLD',
                                'plan_source':'MAIN','extra':{'next_scan_text':'OLD'}},
            '_PANEL_SCAN_TIMING':{},
            '_round_phase_touch':lambda *a,**k:None,'_append_txt':lambda *a,**k:None,
            '_round_watch_finish':lambda *a,**k:None,'fp_yunxing':'unused',
            '_dq_meta':{'wait_only':True,'reason_code':'scan_dq_fail','last_good_age_sec':1000},
            'LAST_GOOD_AGE_MAX_SEC':900,'DQ_STALE_SLEEP_SEC':10,'scan_count':12,
            '_sleep_heartbeat':lambda *a,**k:None,
        })

    def execute_wait_branch(self,path,env):
        tree=parsed(str(path))
        choices=[n for n in ast.walk(tree) if isinstance(n,ast.If)
                 and "_dq_meta" in ast.unparse(n.test) and "get('wait_only')" in ast.unparse(n.test)]
        self.assertEqual(len(choices),1)
        statements=[copy.deepcopy(n) for n in choices[0].body if not isinstance(n,ast.Continue)]
        code=ast.fix_missing_locations(ast.Module(body=statements,type_ignores=[]))
        exec(compile(code,'<dq-retry-branch>','exec'),env)

    def test_recorded_old_branch_leaves_sleep_deadline_stale(self):
        env=self.env()
        self.execute_wait_branch(ROOT/'repair_backups/scan_panel_review_20261023/BOT5-2-414 .py',env)
        self.assertEqual(env['_SCAN_SLEEP_PLAN']['deadline_text'],'OLD')
        self.assertEqual(env['_SCAN_SLEEP_PLAN']['deadline_ts'],1000)

    def test_current_wait_branch_replaces_both_timing_sources(self):
        env=self.env()
        self.execute_wait_branch(ROOT/'BOT5-2-414 .py',env)
        plan=env['_SCAN_SLEEP_PLAN']
        self.assertEqual(plan['deadline_ts'],2010)
        self.assertEqual(plan['plan_source'],'dq_retry')
        self.assertEqual(plan['sleep_total_sec'],10)
        self.assertNotEqual(plan['deadline_text'],'OLD')
        self.assertNotEqual(plan.get('extra',{}).get('next_scan_text'),'OLD')
        timing=env.get('_PANEL_SCAN_TIMING') or {}
        self.assertEqual(timing['next_scan_ts'],2010)

    def test_failed_round_with_leftover_active_flag_is_not_busy(self):
        env=self.env()
        result=env['_scan_runtime_panel_brief']({'active':False,'state':'ERROR'},
            {'active':True,'state':'ERROR','stage':'SCAN_M2'},nowv=2000)
        self.assertFalse(result['runtime_busy'])
        result=env['_scan_runtime_panel_brief']({'active':True,'state':'SCANNING'},
            {'active':True,'state':'SCANNING','stage':'SCAN_M2'},nowv=2000)
        self.assertTrue(result['runtime_busy'])

    def test_dq_wait_is_warning_while_normal_wait_stays_normal(self):
        env=self.env()
        for source,label,color in [('dq_retry','数据校验失败','黄色'),('MAIN','正常等待','绿色')]:
            with self.subTest(source=source):
                env['_scan_sleep_plan_set'](deadline_ts=2010,plan_source=source)
                status=env['_scan_sleep_plan_status'](nowv=2000)
                view=env['_scan_sleep_plan_human_view'](status,nowv=2000)
                self.assertEqual(status['remain_sec'],10)
                self.assertEqual(view['颜色提示'],color)
                self.assertIn(label,view['一句话提示'])


if __name__=='__main__':
    unittest.main()
