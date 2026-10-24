"""Build patch and integrity evidence without importing either application."""
from pathlib import Path
import difflib
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / 'audit_20261022/safety_refactor'
BACKUP = ROOT / 'repair_backups/safety_refactor_20261022'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    manifest = json.loads((REPORT / 'stages.json').read_text(encoding='utf-8'))
    tests = json.loads((REPORT / 'final_all.json').read_text(encoding='utf-8'))
    assert tests['passed'], 'Final suite must pass before publishing verification'
    assert all(sha(ROOT / name) == value for name, value in manifest['protected_baseline'].items())
    assert json.loads((ROOT / 'control.json').read_text(encoding='utf-8-sig'))['run_state'] == 'stopped'
    syntax = []
    diffs = []
    for name in ('BOT5-2-414 .py', '面板4-11.py'):
        source = (ROOT / name).read_text(encoding='utf-8-sig')
        compile(source, name, 'exec')
        syntax.append(name)
        original = (BACKUP / name).read_text(encoding='utf-8-sig')
        diffs.extend(difflib.unified_diff(original.splitlines(keepends=True), source.splitlines(keepends=True),
                                         fromfile='before/' + name, tofile='after/' + name))
    for path in sorted((ROOT / 'bot_core').glob('*.py')):
        source = path.read_text(encoding='utf-8')
        compile(source, str(path), 'exec')
        syntax.append(path.relative_to(ROOT).as_posix())
        diffs.extend(difflib.unified_diff([], source.splitlines(keepends=True), fromfile='/dev/null',
                                         tofile='after/' + path.relative_to(ROOT).as_posix()))
    (REPORT / 'safety_refactor.patch').write_text(''.join(diffs), encoding='utf-8')
    record = {
        'completed_stages': [stage['stage'] for stage in manifest['stages']],
        'final_tests': tests,
        'syntax_checked_without_execution': syntax,
        'source_sha256': {name: sha(ROOT / name) for name in syntax},
        'protected_runtime_files_unchanged': True,
        'protected_runtime_sha256': manifest['protected_baseline'],
        'trading_run_state': 'stopped',
        'live_exchange_validation': False,
        'policy_preservation': 'AST tests cover active strategy bodies, policy thresholds and the active main entry. Only the missing existing helper was hoisted.',
        'remaining_structure': 'Legacy strategy/UI/compatibility integration remains in the original entry files; seven core responsibilities are separated.',
    }
    extra = REPORT / 'final_python314.json'
    if extra.exists():
        record['actual_runtime_python314_tests'] = json.loads(extra.read_text(encoding='utf-8'))
        assert record['actual_runtime_python314_tests']['passed']
    (REPORT / 'verification.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'tests': tests['tests'], 'syntax_files': len(syntax), 'protected_files': len(manifest['protected_baseline']),
                      'run_state': 'stopped', 'patch_bytes': (REPORT / 'safety_refactor.patch').stat().st_size}), flush=True)


if __name__ == '__main__':
    main()
