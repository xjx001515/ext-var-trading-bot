"""Prepare the persistent read-only entry. This script does not restart anything."""
from pathlib import Path
import ast
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / 'repair_backups/controlled_restart_20261023'


def main():
    path = ROOT / 'BOT5-2-414 .py'
    before = path.read_bytes()
    encoding = 'utf-8-sig' if before.startswith(b'\xef\xbb\xbf') else 'utf-8'
    text = before.decode(encoding)
    newline = '\r\n' if '\r\n' in text else '\n'
    text = text.replace('\r\n', '\n')
    marker = '# PERSISTENT_READ_ONLY_VALIDATION_ENTRY_20261023'
    assert marker not in text, 'Read-only entry already prepared'
    needle = 'from __future__ import annotations\n'
    entry = '''from __future__ import annotations
# PERSISTENT_READ_ONLY_VALIDATION_ENTRY_20261023
from bot_core.read_only_validation import requested as _readonly_requested, run as _readonly_run
if __name__ == '__main__' and _readonly_requested(__file__):
    raise SystemExit(_readonly_run(__file__))
'''
    text = text.replace(needle, entry, 1)
    ast.parse(text)
    BACKUP.mkdir(parents=True, exist_ok=True)
    backup = BACKUP / path.name
    assert not backup.exists(), 'Refusing to overwrite backup'
    backup.write_bytes(before)
    path.write_bytes(text.replace('\n', newline).encode(encoding))
    flag = {'enabled': True, 'mode': 'read_only_validation',
            'reason': 'user_requested_controlled_restart_without_trade_restore',
            'automatic_trade_restore': False}
    (ROOT / 'bot_read_only.json').write_text(json.dumps(flag, indent=2), encoding='utf-8')
    (BACKUP / 'source.json').write_text(json.dumps({'before_sha256': hashlib.sha256(before).hexdigest(),
        'after_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'persistent_read_only': True,
        'trading_process_started': False}, indent=2), encoding='utf-8')
    print('Read-only entry and persistent latch prepared; no process started.', flush=True)


if __name__ == '__main__':
    main()
