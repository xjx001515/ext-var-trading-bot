"""Apply one reviewed stage to stopped source files, never start the app."""
from pathlib import Path
import argparse
import ast
import hashlib
import json
import runpy

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / 'repair_backups/safety_refactor_20261022'
REPORT = ROOT / 'audit_20261022/safety_refactor'
PROTECTED = (
    'control.json', 'control_cmd.txt', '面板4-11.config.json',
    'out/positions.json', 'out/equity.json', 'out/equity_live.json',
    'out/state/anchor_state.json', 'out/state/coin_pool_state.json',
    'out/state/m9_wangge_zhangben.json', 'out/executor_results.jsonl',
    'out/trades.jsonl', 'grid_rate_pause_ledger.json', 'out/panel_profit_daily.json',
)


def hashes():
    return {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in PROTECTED if (ROOT / p).exists()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['positions', 'positions_review', 'truth_bridge', 'worker', 'worker_followup', 'execution', 'execution_review', 'panel'])
    args = parser.parse_args()
    control = json.loads((ROOT / 'control.json').read_text(encoding='utf-8-sig'))
    if control.get('run_state') != 'stopped' or control.get('start'):
        raise SystemExit('Refusing to edit while control state is not stopped')
    REPORT.mkdir(parents=True, exist_ok=True)
    manifest = REPORT / 'stages.json'
    state = json.loads(manifest.read_text(encoding='utf-8')) if manifest.exists() else {'protected_baseline': hashes(), 'stages': []}
    assert args.stage not in [s['stage'] for s in state['stages']], 'Stage already applied'
    assert hashes() == state['protected_baseline'], 'Protected runtime files changed; inspect before proceeding'
    target = ROOT / ('面板4-11.py' if args.stage == 'panel' else 'BOT5-2-414 .py')
    before = target.read_bytes()
    encoding = 'utf-8-sig' if before.startswith(b'\xef\xbb\xbf') else 'utf-8'
    source = before.decode(encoding)
    newline = '\r\n' if '\r\n' in source else '\n'
    apply = runpy.run_path(str(BACKUP / f'{args.stage}_edits.py'))['apply']
    updated = apply(source.replace('\r\n', '\n'))
    ast.parse(updated, filename=str(target))
    after = updated.replace('\n', newline).encode(encoding)
    target.write_bytes(after)
    assert hashes() == state['protected_baseline'], 'Protected runtime files changed during edit'
    state['stages'].append({'stage': args.stage, 'target': target.name,
                            'before_sha256': hashlib.sha256(before).hexdigest(),
                            'after_sha256': hashlib.sha256(after).hexdigest(), 'syntax_ok': True})
    manifest.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'{args.stage}: source updated; syntax valid; protected runtime files unchanged.', flush=True)


if __name__ == '__main__':
    main()
