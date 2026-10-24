"""Passive financial evidence journal. No browser, network, or trading actions.

The SQLite file is authoritative and append-only. JSONL is a rebuildable export.
Exchange records and bot observations have deliberately different authority.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

SCHEMA_VERSION = 1
SECRET_KEY = re.compile(r'api.?key|authorization|cookie|password|private.?key|secret|signature|mnemonic|access.?token', re.I)
BOT_FILES = {'executor_results.jsonl', 'trades.jsonl', 'executor_actions.jsonl'}


def number(value):
    if value is None or isinstance(value, bool) or value == '':
        return None
    try:
        result = Decimal(str(value))
        return format(result, 'f') if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def timestamp(value, *, naive_timezone=None):
    if value is None or isinstance(value, bool) or value == '':
        return None
    try:
        if isinstance(value, (int, float, Decimal)) or re.fullmatch(r'\d+(?:\.\d+)?', str(value)):
            seconds = Decimal(str(value))
            if seconds > Decimal('100000000000'):
                seconds /= 1000
            result = datetime.fromtimestamp(float(seconds), timezone.utc)
        else:
            result = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
            if result.tzinfo is None:
                if naive_timezone is None:
                    return None
                result = result.replace(tzinfo=naive_timezone)
        return result.astimezone(timezone.utc).isoformat(timespec='milliseconds')
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def redact(value):
    if isinstance(value, dict):
        return {str(k): '[REDACTED]' if SECRET_KEY.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, float) and number(value) is None:
        return None
    return value


def canonical(value):
    return json.dumps(redact(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.{os.getpid()}.{threading.get_ident()}.tmp')
    temporary.write_text(json.dumps(redact(value), ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    os.replace(temporary, path)


def event(platform, account, kind, source_id, *, source, authority, occurred_at, data, raw=None):
    if not platform or not account or not kind or not source_id:
        raise ValueError('platform, account, kind and source_id are required')
    if authority not in ('exchange_record', 'bot_observation', 'collection_status'):
        raise ValueError('unknown evidence authority')
    return {'schema_version': SCHEMA_VERSION, 'platform': str(platform).upper(),
            'account': str(account), 'kind': str(kind), 'source_id': str(source_id),
            'source': str(source), 'authority': authority, 'occurred_at': occurred_at,
            'data': redact(data), 'raw': redact(raw)}


class Journal:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'journal.sqlite3'
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    entity_key TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_entity ON events(entity_key, seq);
                CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
                BEGIN SELECT RAISE(ABORT, 'Evidence is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
                BEGIN SELECT RAISE(ABORT, 'Evidence is append-only'); END;
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=1.0)
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    def append(self, record):
        payload = canonical(record)
        entity = canonical([record[k] for k in ('authority', 'platform', 'account', 'kind', 'source_id')])
        digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        with self.connect() as db:
            result = db.execute('INSERT OR IGNORE INTO events(event_key, entity_key, observed_at, payload) VALUES (?,?,?,?)',
                                (digest, entity, timestamp(time.time()), payload))
            return result.rowcount == 1

    def records(self, *, latest=False):
        with self.connect() as db:
            chosen = {}
            for seq, entity, observed, payload in db.execute('SELECT seq,entity_key,observed_at,payload FROM events ORDER BY seq'):
                record = dict(json.loads(payload), journal_seq=seq, observed_at=observed)
                if not latest:
                    yield record
                    continue
                # An old CSV/API update imported later must not replace a newer
                # exchange timestamp. Original versions remain in the journal.
                rank = (record.get('occurred_at') or '', seq)
                if entity not in chosen or rank > chosen[entity][0]:
                    chosen[entity] = (rank, record)
            if latest:
                for _, record in sorted(chosen.values(), key=lambda pair: pair[1]['journal_seq']):
                    yield record

    def export(self, destination=None):
        destination = Path(destination or self.directory / 'events.jsonl')
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + f'.{os.getpid()}.tmp')
        count = 0
        with temporary.open('w', encoding='utf-8', newline='\n') as stream:
            for record in self.records():
                stream.write(canonical(record) + '\n')
                count += 1
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        return count

    def summary(self):
        records = list(self.records(latest=True))
        counts = Counter((r['authority'], r['platform'], r['kind']) for r in records)
        components = defaultdict(lambda: {'known_cash_delta': Decimal(0), 'known_count': 0, 'unknown_count': 0})
        bot_by_order = defaultdict(list)
        exchange_accounts_by_order = defaultdict(set)
        for r in records:
            data = r['data']
            if r['authority'] == 'bot_observation' and data.get('order_id'):
                bot_by_order[(r['platform'], str(data['order_id']))].append(r)
            if r['authority'] == 'exchange_record' and r['kind'] == 'fill' and data.get('order_id'):
                exchange_accounts_by_order[(r['platform'], str(data['order_id']))].add(r['account'])
        links = []
        for r in records:
            data = r['data']
            if r['authority'] != 'exchange_record':
                continue
            if data.get('status') == 'confirmed' and r['kind'] in (
                    'fee', 'funding', 'deposit', 'withdrawal', 'transfer', 'realized_pnl', 'adjustment'):
                key = (r['platform'], r['account'], data.get('currency') or 'UNKNOWN', r['kind'])
                bucket = components[key]
                cash = number(data.get('cash_delta'))
                if cash is None:
                    bucket['unknown_count'] += 1
                else:
                    bucket['known_cash_delta'] += Decimal(cash)
                    bucket['known_count'] += 1
            if r['kind'] == 'fill':
                candidates = bot_by_order.get((r['platform'], str(data.get('order_id') or '')), [])
                request_ids = sorted({str(c['data']['req_id']) for c in candidates if c['data'].get('req_id')})
                if len(exchange_accounts_by_order[(r['platform'], str(data.get('order_id') or ''))]) != 1:
                    request_ids = []
                links.append({'platform': r['platform'], 'account': r['account'], 'trade_id': r['source_id'],
                              'order_id': data.get('order_id'),
                              'req_id': request_ids[0] if len(request_ids) == 1 else None,
                              'state': 'exact_order_id_match' if len(request_ids) == 1 else 'unmatched_or_ambiguous'})
        return {'generated_at': timestamp(time.time()),
                'counts': [{'authority': a, 'platform': p, 'kind': k, 'count': v} for (a,p,k),v in sorted(counts.items())],
                'cash_components_observed_only': [dict(platform=p, account=a, currency=c, kind=k,
                    **{**v, 'known_cash_delta': str(v['known_cash_delta'])}) for (p,a,c,k),v in sorted(components.items())],
                'fill_links': links,
                'coverage': [r['data'] for r in records if r['kind'] == 'coverage'],
                'verified_net_profit': None, 'reconciliation_state': 'requires_complete_account_statement',
                'note': 'Observed subtotals are not verified net profit; missing history and unknown signs are not zero.'}


def reconciliation(opening_equity, closing_equity, *, external_cashflow=None, realized_pnl=None,
                   fees=None, funding=None, other_adjustments=None, unrealized_change=None,
                   coverage_complete=False):
    """All inputs must be signed cash deltas, in ONE verified accounting currency.

    realised_pnl must exclude fees/funding already supplied separately. This is
    deliberately not inferred from an exchange's cumulative position summary.
    """
    opening, closing = number(opening_equity), number(closing_equity)
    change = Decimal(closing) - Decimal(opening) if opening is not None and closing is not None else None
    supplied = dict(external_cashflow=external_cashflow, realized_pnl=realized_pnl, fees=fees,
                    funding=funding, other_adjustments=other_adjustments, unrealized_change=unrealized_change)
    normalized = {k: number(v) for k,v in supplied.items()}
    missing = [k for k,v in normalized.items() if v is None]
    if change is None:
        missing.append('equity_boundary')
    complete = coverage_complete is True and not missing
    residual = change - sum((Decimal(v) for v in normalized.values()), Decimal(0)) if complete else None
    return {'equity_change': str(change) if change is not None else None,
            'cashflow_adjusted_equity_change': str(change - Decimal(normalized['external_cashflow']))
                if complete else None,
            'residual': str(residual) if residual is not None else None,
            'state': 'matched' if residual is not None and abs(residual) <= Decimal('0.01') else
                     'unexplained_difference' if complete else 'incomplete',
            'missing': missing, 'coverage_complete': coverage_complete is True}


def bot_event(record, source_name):
    req = record.get('req') if isinstance(record.get('req'), dict) else {}
    meta = req.get('meta') if isinstance(req.get('meta'), dict) else {}
    params = req.get('params') if isinstance(req.get('params'), dict) else {}
    platform = str(record.get('platform') or req.get('platform') or 'UNKNOWN').upper()
    data = {k: record.get(k) for k in ('ok', 'submit_ok', 'postcheck_ok', 'confirmation_state',
            'fill_confirmed', 'filled_qty', 'fill_price', 'filled_notional_u', 'fee', 'fee_currency', 'reason')}
    data.update(req_id=str(record.get('req_id') or req.get('req_id') or meta.get('req_id') or ''),
                trace_id=str(record.get('trace_id') or req.get('trace_id') or meta.get('trace_id') or ''),
                order_id=str(record.get('order_id') or record.get('oid') or req.get('order_id') or ''),
                module=record.get('module') or req.get('module'), coin=record.get('coin') or req.get('coin'),
                action=record.get('action_type') or req.get('action_type'),
                requested_amount=number(req.get('amount_usd') or params.get('amount_usd')),
                source_file=Path(source_name).name,
                exchange_confirmation='not_proven_by_bot_log', status='observation')
    stamp = timestamp(record.get('ts') or record.get('timestamp'))
    source_id = hashlib.sha256(canonical([stamp, data]).encode('utf-8')).hexdigest()
    return event(platform, 'bot_account_unverified', 'bot_execution', source_id,
                 source='bot_log', authority='bot_observation', occurred_at=stamp,
                 data=data, raw={'execution_evidence': record.get('execution_evidence'),
                                 'postcheck_detail': record.get('postcheck_detail')})


def snapshot_events(snapshot):
    stamp = timestamp(snapshot.get('ts'))
    if stamp is None:
        return []
    balances = snapshot.get('balances') if isinstance(snapshot.get('balances'), dict) else {}
    rows = []
    for platform in ('EXT', 'VAR'):
        balance = balances.get(platform) or {}
        rows.append(event(platform, 'bot_account_unverified', 'equity_observation', stamp,
            source='bot_snapshot', authority='bot_observation', occurred_at=stamp,
            data={'equity': number(balance.get('usd') if isinstance(balance, dict) else None),
                  'currency': 'USD', 'data_quality': snapshot.get('data_quality'),
                  'balance_fresh': snapshot.get('balance_fresh'),
                  'unrealized_pnl': None, 'status': 'observation',
                  'positions': (snapshot.get('positions') or {}).get(platform)
                       if isinstance(snapshot.get('positions'), dict) else snapshot.get(platform)},
            raw=None))
    return rows


_instances = {}
_instance_lock = threading.Lock()


def _journal(root):
    directory = str(Path(root).resolve() / 'account_audit')
    with _instance_lock:
        if directory not in _instances:
            _instances[directory] = Journal(directory)
        return _instances[directory]


def _failure(root, exc):
    # No exception contents/credentials, and no recursion into the bot logger.
    try:
        atomic_json(Path(root) / 'account_audit/write_health.json',
                    {'ts': timestamp(time.time()), 'state': 'write_failed', 'error_type': type(exc).__name__})
    except Exception:
        pass


def observe_bot_event(script, path, record):
    if Path(str(path)).name not in BOT_FILES or not isinstance(record, dict):
        return
    root = Path(script).resolve().parent
    try:
        _journal(root).append(bot_event(record, path))
    except Exception as exc:
        _failure(root, exc)


def observe_snapshot(script, snapshot):
    root = Path(script).resolve().parent
    try:
        journal = _journal(root)
        for record in snapshot_events(snapshot):
            journal.append(record)
    except Exception as exc:
        _failure(root, exc)
