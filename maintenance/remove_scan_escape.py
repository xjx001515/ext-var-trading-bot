"""Remove background scan Escape presses without touching the running process."""
from pathlib import Path
import ast
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]


def main():
    path = ROOT / 'BOT5-2-414 .py'
    backup_dir = ROOT / 'repair_backups/login_interruption_20261023'
    backup_dir.mkdir(parents=True, exist_ok=True)
    raw = path.read_bytes()
    encoding = 'utf-8-sig' if raw.startswith(b'\xef\xbb\xbf') else 'utf-8'
    text = raw.decode(encoding)
    newline = '\r\n' if '\r\n' in text else '\n'
    text = text.replace('\r\n', '\n')
    before = '''                            try:
                                _pw_call("m2_scan.ext_escape_1", ext_page.keyboard.press, "Escape")
                                _pw_call("m2_scan.ext_escape_2", ext_page.keyboard.press, "Escape")
                            except _UiDeadError:
                                raise
                            except Exception as _e:
                                _log_exc_level(mod="M2", stage="M2_EXT_ESCAPE_FAIL", level="B", e=_e, plat="EXT", act="reload_recover")
'''
    assert text.count(before) == 1
    text = text.replace(before, '''                            # A background read must not dismiss the user's wallet/login UI.
                            # Recovery authorization is checked by _pw_reload itself.
''', 1)
    ast.parse(text)
    backup = backup_dir / path.name
    if backup.exists():
        raise RuntimeError('Backup already exists; refusing overwrite')
    control_names = ('control.json', 'control_cmd.txt')
    controls = {name: (ROOT / name).read_bytes() for name in control_names}
    backup.write_bytes(raw)
    path.write_bytes(text.replace('\n', newline).encode(encoding))
    assert all((ROOT / name).read_bytes() == value for name, value in controls.items())
    record = {'change': 'remove_unconditional_background_scan_escape',
              'running_process_stopped_or_restarted': False, 'source_only': True,
              'activation': 'not_loaded_into_current_process',
              'control_files_unchanged': True,
              'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    (backup_dir / 'record.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(json.dumps(record), flush=True)


if __name__ == '__main__':
    main()
