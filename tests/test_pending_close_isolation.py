"""Real application gates with fake buttons, temporary journals and AST isolation."""
import ast
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from ast_support import ROOT, load_definitions, parsed
from bot_core import submission_scope as scope
from bot_core.execution_confirmation import verify_position_change
from test_submission_margin import Button, namespace, order


def row(qty=17, side='SHORT'):
    return {'position_state':'FOUND', 'side':side, 'qty_abs':qty,
            'notional_u':qty*10, 'mark_px':10}


def record(kind='open', side='SHORT', coin='TIA', platform='EXT', **extra):
    return dict(state='PENDING_CONFIRM', platform=platform, coin=coin, kind=kind,
                side=side, pre_position={'position_state':'UNAVAILABLE'}, intent_usd=170,
                **extra)


class ScopeRulesTests(unittest.TestCase):
    def test_open_pending_does_not_block_reduce_single_close_or_all(self):
        for platform in ('EXT','VAR'):
            for kind in ('open','increase','add_leg'):
                rec = record(kind=kind, platform=platform)
                for action, coin in (('close','TIA'),('close_all','TIA'),('decrease','TIA'),('close_all','ALL')):
                    with self.subTest(platform=platform, kind=kind, action=action, coin=coin):
                        self.assertFalse(scope.blocks(rec,platform,coin,action,'SHORT'))

    def test_same_held_direction_duplicate_close_blocks(self):
        for kind in ('close','close_all','decrease'):
            self.assertTrue(scope.blocks(record(kind=kind),'EXT','TIA','close','SHORT'))

    def test_opposite_direction_different_coin_and_platform_do_not_block_close(self):
        rec=record(kind='close')
        for platform,coin,side in (('EXT','TIA','LONG'),('EXT','Q','SHORT'),('VAR','TIA','SHORT')):
            self.assertFalse(scope.blocks(rec,platform,coin,'close',side))

    def test_all_close_overlaps_single_close_both_ways(self):
        self.assertTrue(scope.blocks(record(kind='close'),'EXT','ALL','close_all'))
        self.assertTrue(scope.blocks(record(kind='close_all',coin='ALL',side=''),'EXT','TIA','close','LONG'))

    def test_unknown_operation_or_direction_stays_conservative(self):
        for rec in (record(kind=''),record(kind='close',side='')):
            self.assertTrue(scope.blocks(rec,'EXT','TIA','close','SHORT'))

    def test_new_risk_still_blocked_by_any_unsettled_operation(self):
        for kind in ('open','close','close_all',''):
            self.assertTrue(scope.blocks(record(kind=kind),'EXT','TIA','open','LONG'))

    def test_reduce_order_side_is_inverted_to_held_side(self):
        self.assertEqual(scope.order_scope({'reduce_only':True,'side':'buy'}),('close','SHORT'))
        self.assertEqual(scope.order_scope(SimpleNamespace(reduce_only=True,side='sell')),('close','LONG'))
        self.assertEqual(scope.order_scope({'side':'SHORT'}),('open','SHORT'))

    def test_resolved_records_do_not_block(self):
        rec=record(kind='close'); rec['state']='CONFIRMED_SUCCESS'
        self.assertFalse(scope.blocks(rec,'EXT','TIA','close','SHORT'))


class JournalAndSubmitTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.env,self.logs=namespace(self.tmp.name)
        self.env.update(_exec_row_side_notional=lambda r:((r or {}).get('side',''),(r or {}).get('notional_u',0)),
            _exec_verify_submit_by_row=lambda **k:verify_position_change(kind=k['kind'],side=k['side_u'],usd=k['usd'],pre=k['pre_row'],post=k['post_row']),
            _pin=Mock())
        load_definitions({'_safety_resolve_submission','_safety_confirm_flat_scope','_exec_clear_pending',
            '_exec_mark_pending_info','_exec_reconcile_pending_by_row','_exec_pending_active',
            '_exec_mark_pending','_pending_side_coins'},self.env)

    def reserve_open(self, platform='EXT', coin='TIA'):
        self.env['_safety_reserve_submission'](platform,coin,'SHORT','open',None,170,
            reason='submit_click_attempt_pending',submission_attempt_id='old-opening')

    def records(self):
        return self.env['_safety_submission_records']()

    def test_open_and_close_are_independent_and_survive_restart(self):
        self.reserve_open(); old=copy.deepcopy(self.records()['EXT|TIA'])
        self.env['_safety_reserve_submission']('EXT','TIA','SHORT','close',row(),40)
        again,_=namespace(self.tmp.name)
        records=again['_safety_submission_records']()
        self.assertEqual(records['EXT|TIA'],old)
        self.assertIn('EXT|TIA|CLOSE|SHORT',records)
        self.assertTrue(again['_safety_pending_scope']('EXT','TIA','close','SHORT'))
        self.assertFalse(again['_safety_pending_scope']('EXT','TIA','close','LONG'))
        self.assertEqual(json.loads((Path(self.tmp.name)/'state/execution_unconfirmed.json').read_text())['version'],2)

    def test_legacy_close_key_is_reindexed_without_settling_or_losing_evidence(self):
        path=Path(self.tmp.name)/'state/execution_unconfirmed.json'; path.parent.mkdir()
        old=record(kind='close',submission_attempt_id='legacy-close')
        path.write_text(json.dumps({'version':1,'records':{'EXT|TIA':old}}),encoding='utf-8')
        before=path.read_bytes()
        self.assertEqual(self.records(),{'EXT|TIA|CLOSE|SHORT':old})
        self.assertEqual(path.read_bytes(),before,'Loading alone must not mutate the journal')

    def test_duplicate_legacy_and_typed_keys_fail_closed(self):
        rec=record(kind='close')
        with self.assertRaises(ValueError):
            scope.index_records({'EXT|TIA':rec,'EXT|TIA|CLOSE|SHORT':rec})

    def test_close_settlement_does_not_settle_or_replace_open(self):
        self.reserve_open(); old=copy.deepcopy(self.records()['EXT|TIA'])
        self.env['_safety_reserve_submission']('EXT','TIA','SHORT','close',row(),40)
        self.assertTrue(self.env['_safety_resolve_submission']('EXT','TIA',row(13),kind='close'))
        self.assertEqual(self.records()['EXT|TIA'],old)
        self.assertEqual(self.records()['EXT|TIA|CLOSE|SHORT']['state'],'CONFIRMED_SUCCESS')
        self.assertFalse(self.env['_safety_pending_scope']('EXT','TIA','close','SHORT'))
        self.assertTrue(self.env['_safety_pending_scope']('EXT','TIA','open','SHORT'))

    def test_platform_flat_resolves_only_close_records(self):
        self.reserve_open(); old=copy.deepcopy(self.records()['EXT|TIA'])
        self.env['_safety_reserve_submission']('EXT','ALL','','close_all',None,0)
        self.env['_safety_confirm_flat_scope']('EXT')
        self.assertEqual(self.records()['EXT|TIA'],old)
        self.assertEqual(self.records()['EXT|ALL|CLOSE|ALL']['state'],'CONFIRMED_SUCCESS')

    def test_real_close_click_ignores_open_pending_then_blocks_duplicate(self):
        for platform in ('EXT','VAR'):
            for coin in ('TIA','ALL'):
                with self.subTest(platform=platform,coin=coin):
                    with tempfile.TemporaryDirectory() as directory:
                        env,_=namespace(directory)
                        env['_safety_reserve_submission'](platform,'TIA','SHORT','open',None,170)
                        b=Button()
                        click=lambda:env['_submit_close_click'](b,platform,coin,'SHORT' if coin!='ALL' else '',row(),170,100,'fake close')
                        self.assertTrue(click()); self.assertEqual(b.clicks,1)
                        self.assertFalse(click()); self.assertEqual(b.clicks,1)
                        self.assertEqual(len(env['_safety_submission_records']()),2)

    def test_reduce_dispatch_and_post_gate_ignore_unrelated_open_record(self):
        self.reserve_open(); button=Button()
        o=order(reduce_only=True); o.side='buy'; o.usd=40
        def execute(order,dry_run=False):
            self.assertTrue(self.env['_submit_order_click'](button,order,'EXT','SHORT',row(),40,100,'reduce'))
            self.env['_safety_resolve_submission']('EXT','TIA',row(13),kind='close')
            return True
        self.assertTrue(self.env['_exe_open_position_safe'](SimpleNamespace(open_position=execute),o,plat='EXT'))
        self.assertEqual(button.clicks,1)
        self.assertEqual(self.records()['EXT|TIA']['state'],'PENDING_CONFIRM')

    def test_unconfirmed_reduce_blocks_another_reduce_before_executor(self):
        self.reserve_open(); button=Button(); o=order(reduce_only=True); o.side='buy'
        self.assertTrue(self.env['_submit_order_click'](button,o,'EXT','SHORT',row(),40,100,'reduce'))
        method=Mock()
        self.assertFalse(self.env['_exe_open_position_safe'](SimpleNamespace(open_position=method),o,plat='EXT'))
        method.assert_not_called()

    def test_actual_platform_entry_gates_admit_reduce_with_open_pending(self):
        self.reserve_open('EXT'); self.reserve_open('VAR')
        tree=parsed(str(ROOT/'BOT5-2-414 .py'))
        for clsname,platform in (('_ExtExecutor','EXT'),('_VarExecutor','VAR')):
            cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==clsname)
            fn=copy.deepcopy(next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='open_position'))
            cutoff=next(i for i,n in enumerate(fn.body) if isinstance(n,ast.If) and '_safety_pending_scope' in ast.unparse(n.test))
            fn.body=fn.body[:cutoff+1]+[ast.Return(value=ast.Constant('passed_gate'))]
            env=dict(self.env,_exec_normalize_coin=str.upper,_VarExecutor=type('FakeVar',(),{}))
            code=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),fn],type_ignores=[])
            exec(compile(ast.fix_missing_locations(code),'<actual platform entry>','exec'),env)
            selfobj=env['_VarExecutor']() if platform=='VAR' else object()
            o=order(platform=platform,reduce_only=True); o.side='buy'
            self.assertEqual(env['open_position'](selfobj,o),'passed_gate')
            o.reduce_only=False
            self.assertFalse(env['open_position'](selfobj,o))

    def test_actual_single_and_global_close_gates_admit_open_pending(self):
        self.reserve_open('EXT'); self.reserve_open('VAR')
        tree=parsed(str(ROOT/'BOT5-2-414 .py'))
        for clsname in ('_ExtExecutor','_VarExecutor'):
            cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==clsname)
            for name in ('close_all','close_all_global'):
                fn=copy.deepcopy(next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name==name))
                cutoff=next(i for i,n in enumerate(fn.body) if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call)
                            and isinstance(n.value.func,ast.Name) and n.value.func.id=='_exec_ensure_active_page')
                fn.body=fn.body[:cutoff]+[ast.Return(value=ast.Constant('passed_gate'))]
                env=dict(self.env,_exec_normalize_coin=str.upper,_exec_log=Mock(),_pin_exec_reduce_phase=Mock(),
                         _pin=Mock(),_log_exc=Mock(),EXT={},VAR={})
                exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),'<actual close gate>','exec'),env)
                args=('TIA',) if name=='close_all' else ()
                self.assertEqual(env[name](SimpleNamespace(page=object()),*args),'passed_gate')

    def test_open_and_close_memory_do_not_overwrite_or_clear_each_other(self):
        mark=self.env['_exec_mark_pending_info']
        mark('EXT','TIA','SHORT',kind='open',pre_row=None,usd=170)
        mark('EXT','TIA','SHORT',kind='close',pre_row=row(),usd=40)
        self.assertEqual(len(self.env['_EXEC_PENDING']),2)
        self.env['_safety_resolve_submission']('EXT','TIA',row(13),kind='close')
        self.env['_exec_clear_pending']('EXT','TIA','SHORT',kind='close')
        self.assertIn(('EXT','TIA','SHORT'),self.env['_EXEC_PENDING'])
        self.assertNotIn(('EXT','TIA','SHORT','close'),self.env['_EXEC_PENDING'])
        self.assertFalse(self.env['_exec_pending_active']('EXT','TIA','SHORT',kind='close'))
        self.assertTrue(self.env['_exec_pending_active']('EXT','TIA','SHORT',kind='open'))
        self.assertEqual(self.env['_pending_side_coins']('EXT','SHORT'),['TIA'])

    def test_single_close_rechecks_actual_held_side_before_blocking(self):
        tree=parsed(str(ROOT/'BOT5-2-414 .py'))
        for clsname,platform in (('_ExtExecutor','EXT'),('_VarExecutor','VAR')):
            cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==clsname)
            fn=copy.deepcopy(next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='close_all'))
            cutoff=next(i for i,n in enumerate(fn.body) if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call)
                        and isinstance(n.value.func,ast.Name) and n.value.func.id=='_exec_ensure_active_page')
            fn.body=fn.body[:cutoff]+[ast.Return(value=ast.Constant('passed_gate'))]
            self.env.update(_exec_normalize_coin=str.upper,_exec_log=Mock(),_pin_exec_reduce_phase=Mock(),
                            _pin=Mock(),_log_exc=Mock(),EXT={},VAR={})
            exec(compile(ast.fix_missing_locations(ast.Module(body=[fn],type_ignores=[])),
                         '<actual single close direction gate>','exec'),self.env)
            self.reserve_open(platform)
            self.env['_safety_reserve_submission'](platform,'TIA','SHORT','close',row(),40)
            for observed,expected in ((row(side='SHORT'),False),(row(side='LONG'),'passed_gate'),
                                      ({'position_state':'UNAVAILABLE'},False)):
                with self.subTest(platform=platform,observed=observed):
                    reader=Mock(return_value=observed); self.env['_exec_read_position_truth']=reader
                    self.assertEqual(self.env['close_all'](SimpleNamespace(page=object()),'TIA'),expected)
                    reader.assert_called_once()

    def test_opposite_side_closes_have_separate_storage(self):
        for s in ('LONG','SHORT'):
            self.env['_safety_reserve_submission']('EXT','TIA',s,'close',row(side=s),40)
        self.assertEqual(set(self.records()),{'EXT|TIA|CLOSE|SHORT','EXT|TIA|CLOSE|LONG'})

    def test_all_and_single_close_claims_conflict_even_with_allow_existing(self):
        self.reserve_open()
        self.env['_safety_reserve_submission']('EXT','ALL','','close_all',None,0)
        with self.assertRaises(RuntimeError):
            self.env['_safety_reserve_submission']('EXT','TIA','SHORT','close',row(),40,allow_existing=True)

    def test_deferred_close_attaches_to_close_record_only(self):
        from test_postcheck_lifecycle import namespace as post_namespace, request
        env,clock=post_namespace(self.tmp.name)
        env['_safety_reserve_submission']('EXT','TIA','SHORT','open',None,170)
        before=copy.deepcopy(env['_safety_submission_records']()['EXT|TIA'])
        pre={'ext_side':'SHORT','ext_qty':17,'ext_notional_u':170,'ext_position_state':'FOUND'}
        req=request(module='M9',action_type='decrease',side='BUY',amount_usd=40)
        env['_safety_reserve_submission']('EXT','TIA','SHORT','close',row(),40,
            reason='submit_click_dispatched',submission_attempt_id='close-click',module='M9')
        self.assertTrue(env['_postcheck_defer_to_scan'](req,payload={'pre_state':pre,'created_ts':clock.time()}))
        recs=env['_safety_submission_records']()
        self.assertEqual(recs['EXT|TIA'],before)
        self.assertEqual(recs['EXT|TIA|CLOSE|SHORT']['deferred_postcheck']['module'],'M9')


class FullCloseEvidenceTests(unittest.TestCase):
    def test_report_needs_real_flat_evidence_and_names_failure_platform_and_coin(self):
        logs=[]
        env={'EXT':{'name':'EXT'},'VAR':{'name':'VAR'},'now_ts':lambda:0,
             'safe_log':lambda f:logs.append(f()),
             '_exec_positions_confirmed_empty':lambda page,cfg:page=='flat',
             '_exec_read_portfolio_snapshot':lambda *a,**k:{'available':True,'positions':{'TIA':row()}},
             '_safety_submission_records':lambda:{'EXT|TIA':record()}}
        load_definitions({'_exec_global_close_report'},env)
        report=env['_exec_global_close_report'](SimpleNamespace(page='position'),'EXT',True,coins=['TIA'])
        self.assertFalse(report['ok']); self.assertEqual(report['remaining_coins'],['TIA'])
        self.assertEqual(report['blocked_close_records'],[])
        self.assertIn('platform=EXT',logs[-1]); self.assertIn("remaining_coins=['TIA']",logs[-1])
        self.assertTrue(env['_exec_global_close_report'](SimpleNamespace(page='flat'),'EXT',True)['ok'])
        self.assertFalse(env['_exec_global_close_report'](SimpleNamespace(page='flat'),'EXT',False)['ok'])

    def test_unknown_page_and_blocking_close_keep_explicit_failure_evidence(self):
        logs=[]
        env={'EXT':{},'VAR':{},'now_ts':lambda:0,'safe_log':lambda f:logs.append(f()),
             '_exec_positions_confirmed_empty':Mock(side_effect=AssertionError('broken page must not be reused')),
             '_safety_submission_records':lambda:{'VAR|TIA|CLOSE|SHORT':record(kind='close',platform='VAR')}}
        load_definitions({'_exec_global_close_report'},env)
        r=env['_exec_global_close_report'](SimpleNamespace(page='broken'),'VAR',False,coins=['TIA'],error='page timeout')
        self.assertFalse(r['ok']); self.assertEqual(r['unverified_coins'],['TIA'])
        self.assertEqual(r['blocked_close_records'][0]['coin'],'TIA')
        self.assertIn('page timeout',logs[-1]); env['_exec_positions_confirmed_empty'].assert_not_called()


if __name__=='__main__':
    unittest.main()
