"""Audited settlement inbox. Only the running bot writes the trading journal."""
from copy import deepcopy
from pathlib import Path
import json
import os
import re
import time
import uuid

from .legacy_reconciliation import fingerprint, manual_resolution, TERMINAL


def validate_manual_record(record, *, allow_special=False):
    if not isinstance(record, dict) or not record:
        raise ValueError('record_not_found')
    module = str(record.get('module') or '').upper()
    if (record.get('m10_step3_confirmation') or module.startswith(('M9', 'M10'))) and not allow_special:
        raise ValueError('special_m9_m10_confirmation_requires_its_own_review')
    if record.get('state') in TERMINAL:
        raise ValueError('record_already_settled')


def proposal(key, record, *, state, reason, expected_fingerprint, now, allow_special=False):
    if allow_special and state != 'CONFIRMED_NOT_FILLED':
        raise ValueError('special_review_only_allows_confirmed_not_filled')
    validate_manual_record(record, allow_special=allow_special)
    manual_resolution(record, expected_fingerprint=expected_fingerprint,
                      state=state, reason=reason, now=now)
    return {'version':1, 'id':uuid.uuid4().hex, 'record_key':key,
            'expected_fingerprint':expected_fingerprint, 'state':state,
            'reason':str(reason).strip(), 'created_ts':now,
            'special_review': bool(allow_special)}


class SettlementInbox:
    def __init__(self, out_dir):
        self.root = Path(out_dir) / 'state' / 'manual_settlements'
        self.audit_path = Path(out_dir) / 'state' / 'execution_settlement_audit.jsonl'

    @staticmethod
    def valid_id(value):
        if not isinstance(value, str) or re.fullmatch('[0-9a-f]{32}', value) is None:
            raise ValueError('invalid_command_id')
        return value

    @staticmethod
    def _atomic(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with tmp.open('x', encoding='utf-8') as fp:
                json.dump(value, fp, ensure_ascii=False, indent=2, allow_nan=False)
                fp.flush(); os.fsync(fp.fileno())
            os.replace(tmp, path)
        finally:
            if tmp.exists():tmp.unlink()

    def audit(self, event):
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open('a', encoding='utf-8') as fp:
            fp.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n')
            fp.flush(); os.fsync(fp.fileno())

    def enqueue(self, command):
        ident = self.valid_id(command.get('id'))
        path = self.root/'requests'/(ident+'.json')
        if path.exists():
            if json.loads(path.read_text(encoding='utf-8')) != command:
                raise ValueError('command_id_conflict')
            return ident
        self._atomic(path, command)
        return ident

    def status(self, ident):
        ident = self.valid_id(ident)
        result = self.root/'results'/(ident+'.json')
        if result.exists():return json.loads(result.read_text(encoding='utf-8'))
        return {'id':ident, 'status':'QUEUED' if (self.root/'requests'/(ident+'.json')).exists() else 'NOT_FOUND'}

    def consume(self, records, save, *, now):
        """Caller holds the same lock used by submission reservations."""
        results = []
        for path in sorted((self.root/'requests').glob('*.json')):
            if len(results) >= 100:break
            try:ident = self.valid_id(path.stem)
            except ValueError:continue  # Only UUID-named commands belong to this protocol.
            target = self.root/'results'/(ident+'.json')
            if target.exists():continue
            key = ''
            try:
                if path.stat().st_size > 65536:raise ValueError('command_too_large')
                cmd = json.loads(path.read_text(encoding='utf-8'))
                if cmd.get('version') != 1 or cmd.get('id') != ident:raise ValueError('invalid_command')
                if not isinstance(cmd.get('record_key'), str) or not cmd['record_key']:
                    raise ValueError('invalid_record_key')
                if not isinstance(cmd.get('reason'), str) or not cmd['reason'].strip():
                    raise ValueError('explicit_reason_required')
                key = cmd['record_key']; old = records.get(key)
                if old and old.get('manual_settlement_id') == ident:
                    result = {'id':ident,'record_key':key,'status':'APPLIED','state':old['state'],
                              'reason':'already_committed_before_restart','ts':now}
                else:
                    special_review = bool(cmd.get('special_review'))
                    if special_review and cmd.get('state') != 'CONFIRMED_NOT_FILLED':
                        raise ValueError('special_review_only_allows_confirmed_not_filled')
                    validate_manual_record(old, allow_special=special_review)
                    updated, evidence = manual_resolution(old, expected_fingerprint=cmd['expected_fingerprint'],
                        state=cmd['state'], reason=cmd['reason'], now=now)
                    updated['manual_settlement_id'] = ident
                    event = dict(evidence, id=ident, record_key=key, source='manual', phase='PREPARE',
                                 before_record=deepcopy(old))
                    self.audit(event)  # No ledger mutation if the audit cannot be written.
                    records[key] = updated
                    try:
                        if not save():raise OSError('journal_save_failed')
                    except Exception:
                        records[key] = old
                        raise
                    result = {'id':ident,'record_key':key,'status':'APPLIED','state':updated['state'],
                              'reason':cmd['reason'],'ts':now}
                    # If receipt writing fails after commit, the journal ID makes a retry idempotent.
                    self.audit(dict(event, phase='APPLIED', after_fingerprint=fingerprint(updated)))
            except Exception as exc:
                if records.get(key, {}).get('manual_settlement_id') == ident:
                    result = {'id':ident,'record_key':key,'status':'APPLIED','state':records[key]['state'],
                              'reason':'committed_receipt_recovery','ts':now}
                else:
                    result = {'id':ident,'record_key':key,'status':'REJECTED','reason':str(exc),'ts':now}
            self._atomic(target, result)
            results.append(result)
        return results


def commit_reconciliation(records, updated, decisions, *, save, inbox, now, stage):
    """Atomically replace a planned batch in memory only after durable audit prep."""
    if records == updated:return True
    before = deepcopy(records)
    transitions = [d for d in decisions if d['original_state'] != d['proposed_state']]
    event = {'id':uuid.uuid4().hex,'source':'scan_reconciliation','stage':stage,'ts':now,
             'phase':'PREPARE','transitions':transitions,
             'before_records':{d['record_key']:before[d['record_key']] for d in transitions if d.get('record_key') in before},
             'after_fingerprints':{d['record_key']:fingerprint(updated[d['record_key']]) for d in transitions if d.get('record_key') in updated}}
    if transitions:inbox.audit(event)
    records.clear();records.update(updated)
    try:
        if not save():raise OSError('journal_save_failed')
    except Exception:
        records.clear();records.update(before)
        return False
    if transitions:inbox.audit(dict(event,phase='APPLIED'))
    return True
