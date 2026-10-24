"""Read-only account observation, isolated from the legacy trading runtime.

No browser input, navigation, reload, order submission, account/ledger write or
automatic transition to trading exists in this module. Importing it is inert.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

from .platform_access import inspect_platform_access
from .symbols import canonical_symbol

LATCH = 'bot_read_only.json'
TZ = timezone(timedelta(hours=8))


def requested(script_path, argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if '--read-only' in args:
        return True
    path = Path(script_path).resolve().parent / LATCH
    if not path.exists():
        return False
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        # A malformed/ambiguous latch never falls through to live trading.
        return not (isinstance(value, dict) and value.get('enabled') is False)
    except Exception:
        return True


def number(text):
    if text is None or isinstance(text, bool):
        return None
    match = re.search(r'[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?', str(text).replace('−', '-'))
    if not match:
        return None
    value = float(match.group().replace(',', ''))
    return value if math.isfinite(value) else None


def labeled_number(text, label):
    lines = [s.strip() for s in str(text or '').splitlines() if s.strip()]
    for index, line in enumerate(lines):
        if line.casefold() == label.casefold():
            if index + 1 < len(lines):
                return number(lines[index + 1])
        elif line.casefold().startswith(label.casefold() + ' '):
            return number(line[len(label):])
    return None


CAPTURE = r'''() => {
    const visible = e => !!e && e.getClientRects().length > 0;
    const tables = Array.from(document.querySelectorAll('table')).filter(visible).map(table => {
        const rows = Array.from(table.querySelectorAll('tr')).filter(visible).map(row => ({
            cells: Array.from(row.querySelectorAll('th,td')).map(cell => cell.innerText.trim()),
            links: Array.from(row.querySelectorAll('a[href]')).map(a => a.getAttribute('href'))
        }));
        const virtualSelector = 'svelte-virtual-list-viewport,[data-virtualizer],[data-virtualized="true"]';
        return {rows, virtualized: !!(table.closest(virtualSelector) || table.querySelector(virtualSelector)),
                busy: table.getAttribute('aria-busy') === 'true'};
    });
    const checked = Array.from(document.querySelectorAll('input[type="radio"]')).filter(e => e.checked);
    const allPositions = checked.some(e => e.value === 'ALL' || e.id.endsWith(':ALL'));
    const walletVisible = Array.from(document.querySelectorAll('button')).filter(visible)
        .some(e => /0x[a-f0-9]{2,}/i.test(e.innerText));
    return {url: location.href, text: document.body ? document.body.innerText : '',
            tables, all_positions_selected: allPositions, wallet_visible: walletVisible};
}'''


def parse_capture(platform, capture):
    capture = capture if isinstance(capture, dict) else {}
    text = str(capture.get('text') or '')
    access = inspect_platform_access(platform, text)
    login = bool(capture.get('wallet_visible')) and access['reason'] != platform.lower() + '_auth_required'
    equity = labeled_number(text, 'Equity' if platform == 'EXT' else 'Portfolio Value') if login else None
    available = labeled_number(text, 'Available To Trade') if login else None
    positions = []
    table_seen = False
    partial = False
    for table in capture.get('tables') or []:
        rows = table.get('rows') or []
        if not rows:
            continue
        headers = [str(v).strip().upper() for v in rows[0].get('cells') or []]
        market_key = 'MARKET' if platform == 'EXT' else 'INSTRUMENT'
        if market_key not in headers or not any('VALUE' in h for h in headers):
            continue
        table_seen = True
        partial = partial or bool(table.get('virtualized') or table.get('busy'))
        for row in rows[1:]:
            cells = row.get('cells') or []
            if len(cells) < len(headers):
                partial = True
                continue
            values = dict(zip(headers, cells))
            coin = canonical_symbol(values.get(market_key))
            if not coin:
                partial = True
                continue
            quantity_text = values.get('QUANTITY')
            quantity = number(quantity_text) if quantity_text is not None else None
            side = str(values.get('SIDE') or '').upper()
            if side not in ('LONG', 'SHORT') and quantity is not None and quantity != 0:
                side = 'LONG' if quantity > 0 else 'SHORT'
            notional_text = next((v for k, v in values.items() if 'VALUE' in k), None)
            if platform == 'EXT' and quantity is None and notional_text:
                amounts = re.findall(r'[-+]?\d[\d,]*(?:\.\d+)?', str(notional_text))
                if len(amounts) >= 2:
                    quantity = number(amounts[1])
            if side not in ('LONG', 'SHORT') or number(notional_text) is None or quantity is None:
                partial = True
            upnl_text = next((v for k, v in values.items() if k.replace('.', '').replace(' ', '') in ('UPNL', 'UNREALIZEDPNL')), None)
            positions.append({'coin': coin, 'platform': platform, 'side': side or 'UNKNOWN',
                              'quantity': quantity, 'notional_u': number(notional_text),
                              'upnl_u': number(upnl_text), 'visible_cells': values})
    empty_hint = any(
        re.fullmatch(r'(?:no positions?|no open positions|you have no (?:open )?positions)\.?',
                     line.strip(), flags=re.IGNORECASE) is not None
        for line in text.splitlines())
    scope_all = platform == 'VAR' or capture.get('all_positions_selected') is True
    empty = login and table_seen and empty_hint and not positions and scope_all and not partial
    positions_ok = login and table_seen and scope_all and not partial and (bool(positions) or empty)
    return {'platform': platform, 'url': str(capture.get('url') or ''),
            'access': access, 'login_verified': login, 'equity': equity,
            'available_to_trade': available, 'funds_readable': equity is not None and available is not None,
            'positions': positions, 'positions_table_seen': table_seen,
            'positions_scope_all': scope_all, 'positions_partial': partial,
            'confirmed_empty': bool(empty), 'positions_readable': bool(positions_ok),
            'scan_ok': bool(login and equity is not None and available is not None and positions_ok and not access['blocked'])}


def choose_control(root):
    choices = []
    for path in (root / 'control.json', root / 'out/control.json'):
        try:
            value = json.loads(path.read_text(encoding='utf-8-sig'))
            if isinstance(value, dict):
                choices.append(value)
        except Exception:
            pass
    return max(choices, key=lambda v: float(v.get('updated_ts') or 0)) if choices else {}


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def combine(platforms, cycle, *, timestamp=None):
    timestamp = time.time() if timestamp is None else timestamp
    ok = all(platforms.get(p, {}).get('scan_ok') is True for p in ('EXT', 'VAR'))
    amounts = [platforms.get(p, {}).get('equity') for p in ('EXT', 'VAR')]
    total = sum(amounts) if all(isinstance(v, (int, float)) and math.isfinite(v) for v in amounts) else None
    return {'ts': timestamp, 'ts_human': datetime.fromtimestamp(timestamp, TZ).strftime('%Y-%m-%d %H:%M:%S'),
            'pid': os.getpid(), 'mode': 'read_only_validation', 'read_only': True,
            'trade_enabled': False, 'cycle': cycle, 'scan_ok': ok,
            'total_equity': total, 'platforms': platforms,
            'automatic_trade_restore': False, 'source': 'existing_browser_visible_dom'}


def publish(root, report):
    # Dedicated observation artifact; never overwrite positions/anchor/grid/order ledgers.
    atomic_json(root / 'out/read_only_validation.json', report)
    stamp = report['ts']
    problems = [p + ':' + str(d.get('access', {}).get('reason') or 'observation_incomplete')
                for p, d in report['platforms'].items() if not d.get('scan_ok')]
    reason = '|'.join(problems) or 'read_only_validation_trade_locked'
    state = {'mainloop_state': 'SCANNING', 'round_phase': 'READ_ONLY_OBSERVE',
             'round_state': 'DONE' if report['scan_ok'] else 'WAITING',
             'unified_state': 'READ_ONLY_OBSERVE', 'ts': stamp,
             'mainloop_state_text': '只读验证，交易关闭', 'msg': reason,
             'reason_code': reason, 'runtime_source': 'read_only_validation'}
    pos = {'status': 'OK' if report['scan_ok'] else 'FAIL', 'reason': reason, 'ts': stamp}
    status = {'ts': stamp, 'pid': os.getpid(), **state, 'runtime_state': state,
              'read_only': True, 'trade_enabled': False, 'last_block_reason': 'read_only_validation',
              'hint': '只读验证进程：未运行策略，交易保持关闭', 'pos_status': pos,
              'scan_stage': 'read_only_observe', 'scan_state': state['round_state'],
              'readiness': {'trade_enabled': False, 'pos_status': pos,
                            'gate_status': {'status': 'FAIL', 'reason': 'read_only_validation', 'ts': stamp}},
              'validation': report}
    atomic_json(root / 'out/status.json', status)
    atomic_json(root / 'out/risk.json', {'ts': stamp, 'read_only': True, 'trade_enabled': False,
                                        'last_block_reason': 'read_only_validation'})
    log = root / 'logs/read_only_validation.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'ts': report['ts_human'], 'cycle': report['cycle'], 'scan_ok': report['scan_ok'],
                                 'reason': reason, 'trade_enabled': False}, ensure_ascii=False) + '\n')


def run(script_path, argv=None):
    parser = argparse.ArgumentParser(description='Read-only validation; cannot submit trades')
    parser.add_argument('--read-only', action='store_true')
    parser.add_argument('--cdp', default='http://127.0.0.1:9222')
    parser.add_argument('--interval', type=float, default=20.0)
    parser.add_argument('--cycles', type=int, default=0)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    address = urlsplit(args.cdp)
    if address.scheme != 'http' or address.hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise SystemExit('Only an existing local browser endpoint is permitted')
    root = Path(script_path).resolve().parent
    from playwright.sync_api import sync_playwright
    import msvcrt
    lock_path = root / 'out/read_only_validation_process.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open('a+b')
    lock_file.seek(0)
    if not lock_file.read(1):
        lock_file.write(b'0')
        lock_file.flush()
    lock_file.seek(0)
    try:
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print('Read-only validator is already active.', flush=True)
        return 2
    cycle = 0
    print('[READ_ONLY] Trading runtime bypassed; no inputs, orders, ledger resets or automatic restore.', flush=True)
    try:
        with sync_playwright() as playwright:
            browser = None
            while True:
                control = choose_control(root)
                if str(control.get('cmd') or '').lower() in ('exit', 'quit', 'stop') or control.get('exit') is True:
                    break
                if str(control.get('run_state') or '').lower() == 'paused':
                    time.sleep(1)
                    continue
                cycle += 1
                platforms = {}
                try:
                    if browser is None or not browser.is_connected():
                        browser = playwright.chromium.connect_over_cdp(args.cdp, timeout=10000)
                    pages = [page for context in browser.contexts for page in context.pages if not page.is_closed()]
                    for platform, host in (('EXT', 'app.extended.exchange'), ('VAR', 'omni.variational.io')):
                        candidates = []
                        for page in pages:
                            if urlsplit(page.url).hostname != host:
                                continue
                            capture = page.evaluate(CAPTURE)
                            candidates.append(parse_capture(platform, capture))
                        platforms[platform] = max(candidates, key=lambda d: (d['login_verified'], d['scan_ok'], d['funds_readable'])) if candidates else {
                            'platform': platform, 'scan_ok': False, 'login_verified': False,
                            'equity': None, 'access': {'blocked': True, 'reason': 'existing_tab_not_found'}}
                except Exception as exc:
                    platforms['ERROR'] = {'scan_ok': False, 'access': {'blocked': True, 'reason': type(exc).__name__ + ':' + str(exc)[:250]}}
                    browser = None
                report = combine(platforms, cycle)
                publish(root, report)
                print(f"[READ_ONLY] cycle={cycle} scan_ok={report['scan_ok']} total={report['total_equity']} trade_enabled=False", flush=True)
                if args.cycles > 0 and cycle >= args.cycles:
                    break
                deadline = time.monotonic() + max(5.0, args.interval)
                while time.monotonic() < deadline:
                    if str(choose_control(root).get('cmd') or '').lower() in ('exit', 'quit', 'stop'):
                        break
                    time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    finally:
        # Disconnecting Playwright does not close user-owned browser tabs.
        lock_file.close()
    return 0
