"""Account evidence collection/import CLI; never starts or controls the bot."""
from __future__ import annotations

import argparse
from datetime import timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bot_core.account_journal import Journal, atomic_json, bot_event, event, snapshot_events, timestamp
from bot_core.exchange_history import ExtendedReader, ReadError, import_var_csv


class LocalCollector:
    def __init__(self, root, journal):
        self.root, self.journal = Path(root), journal
        self.offsets = {}
        self.snapshot_signature = None

    def collect(self):
        added = 0
        for name in ('out/executor_results.jsonl', 'out/trades.jsonl', 'out/executor_actions.jsonl'):
            path = self.root / name
            if not path.exists():
                continue
            try:
                stat = path.stat()
                with path.open('rb') as stream:
                    prefix = hashlib.sha256(stream.read(min(stat.st_size, 128))).hexdigest()
                    state = self.offsets.get(name)
                    offset = 0
                    if state:
                        old_size, old_prefix_size, old_prefix, old_offset = state
                        stream.seek(0)
                        original_prefix = hashlib.sha256(stream.read(old_prefix_size)).hexdigest()
                        reset = stat.st_size < old_offset or original_prefix != old_prefix
                        if not reset:
                            offset = old_offset
                        else:
                            added += int(self.journal.append(event('LOCAL', 'bot', 'source_reset', name + ':' + str(stat.st_mtime_ns),
                                source='local_collector', authority='collection_status', occurred_at=timestamp(time.time()),
                                data={'path': name, 'previous_size': old_size, 'current_size': stat.st_size,
                                      'complete': False, 'reason': 'bot_log_rotated_or_truncated'})))
                    stream.seek(offset)
                    while True:
                        beginning = stream.tell()
                        line = stream.readline(8 * 1024 * 1024 + 1)
                        if not line:
                            break
                        if len(line) > 8 * 1024 * 1024:
                            raise ValueError('record_too_large')
                        if not line.endswith(b'\n'):
                            stream.seek(beginning)
                            break  # The writer may still be appending this JSON record.
                        try:
                            row = json.loads(line.decode('utf-8-sig'))
                            if not isinstance(row, dict):
                                raise ValueError('not_object')
                            added += int(self.journal.append(bot_event(row, name)))
                        except (ValueError, TypeError, UnicodeError):
                            added += int(self.journal.append(event('LOCAL', 'bot', 'parse_error',
                                name + ':' + hashlib.sha256(line).hexdigest(), source='local_collector',
                                authority='collection_status', occurred_at=None,
                                data={'path': name, 'offset': beginning, 'error': 'invalid_bot_log_record'})))
                    self.offsets[name] = (stat.st_size, min(stat.st_size, 128), prefix, stream.tell())
            except (OSError, ValueError) as exc:
                added += int(self.journal.append(event('LOCAL', 'bot', 'collection_error', name + ':' + str(int(time.time() // 60)),
                    source='local_collector', authority='collection_status', occurred_at=timestamp(time.time()),
                    data={'path': name, 'error_type': type(exc).__name__, 'complete': False})))
        path = self.root / 'out/equity.json'
        if path.exists():
            try:
                raw = path.read_bytes()
                signature = hashlib.sha256(raw).hexdigest()
                if signature != self.snapshot_signature:
                    snapshot = json.loads(raw.decode('utf-8-sig'))
                    for record in snapshot_events(snapshot):
                        added += int(self.journal.append(record))
                    self.snapshot_signature = signature
            except (OSError, ValueError, TypeError):
                added += int(self.journal.append(event('LOCAL', 'bot', 'collection_error', 'equity:' + str(int(time.time() // 60)),
                    source='local_collector', authority='collection_status', occurred_at=timestamp(time.time()),
                    data={'path': 'out/equity.json', 'error': 'snapshot_unreadable', 'complete': False})))
        return added


def write_views(journal):
    count = journal.export()
    summary = journal.summary()
    atomic_json(journal.directory / 'summary.json', summary)
    return count


def local_health(journal, count, added):
    atomic_json(journal.directory / 'collector_status.json', {
        'ts': timestamp(time.time()), 'pid': os.getpid(), 'mode': 'local_files_only', 'state': 'collecting',
        'records': count, 'added_this_pass': added, 'trading_actions': False,
        'EXT': 'not_connected_run_sync_ext_with_API_key',
        'VAR': 'requires_official_CSV_import',
        'history_complete': False,
        'note': 'Bot observations are not exchange-confirmed fills or a complete account statement.'})


def watch(root, journal, interval, duration=0):
    # The collector has its own lock; it does not create/consult any trading lock.
    lock = (journal.directory / 'collector.lock').open('a+b')
    lock.seek(0)
    if not lock.read(1):
        lock.write(b'0'); lock.flush()
    lock.seek(0)
    if os.name == 'nt':
        import msvcrt
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            lock.close()
            raise SystemExit('Account collector already running')
    else:
        import fcntl
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise SystemExit('Account collector already running')
    collector = LocalCollector(root, journal)
    started = time.monotonic()
    count = -1
    try:
        while True:
            try:
                added = collector.collect()
                if added or count < 0:
                    count = write_views(journal)
                local_health(journal, count, added)
            except Exception as exc:
                count = -1  # Rebuild derived views after the storage recovers.
                try:
                    atomic_json(journal.directory / 'collector_status.json', {
                        'ts': timestamp(time.time()), 'pid': os.getpid(), 'state': 'collection_error',
                        'error_type': type(exc).__name__, 'history_complete': False,
                        'mode': 'local_files_only', 'trading_actions': False})
                except Exception:
                    pass
            if duration > 0 and time.monotonic() - started >= duration:
                break
            time.sleep(max(2.0, interval))
    finally:
        lock.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('collect-local', help='Preserve current bot logs and equity snapshot once')
    worker = commands.add_parser('watch-local', help='Continuously preserve local files; no network or bot controls')
    worker.add_argument('--interval', type=float, default=5)
    worker.add_argument('--duration', type=float, default=0)
    commands.add_parser('export', help='Rebuild JSONL export and evidence summary from the durable database')
    commands.add_parser('status')
    csv_cmd = commands.add_parser('import-var', help='Import an official Variational CSV')
    csv_cmd.add_argument('path', type=Path)
    csv_cmd.add_argument('--account', required=True, help='Stable local label for the actual account that exported this file')
    csv_cmd.add_argument('--kind', choices=('trades', 'transfers'), required=True)
    csv_cmd.add_argument('--naive-timezone', choices=('UTC', 'Asia/Shanghai'))
    csv_cmd.add_argument('--quantity-convention', choices=('unknown', 'signed'), default='unknown',
                         help='Use signed only after verifying the CSV positive/negative direction against the exchange')
    ext = commands.add_parser('sync-ext', help='Read Extended histories with GET only; API key from an environment variable')
    ext.add_argument('--key-env', default='BOT_AUDIT_EXT_API_KEY')
    ext.add_argument('--start', required=True, help='Funding-history start time, ISO 8601 with timezone')
    ext.add_argument('--max-pages', type=int, default=25)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    root = args.root.resolve()
    journal = Journal(root / 'account_audit')
    if args.command == 'watch-local':
        watch(root, journal, args.interval, args.duration)
        return 0
    if args.command == 'collect-local':
        added = LocalCollector(root, journal).collect()
        count = write_views(journal)
        local_health(journal, count, added)
        result = {'records': count, 'added': added, 'exchange_connection': 'not_attempted'}
    elif args.command == 'import-var':
        tz = {'UTC': timezone.utc, 'Asia/Shanghai': timezone(timedelta(hours=8))}.get(args.naive_timezone)
        result = import_var_csv(journal, args.path, account=args.account, channel=args.kind,
                                naive_timezone=tz, quantity_convention=args.quantity_convention)
        write_views(journal)
    elif args.command == 'sync-ext':
        from datetime import datetime
        start = timestamp(args.start)
        if start is None:
            raise SystemExit('--start must contain an explicit timezone')
        start_ms = int(datetime.fromisoformat(start).timestamp() * 1000)
        try:
            result = ExtendedReader(key_env=args.key_env).sync(journal, start_ms=start_ms, max_pages=args.max_pages)
        except ReadError as exc:
            result = {'platform': 'EXT', 'complete': False, 'reason': str(exc)}
            atomic_json(journal.directory / 'ext_sync_status.json', result)
            print(json.dumps(result))
            return 2
        atomic_json(journal.directory / 'ext_sync_status.json', result)
        write_views(journal)
    elif args.command == 'export':
        result = {'exported': write_views(journal)}
    else:
        result = journal.summary()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
