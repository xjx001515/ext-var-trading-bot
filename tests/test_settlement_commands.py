"""Operator commands use temp inboxes and a fake sole journal writer."""
import copy
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from bot_core.legacy_reconciliation import fingerprint
from bot_core.settlement_commands import SettlementInbox, proposal, commit_reconciliation
from maintenance.settle_unconfirmed import main
from test_legacy_reconciliation import record


class CommandTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.inbox=SettlementInbox(self.root)
        self.records={'VAR|TEST':record()}
        self.disk=self.root/'state/execution_unconfirmed.json';self.disk.parent.mkdir()
        self.save=Mock(side_effect=self.write)
        self.write()

    def write(self):
        self.disk.write_text(json.dumps({'version':2,'records':self.records}),encoding='utf-8');return True

    def command(self):
        return proposal('VAR|TEST',self.records['VAR|TEST'],state='RESOLVED_FLAT',
            reason='operator reviewed closed position',expected_fingerprint=fingerprint(self.records['VAR|TEST']),now=1300)

    def test_enqueue_does_not_edit_live_journal(self):
        before=self.disk.read_bytes();cmd=self.command();self.inbox.enqueue(cmd)
        self.assertEqual(self.disk.read_bytes(),before)
        self.assertEqual(self.inbox.status(cmd['id'])['status'],'QUEUED')

    def test_sole_writer_commits_once_with_audit_and_receipt(self):
        cmd=self.command();self.inbox.enqueue(cmd)
        result=self.inbox.consume(self.records,self.save,now=1301)
        self.assertEqual(result[0]['status'],'APPLIED');self.save.assert_called_once()
        self.assertEqual(self.records['VAR|TEST']['state'],'RESOLVED_FLAT')
        events=[json.loads(x) for x in self.inbox.audit_path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual([x['phase'] for x in events],['PREPARE','APPLIED'])
        self.assertEqual(self.inbox.consume(self.records,self.save,now=1400),[])
        self.save.assert_called_once()

    def test_changed_order_fingerprint_rejects_command(self):
        cmd=self.command();self.inbox.enqueue(cmd)
        self.records['VAR|TEST']['submission_attempt_id']='new-order'
        result=self.inbox.consume(self.records,self.save,now=1301)
        self.assertEqual(result[0]['status'],'REJECTED');self.save.assert_not_called()
        self.assertIn('record_changed',result[0]['reason'])

    def test_unwritable_audit_cannot_clear_record(self):
        self.inbox.enqueue(self.command());self.inbox.audit=Mock(side_effect=OSError('audit unavailable'))
        result=self.inbox.consume(self.records,self.save,now=1301)
        self.assertEqual(result[0]['status'],'REJECTED');self.save.assert_not_called()
        self.assertEqual(self.records['VAR|TEST']['state'],'PENDING_CONFIRM')

    def test_save_failure_restores_in_memory_record(self):
        self.inbox.enqueue(self.command());before=copy.deepcopy(self.records)
        result=self.inbox.consume(self.records,lambda:False,now=1301)
        self.assertEqual(result[0]['status'],'REJECTED');self.assertEqual(self.records,before)

    def test_restart_after_commit_before_receipt_is_idempotent(self):
        cmd=self.command();self.inbox.enqueue(cmd)
        self.records['VAR|TEST'].update(state='RESOLVED_FLAT',manual_settlement_id=cmd['id'])
        result=self.inbox.consume(self.records,self.save,now=1400)
        self.assertEqual(result[0]['status'],'APPLIED');self.save.assert_not_called()
        self.assertEqual(result[0]['reason'],'already_committed_before_restart')

    def test_special_m10_and_m9_records_need_their_own_review(self):
        for extra in ({'module':'M9'},{'module':'M10'},{'m10_step3_confirmation':{'usd':100}}):
            r=record(**extra)
            with self.assertRaisesRegex(ValueError,'special_m9_m10'):
                proposal('VAR|TEST',r,state='CONFIRMED_SUCCESS',reason='reviewed',expected_fingerprint=fingerprint(r),now=1300)

    def test_cli_preview_and_apply_only_create_inbox_command(self):
        before=self.disk.read_bytes();args=['--out-dir',str(self.root),'settle','--key','VAR|TEST',
            '--state','RESOLVED_FLAT','--reason','manual review','--expected',fingerprint(self.records['VAR|TEST'])]
        with redirect_stdout(io.StringIO()):
            preview=main(args);queued=main(args+['--apply'])
            status=main(['--out-dir',str(self.root),'status',queued['command']['id']])
        self.assertEqual(preview['mode'],'PREVIEW');self.assertEqual(status['status'],'QUEUED')
        self.assertEqual(self.disk.read_bytes(),before)

    def test_cli_list_exposes_fingerprint_and_last_reconciliation(self):
        with redirect_stdout(io.StringIO()):result=main(['--out-dir',str(self.root),'list'])
        self.assertEqual(result['records'][0]['fingerprint'],fingerprint(self.records['VAR|TEST']))

    def test_traversal_or_conflicting_command_id_is_rejected(self):
        with self.assertRaises(ValueError):self.inbox.status('../journal')
        cmd=self.command();self.inbox.enqueue(cmd)
        with self.assertRaisesRegex(ValueError,'conflict'):self.inbox.enqueue(dict(cmd,reason='changed'))

    def test_malformed_command_cannot_stop_other_settlements(self):
        bad=self.command();bad['record_key']=[];self.inbox.enqueue(bad)
        good=self.command();self.inbox.enqueue(good)
        results=self.inbox.consume(self.records,self.save,now=1301)
        self.assertEqual({r['status'] for r in results},{'REJECTED','APPLIED'})
        self.assertEqual(self.records['VAR|TEST']['state'],'RESOLVED_FLAT')

    def test_automatic_batch_save_failure_rolls_back_without_success_audit(self):
        before=copy.deepcopy(self.records);updated=copy.deepcopy(before);updated['VAR|TEST']['state']='CONFIRMED_SUCCESS'
        ok=commit_reconciliation(self.records,updated,[{'original_state':'PENDING_CONFIRM','proposed_state':'CONFIRMED_SUCCESS'}],
            save=lambda:False,inbox=self.inbox,now=1300,stage='STARTUP')
        self.assertFalse(ok);self.assertEqual(self.records,before)
        self.assertNotIn('APPLIED',self.inbox.audit_path.read_text())


if __name__=='__main__':unittest.main()
