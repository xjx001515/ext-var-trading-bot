import unittest
from bot_core.execution_confirmation import *

def row(q, n=1000, side='LONG'):
    return {'side': side, 'qty_abs': q, 'notional_u': n}

class ConfirmationTests(unittest.TestCase):
    def verify(self, pre, post, kind='close', usd=120):
        return verify_position_change(kind=kind, side='LONG', usd=usd, pre=pre, post=post)[0]
    def test_price_move_not_fill(self):
        self.assertFalse(self.verify(row(10,1000), row(10,999.99)))
        self.assertFalse(self.verify(row(100,10000), row(100,10050), 'open', 60))
    def test_none_and_zero_unknown(self):
        self.assertFalse(self.verify(row(10), None))
        self.assertFalse(self.verify(row(10), {'side':'NONE','qty_abs':0,'notional_u':0}))
        self.assertFalse(self.verify({'side':'LONG','notional_u':1000}, {'side':'LONG','notional_u':800}))
    def test_explicit_empty(self):
        empty={'position_state':'CONFIRMED_EMPTY'}
        self.assertTrue(self.verify(row(10), empty, 'close_all'))
        self.assertTrue(self.verify(empty, row(1.2,120), 'open'))
    def test_quantity_and_partial(self):
        self.assertTrue(self.verify(row(10), row(8.8,880)))
        self.assertFalse(self.verify(row(10), row(9.9,990)))
    def test_side_mismatch(self):
        self.assertFalse(self.verify(row(10), row(8,800,'SHORT')))
    def test_final_fields_override_old_evidence(self):
        r=apply_confirmation_fields({'ok':True,'postcheck_ok':True,'execution_evidence':{'final_state':'submitted_unchecked'}})
        self.assertEqual(r['confirmation_state'],'confirmed_success')
        r=apply_confirmation_fields({'ok':True,'postcheck_ok':False,'execution_evidence':{'final_state':'confirmed_success'}})
        self.assertFalse(r['trade_effective'])
        self.assertTrue(r['pending_confirm'])
    def test_final_success_clears_all_pending_aliases(self):
        r=apply_confirmation_fields({'ok':True,'postcheck_ok':True,'pending':True,
            'pending_confirm':True,'pending_timed_out':True,'reason':'submitted_pending_confirm'})
        self.assertFalse(r['pending'])
        self.assertFalse(r['pending_confirm'])
        self.assertFalse(r['pending_timed_out'])
        self.assertEqual(r['reason'],'confirmed_success')
    def test_present_row_zero_quantity_is_not_known_zero(self):
        self.assertFalse(self.verify(row(0,1000),row(10,1100),'open',100))
    def test_pending_never_auto_retry_failure(self):
        r=apply_confirmation_fields({'ok':True,'reason':'m9_increase_done','pending_timed_out':True})
        self.assertEqual(r['reason'],'submitted_pending_confirm')
        self.assertTrue(r['pending_confirm'])
        self.assertFalse(r['confirmed_fail'])
    def test_requested_fields_not_receipts(self):
        for r in ({'qty':120}, {'order_id':'1'}, {'fill_price':100}, {'fill_confirmed':True}):
            self.assertFalse(has_fill_evidence(r))
        self.assertTrue(has_fill_evidence({'filled_qty':1}))
    def test_scoped_empty_not_inferred(self):
        pre={'ext_side':'LONG','ext_qty':10,'ext_notional_u':1000}
        self.assertFalse(verify_scoped_change(action='close',platform='EXT',side='LONG',usd=0,pre=pre,post={})[0])
        self.assertTrue(verify_scoped_change(action='close',platform='EXT',side='LONG',usd=0,pre=pre,post={'ext_position_state':'CONFIRMED_EMPTY'})[0])
    def test_contradictory_empty_and_stale_rejected(self):
        self.assertEqual(position_read({'position_state':'CONFIRMED_EMPTY','notional_u':5})[0], UNAVAILABLE)
        self.assertFalse(self.verify(row(10),dict(row(8),recovered_from_last_good=True)))
    def test_nonfinite_quantities_rejected(self):
        self.assertFalse(self.verify(row(float('nan')),row(5)))
        self.assertFalse(self.verify(row(10),row(float('inf'))))
    def test_flatten_requires_both_platforms_explicit(self):
        pre={'ext_side':'LONG','ext_qty':10,'ext_notional_u':1000,'var_position_state':'CONFIRMED_EMPTY'}
        post={'ext_position_state':'CONFIRMED_EMPTY'}
        self.assertFalse(verify_scoped_change(action='close_pair',platform='PAIR',side='',usd=0,pre=pre,post=post)[0])
        post['var_position_state']='CONFIRMED_EMPTY'
        self.assertTrue(verify_scoped_change(action='close_pair',platform='PAIR',side='',usd=0,pre=pre,post=post)[0])
    def test_wrong_held_position_side_rejected(self):
        self.assertFalse(verify_position_change(kind='close',side='SHORT',usd=120,pre=row(10),post=row(8.8,880))[0])
    def test_shared_truth_rejects_failed_and_stale_positive_rows(self):
        for extra in ({'reason':'read_fail'}, {'reason':'row_not_found'}, {'stale':True}):
            self.assertEqual(position_read(dict(row(10),**extra))[0],UNAVAILABLE)
    def test_explicit_empty_bulk_absent_survives(self):
        result=scoped_position({'ext_position_state':CONFIRMED_EMPTY,'ext_reason':'trusted_bulk_absent'},'EXT')
        self.assertEqual(position_read(result)[0],CONFIRMED_EMPTY)
    def test_size_quantity_and_bool_rejection(self):
        self.assertEqual(quantity({'size':2}),2)
        self.assertIsNone(quantity({'qty':True}))
    def test_pending_survives_time_and_raw_false(self):
        self.assertTrue(pending_blocks({'state':'PENDING_CONFIRM','ts':1,'ok':False,'ttl':0}))
        self.assertTrue(pending_blocks({'state':'PENDING_CONFIRM','retries':0}))
        self.assertFalse(pending_blocks({'state':'CONFIRMED_REJECTED'}))
        self.assertFalse(pending_blocks({'state':'CONFIRMED_CANCELLED'}))
    def test_old_ledger_without_quantity_is_not_settled(self):
        self.assertFalse(verify_pending_settlement({'pre_notional_u':1000,'side':'LONG'},row(8,800))[0])
    def test_missing_requested_amount_does_not_confirm_partial(self):
        self.assertFalse(self.verify(row(10),row(8,800),usd=0))
    def test_ledger_market_move_never_settles(self):
        pending={'pre_position':row(10),'side':'LONG','kind':'close','intent_usd':120}
        self.assertFalse(verify_pending_settlement(pending,row(10,700))[0])
        self.assertTrue(verify_pending_settlement(pending,row(8.8,880))[0])

class AdapterTests(unittest.TestCase):
    """Extract only the pure read adapter; never import or execute the BOT."""
    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        import runpy
        root=Path(__file__).resolve().parents[1]
        patch=runpy.run_path(str(root/'repair_backups/safety_refactor_20261022/execution_edits.py'))['apply']
        text=patch((root/'BOT5-2-414 .py').read_text(encoding='utf-8-sig'))
        review=runpy.run_path(str(root/'repair_backups/safety_refactor_20261022/execution_review_edits.py'))['apply']
        text=review(text)
        begin=text.index('def _exec_read_position_truth(')
        end=text.index('\ndef ',begin+4)
        cls.adapter=text[begin:end]
        begin=text.index('def _exe_open_position_safe(')
        end=text.index('\ndef ',begin+4)
        cls.safe_open=text[begin:end]
        cls.transformed=text
    def isolated_function(self,name):
        begin=self.transformed.index('def '+name+'(')
        end=self.transformed.index('\ndef ',begin+4)
        return self.transformed[begin:end]
    def run_read(self, row_value, empty=False, fail=False):
        def read(*args):
            if fail:
                raise RuntimeError('disconnected')
            return row_value
        def normalize(value):
            state, r=position_read(value)
            return {'state':state,'row':r}
        ns={'read_coin_row_basic':read, '_safety_normalize_position_read':normalize,
            '_safety_resolve_submission':lambda *args:False,
            '_exec_positions_confirmed_empty':lambda *args:empty}
        exec(compile(self.adapter,'isolated_adapter','exec'),ns)
        return ns['_exec_read_position_truth'](object(),{},'Q')
    def test_missing_without_visible_empty_is_unavailable(self):
        self.assertEqual(self.run_read(None)['position_state'],UNAVAILABLE)
    def test_visible_explicit_empty_is_distinct(self):
        self.assertEqual(self.run_read(None,empty=True)['position_state'],CONFIRMED_EMPTY)
    def test_exception_never_becomes_empty(self):
        self.assertEqual(self.run_read(None,empty=True,fail=True)['position_state'],UNAVAILABLE)
    def test_found_quantity_is_preserved(self):
        result=self.run_read(row(2,200))
        self.assertEqual(result['position_state'],FOUND)
        self.assertEqual(result['qty_abs'],2)
    def open_wrapper(self, exe, *, pending=lambda:False, dry_run=False):
        from types import SimpleNamespace
        import os
        ns={'Any':object, 'os':os, '_safety_pending_scope':lambda *a:pending(),
            '_submit_attempt_begin':lambda order:None, '_submit_attempt_set':lambda *a,**k:None,
            '_set_last_exec_err':lambda **k:None, '_pw_guard':lambda **k:True,
            '_runtime_recovering_gate':lambda *a,**k:(True,'',{}),
            '_a1_norm_side':lambda x:'LONG', '_failed_close_pending_should_block_new_open':lambda *a:(False,{})}
        exec(compile(self.safe_open,'isolated_safe_open','exec'),ns)
        return ns['_exe_open_position_safe'](exe,SimpleNamespace(coin='Q',side='BUY',platform='EXT'),plat='EXT',dry_run=dry_run)
    def test_internal_typeerror_never_replays_order(self):
        class Exe:
            calls=0
            def open_position(self,order,dry_run=False):
                self.calls+=1
                raise TypeError('raised after possible submit')
        exe=Exe()
        self.assertFalse(self.open_wrapper(exe))
        self.assertEqual(exe.calls,1)
    def test_pending_blocks_before_next_ui_call(self):
        class Exe:
            calls=0
            def open_position(self,order,dry_run=False):
                self.calls+=1
                return True
        exe=Exe()
        self.assertFalse(self.open_wrapper(exe,pending=lambda:True))
        self.assertEqual(exe.calls,0)
    def test_pending_after_submit_stops_next_group_step(self):
        class Exe:
            calls=0
            def open_position(self,order,dry_run=False):
                self.calls+=1
                return True
        exe=Exe()
        self.assertFalse(self.open_wrapper(exe,pending=lambda:exe.calls>0))
        self.assertEqual(exe.calls,1)
    def test_dry_run_cannot_fallback_to_live_signature(self):
        class Exe:
            calls=0
            def open_position(self,order):
                self.calls+=1
                return True
        exe=Exe()
        self.assertFalse(self.open_wrapper(exe,dry_run=True))
        self.assertEqual(exe.calls,0)
    def journal_namespace(self,directory,writer=None):
        import json,os,time,threading
        from pathlib import Path
        def safe_write(path,obj,**kwargs):
            Path(path).write_text(json.dumps(obj),encoding='utf-8')
            return True
        ns={'OUT_DIR':directory,'os':os,'time':time,'_SYNC_PW_WORKER_LOCK':threading.RLock(),
            'safe_write_json':writer or safe_write,'_safety_pending_blocks':pending_blocks,
            '_safety_verify_pending_settlement':verify_pending_settlement}
        for name in ('_safety_submission_records','_safety_submission_save','_safety_pending_scope',
                     '_safety_reserve_submission','_safety_resolve_submission','_safety_confirm_flat_scope'):
            exec(compile(self.isolated_function(name),'isolated_journal','exec'),ns)
        return ns
    def test_journal_load_is_lazy_and_persists_pending(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            ns=self.journal_namespace(directory)
            self.assertEqual(list(Path(directory).iterdir()),[])
            ns['_safety_reserve_submission']('EXT','Q','LONG','close',row(10),120)
            again=self.journal_namespace(directory)
            self.assertTrue(again['_safety_pending_scope']('EXT','Q'))
            self.assertFalse(again['_safety_resolve_submission']('EXT','Q',row(10,700)))
            self.assertTrue(again['_safety_resolve_submission']('EXT','Q',row(8.8,880)))
            self.assertFalse(again['_safety_pending_scope']('EXT','Q'))
    def test_double_reserve_is_idempotent_not_a_self_deadlock(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            ns=self.journal_namespace(directory)
            reserve=ns['_safety_reserve_submission']
            reserve('EXT','Q','LONG','close',row(10),120)
            before=dict(ns['_safety_pending_scope']('EXT','Q'))
            with self.assertRaises(RuntimeError):
                reserve('EXT','Q','LONG','close',row(8),999)
            reserve('EXT','Q','LONG','close',row(8),999,allow_existing=True)
            self.assertEqual(ns['_safety_pending_scope']('EXT','Q'),before)
    def test_concurrent_claims_allow_only_one_submit(self):
        import tempfile,threading
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as directory:
            ns=self.journal_namespace(directory)
            # Both requests have already passed the preliminary scope check.
            barrier=threading.Barrier(2)
            submits=[]
            def request():
                self.assertFalse(ns['_safety_pending_scope']('EXT','Q'))
                barrier.wait()
                try:
                    ns['_safety_reserve_submission']('EXT','Q','LONG','open',{'position_state':CONFIRMED_EMPTY},120)
                except RuntimeError:
                    return
                submits.append('one simulated submit')
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _:request(),range(2)))
            self.assertEqual(len(submits),1)
    def test_all_and_single_coin_claims_are_mutually_exclusive(self):
        import tempfile
        for first,second in (('ALL','Q'),('Q','ALL')):
            with tempfile.TemporaryDirectory() as directory:
                ns=self.journal_namespace(directory)
                reserve=ns['_safety_reserve_submission']
                reserve('EXT',first,'LONG','close_all',row(10),120)
                with self.assertRaises(RuntimeError):
                    reserve('EXT',second,'LONG','close_all',row(10),120)
    def test_corrupt_journal_blocks_without_overwriting(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state'/'execution_unconfirmed.json'
            path.parent.mkdir()
            path.write_text('{broken',encoding='utf-8')
            ns=self.journal_namespace(directory)
            self.assertTrue(ns['_safety_pending_scope']('EXT','Q'))
            with self.assertRaises(RuntimeError):
                ns['_safety_reserve_submission']('EXT','Q','LONG','close',row(10),120)
            self.assertEqual(path.read_text(encoding='utf-8'),'{broken')
    def test_failed_or_silent_journal_write_prevents_submit(self):
        import tempfile
        for writer in (lambda *a,**k:False,lambda *a,**k:None):
            with tempfile.TemporaryDirectory() as directory:
                ns=self.journal_namespace(directory,writer)
                with self.assertRaises(RuntimeError):
                    ns['_safety_reserve_submission']('EXT','Q','LONG','close',row(10),120)
                self.assertTrue(ns['_safety_pending_scope']('EXT','Q'))
    def test_no_second_click_on_timeout(self):
        class Locator:
            calls=0
            def wait_for(self,**kwargs): pass
            def click(self,**kwargs):
                self.calls+=1
                raise TimeoutError('may have reached exchange')
        ns={'_pw_call':lambda description,fn,**kwargs:fn(**kwargs)}
        exec(compile(self.isolated_function('_safety_click_submit_once'),'isolated_click','exec'),ns)
        locator=Locator()
        self.assertFalse(ns['_safety_click_submit_once'](locator,100,'test'))
        self.assertEqual(locator.calls,1)
    def test_legacy_unscoped_dialog_cannot_submit(self):
        class BombPage:
            def locator(self,*args):
                raise AssertionError('must not interact with a generic confirmation dialog')
        ns={}
        exec(compile(self.isolated_function('_exec_try_confirm_dialog'),'isolated_disabled_dialog','exec'),ns)
        self.assertFalse(ns['_exec_try_confirm_dialog'](BombPage(),100))
    def test_postcheck_adapter_preserves_missing_quantity(self):
        from bot_core.position_truth import normalize_position_read
        truth={'ext':{'side':'LONG','notional_u':1000,'qty':None},
               'var':{'position_state':CONFIRMED_EMPTY,'side':'NONE','qty':0},
               'ext_side':'LONG','ext_notional_u':1000,'ext_has':True}
        ns={'Dict':dict,'Any':object,'_tpm10_coin_truth':lambda *a:truth,
            '_safety_normalize_position_read':normalize_position_read,
            'safe_float':lambda value,default=0:float(value) if value is not None else default}
        exec(compile(self.isolated_function('_postcheck_extract_coin_state'),'isolated_postcheck_extract','exec'),ns)
        result=ns['_postcheck_extract_coin_state']({},'Q')
        self.assertIsNone(result['ext_qty'])
        self.assertEqual(result['ext_position_state'],FOUND)
        self.assertEqual(result['var_position_state'],CONFIRMED_EMPTY)

if __name__ == '__main__':
    unittest.main()
