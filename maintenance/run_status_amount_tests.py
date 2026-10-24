"""Run repair regressions without process launches, network or live state writes."""
from pathlib import Path
import hashlib
import json
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from maintenance.run_account_audit_tests import guard


def main():
    report_dir = ROOT / 'audit_20261023/status_amount_fix'
    report_dir.mkdir(parents=True, exist_ok=True)
    names = ('control.json', 'out/control.json', 'control_cmd.txt', 'out/control_cmd.txt')
    before = {n: hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names}
    sys.addaudithook(guard)
    patterns = sys.argv[1:] or ['test_status_amount_fix.py']
    suite = unittest.TestSuite()
    for pattern in patterns:
        suite.addTests(unittest.defaultTestLoader.discover(str(ROOT/'tests'), pattern=pattern))
    with (report_dir/'tests.txt').open('w', encoding='utf-8') as output:
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(suite)
    for name in ('BOT5-2-414 .py', 'bot_core/panel_state.py'):
        compile((ROOT/name).read_text(encoding='utf-8-sig'), name, 'exec')
    after = {n: hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names}
    report = {'tests': result.testsRun, 'passed': result.wasSuccessful(),
              'live_actions_disabled': True, 'control_files_unchanged': before == after,
              'main_program_not_imported': True, 'patterns': patterns}
    (report_dir/'tests.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report), flush=True)
    for case, error in result.errors + result.failures:
        print(str(case) + '\n' + error[:3500], flush=True)
    if not result.wasSuccessful():
        raise SystemExit(1)


if __name__ == '__main__':
    main()
