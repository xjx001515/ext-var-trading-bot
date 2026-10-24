"""Offline audit tests: block live network/process launch and trading-file writes."""
from pathlib import Path
import hashlib
import json
import os
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
REPORT = ROOT / 'audit_20261023/account_journal'


def protected(path):
    if not isinstance(path, (str, bytes, os.PathLike)):
        return False
    try:
        relative = Path(os.fsdecode(path)).resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return False
    return relative.startswith(('out/', 'logs/', 'account_audit/', 'edge_profile/', 'chrome_profile/', '_chrome_cdp_profile_9222/')) or relative in ('control.json', 'control_cmd.txt', 'bot_read_only.json')


def guard(name, args):
    if name in ('subprocess.Popen', 'os.system', 'socket.connect', 'socket.bind'):
        raise RuntimeError('Live actions disabled in account-audit tests')
    if name == 'sqlite3.connect' and protected(args[0]):
        raise RuntimeError('Live database access disabled in tests')
    if name == 'open':
        path, mode, flags = args
        writing = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
        if writing and protected(path):
            raise RuntimeError('Live trading/account files cannot be written by tests')
    if name in ('os.remove', 'os.rename', 'os.rmdir') and any(protected(p) for p in args[:2]):
        raise RuntimeError('Live files cannot be mutated by tests')


def main():
    REPORT.mkdir(parents=True, exist_ok=True)
    before = {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ('control.json','control_cmd.txt')}
    sys.addaudithook(guard)
    suite = unittest.defaultTestLoader.discover(str(ROOT/'tests'), pattern='test_account_journal.py')
    with (REPORT/'tests.txt').open('w',encoding='utf-8') as output:
        result = unittest.TextTestRunner(stream=output,verbosity=2).run(suite)
    for name in ('BOT5-2-414 .py','bot_core/account_journal.py','bot_core/exchange_history.py','maintenance/account_audit.py'):
        compile((ROOT/name).read_text(encoding='utf-8-sig'),name,'exec')
    after = {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in before}
    report = {'tests':result.testsRun,'passed':result.wasSuccessful(),'live_actions_disabled':True,
              'control_files_unchanged':before==after,'main_program_not_imported':True}
    (REPORT/'tests.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report),flush=True)
    if not result.wasSuccessful():
        for test, detail in result.errors+result.failures:
            print(str(test)+'\n'+detail[:2400],flush=True)
        raise SystemExit(1)


if __name__=='__main__':
    main()
