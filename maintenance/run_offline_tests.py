"""Offline test runner with real network/process/trading-state writes disabled."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--pattern', default='test_*.py')
    parser.add_argument('--report', default='tests_all.txt')
    args = parser.parse_args()
    report_dir = ROOT / 'audit_20261022/safety_refactor'
    report_dir.mkdir(parents=True, exist_ok=True)
    stages = json.loads((report_dir / 'stages.json').read_text(encoding='utf-8'))
    protected = set(stages['protected_baseline'])

    def digest_state():
        return {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in protected}

    assert digest_state() == stages['protected_baseline'], 'Runtime data changed before tests'
    control = json.loads((ROOT / 'control.json').read_text(encoding='utf-8-sig'))
    assert control.get('run_state') == 'stopped'

    def is_protected(path):
        if not isinstance(path, (str, bytes, os.PathLike)):
            return False
        try:
            relative = Path(os.fsdecode(path)).resolve().relative_to(ROOT).as_posix()
        except (ValueError, OSError):
            return False
        return relative in protected or relative.startswith(('out/', 'logs/', 'rizhi/', 'chrome_profile/', 'edge_profile/'))

    def audit(event, values):
        if event in ('subprocess.Popen', 'os.system', 'socket.connect', 'socket.bind'):
            raise RuntimeError(f'Offline test attempted prohibited operation: {event}')
        if event == 'open':
            path, mode, flags = values
            write = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            if write and is_protected(path):
                raise RuntimeError(f'Offline test attempted trading-state write: {path}')
        if event in ('os.remove', 'os.rename', 'os.rmdir') and any(is_protected(v) for v in values[:2]):
            raise RuntimeError(f'Offline test attempted trading-state mutation: {event}')
        if event == 'import' and str(values[0]).startswith('playwright'):
            raise RuntimeError('Offline tests cannot load real Playwright')

    sys.addaudithook(audit)
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern=args.pattern)
    with (report_dir / args.report).open('w', encoding='utf-8') as report:
        result = unittest.TextTestRunner(stream=report, verbosity=2).run(suite)
    assert digest_state() == stages['protected_baseline'], 'Runtime data changed during tests'
    record = {'pattern': args.pattern, 'tests': result.testsRun,
              'passed': result.wasSuccessful(), 'failures': len(result.failures), 'errors': len(result.errors),
              'protected_state_unchanged': True, 'real_network_and_processes_disabled': True}
    (report_dir / (Path(args.report).stem + '.json')).write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(json.dumps(record), flush=True)
    if not result.wasSuccessful():
        for test, detail in result.errors + result.failures:
            print(str(test) + '\n' + detail[:3500], flush=True)
        print('Full report: ' + str(report_dir / args.report), flush=True)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
