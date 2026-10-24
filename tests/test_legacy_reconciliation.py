"""Offline reconciliation proposals only; every file fixture lives in a temp dir."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from bot_core.legacy_reconciliation import reconcile, replay, fingerprint, manual_resolution
from maintenance.replay_unconfirmed import run


def record(**kw):
    r={'state':'PENDING_CONFIRM','platform':'VAR','coin':'TEST','side':'LONG','kind':'open',
       'module':None,'intent_usd':170,'pre_position':{'position_state':'UNAVAILABLE'},'ts':1000,
       'reason':'submit_click_attempt_pending','deferred_postcheck':{'module':'M9'}}
    r.update(kw);return r


def row(value=253,side='LONG'):
    return {'position_state':'FOUND','side':side,'qty_abs':value/10,'notional_u':value,'entry_px':10}


def snapshot(ts=1300,position=None,**kw):
    s={'ts':ts,'data_quality':'ok','raw_positions':{'VAR':{'TEST':position if position is not None else row()}}}
    s.update(kw);return s


class ReconciliationTests(unittest.TestCase):
    def test_legacy_observer_m9_is_not_treated_as_submitting_module(self):
        r=record();before=copy.deepcopy(r)
        after,decision=reconcile(r,snapshot(),now=1300,scan_id='1')
        self.assertEqual(after['state'],'CONFIRMED_SUCCESS')
        self.assertEqual(decision['reason'],'legacy_same_side_at_least_half_intent')
        self.assertEqual(r,before)

    def test_later_adds_do_not_need_to_match_original_amount(self):
        for value in (85,253,1000):
            after,_=reconcile(record(),snapshot(position=row(value)),now=1300,scan_id='1')
            self.assertEqual(after['state'],'CONFIRMED_SUCCESS')

    def test_less_than_half_or_wrong_direction_stays_pending(self):
        for pos,reason in ((row(84.99),'position_below_half_intent'),(row(253,'SHORT'),'position_side_mismatch')):
            after,d=reconcile(record(),snapshot(position=pos),now=1300,scan_id='1')
            self.assertEqual(after['state'],'PENDING_CONFIRM');self.assertEqual(d['reason'],reason)

    def test_known_pre_quantity_cannot_use_half_amount_shortcut(self):
        r=record(pre_position=row(250))
        after,d=reconcile(r,snapshot(position=row(253)),now=1300,scan_id='1')
        self.assertEqual(after['state'],'PENDING_CONFIRM')
        self.assertEqual(d['reason'],'partial_quantity_change_pending')

    def test_real_m9_and_m10_step3_keep_their_separate_policy(self):
        for changes in ({'module':'M9'},{'module':'M10'},{'m10_step3_confirmation':{'usd':120}}):
            after,d=reconcile(record(**changes),snapshot(),now=1300,scan_id='1')
            self.assertEqual(after['state'],'PENDING_CONFIRM')
            self.assertEqual(d['reason'],'separate_m9_m10_confirmation_policy')

    def test_stale_partial_or_pre_submit_snapshot_cannot_settle(self):
        for s in (snapshot(ts=999),snapshot(data_quality='fail'),snapshot(stale=True),
                  snapshot(scan_scope='partial'),snapshot(meta={'cache_hit':True})):
            after,_=reconcile(record(),s,now=1300,scan_id='1')
            self.assertEqual(after['state'],'PENDING_CONFIRM')
        after,_=reconcile(record(),snapshot(),now=1500,scan_id='1')
        self.assertEqual(after['state'],'PENDING_CONFIRM')

    def test_missing_from_filtered_snapshot_is_not_empty(self):
        s=snapshot();s['raw_positions']['VAR']={}
        after,d=reconcile(record(),s,now=1300,scan_id='1')
        self.assertEqual(after['state'],'PENDING_CONFIRM')
        self.assertEqual(d['reason'],'position_unavailable_or_not_covered')

    def test_exact_symbol_q_does_not_match_rfq(self):
        s=snapshot();s['raw_positions']['VAR']={'RFQ':row(500)}
        _,d=reconcile(record(coin='Q'),s,now=1300,scan_id='1')
        self.assertEqual(d['reason'],'position_unavailable_or_not_covered')

    def test_no_quantity_is_not_positive_position_evidence(self):
        pos=row();pos['qty_abs']=None
        after,d=reconcile(record(),snapshot(position=pos),now=1300,scan_id='1')
        self.assertEqual(after['state'],'PENDING_CONFIRM');self.assertEqual(d['reason'],'position_quantity_unavailable')

    def empty(self,ts):return snapshot(ts,{'position_state':'CONFIRMED_EMPTY','reason':'trusted_bulk_absent'})

    def test_three_independent_empty_scans_infer_not_filled(self):
        r=record()
        for i,ts in enumerate((1300,1330,1360)):
            r,d=reconcile(r,self.empty(ts),now=ts,scan_id=str(i))
        self.assertEqual(r['state'],'CONFIRMED_NOT_FILLED')
        self.assertFalse(r['evidence']['exchange_rejection_confirmed'])

    def test_duplicate_and_close_together_scans_do_not_count_three(self):
        r,_=reconcile(record(),self.empty(1300),now=1300,scan_id='one')
        for scan_id,ts in (('one',1330),('another',1300),('too-soon',1310)):
            updated,_=reconcile(r,self.empty(ts),now=ts,scan_id=scan_id)
            self.assertEqual(updated['empty_scan_evidence']['count'],1)

    def test_bad_snapshot_breaks_empty_consensus(self):
        r=record()
        for i,ts in enumerate((1300,1330)):
            r,_=reconcile(r,self.empty(ts),now=ts,scan_id=str(i))
        s=self.empty(1360);s['data_quality']='fail'
        r,_=reconcile(r,s,now=1360,scan_id='bad')
        r,_=reconcile(r,self.empty(1390),now=1390,scan_id='next')
        self.assertEqual(r['state'],'PENDING_CONFIRM');self.assertEqual(r['empty_scan_evidence']['count'],1)

    def test_previously_observed_position_is_flat_not_never_filled(self):
        r=record(empty_scan_evidence={'position_seen':True})
        for i,ts in enumerate((1300,1330,1360)):
            r,_=reconcile(r,self.empty(ts),now=ts,scan_id=str(i))
        self.assertEqual(r['state'],'RESOLVED_FLAT')

    def test_confirmed_later_platform_close_plus_fresh_empty_resolves_flat(self):
        r=record(empty_scan_evidence={'position_seen':True})
        after,d=reconcile(r,self.empty(1300),now=1300,scan_id='one',platform_closes={'VAR':1200})
        self.assertEqual(after['state'],'RESOLVED_FLAT')
        self.assertEqual(d['reason'],'later_confirmed_platform_close_and_fresh_empty')

    def test_working_order_cannot_be_cleared_by_empty_or_prior_close(self):
        r=record(open_order_active=True,empty_scan_evidence={'position_seen':True,'count':2})
        after,d=reconcile(r,self.empty(1300),now=1300,scan_id='new',platform_closes={'VAR':1200})
        self.assertEqual(after['state'],'PENDING_CONFIRM');self.assertEqual(d['reason'],'working_order_not_resolved')

    def test_already_terminal_is_unchanged_and_does_not_create_fills(self):
        r=record(state='CONFIRMED_SUCCESS');after,d=reconcile(r,snapshot(),now=1300,scan_id='one')
        self.assertEqual(after,r);self.assertFalse(d['creates_fill_or_profit_or_grid_layer'])

    def test_manual_plan_checks_exact_record_fingerprint(self):
        r=record();before=fingerprint(r)
        changed=record(ts=1200,submission_attempt_id='new-order')
        with self.assertRaisesRegex(ValueError,'record_changed'):
            manual_resolution(changed,expected_fingerprint=before,state='RESOLVED_FLAT',reason='reviewed',now=1300)
        updated,audit=manual_resolution(r,expected_fingerprint=before,state='RESOLVED_FLAT',reason='reviewed',now=1300)
        self.assertEqual(updated['state'],'RESOLVED_FLAT');self.assertEqual(r['state'],'PENDING_CONFIRM')
        self.assertEqual(audit['before_fingerprint'],before)


class ReplayFilesTests(unittest.TestCase):
    def test_replay_writes_only_separate_proposals_and_preserves_input_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);j=base/'journal.json';s=base/'positions.json'
            j.write_text(json.dumps({'version':2,'records':{'VAR|TEST':record()}}),encoding='utf-8')
            s.write_text(json.dumps(snapshot()),encoding='utf-8');before=(j.read_bytes(),s.read_bytes())
            result,folder=run(j,s,base/'replay')
            self.assertEqual(result['changed_records'],1);self.assertFalse(result['runtime_auto_reconciliation_enabled'])
            self.assertEqual((j.read_bytes(),s.read_bytes()),before)
            self.assertTrue((folder/'replay.txt').exists())
            with self.assertRaises(FileExistsError):run(j,s,folder)

    def test_replay_cannot_write_beside_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);j=base/'j.json';s=base/'p.json'
            j.write_text(json.dumps({'records':{}}));s.write_text(json.dumps(snapshot()))
            with self.assertRaisesRegex(ValueError,'live_inputs'):run(j,s,base)


if __name__=='__main__':unittest.main()
