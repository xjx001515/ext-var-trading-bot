"""Stop only the verified current bot; never close browser tabs or positions."""
from pathlib import Path
import hashlib
import json
import time

import psutil

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'audit_20261023/controlled_restart'
PROTECTED = ('out/positions.json', 'out/equity.json', 'out/state/anchor_state.json',
             'out/state/coin_pool_state.json', 'out/state/m9_wangge_zhangben.json',
             'grid_rate_pause_ledger.json', 'out/trades.jsonl')


def hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in PROTECTED if (ROOT / name).exists()}


def atomic(path, content):
    temp = path.with_name(path.name + '.controlled_restart.tmp')
    temp.write_text(content, encoding='utf-8')
    temp.replace(path)


def main():
    assert json.loads((ROOT / 'bot_read_only.json').read_text())['enabled'] is True
    record = json.loads((ROOT / 'out/panel_bot_pid.txt').read_text(encoding='utf-8-sig'))
    script = str((ROOT / 'BOT5-2-414 .py').resolve()).casefold()
    parent = psutil.Process(int(record['pid']))
    expected_start = (int(record['created']) - 621355968000000000) / 10000000
    assert abs(parent.create_time() - expected_start) < 0.1, 'PID identity changed'
    assert script in [str(arg).casefold() for arg in parent.cmdline()], 'Unexpected process command'
    status = json.loads((ROOT / 'out/status.json').read_text(encoding='utf-8-sig'))
    queue = json.loads((ROOT / 'out/queue.json').read_text(encoding='utf-8-sig'))
    assert not status.get('trade_enabled'), 'Trading gate must be closed for this controlled restart'
    assert int(queue.get('req_queue_len') or 0) == 0, 'Queued requests need inspection'
    candidates = [parent] + [p for p in parent.children(recursive=True)
                             if script in [str(arg).casefold() for arg in p.cmdline()]]
    identities = {p.pid: p.create_time() for p in candidates}
    before = hashes()
    REPORT.mkdir(parents=True, exist_ok=True)
    backup = REPORT / 'control_before_restart'
    backup.mkdir(exist_ok=True)
    now = time.time()
    payload = {'cmd': 'exit', 'command': 'exit', 'action': 'exit', 'updated_ts': now,
               'seq': int(now * 1000), 'run_state': 'stopped', 'start': False, 'pause': False,
               'resume': False, 'exit': True, 'read_only': True,
               'reason': 'user_authorized_controlled_restart_for_read_only_validation'}
    for directory, label in ((ROOT, 'root'), (ROOT / 'out', 'out')):
        for name in ('control.json', 'control_cmd.txt'):
            source = directory / name
            if source.exists():
                (backup / (label + '_' + name)).write_bytes(source.read_bytes())
        atomic(directory / 'control.json', json.dumps(payload, ensure_ascii=False, indent=2))
        atomic(directory / 'control_cmd.txt', 'exit')
    gone, alive = psutil.wait_procs(candidates, timeout=25)
    terminated = []
    for process in alive:
        try:
            assert abs(process.create_time() - identities[process.pid]) < 0.01
            assert script in [str(arg).casefold() for arg in process.cmdline()]
            process.terminate()
            terminated.append(process.pid)
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(alive, timeout=10)
    assert not alive, 'Verified old bot still running; do not start another'
    after = hashes()
    result = {'old_pids': list(identities), 'old_processes_exited': True,
              'graceful': not terminated, 'terminated_verified_pids': terminated,
              'protected_before_stop': before, 'protected_after_stop': after,
              'protected_unchanged_on_stop': before == after, 'browser_stopped': False}
    (REPORT / 'stop_result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('old_pids', 'old_processes_exited', 'graceful', 'protected_unchanged_on_stop')}), flush=True)


if __name__ == '__main__':
    main()
