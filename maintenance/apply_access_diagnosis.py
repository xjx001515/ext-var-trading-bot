"""Stage a source patch while leaving the running process and control untouched."""
from pathlib import Path
import ast
import hashlib
import json

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / 'repair_backups/access_diagnosis_20261023'


def main():
    target = ROOT / 'BOT5-2-414 .py'
    truth = ROOT / 'bot_core/position_truth.py'
    BACKUP.mkdir(parents=True, exist_ok=True)
    before = target.read_bytes()
    encoding = 'utf-8-sig' if before.startswith(b'\xef\xbb\xbf') else 'utf-8'
    source = before.decode(encoding).replace('\r\n', '\n')
    newline = '\r\n' if b'\r\n' in before else '\n'
    control_before = (ROOT / 'control.json').read_bytes()
    command_before = (ROOT / 'control_cmd.txt').read_bytes()
    def replace(old, new):
        nonlocal source
        assert source.count(old) == 1, (source.count(old), old[:100])
        source = source.replace(old, new, 1)
    replace('from bot_core.positions_view import table_coverage_verified as _table_coverage_verified',
            'from bot_core.platform_access import inspect_platform_access as _inspect_platform_access\nfrom bot_core.platform_access import snapshot_access_block as _snapshot_access_block\nfrom bot_core.positions_view import table_coverage_verified as _table_coverage_verified')
    replace('def _m2_ext_should_reload_after_zero_rows(', '''def _platform_access_probe(page: Any, platform: str) -> dict:
    try:
        body = _pw_call(f'access_probe.{platform}.body', page.locator, 'body')
        text = _pw_call(f'access_probe.{platform}.text', body.inner_text, timeout=2500)
        return _inspect_platform_access(platform, text)
    except Exception:
        return {'blocked': True, 'reason': str(platform).lower() + '_page_text_unavailable', 'user_action_required': False}

def _m2_ext_should_reload_after_zero_rows(''')
    replace('''        state = _EXT_ZERO_ROWS_RELOAD_STATE if isinstance(_EXT_ZERO_ROWS_RELOAD_STATE, dict) else {"streak": 0.0, "last_reload_ts": 0.0}
        nowv = float(time.time())''', '''        state = _EXT_ZERO_ROWS_RELOAD_STATE if isinstance(_EXT_ZERO_ROWS_RELOAD_STATE, dict) else {"streak": 0.0, "last_reload_ts": 0.0}
        access = _platform_access_probe(page, 'EXT')
        if access.get('blocked'):
            state['streak'] = 0.0
            detail.update(access=access, decision='wait_for_platform_access_no_reload')
            return False, str(access['reason']), detail
        nowv = float(time.time())''')
    replace("        if cur_url and ('extended.exchange' not in cur_url or '/perp/' not in cur_url):", "        if cur_url and not _ext_url_is_perp(cur_url):")
    replace('''    detail = {'platform': plat, 'rows_count': None, 'confirmed_empty': False}
    try:''', '''    detail = {'platform': plat, 'rows_count': None, 'confirmed_empty': False}
    access = _platform_access_probe(page, plat)
    if access.get('blocked'):
        detail['access'] = access
        return False, str(access['reason']), detail
    try:''')
    replace('''def read_ext_equity_balance(page: Any) -> Tuple[Optional[float], str]:
    try:''', '''def read_ext_equity_balance(page: Any) -> Tuple[Optional[float], str]:
    access = _platform_access_probe(page, 'EXT')
    if access.get('blocked'):
        return None, str(access['reason'])
    try:''')
    replace('''        var_page = ensure_page(browser, VAR["url_contains"], VAR["open_url"], bring_to_front=False, do_goto=False)
        try:''', '''        var_page = ensure_page(browser, VAR["url_contains"], VAR["open_url"], bring_to_front=False, do_goto=False)
        snapshot['raw']['platform_access'] = {
            'EXT': _platform_access_probe(ext_page, 'EXT'),
            'VAR': _platform_access_probe(var_page, 'VAR'),
        }
        access_reason = _snapshot_access_block(snapshot)
        if access_reason:
            snapshot.update(data_quality='fail', data_quality_reason=access_reason, dq_reason=access_reason)
            _readiness_set('pos', 'FAIL', access_reason, scope=('global' if _is_global_scan else 'local'), source='platform_access')
            _m2_scan_watch_update('platform_access_wait', 20, '等待平台登录或访问限制解除', reason=access_reason, state='RECOVERING_UI', touch_panel=True)
            progress(logfp, 20, '平台访问受阻，保留持仓记录并等待', reason=access_reason)
            _pw_set_alive(False)
            return snapshot
        try:''')
    truth_before = truth.read_bytes()
    truth_source = truth_before.decode('utf-8').replace('\r\n', '\n')
    old = "    meta = {'wait_only': False, 'fallback_used': False, 'reason_code': 'ok', 'new_risk_blocked': False}\n"
    new = old + '''    from .platform_access import snapshot_access_block
    access_reason = snapshot_access_block(snapshot)
    if access_reason:
        meta.update(wait_only=True, reason_code=access_reason, new_risk_blocked=True,
                    user_action_required=True, last_good_age_sec=None)
        return None, meta
'''
    assert truth_source.count(old) == 1
    truth_source = truth_source.replace(old, new, 1)
    ast.parse(source)
    ast.parse(truth_source)
    for path, data in ((BACKUP / target.name, before), (BACKUP / truth.name, truth_before)):
        if path.exists():
            raise RuntimeError('Refusing to overwrite backup: ' + str(path))
        path.write_bytes(data)
    target.write_bytes(source.replace('\n', newline).encode(encoding))
    truth.write_text(truth_source, encoding='utf-8', newline='\n')
    assert control_before == (ROOT / 'control.json').read_bytes()
    assert command_before == (ROOT / 'control_cmd.txt').read_bytes()
    record = {'source_staged_only': True, 'running_process_restarted': False,
              'control_files_unchanged': True, 'live_activation': 'pending_user_controlled_reload',
              'source_sha256': hashlib.sha256(target.read_bytes()).hexdigest()}
    (BACKUP / 'record.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(json.dumps(record), flush=True)


if __name__ == '__main__':
    main()
