"""Test staged source while the real bot stays running, with no live side effects."""
from pathlib import Path
import hashlib
import json
import os
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
REPORT = ROOT / 'audit_20261023/access_diagnosis'


def protected(path):
    if not isinstance(path, (str, bytes, os.PathLike)):
        return False
    try:
        relative = Path(os.fsdecode(path)).resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return False
    return relative.startswith(('out/', 'logs/', 'rizhi/', 'edge_profile/', 'chrome_profile/')) or relative in ('control.json', 'control_cmd.txt')


def audit(event, args):
    if event in ('subprocess.Popen', 'os.system', 'socket.connect', 'socket.bind'):
        raise RuntimeError('No live actions in source tests: ' + event)
    if event == 'open':
        path, mode, flags = args
        writing = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
        if writing and protected(path):
            raise RuntimeError('No trading-state writes in source tests')
    if event in ('os.remove', 'os.rename', 'os.rmdir') and any(protected(a) for a in args[:2]):
        raise RuntimeError('No trading-state mutations in source tests')
    if event == 'import' and str(args[0]).startswith('playwright'):
        raise RuntimeError('Real Playwright cannot load in source tests')


def main():
    REPORT.mkdir(parents=True, exist_ok=True)
    before = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in ('control.json', 'control_cmd.txt')}
    sys.addaudithook(audit)
    suite = unittest.TestSuite()
    for pattern in ('test_platform_access.py', 'test_position_truth.py', 'test_scan_safety_integration.py', 'test_ext_recovery.py', 'test_scan_no_input.py'):
        suite.addTests(unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern=pattern))
    with (REPORT / 'tests.txt').open('w', encoding='utf-8') as output:
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    for name in ('BOT5-2-414 .py', 'bot_core/platform_access.py', 'bot_core/position_truth.py'):
        compile((ROOT / name).read_text(encoding='utf-8-sig'), name, 'exec')
    after = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in before}
    record = {'tests': result.testsRun, 'passed': result.wasSuccessful(),
              'control_files_unchanged': before == after, 'live_actions_disabled': True,
              'source_imported_or_bot_restarted': False, 'activation': 'source_only_not_hot_loaded'}
    (REPORT / 'tests.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(json.dumps(record), flush=True)
    if not result.wasSuccessful():
        for test, detail in result.errors + result.failures:
            print(str(test) + '\n' + detail[:2500], flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
