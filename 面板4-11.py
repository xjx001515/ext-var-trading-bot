
# -*- coding: utf-8 -*-
"""
local_panel_desktop_v2_7_tk.py

本地桌面面板（Tkinter（内置）| 零三方依赖）

本版改动：
1) 模块状态改为横向排列；其下方空出的空间放“全量日志”
2) 修复“没数据”的核心痛点：增加自动探测数据目录（out/logs/TEMP 回退）、并在界面展示数据状态+原因+实际读取路径
3) 保留：顶部两条队列横幅、摘要、M1 运行情况、扫描时间、本次/下次扫描、小挂件、提示音、去黑框、控制文件写入多路径兼容

数据源（自动探测）：
- out/equity.json
- out/positions.json
- out/trades.jsonl
- logs/BOT_grid_full.jsonl（或 TEMP 回退路径 / 最近日志）
- out/bot_error.txt（可选）

控制输出（多路径）：
- out/control.json / control.json / %TEMP%/bot_panel_bridge/out/control.json
"""

from __future__ import annotations

import os
import sys
import json
import time
import re
import hashlib
import socket
from collections import deque, Counter, defaultdict
import tempfile
import subprocess
import webbrowser
from dataclasses import dataclass, asdict
from pathlib import Path
from datetime import datetime, date, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
import copy
from bot_core import panel_state as _panel_state

# Word 日报输出不再依赖 python-docx，避免用户本机缺模块直接秒退。
# 如环境已安装 python-docx，也不强依赖，统一走内置无依赖 docx 生成器。
from zipfile import ZipFile, ZIP_DEFLATED
from xml.sax.saxutils import escape as _xml_escape

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
except Exception as e:
    raise RuntimeError(f"tkinter 不可用：{e}")


_JSON_FILE_CACHE: Dict[Tuple[str, str], Dict[str, Any]] = {}


def _json_cache_get(fp: Optional[Path], *, want: str = "any") -> Optional[object]:
    try:
        if not fp:
            return None
        fp = Path(fp)
        if (not fp.exists()) or fp.stat().st_size <= 0:
            return None
        st = fp.stat()
        key = (str(fp), str(want))
        ent = _JSON_FILE_CACHE.get(key)
        if not isinstance(ent, dict):
            return None
        if int(ent.get("size", -1)) != int(st.st_size):
            return None
        if int(ent.get("mtime_ns", -1)) != int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))):
            return None
        obj = ent.get("obj")
        return copy.deepcopy(obj)
    except Exception:
        return None


def _json_cache_put(fp: Optional[Path], obj: object, *, want: str = "any") -> None:
    try:
        if not fp:
            return
        fp = Path(fp)
        st = fp.stat()
        key = (str(fp), str(want))
        _JSON_FILE_CACHE[key] = {
            "size": int(st.st_size),
            "mtime_ns": int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
            "obj": copy.deepcopy(obj),
        }
        if len(_JSON_FILE_CACHE) > 64:
            for k in list(_JSON_FILE_CACHE.keys())[:16]:
                _JSON_FILE_CACHE.pop(k, None)
    except Exception:
        return


def _read_json_safe(fp: Optional[Path]) -> dict:
    """读取 JSON（JavaScript Object Notation，结构化文本）文件，失败返回空 dict。"""
    if not fp:
        return {}
    try:
        cached = _json_cache_get(fp, want="dict")
        if isinstance(cached, dict):
            return cached
        if (not fp.exists()) or fp.stat().st_size <= 0:
            return {}
        txt = fp.read_text(encoding="utf-8", errors="ignore").strip()
        if not txt:
            return {}
        obj = json.loads(txt)
        if isinstance(obj, dict):
            _json_cache_put(fp, obj, want="dict")
            return obj
        return {}
    except Exception:
        return {}


APP_NAME = "北境 BOT 面板 v3.0.2"
ANCHOR_U = 2000.0
REFRESH_MS = 1500
STALE_SEC = 1200
PANEL_CONFIG_FP = Path(__file__).with_suffix(".config.json")

# 历史真实成交摘要：只保留最近少量记录，避免 Tk Treeview 和 action_rows 越积越大后长期运行变卡。
SUMMARY_MAX_ROWS = 200
SUMMARY_PARSE_WINDOW = 120
SUMMARY_SIG_MAX = 240
ACTION_ROWS_MAX_KEEP = 200
LOG_MAX_ROUNDS_KEEP = 10
LOG_TAIL_FALLBACK_LINES = 1200

MACHINE_DEBUG_LOG_BASENAME = "machine_debug_full.jsonl"
HUMAN_OBSERVE_LOG_BASENAME = "system_observe.log"
GRID_DATA_BASENAME = "grid_local_ledger.json"


REPORT_TZ = timezone(timedelta(hours=8))
DAILY_REPORT_DIRNAME = "日报"
DAILY_REPORT_WINDOW_HOURS = 24
DAILY_REPORT_MAX_ITEMS = 12


def _fmt_report_dt(ts: Any) -> str:
    try:
        v = _parse_ts_sec_any(ts)
        if v <= 0:
            return "-"
        return datetime.fromtimestamp(v, REPORT_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "-"


def _iter_jsonl_rows(fp: Optional[Path]):
    try:
        if (not fp) or (not Path(fp).exists()) or Path(fp).stat().st_size <= 0:
            return
        with Path(fp).open('r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                s = str(line or '').strip()
                if not s or (not s.startswith('{')):
                    continue
                try:
                    obj = json.loads(s)
                    if isinstance(obj, dict):
                        yield obj
                except Exception:
                    continue
    except Exception:
        return


def _pick_row_ts(row: Dict[str, Any]) -> float:
    try:
        for key in ('ts', 'timestamp', 'time_ts', 't', 'time', 'datetime', 'dt', 'created_at', 'updated_at'):
            ts = _parse_ts_sec_any(row.get(key))
            if ts > 0:
                return float(ts)
        req = row.get('req') or {}
        if isinstance(req, dict):
            for key in ('ts', 'timestamp', 'time', 't'):
                ts = _parse_ts_sec_any(req.get(key))
                if ts > 0:
                    return float(ts)
            params = req.get('params') or {}
            if isinstance(params, dict):
                for key in ('ts', 'timestamp', 'time', 'submit_ts', 'ts_submit'):
                    ts = _parse_ts_sec_any(params.get(key))
                    if ts > 0:
                        return float(ts)
    except Exception:
        pass
    return 0.0


def _report_first_nonempty(*vals: Any) -> str:
    for v in vals:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ''


def _normalize_report_reason(reason: str) -> str:
    try:
        s = str(reason or '').strip()
        if not s:
            return ''
        s = re.sub(r'EXECUTOR_worker_timeout:\d+(?:\.\d+)?s', 'EXECUTOR_worker_timeout', s, flags=re.I)
        s = re.sub(r'\breq[_-]?id[:=][^|,;\s]+', 'req_id=*', s, flags=re.I)
        s = re.sub(r'\btrace[_-]?id[:=][^|,;\s]+', 'trace_id=*', s, flags=re.I)
        s = re.sub(r'\s+', ' ', s).strip()
        return s
    except Exception:
        return str(reason or '').strip()


def _report_pick_side(row: Dict[str, Any]) -> str:
    try:
        side = str(_report_first_nonempty(row.get('side'), row.get('side_hint'), row.get('dir'), row.get('direction'))).strip().lower()
        if side in ('buy', 'long', '多', '做多'):
            return 'LONG'
        if side in ('sell', 'short', '空', '做空'):
            return 'SHORT'
        return '-'
    except Exception:
        return '-'


def _report_pick_action(row: Dict[str, Any]) -> str:
    a = str(_report_first_nonempty(row.get('action_type'), row.get('action'), row.get('exec_action'), '-')).strip().lower()
    if any(k in a for k in ('open', 'increase', 'add', 'reopen')):
        return '开仓'
    if any(k in a for k in ('close', 'decrease', 'reduce', 'take_profit')):
        return '平仓'
    return a or '-'


def _report_pick_amount(row: Dict[str, Any]) -> float:
    try:
        return float(abs(_extract_summary_notional_u(row) or 0.0))
    except Exception:
        return _safe_float(row.get('amount_u'), _safe_float(row.get('notional_u'), _safe_float(row.get('amount_usd'), 0.0)))


def _report_severity_from_reason(reason: str) -> str:
    s = str(reason or '').lower()
    if ('timeout' in s) or ('internal_reason_missing' in s) or ('unknown:no_reason_trace' in s) or ('traceback' in s) or ('exception' in s):
        return '高'
    if ('conflict' in s) or ('reject' in s) or ('失败' in s) or ('报错' in s):
        return '中'
    return '低'


def _humanize_report_reason(reason: str) -> str:
    try:
        s = str(reason or '').strip()
        if not s:
            return '未记录具体原因'
        sl = s.lower()
        rules = [
            ('executor_worker_timeout', '执行器超时，页面操作在限定时间内未完成'),
            ('timeout', '执行超时，流程未在限定时间内完成'),
            ('internal_reason_missing', '结果链缺失，日志未留下最终失败原因'),
            ('unknown:no_reason_trace', '原因链断裂，未追到明确失败节点'),
            ('platform_leg_side_conflict', '平台、腿或方向不一致，请求与执行结果冲突'),
            ('conflict', '执行冲突，同一对象存在互斥动作或状态不一致'),
            ('reject', '请求被门禁或执行器拒绝，未进入有效执行'),
            ('postcheck', '复检未通过，下单后未确认到预期结果'),
            ('captcha', '页面出现验证或风控拦截，导致流程中断'),
            ('about:blank', '页面跳转异常，落在空白页或目标页未正常加载'),
            ('selector', '页面元素未找到，界面结构与脚本预期不一致'),
            ('ui', '页面状态异常，导致读取或点击失败'),
            ('traceback', '脚本报错，触发异常堆栈'),
            ('exception', '脚本抛出异常，流程被中断'),
            ('m9_increase_fail', '网格加仓未成功，预期加仓动作未落地'),
            ('decrease_tp_close_disabled', '网格平仓被策略门禁关闭，止盈请求未放行'),
        ]
        for k, v in rules:
            if k in sl:
                return v
        return s
    except Exception:
        return str(reason or '').strip() or '未记录具体原因'


def _report_anomaly_subject(item: Dict[str, Any]) -> str:
    try:
        parts = []
        for key in ('coin', 'platform', 'module'):
            v = str(item.get(key) or '').strip()
            if v and v != '-':
                parts.append(v)
        return '/'.join(parts) if parts else '-'
    except Exception:
        return '-'


def _load_json_safe_for_report(fp: Optional[Path]) -> Dict[str, Any]:
    try:
        if not fp:
            return {}
        p = Path(fp)
        if (not p.exists()) or p.stat().st_size <= 0:
            return {}
        return json.loads(p.read_text(encoding='utf-8', errors='ignore').lstrip('\ufeff'))
    except Exception:
        return {}


def _build_daily_report(out_dir: Optional[Path], logs_dir: Optional[Path], window_hours: int = DAILY_REPORT_WINDOW_HOURS) -> Dict[str, Any]:
    now_ts = time.time()
    datasets: Dict[str, List[Dict[str, Any]]] = {}
    candidates: List[Tuple[str, Path]] = []
    seen_paths = set()

    def _add(src: str, fp: Optional[Path]) -> None:
        try:
            if not fp:
                return
            p = Path(fp)
            if (not p.exists()) or p.stat().st_size <= 0:
                return
            ps = str(p.resolve())
            if ps in seen_paths:
                return
            seen_paths.add(ps)
            candidates.append((src, p))
        except Exception:
            return

    od = Path(out_dir) if out_dir else None
    ld = Path(logs_dir) if logs_dir else None
    if od:
        for name in ('trades.jsonl', 'executor_actions.jsonl', 'executor_results.jsonl', 'telemetry.jsonl', 'arbitration_audit.jsonl', 'trace_events.jsonl'):
            _add(name, od / name)
        for name in (HUMAN_OBSERVE_LOG_BASENAME, 'bot_console.log', 'bot_console.txt', 'panel_spawn_stdout.txt'):
            _add(name, od / name)
    if ld:
        for name in ('executor_actions.jsonl', 'executor_results.jsonl', 'telemetry.jsonl', 'arbitration_audit.jsonl', 'trace_events.jsonl', 'trades.jsonl', MACHINE_DEBUG_LOG_BASENAME, 'BOT_grid_full.jsonl', HUMAN_OBSERVE_LOG_BASENAME, 'bot_console.log', 'bot_console.txt'):
            _add(name, ld / name)

    latest_ts = 0.0
    for src, fp in candidates:
        if src.endswith('.jsonl'):
            rows = []
            for row in _iter_jsonl_rows(fp):
                ts = _pick_row_ts(row)
                if ts > 0:
                    latest_ts = max(latest_ts, ts)
                rows.append(row)
            datasets[src] = rows
    if latest_ts <= 0:
        latest_ts = now_ts
    start_ts = latest_ts - max(1, int(window_hours)) * 3600

    def _in_win(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out = []
        for row in rows:
            ts = _pick_row_ts(row)
            if ts <= 0:
                continue
            if start_ts <= ts <= latest_ts:
                r = dict(row)
                r['_ts'] = ts
                out.append(r)
        return out

    trades = _in_win(datasets.get('trades.jsonl', []))
    exec_actions = _in_win(datasets.get('executor_actions.jsonl', []))
    exec_results = _in_win(datasets.get('executor_results.jsonl', []))
    telemetry = _in_win(datasets.get('telemetry.jsonl', []))
    arbitration = _in_win(datasets.get('arbitration_audit.jsonl', []))
    trace_events = _in_win(datasets.get('trace_events.jsonl', []))

    risk = _load_json_safe_for_report((od / 'risk.json') if od else None)
    equity = _load_json_safe_for_report((od / 'equity.json') if od else None)
    positions = _load_json_safe_for_report((od / 'positions.json') if od else None)

    trade_seen = set()
    trade_rows = []
    trade_count = 0
    realized_pnl = 0.0
    realized_trade_count = 0
    open_u = 0.0
    close_u = 0.0
    module_pnl = defaultdict(float)
    coin_pnl = defaultdict(float)
    platform_pnl = defaultdict(float)
    side_pnl = defaultdict(float)
    module_count = Counter()
    coin_count = Counter()
    platform_count = Counter()
    side_count = Counter()
    large_trade_events = []

    for row in sorted(trades, key=lambda x: float(x.get('_ts') or 0.0), reverse=True):
        ts = float(row.get('_ts') or 0.0)
        coin = (_report_first_nonempty(row.get('coin'), row.get('symbol'), '-') or '-').upper()
        plat = _norm_platform(_report_first_nonempty(row.get('platform'), row.get('plat'), '-'))
        module = _report_first_nonempty(row.get('module'), row.get('req_mod'), row.get('source_module'), '-') or '-'
        action_cn = _report_pick_action(row)
        side = _report_pick_side(row)
        amount = _report_pick_amount(row)
        try:
            pnl = float(row.get('realized_pnl_u')) if row.get('realized_pnl_u') not in (None, '') else None
        except Exception:
            pnl = None
        sig = _report_first_nonempty(row.get('trade_id'), row.get('req_id'), row.get('trace_id'))
        if not sig:
            sig = f"{int(ts)}|{coin}|{plat}|{module}|{action_cn}|{side}|{round(float(amount or 0.0), 2)}"
        if sig in trade_seen:
            continue
        trade_seen.add(sig)
        trade_count += 1
        trade_rows.append({'ts': ts, 'coin': coin, 'platform': plat, 'module': module, 'action': action_cn, 'side': side, 'amount': float(amount or 0.0), 'pnl': pnl})
        coin_count[coin] += 1
        module_count[module] += 1
        platform_count[plat] += 1
        side_count[side] += 1
        amt = float(amount or 0.0)
        if action_cn == '开仓':
            open_u += amt
        elif action_cn == '平仓':
            close_u += amt
        if pnl is not None:
            realized_pnl += float(pnl)
            realized_trade_count += 1
            module_pnl[module] += float(pnl)
            coin_pnl[coin] += float(pnl)
            platform_pnl[plat] += float(pnl)
            side_pnl[side] += float(pnl)
        if amt >= 300 or abs(float(pnl or 0.0)) >= 50:
            large_trade_events.append({'rank': abs(float(pnl or 0.0)) * 25.0 + amt, 'ts': ts, 'source': 'trades.jsonl', 'title': f'大额成交 | {coin} | {plat} | {module}', 'detail': f'动作={action_cn} 方向={side} 金额={_fmt_u(amt)}' + (f' 已实现={float(pnl):+.2f}U' if pnl is not None else ''), 'kind': 'trade'})

    fail_groups: Dict[str, Dict[str, Any]] = {}
    def _add_fail(source: str, row: Dict[str, Any], is_fail: bool) -> None:
        if not is_fail:
            return
        ts = float(row.get('_ts') or 0.0)
        coin = (_report_first_nonempty(row.get('coin'), row.get('symbol'), '-') or '-').upper()
        plat = _norm_platform(_report_first_nonempty(row.get('platform'), row.get('plat'), '-'))
        module = _report_first_nonempty(row.get('module'), row.get('req_mod'), row.get('source_module'), '-') or '-'
        reason = _normalize_report_reason(_report_first_nonempty(row.get('reason'), row.get('postcheck_reason'), row.get('gate_reason'), row.get('reject_reason'), row.get('error'), row.get('message'), row.get('msg')))
        if (not reason) or (reason in ('placeholder_event', 'postcheck_closed_ok')) or reason.endswith('_ok'):
            return
        key = f"{source}|{module}|{coin}|{plat}|{reason}"
        g = fail_groups.setdefault(key, {'count': 0, 'first_ts': ts, 'last_ts': ts, 'source': source, 'module': module, 'coin': coin, 'platform': plat, 'reason': reason})
        g['count'] += 1
        g['first_ts'] = min(float(g.get('first_ts') or ts), ts)
        g['last_ts'] = max(float(g.get('last_ts') or ts), ts)

    for row in exec_results:
        _add_fail('executor_results.jsonl', row, row.get('ok') in (False, 0, 'false', 'False'))
    for row in exec_actions:
        _add_fail('executor_actions.jsonl', row, row.get('ok') in (False, 0, 'false', 'False'))
    for row in arbitration:
        _add_fail('arbitration_audit.jsonl', row, row.get('accepted') in (False, 0, 'false', 'False'))
    for source_name, rows in (('telemetry.jsonl', telemetry), ('trace_events.jsonl', trace_events)):
        for row in rows:
            msg = _report_first_nonempty(row.get('msg'), row.get('message'), row.get('text'))
            low = msg.lower()
            _add_fail(source_name, row, ('timeout' in low) or ('error' in low) or ('exception' in low) or ('失败' in msg) or ('报错' in msg))

    fail_items = []
    fail_reason_count = Counter()
    for g in fail_groups.values():
        score = 1000 + g['count'] * 40
        rs = str(g['reason'])
        if 'timeout' in rs.lower():
            score += 180
        if 'internal_reason_missing' in rs.lower() or 'unknown:no_reason_trace' in rs.lower():
            score += 220
        if 'platform_leg_side_conflict' in rs.lower():
            score += 160
        fail_reason_count[rs] += g['count']
        fail_items.append({'rank': score, 'ts': float(g['last_ts'] or 0.0), 'source': g['source'], 'title': f"异常 | {g['module']} | {g['coin']} | {g['platform']}", 'detail': f"原因={g['reason']} | 重复={g['count']}次 | 首次={_fmt_report_dt(g['first_ts'])}", 'kind': 'fail', 'severity': _report_severity_from_reason(g['reason']), 'first_ts': g['first_ts'], 'last_ts': g['last_ts'], 'count': g['count'], 'module': g['module'], 'coin': g['coin'], 'platform': g['platform'], 'reason': g['reason']})

    round_ids = set()
    console_events = []
    console_paths = []
    if od:
        for name in (HUMAN_OBSERVE_LOG_BASENAME, 'bot_console.log', 'bot_console.txt', 'panel_spawn_stdout.txt'):
            p = od / name
            if p.exists() and p.stat().st_size > 0:
                console_paths.append((name, p))
    if ld:
        for name in (HUMAN_OBSERVE_LOG_BASENAME, 'bot_console.log', 'bot_console.txt', MACHINE_DEBUG_LOG_BASENAME, 'BOT_grid_full.jsonl'):
            p = ld / name
            if p.exists() and p.stat().st_size > 0:
                console_paths.append((name, p))
    seen_console = set()
    for src, fp in console_paths:
        try:
            with fp.open('r', encoding='utf-8', errors='ignore') as f:
                for raw in f:
                    line = str(raw or '').strip()
                    if not line:
                        continue
                    ts = 0.0
                    m = re.search(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', line)
                    if m:
                        try:
                            ts = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S').replace(tzinfo=REPORT_TZ).timestamp()
                        except Exception:
                            ts = 0.0
                    else:
                        m2 = re.search(r'^\[(\d{2}:\d{2}:\d{2})\]', line)
                        if m2:
                            try:
                                base_day = datetime.fromtimestamp(latest_ts, REPORT_TZ).strftime('%Y-%m-%d')
                                ts = datetime.strptime(f'{base_day} {m2.group(1)}', '%Y-%m-%d %H:%M:%S').replace(tzinfo=REPORT_TZ).timestamp()
                            except Exception:
                                ts = 0.0
                    if ts <= 0 or ts < start_ts or ts > latest_ts:
                        continue
                    m_round = re.search(r'第(\d+)轮开始', line)
                    if m_round:
                        round_ids.add(m_round.group(1))
                    low = line.lower()
                    important = False
                    score = 0
                    title = ''
                    detail = ''
                    if '风险状态' in line and ('正常' not in line):
                        important = True; score = 900; title = '风险状态变化'; detail = line
                    elif '全平' in line or '双平' in line:
                        important = True; score = 850; title = '关键平仓动作'; detail = line
                    elif 'timeout' in low or '超时' in line:
                        important = True; score = 1080; title = '执行超时'; detail = line
                    elif '[err]' in low or 'traceback' in low or ' exception' in low or '报错' in line or '失败' in line:
                        important = True; score = 1020; title = '控制台异常'; detail = line
                    if not important:
                        continue
                    sig = f"{src}|{int(ts)}|{title}|{_normalize_report_reason(detail)}"
                    if sig in seen_console:
                        continue
                    seen_console.add(sig)
                    console_events.append({'rank': score, 'ts': ts, 'source': src, 'title': title, 'detail': detail[:220], 'kind': 'console'})
        except Exception:
            continue

    top_anomalies = sorted(fail_items, key=lambda x: (float(x.get('rank') or 0.0), float(x.get('last_ts') or 0.0)), reverse=True)[:8]
    all_items = large_trade_events + top_anomalies + console_events
    all_items.sort(key=lambda x: (float(x.get('rank') or 0.0), float(x.get('ts') or 0.0)), reverse=True)
    top_items = all_items[:DAILY_REPORT_MAX_ITEMS]

    net_exposure = _safe_float(risk.get('net_exposure_u'), _safe_float(risk.get('net_u'), _safe_float(risk.get('delta_v_minus_e'), 0.0)))
    gross_exposure = _safe_float(risk.get('gross_exposure_u'), _safe_float(risk.get('gross_u'), _safe_float(risk.get('holding_total_u'), 0.0)))
    ext_exposure = _safe_float(risk.get('ext_holding_u'), _safe_float(risk.get('e_holding_u'), 0.0))
    var_exposure = _safe_float(risk.get('var_holding_u'), _safe_float(risk.get('v_holding_u'), 0.0))
    total_equity = _safe_float(equity.get('total'), _safe_float(equity.get('total_usd'), _safe_float(equity.get('t'), 0.0)))

    pos_rows = []
    raw_positions = positions.get('positions') if isinstance(positions, dict) and isinstance(positions.get('positions'), list) else (positions.get('items') if isinstance(positions, dict) and isinstance(positions.get('items'), list) else [])
    for p in raw_positions:
        if not isinstance(p, dict):
            continue
        coin = (_report_first_nonempty(p.get('coin'), p.get('symbol'), '-') or '-').upper()
        platform = _norm_platform(_report_first_nonempty(p.get('platform'), p.get('plat'), '-'))
        side = _report_pick_side(p)
        hold_u = _safe_float(p.get('holding_u'), _safe_float(p.get('value_u'), _safe_float(p.get('notional_u'), 0.0)))
        pnl_u = _safe_float(p.get('pnl_u'), _safe_float(p.get('unrealized_pnl_u'), 0.0))
        if abs(hold_u) <= 0 and abs(pnl_u) <= 0:
            continue
        pos_rows.append({'coin': coin, 'platform': platform, 'side': side, 'holding_u': hold_u, 'pnl_u': pnl_u})
    exposure_by_coin = defaultdict(float)
    imbalance_by_coin = defaultdict(float)
    for p in pos_rows:
        exposure_by_coin[p['coin']] += abs(float(p['holding_u'] or 0.0))
        sign = 1.0 if p['side'] == 'LONG' else -1.0 if p['side'] == 'SHORT' else 0.0
        imbalance_by_coin[p['coin']] += sign * abs(float(p['holding_u'] or 0.0))
    top_exposure_coins = sorted(exposure_by_coin.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    top_imbalance_coins = sorted(imbalance_by_coin.items(), key=lambda kv: (-abs(kv[1]), kv[0]))[:5]

    exec_total = len(exec_actions) if exec_actions else len(exec_results)
    exec_success = 0
    durations = []
    for row in exec_results:
        if row.get('ok') in (True, 1, 'true', 'True'):
            exec_success += 1
        d = _safe_float(row.get('elapsed_s'), _safe_float(row.get('cost_s'), _safe_float(row.get('duration_s'), -1.0)))
        if d >= 0:
            durations.append(float(d))
    timeout_count = sum(1 for x in fail_items if 'timeout' in str(x.get('reason') or '').lower())
    reject_count = sum(1 for x in fail_items if ('reject' in str(x.get('reason') or '').lower()) or ('conflict' in str(x.get('reason') or '').lower()))
    avg_dur = (sum(durations) / len(durations)) if durations else 0.0
    p95_dur = sorted(durations)[max(0, min(len(durations) - 1, int(round((len(durations) - 1) * 0.95))))] if durations else 0.0

    pnl_gain_top = sorted(((k, v) for k, v in coin_pnl.items() if v > 0), key=lambda kv: (-kv[1], kv[0]))[:5]
    pnl_drag_top = sorted(((k, v) for k, v in coin_pnl.items() if v < 0), key=lambda kv: (kv[1], kv[0]))[:5]

    risk_state = _report_first_nonempty(risk.get('state'), risk.get('risk_state'), risk.get('status'), '正常')
    if top_anomalies and any(x.get('severity') == '高' for x in top_anomalies):
        risk_brief = '执行风险偏高'
    elif abs(net_exposure) > max(300.0, gross_exposure * 0.25 if gross_exposure > 0 else 300.0):
        risk_brief = '净敞口偏移'
    else:
        risk_brief = '风险整体可控'

    insights = [f"近24小时共识别 {trade_count} 笔成交，已实现盈亏 {realized_pnl:+.2f}U，开仓额 {open_u:.2f}U，平仓额 {close_u:.2f}U。"]
    if pnl_gain_top:
        c, v = pnl_gain_top[0]
        insights.append(f"盈利贡献最高币种为 {c}，近24小时已实现贡献 {v:+.2f}U。")
    if pnl_drag_top:
        c, v = pnl_drag_top[0]
        insights.append(f"主要拖累币种为 {c}，近24小时已实现贡献 {v:+.2f}U。")
    if top_anomalies:
        a0 = top_anomalies[0]
        insights.append(f"当前最重要异常为“{a0['reason']}”，重复 {a0['count']} 次，来源 {a0['source']}。")
    if top_imbalance_coins:
        c, v = top_imbalance_coins[0]
        insights.append(f"当前结构最不平衡币种为 {c}，名义偏移 {abs(v):.2f}U，方向 {'偏多' if v > 0 else '偏空'}。")

    report = {
        'generated_at': now_ts,
        'start_ts': start_ts,
        'end_ts': latest_ts,
        'sources': sorted({x[0] for x in candidates}),
        'summary': {
            'trade_count': trade_count,
            'realized_pnl_u': round(realized_pnl, 6),
            'realized_trade_count': realized_trade_count,
            'open_u': round(open_u, 2),
            'close_u': round(close_u, 2),
            'round_count': len(round_ids),
            'dedup_fail_kind_count': len(fail_items),
            'risk_state': risk_state,
            'risk_brief': risk_brief,
            'gross_exposure_u': round(gross_exposure, 2),
            'net_exposure_u': round(net_exposure, 2),
            'ext_exposure_u': round(ext_exposure, 2),
            'var_exposure_u': round(var_exposure, 2),
            'total_equity_u': round(total_equity, 2),
            'execution_total': exec_total,
            'execution_success': exec_success,
            'execution_success_rate': round((exec_success / exec_total * 100.0), 2) if exec_total else 0.0,
            'avg_exec_s': round(avg_dur, 2),
            'p95_exec_s': round(p95_dur, 2),
            'timeout_count': timeout_count,
            'reject_count': reject_count,
        },
        'executive_insights': insights[:5],
        'pnl_attribution': {
            'by_module': [{'name': k, 'pnl_u': round(v, 2), 'count': module_count.get(k, 0)} for k, v in sorted(module_pnl.items(), key=lambda kv: (-abs(kv[1]), kv[0]))[:8]],
            'by_coin': [{'name': k, 'pnl_u': round(v, 2), 'count': coin_count.get(k, 0)} for k, v in sorted(coin_pnl.items(), key=lambda kv: (-abs(kv[1]), kv[0]))[:8]],
            'by_platform': [{'name': k, 'pnl_u': round(v, 2), 'count': platform_count.get(k, 0)} for k, v in sorted(platform_pnl.items(), key=lambda kv: (-abs(kv[1]), kv[0]))[:4]],
            'by_side': [{'name': k, 'pnl_u': round(v, 2), 'count': side_count.get(k, 0)} for k, v in sorted(side_pnl.items(), key=lambda kv: (-abs(kv[1]), kv[0]))[:4]],
            'top_gainers': [{'name': k, 'pnl_u': round(v, 2)} for k, v in pnl_gain_top],
            'top_drags': [{'name': k, 'pnl_u': round(v, 2)} for k, v in pnl_drag_top],
        },
        'risk_exposure': {
            'top_exposure_coins': [{'name': k, 'holding_u': round(v, 2)} for k, v in top_exposure_coins],
            'top_imbalance_coins': [{'name': k, 'imbalance_u': round(v, 2)} for k, v in top_imbalance_coins],
            'position_count': len(pos_rows),
        },
        'execution_quality': {
            'avg_exec_s': round(avg_dur, 2),
            'p95_exec_s': round(p95_dur, 2),
            'timeout_count': timeout_count,
            'reject_count': reject_count,
            'top_fail_reasons': [{'reason': k, 'count': v} for k, v in fail_reason_count.most_common(8)],
        },
        'top_anomalies': top_anomalies,
        'top_events': top_items,
        'timeline': sorted(all_items, key=lambda x: float(x.get('ts') or 0.0), reverse=True)[:20],
        'positions_snapshot': pos_rows[:40],
        'markdown': '',
    }
    report['markdown'] = _build_daily_report_preview_text(report)
    return report


def _build_daily_report_preview_text(report: Dict[str, Any]) -> str:
    s = report.get('summary') or {}
    lines = ['近24小时量化专业版日报（预览）', '']
    lines.append(f"统计区间：{_fmt_report_dt(report.get('start_ts'))} ~ {_fmt_report_dt(report.get('end_ts'))}")
    lines.append(f"生成时间：{_fmt_report_dt(report.get('generated_at'))}")
    lines.append(f"风险结论：{s.get('risk_brief', '-')}")
    lines.append('')
    lines.append('一、执行摘要')
    lines.append(f"1. 近24小时累计成交 {s.get('trade_count', 0)} 笔，已实现盈亏 {float(s.get('realized_pnl_u') or 0.0):+.2f}U，执行成功率 {float(s.get('execution_success_rate') or 0.0):.2f}% 。")
    lines.append(f"2. 当前总敞口 {float(s.get('gross_exposure_u') or 0.0):.2f}U，净敞口 {float(s.get('net_exposure_u') or 0.0):+.2f}U；EXT {float(s.get('ext_exposure_u') or 0.0):.2f}U，VAR {float(s.get('var_exposure_u') or 0.0):.2f}U。")
    for idx, item in enumerate(report.get('executive_insights') or [], 3):
        lines.append(f"{idx}. {item}")
    lines.append('')
    lines.append('二、重点异常')
    top_anomalies = report.get('top_anomalies') or []
    if top_anomalies:
        for item in top_anomalies[:5]:
            lines.append(
                f"- [{item.get('severity', '-')}] {_humanize_report_reason(item.get('reason'))} | 对象={_report_anomaly_subject(item)} | 重复={item.get('count', 0)}次 | 来源={item.get('source', '-')} | 时间={_fmt_report_dt(item.get('last_ts'))}"
            )
    else:
        lines.append('- 无重大异常')
    lines.append('')
    lines.append('三、关键事件')
    for item in report.get('top_events') or []:
        lines.append(f"- {_fmt_report_dt(item.get('ts'))} | {item.get('title')} | 来源={item.get('source')} | {item.get('detail')}")
    return '\n'.join(lines).strip() + '\n'


def _set_east_asia_font(*args, **kwargs) -> None:
    return None


def _set_cell_text(*args, **kwargs) -> None:
    return None


def _set_table_borders(*args, **kwargs) -> None:
    return None


def _make_table(*args, **kwargs):
    """兼容旧调用口径；无依赖版 docx 生成不直接使用这个函数。"""
    return None


def _init_gov_docx_styles(*args, **kwargs) -> None:
    """兼容旧调用口径；无依赖版 docx 生成不直接使用这个函数。"""
    return None


def _docx_escape(s: Any) -> str:
    return _xml_escape(str(s if s is not None else ''))


def _docx_paragraph_xml(text: str, *, align: str = 'left', bold: bool = False, size_half_pt: int = 32,
                        east_asia_font: str = '仿宋_GB2312', first_line_chars: int = 0,
                        before_twips: int = 0, after_twips: int = 0) -> str:
    jc = {'left': 'left', 'center': 'center', 'right': 'right'}.get(str(align).lower(), 'left')
    ind = f'<w:ind w:firstLineChars="{int(first_line_chars)}"/>' if first_line_chars else ''
    rpr = (
        f'<w:rPr>'
        f'<w:rFonts w:ascii="{_docx_escape(east_asia_font)}" w:hAnsi="{_docx_escape(east_asia_font)}" w:eastAsia="{_docx_escape(east_asia_font)}"/>'
        f'<w:sz w:val="{int(size_half_pt)}"/><w:szCs w:val="{int(size_half_pt)}"/>'
        f'{"<w:b/>" if bold else ""}'
        f'</w:rPr>'
    )
    return (
        f'<w:p><w:pPr><w:jc w:val="{jc}"/><w:spacing w:before="{int(before_twips)}" w:after="{int(after_twips)}" w:line="360" w:lineRule="auto"/>{ind}</w:pPr>'
        f'<w:r>{rpr}<w:t xml:space="preserve">{_docx_escape(text)}</w:t></w:r></w:p>'
    )


def _docx_table_xml(headers: List[str], rows: List[List[str]], col_widths_cm: Optional[List[float]] = None) -> str:
    if not headers:
        headers = ['-']
    cols = len(headers)
    if not rows:
        rows = [['-'] * cols]
    if col_widths_cm and len(col_widths_cm) == cols:
        widths = [max(600, int(w * 567)) for w in col_widths_cm]
    else:
        widths = [int(9000 / cols)] * cols
    grid = ''.join(f'<w:gridCol w:w="{w}"/>' for w in widths)

    def cell(text: Any, width: int, *, bold: bool = False, align: str = 'left', font='仿宋_GB2312') -> str:
        jc = {'left': 'left', 'center': 'center', 'right': 'right'}.get(str(align).lower(), 'left')
        return (
            f'<w:tc><w:tcPr><w:tcW w:w="{int(width)}" w:type="dxa"/><w:vAlign w:val="center"/></w:tcPr>'
            f'<w:p><w:pPr><w:jc w:val="{jc}"/><w:spacing w:before="40" w:after="40" w:line="300" w:lineRule="auto"/></w:pPr>'
            f'<w:r><w:rPr><w:rFonts w:ascii="{font}" w:hAnsi="{font}" w:eastAsia="{font}"/><w:sz w:val="24"/><w:szCs w:val="24"/>{"<w:b/>" if bold else ""}</w:rPr>'
            f'<w:t xml:space="preserve">{_docx_escape(text)}</w:t></w:r></w:p></w:tc>'
        )

    def row_xml(vals: List[Any], *, header=False) -> str:
        tds = []
        for i in range(cols):
            val = vals[i] if i < len(vals) else ''
            align = 'center' if i == 0 else 'left'
            sval = str(val)
            if isinstance(val, (int, float)) or re.fullmatch(r'[+\-]?\d+(?:\.\d+)?(?:U|%|s)?', sval or ''):
                align = 'right' if i != 0 else 'center'
            tds.append(cell(sval, widths[i], bold=header, align=align, font='黑体' if header else '仿宋_GB2312'))
        return f'<w:tr>{"".join(tds)}</w:tr>'

    borders = (
      '<w:tblBorders>'
      '<w:top w:val="single" w:sz="6" w:space="0" w:color="888888"/>'
      '<w:left w:val="single" w:sz="6" w:space="0" w:color="888888"/>'
      '<w:bottom w:val="single" w:sz="6" w:space="0" w:color="888888"/>'
      '<w:right w:val="single" w:sz="6" w:space="0" w:color="888888"/>'
      '<w:insideH w:val="single" w:sz="6" w:space="0" w:color="BBBBBB"/>'
      '<w:insideV w:val="single" w:sz="6" w:space="0" w:color="BBBBBB"/>'
      '</w:tblBorders>'
    )
    return (
        f'<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/><w:jc w:val="center"/>{borders}'
        f'<w:tblLook w:val="04A0" w:firstRow="1" w:lastRow="0" w:firstColumn="0" w:lastColumn="0" w:noHBand="0" w:noVBand="1"/></w:tblPr>'
        f'<w:tblGrid>{grid}</w:tblGrid>{row_xml(headers, header=True)}{"".join(row_xml(r) for r in rows)}</w:tbl>'
    )


def _build_daily_report_doc_xml(report: Dict[str, Any]) -> str:
    s = report.get('summary') or {}
    pa = report.get('pnl_attribution') or {}
    rx = report.get('risk_exposure') or {}
    eq = report.get('execution_quality') or {}
    anomalies = report.get('top_anomalies') or []
    timeline = report.get('top_events') or []

    parts: List[str] = []
    parts.append(_docx_paragraph_xml('策略交易运行日报', align='center', bold=True, size_half_pt=44, east_asia_font='方正小标宋简体', after_twips=120))
    parts.append(_docx_paragraph_xml('（近24小时量化专业版）', align='center', size_half_pt=32, east_asia_font='楷体_GB2312', after_twips=180))
    meta_txt = (
        f"报告周期：{_fmt_report_dt(report.get('start_ts'))} 至 {_fmt_report_dt(report.get('end_ts'))}\n"
        f"生成时间：{_fmt_report_dt(report.get('generated_at'))}\n"
        f"日志来源：{'、'.join(report.get('sources') or []) or '-'}"
    )
    for line in meta_txt.split('\n'):
        parts.append(_docx_paragraph_xml(line, size_half_pt=32, first_line_chars=0, after_twips=20))

    def heading(txt: str):
        parts.append(_docx_paragraph_xml(txt, bold=True, size_half_pt=32, east_asia_font='黑体', first_line_chars=0, before_twips=80, after_twips=40))

    heading('一、执行摘要')
    summary_lines = [
        f"（一）近24小时累计成交 {s.get('trade_count', 0)} 笔，已实现盈亏 {float(s.get('realized_pnl_u') or 0.0):+.2f}U，执行成功率 {float(s.get('execution_success_rate') or 0.0):.2f}%。",
        f"（二）当前风险状态为{s.get('risk_state', '-') or '-'}，总敞口 {float(s.get('gross_exposure_u') or 0.0):.2f}U，净敞口 {float(s.get('net_exposure_u') or 0.0):+.2f}U。",
    ]
    summary_lines.extend(report.get('executive_insights') or [])
    for line in summary_lines[:6]:
        parts.append(_docx_paragraph_xml(line, size_half_pt=32, first_line_chars=200))

    heading('二、核心指标概览')
    parts.append(_docx_table_xml(['指标', '数值', '指标', '数值'], [
        ['已实现盈亏', f"{float(s.get('realized_pnl_u') or 0.0):+.2f}U", '成交笔数', str(s.get('trade_count', 0))],
        ['开仓额', f"{float(s.get('open_u') or 0.0):.2f}U", '平仓额', f"{float(s.get('close_u') or 0.0):.2f}U"],
        ['总敞口', f"{float(s.get('gross_exposure_u') or 0.0):.2f}U", '净敞口', f"{float(s.get('net_exposure_u') or 0.0):+.2f}U"],
        ['EXT敞口', f"{float(s.get('ext_exposure_u') or 0.0):.2f}U", 'VAR敞口', f"{float(s.get('var_exposure_u') or 0.0):.2f}U"],
        ['平均执行耗时', f"{float(s.get('avg_exec_s') or 0.0):.2f}s", 'P95执行耗时', f"{float(s.get('p95_exec_s') or 0.0):.2f}s"],
    ], [3.1, 3.7, 3.1, 3.7]))

    heading('三、收益归因')
    parts.append(_docx_paragraph_xml('（一）模块归因', first_line_chars=0, size_half_pt=32))
    parts.append(_docx_table_xml(['模块', '已实现盈亏(U)', '成交笔数'],
        [[x.get('name','-'), f"{float(x.get('pnl_u') or 0.0):+.2f}", str(x.get('count',0))] for x in (pa.get('by_module') or [])[:6]] or [['-', '0.00', '0']],
        [4.6, 3.2, 2.6]))
    parts.append(_docx_paragraph_xml('', first_line_chars=0, size_half_pt=10))
    parts.append(_docx_paragraph_xml('（二）币种归因', first_line_chars=0, size_half_pt=32))
    parts.append(_docx_table_xml(['币种', '已实现盈亏(U)', '成交笔数'],
        [[x.get('name','-'), f"{float(x.get('pnl_u') or 0.0):+.2f}", str(x.get('count',0))] for x in (pa.get('by_coin') or [])[:6]] or [['-', '0.00', '0']],
        [3.6, 3.8, 3.0]))
    by_platform = pa.get('by_platform') or []
    by_side = pa.get('by_side') or []
    combo_rows = []
    top_n = max(len(by_platform), len(by_side), 1)
    for i in range(top_n):
        combo_rows.append([
            by_platform[i].get('name','-') if i < len(by_platform) else '-',
            f"{float(by_platform[i].get('pnl_u') or 0.0):+.2f}" if i < len(by_platform) else '0.00',
            by_side[i].get('name','-') if i < len(by_side) else '-',
            f"{float(by_side[i].get('pnl_u') or 0.0):+.2f}" if i < len(by_side) else '0.00',
        ])
    parts.append(_docx_paragraph_xml('', first_line_chars=0, size_half_pt=10))
    parts.append(_docx_paragraph_xml('（三）平台及方向归因', first_line_chars=0, size_half_pt=32))
    parts.append(_docx_table_xml(['平台', '盈亏(U)', '方向', '盈亏(U)'], combo_rows, [3.0, 3.0, 3.0, 3.0]))
    gainers = pa.get('top_gainers') or []
    drags = pa.get('top_drags') or []
    rank_rows = []
    rank_n = max(len(gainers), len(drags), 1)
    for i in range(rank_n):
        rank_rows.append([
            gainers[i].get('name','-') if i < len(gainers) else '-',
            f"{float(gainers[i].get('pnl_u') or 0.0):+.2f}" if i < len(gainers) else '0.00',
            drags[i].get('name','-') if i < len(drags) else '-',
            f"{float(drags[i].get('pnl_u') or 0.0):+.2f}" if i < len(drags) else '0.00',
        ])
    parts.append(_docx_paragraph_xml('', first_line_chars=0, size_half_pt=10))
    parts.append(_docx_paragraph_xml('（四）收益前列与拖累前列', first_line_chars=0, size_half_pt=32))
    parts.append(_docx_table_xml(['收益前列', '盈亏(U)', '拖累前列', '盈亏(U)'], rank_rows, [3.0, 3.0, 3.0, 3.0]))

    heading('四、风险与敞口')
    parts.append(_docx_paragraph_xml(
        f"当前账户总权益 {float(s.get('total_equity_u') or 0.0):.2f}U，持仓条目 {int(rx.get('position_count') or 0)} 个；总敞口 {float(s.get('gross_exposure_u') or 0.0):.2f}U，净敞口 {float(s.get('net_exposure_u') or 0.0):+.2f}U。",
        size_half_pt=32, first_line_chars=200))
    exp = rx.get('top_exposure_coins') or []
    imb = rx.get('top_imbalance_coins') or []
    rows = []
    n = max(len(exp), len(imb), 1)
    for i in range(n):
        rows.append([
            exp[i].get('name','-') if i < len(exp) else '-',
            f"{float(exp[i].get('holding_u',0.0)):.2f}" if i < len(exp) else '0.00',
            imb[i].get('name','-') if i < len(imb) else '-',
            f"{float(imb[i].get('imbalance_u',0.0)):+.2f}" if i < len(imb) else '0.00',
        ])
    parts.append(_docx_table_xml(['敞口排名币种', '名义敞口(U)', '结构失衡币种', '偏移(U)'], rows, [3.8, 2.8, 3.8, 2.8]))

    heading('五、执行质量')
    q_rows = [
        ['执行总次数', str(s.get('execution_total', 0)), '执行成功次数', str(s.get('execution_success', 0))],
        ['执行成功率', f"{float(s.get('execution_success_rate') or 0.0):.2f}%", '平均执行耗时', f"{float(eq.get('avg_exec_s') or 0.0):.2f}s"],
        ['P95执行耗时', f"{float(eq.get('p95_exec_s') or 0.0):.2f}s", '超时次数', str(eq.get('timeout_count', 0))],
        ['拒绝/冲突次数', str(eq.get('reject_count', 0)), '异常类别数', str(s.get('dedup_fail_kind_count', 0))],
    ]
    parts.append(_docx_table_xml(['指标', '数值', '指标', '数值'], q_rows, [3.0, 3.0, 3.0, 3.0]))
    fail_rows = [[_humanize_report_reason(x.get('reason')), str(x.get('count', 0))] for x in (eq.get('top_fail_reasons') or [])[:6]]
    if fail_rows:
        parts.append(_docx_paragraph_xml('', first_line_chars=0, size_half_pt=10))
        parts.append(_docx_paragraph_xml('主要失败原因分布', first_line_chars=0, size_half_pt=32))
        parts.append(_docx_table_xml(['失败原因', '次数'], fail_rows, [8.5, 3.5]))

    heading('六、异常与告警（按重要性排序）')
    an_rows = []
    for x in anomalies[:8]:
        an_rows.append([
            x.get('severity','-'),
            _report_anomaly_subject(x),
            str(x.get('count',0)),
            x.get('source','-'),
            _fmt_report_dt(x.get('last_ts')),
        ])
    parts.append(_docx_table_xml(['等级', '影响对象', '重复', '来源', '最后时间'], an_rows or [['-', '-', '0', '-', '-']], [1.4, 3.0, 1.2, 3.0, 3.4]))
    for idx, x in enumerate(anomalies[:5], 1):
        parts.append(_docx_paragraph_xml(
            f"{idx}. {_humanize_report_reason(x.get('reason'))}。影响对象：{_report_anomaly_subject(x)}；首次时间：{_fmt_report_dt(x.get('first_ts'))}；最后时间：{_fmt_report_dt(x.get('last_ts'))}；来源：{x.get('source','-')}。",
            size_half_pt=32, first_line_chars=0))

    heading('七、关键事件时间线')
    tl_rows = [[_fmt_report_dt(x.get('ts')), x.get('title','-')[:22], x.get('source','-'), str(x.get('detail','-'))[:40]] for x in timeline[:10]]
    parts.append(_docx_table_xml(['时间', '事件', '来源', '说明'], tl_rows or [['-', '无', '-', '-']], [3.0, 3.2, 2.2, 3.6]))

    body = ''.join(parts)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:wpc="http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas" '
        'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
        'xmlns:o="urn:schemas-microsoft-com:office:office" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
        'xmlns:v="urn:schemas-microsoft-com:vml" '
        'xmlns:wp14="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:w10="urn:schemas-microsoft-com:office:word" '
        'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
        'xmlns:wpg="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup" '
        'xmlns:wpi="http://schemas.microsoft.com/office/word/2010/wordprocessingInk" '
        'xmlns:wne="http://schemas.microsoft.com/office/word/2006/wordml" '
        'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
        'mc:Ignorable="w14 wp14">'
        f'<w:body>{body}'
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="2098" w:right="1474" w:bottom="1984" w:left="1587" w:header="851" w:footer="992" w:gutter="0"/>'
        '<w:docGrid w:type="lines" w:linePitch="312"/></w:sectPr></w:body></w:document>'
    )


def _write_daily_report_docx(docx_fp: Path, report: Dict[str, Any]) -> Path:
    doc_xml = _build_daily_report_doc_xml(report)
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>'''
    rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>'''
    doc_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"></Relationships>'''
    now_iso = datetime.now(REPORT_TZ).strftime('%Y-%m-%dT%H:%M:%SZ')
    core = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:dcterms="http://purl.org/dc/terms/"
 xmlns:dcmitype="http://purl.org/dc/dcmitype/"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>策略交易运行日报</dc:title>
  <dc:creator>ChatGPT</dc:creator>
  <cp:lastModifiedBy>ChatGPT</cp:lastModifiedBy>
  <dcterms:created xsi:type="dcterms:W3CDTF">{now_iso}</dcterms:created>
  <dcterms:modified xsi:type="dcterms:W3CDTF">{now_iso}</dcterms:modified>
</cp:coreProperties>'''
    app = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
 xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Microsoft Office Word</Application>
</Properties>'''
    docx_fp.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(docx_fp, 'w', ZIP_DEFLATED) as zf:
        zf.writestr('[Content_Types].xml', content_types)
        zf.writestr('_rels/.rels', rels)
        zf.writestr('docProps/core.xml', core)
        zf.writestr('docProps/app.xml', app)
        zf.writestr('word/document.xml', doc_xml)
        zf.writestr('word/_rels/document.xml.rels', doc_rels)
    return docx_fp
# 资金字段候选（兼容）
E_KEY_CANDIDATES = ["e", "ext_usd", "ext_equity", "ext_equity_usd", "e_usd", "equity_ext", "ext", "EXT"]
V_KEY_CANDIDATES = ["v", "var_usd", "var_equity", "var_equity_usd", "v_usd", "equity_var", "var", "VAR"]
T_KEY_CANDIDATES = ["total", "total_usd", "t", "TOTAL", "all_usd", "sum_usd"]


def _hide_console_windows() -> None:
    """去掉黑框：Windows 隐藏控制台。"""
    try:
        if os.name != "nt":
            return
        import ctypes
        whnd = ctypes.windll.kernel32.GetConsoleWindow()
        if whnd:
            ctypes.windll.user32.ShowWindow(whnd, 0)  # SW_HIDE
    except Exception:
        pass


_SINGLE_INSTANCE_SOCK = None  # type: ignore

def _acquire_single_instance(port: int = 48658) -> bool:
    """单实例锁：通过本地端口独占，防止同一台电脑多开面板。

    - 成功：返回 True，并持有 socket 直到进程退出
    - 失败：返回 False
    """
    global _SINGLE_INSTANCE_SOCK
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # 不设置 SO_REUSEADDR：Windows 下会削弱独占效果
        s.bind(("127.0.0.1", int(port)))
        s.listen(1)
        _SINGLE_INSTANCE_SOCK = s
        return True
    except Exception:
        try:
            if _SINGLE_INSTANCE_SOCK:
                _SINGLE_INSTANCE_SOCK.close()
        except Exception:
            pass
        _SINGLE_INSTANCE_SOCK = None
        return False


def _msgbox_nt(title: str, text: str) -> None:
    """无 Tk 的兜底弹窗（Windows）。"""
    try:
        if os.name != "nt":
            return
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(text), str(title), 0x10)  # MB_ICONERROR
    except Exception:
        pass



def _win_beep(kind: str) -> None:
    """提示音：order / error 两种（Windows 内置 winsound）。"""
    if os.name != "nt":
        return
    try:
        import winsound
        if kind == "error":
            for _ in range(2):
                winsound.Beep(220, 260)
                time.sleep(0.06)
        else:
            winsound.Beep(880, 90)
            time.sleep(0.05)
            winsound.Beep(880, 90)
    except Exception:
        pass


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or isinstance(x, bool):
            return float(default)
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip().replace(",", "")
        if not s:
            return float(default)
        neg_by_bracket = ("(" in s and ")" in s)
        m = re.search(r"[-+]?(?:\d+\.\d+|\d+)", s.replace("−", "-"))
        if not m:
            return float(default)
        v = float(m.group(0))
        head = s.lstrip()
        if m.group(0) and m.group(0)[0] not in "+-" and head.startswith("-"):
            v = -abs(v)
        if neg_by_bracket:
            v = -abs(v)
        return float(v)
    except Exception:
        return float(default)


def _parse_ts_sec_any(v: Any) -> float:
    """把各种可能的时间字段解析成“秒级 epoch(float)”。

    支持：
    - 秒/毫秒/微秒 epoch（int/float/纯数字字符串）
    - 'YYYY-MM-DD HH:MM:SS'
    - ISO: 'YYYY-MM-DDTHH:MM:SSZ' / '+08:00' 等
    - 仅 'HH:MM:SS'（会用本机当天日期拼接；若提供 date/day 字段请在调用侧优先传入）
    """
    try:
        if v is None:
            return 0.0

        # 数字/数字字符串：秒/毫秒/微秒
        if isinstance(v, (int, float)) and (not isinstance(v, bool)):
            ts = float(v)
        else:
            s = str(v).strip()
            if not s:
                return 0.0
            if re.fullmatch(r"\d+(?:\.\d+)?", s):
                ts = float(s)
            else:
                # ISO
                if "T" in s and ("Z" in s or "+" in s or "-" in s[10:]):
                    ss = s.replace("Z", "+00:00")
                    try:
                        return float(datetime.fromisoformat(ss).timestamp())
                    except Exception:
                        pass
                # YYYY-MM-DD HH:MM:SS
                try:
                    return float(datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").timestamp())
                except Exception:
                    pass
                # HH:MM:SS
                if re.fullmatch(r"\d{2}:\d{2}:\d{2}", s[:8]):
                    today = datetime.now().strftime("%Y-%m-%d")
                    try:
                        return float(datetime.strptime(f"{today} {s[:8]}", "%Y-%m-%d %H:%M:%S").timestamp())
                    except Exception:
                        return 0.0
                return 0.0

        # 归一化单位
        if ts >= 1e15:  # 微秒
            ts = ts / 1e6
        elif ts >= 1e12:  # 毫秒
            ts = ts / 1e3
        return float(ts) if ts > 0 else 0.0
    except Exception:
        return 0.0


# -------------------------
# 指标（RSI/ADX）“当时值”缓存：用于摘要逐笔展示
# - 优先：从 logs/full_all.jsonl 或 logs/BOT_grid_full.jsonl 等里解析 “指标刷新 XXX” 事件（带 ts/rsi/adx）
# - 回退：out/ind_cache.json（每币最近一次指标）
# 说明：摘要要求展示“执行器操作当时”的指标，因此这里按 coin+ts 做“就近<=ts”的查找
# -------------------------

_IND_OFFSETS: Dict[str, int] = {}
_IND_FILE_SIG: Dict[str, Tuple[int, float]] = {}   # path -> (size, mtime)
_IND_HIST: Dict[str, List[Tuple[float, float, float]]] = {}  # coin -> [(ts,rsi,adx),...]
_IND_CACHE_LAST: Dict[str, Dict[str, Any]] = {}  # coin -> {rsi, adx, ts}
_IND_LAST_LOAD_AT: float = 0.0
_IND_CACHE_JSON_SIG: Tuple[int, float, str] = (0, 0.0, "")
_IND_CACHE_JSON_COINS: Dict[str, Dict[str, Any]] = {}


def _extract_coin_from_msg(msg: str) -> str:
    try:
        if not msg:
            return ""
        s = str(msg).upper()
        # 常见：指标刷新 ADA
        m = re.search(r"\b([A-Z]{2,10})\b\s*$", s)
        if m:
            return m.group(1)
        # 兜底：取最后一个大写 token
        toks = re.findall(r"[A-Z]{2,10}", s)
        return toks[-1] if toks else ""
    except Exception:
        return ""


def _ingest_ind_from_jsonl(path: Path, max_add: int = 5000) -> None:
    """增量读取指标来源，更新 _IND_HIST / _IND_CACHE_LAST。

    兼容来源：
    - 指标刷新日志（rsi/adx）
    - 成交/执行 jsonl（ind_rsi/ind_adx、嵌套 req/meta/params）
    - out/trades.jsonl / executor_results.jsonl / full_all.jsonl / BOT_grid_full.jsonl
    """
    try:
        p = Path(path)
        if (not p.exists()) or p.stat().st_size <= 0:
            return

        size = int(p.stat().st_size)
        mtime = float(p.stat().st_mtime)
        sig = (size, mtime)
        ps = str(p)

        last_sig = _IND_FILE_SIG.get(ps)
        if last_sig and size < last_sig[0]:
            _IND_OFFSETS[ps] = 0

        off = int(_IND_OFFSETS.get(ps, 0) or 0)
        if off < 0 or off > size:
            off = 0

        def _pick_ts(d: dict) -> float:
            for k in ("ts", "timestamp", "time_ts", "t", "time", "datetime", "dt"):
                ts0 = _parse_ts_sec_any(d.get(k))
                if ts0 > 0:
                    return float(ts0)
            req = d.get("req") or {}
            if isinstance(req, dict):
                for k in ("ts", "timestamp", "time", "t"):
                    ts0 = _parse_ts_sec_any(req.get(k))
                    if ts0 > 0:
                        return float(ts0)
                p0 = req.get("params") or {}
                if isinstance(p0, dict):
                    for k in ("ts", "timestamp", "time", "ts_submit", "submit_ts"):
                        ts0 = _parse_ts_sec_any(p0.get(k))
                        if ts0 > 0:
                            return float(ts0)
            return 0.0

        def _pick_coin(d: dict) -> str:
            for k in ("coin", "symbol", "asset"):
                v = d.get(k)
                if v:
                    return str(v).upper().strip()
            req = d.get("req") or {}
            if isinstance(req, dict):
                for k in ("coin", "symbol", "asset"):
                    v = req.get(k)
                    if v:
                        return str(v).upper().strip()
                p0 = req.get("params") or {}
                if isinstance(p0, dict):
                    for k in ("coin", "symbol", "asset"):
                        v = p0.get(k)
                        if v:
                            return str(v).upper().strip()
            return _extract_coin_from_msg(d.get("msg") or d.get("message") or d.get("text") or "")

        added = 0
        with p.open("rb") as f:
            f.seek(off, 0)
            for raw in f:
                if added >= max_add:
                    break
                try:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("{"):
                        continue
                    d = json.loads(line)
                    if not isinstance(d, dict):
                        continue
                    coin = _pick_coin(d)
                    if not coin:
                        continue
                    ts = _pick_ts(d)
                    if ts <= 0:
                        continue

                    rsi = _safe_float(d.get("rsi"), float("nan"))
                    adx = _safe_float(d.get("adx"), float("nan"))
                    if not (rsi == rsi and adx == adx):
                        try:
                            rsi2, adx2 = _extract_rsi_adx_from_row(d, coin, ts)
                        except Exception:
                            rsi2, adx2 = (None, None)
                        rsi = float(rsi2) if (rsi2 is not None) else float("nan")
                        adx = float(adx2) if (adx2 is not None) else float("nan")
                    if not (rsi == rsi and adx == adx):
                        continue

                    _IND_HIST.setdefault(coin, []).append((ts, float(rsi), float(adx)))
                    _IND_CACHE_LAST[coin] = {"rsi": float(rsi), "adx": float(adx), "ts": float(ts)}
                    added += 1
                except Exception:
                    continue

            try:
                _IND_OFFSETS[ps] = int(f.tell())
            except Exception:
                _IND_OFFSETS[ps] = size

        _IND_FILE_SIG[ps] = sig

        for c, lst in list(_IND_HIST.items()):
            try:
                if not lst:
                    continue
                lst.sort(key=lambda x: x[0])
                if len(lst) > 2000:
                    _IND_HIST[c] = lst[-2000:]
                last = _IND_HIST[c][-1]
                _IND_CACHE_LAST[c] = {"rsi": float(last[1]), "adx": float(last[2]), "ts": float(last[0])}
            except Exception:
                continue
    except Exception:
        return


def _load_ind_cache_json(out_dir: Optional[Path]) -> None:
    """读取 out/ind_cache.json 作为兜底。"""
    try:
        if not out_dir:
            return
        fp = Path(out_dir) / "ind_cache.json"
        if (not fp.exists()) or fp.stat().st_size <= 0:
            return
        obj = json.loads(fp.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff"))
        coins = obj.get("coins") if isinstance(obj, dict) else None
        if not isinstance(coins, dict):
            return
        for coin, v in coins.items():
            if not isinstance(v, dict):
                continue
            c = str(coin).upper().strip()
            rsi = _safe_float(v.get("rsi"), float("nan"))
            adx = _safe_float(v.get("adx"), float("nan"))
            ts = _parse_ts_sec_any(v.get("ts"))
            if rsi == rsi and adx == adx:
                _IND_CACHE_LAST[c] = {"rsi": float(rsi), "adx": float(adx), "ts": float(ts or 0.0)}
    except Exception:
        return


def _read_latest_ind_cache_coin(out_dir: Optional[Path], coin: str) -> Tuple[Optional[float], Optional[float]]:
    """直接读取 out/ind_cache.json 里的最新指标。"""
    global _IND_CACHE_JSON_SIG, _IND_CACHE_JSON_COINS
    try:
        c = str(coin or '').upper().strip()
        if (not out_dir) or (not c):
            return (None, None)
        fp = Path(out_dir) / 'ind_cache.json'
        if (not fp.exists()) or fp.stat().st_size <= 0:
            return (None, None)
        sig = (int(fp.stat().st_size), float(fp.stat().st_mtime), str(fp))
        if sig != _IND_CACHE_JSON_SIG:
            obj = json.loads(fp.read_text(encoding='utf-8', errors='replace').lstrip('\ufeff'))
            coins = (obj.get('coins') if isinstance(obj, dict) else None) or {}
            data = {}
            if isinstance(coins, dict):
                for k, v in coins.items():
                    if isinstance(v, dict):
                        data[str(k).upper().strip()] = v
            _IND_CACHE_JSON_COINS = data
            _IND_CACHE_JSON_SIG = sig
        v = _IND_CACHE_JSON_COINS.get(c) or {}
        if isinstance(v, dict):
            rsi = v.get('rsi')
            adx = v.get('adx')
            if (rsi is not None) and (adx is not None):
                try:
                    return (float(rsi), float(adx))
                except Exception:
                    pass
    except Exception:
        pass
    return (None, None)


def _lookup_latest_rsi_adx(coin: str) -> Tuple[Optional[float], Optional[float]]:
    """返回某币种最新 RSI/ADX（用于多空比列表）。"""
    try:
        c = str(coin or '').upper().strip()
        if not c:
            return (None, None)
        v = _IND_CACHE_LAST.get(c)
        if isinstance(v, dict):
            rsi = v.get('rsi')
            adx = v.get('adx')
            if rsi is not None and adx is not None:
                return (float(rsi), float(adx))
        lst = _IND_HIST.get(c) or []
        if lst:
            ts0, rsi, adx = lst[-1]
            _IND_CACHE_LAST[c] = {'rsi': float(rsi), 'adx': float(adx), 'ts': float(ts0)}
            return (float(rsi), float(adx))
    except Exception:
        pass
    return (None, None)


def _lookup_latest_rsi_adx_live(out_dir: Optional[Path], logs_dir: Optional[Path], coin: str) -> Tuple[Optional[float], Optional[float]]:
    """多空比列表专用：优先读 ind_cache.json，再回退到增量缓存。"""
    try:
        _ensure_ind_sources_loaded(out_dir, logs_dir)
    except Exception:
        pass
    try:
        rsi, adx = _read_latest_ind_cache_coin(out_dir, coin)
        if (rsi is not None) and (adx is not None):
            return (rsi, adx)
    except Exception:
        pass
    return _lookup_latest_rsi_adx(coin)


def _ensure_ind_sources_loaded(out_dir: Optional[Path], logs_dir: Optional[Path]) -> None:
    """在 UI tick 中轻量调用：增量 ingest + 兜底 cache。"""
    global _IND_LAST_LOAD_AT
    try:
        now = time.time()
        # 限流：0.8s 内不重复扫
        if now - float(_IND_LAST_LOAD_AT or 0.0) < 0.8:
            return
        _IND_LAST_LOAD_AT = now

        # 优先 ingest logs/out 多来源（很多版本把指标直接写在 trades / executor_results 里）
        cand = []
        if out_dir:
            for fn in ("trades.jsonl", "executor_results.jsonl", "executor_result.jsonl", "exec_results.jsonl", "trace_events.jsonl", "telemetry.jsonl"):
                p = Path(out_dir) / fn
                if p.exists() and p.stat().st_size > 0:
                    cand.append(p)
        if logs_dir:
            for fn in ("full_all.jsonl", "BOT_grid_full.jsonl", "executor_results.jsonl", "exec_results.jsonl", "trades.jsonl"):
                p = Path(logs_dir) / fn
                if p.exists() and p.stat().st_size > 0:
                    cand.append(p)
        for p in cand:
            _ingest_ind_from_jsonl(p)

        # 再读 out/ind_cache.json
        _load_ind_cache_json(out_dir)
    except Exception:
        return


def _lookup_rsi_adx_at(coin: str, ts: float) -> Tuple[Optional[float], Optional[float]]:
    """返回 (rsi, adx)，尽量取 <=ts 的最近一条。"""
    try:
        c = str(coin or "").upper().strip()
        t = float(ts or 0.0)
        if not c:
            return (None, None)

        lst = _IND_HIST.get(c) or []
        if lst and t > 0:
            # 二分找 rightmost <= t
            import bisect
            xs = [x[0] for x in lst]
            i = bisect.bisect_right(xs, t) - 1
            if i >= 0:
                rsi, adx = lst[i][1], lst[i][2]
                return (float(rsi), float(adx))

        # 回退：最近 cache
        v = _IND_CACHE_LAST.get(c)
        if isinstance(v, dict):
            rsi = v.get("rsi")
            adx = v.get("adx")
            if rsi is not None and adx is not None:
                return (float(rsi), float(adx))
    except Exception:
        pass
    return (None, None)


def _extract_rsi_adx_from_row(row: Dict[str, Any], coin: str, ts: float) -> Tuple[Optional[float], Optional[float]]:
    """优先从当前动作行直接取指标；缺失时再回退到历史缓存。

    兼容字段：
    - rsi/adx, rsi14/adx14, rsi_val/adx_val
    - ind_rsi/ind_adx
    - req/meta/gate_eval 等嵌套字典中的同名字段
    """
    try:
        d = row if isinstance(row, dict) else {}

        def _take(obj: Any, keys: Tuple[str, ...]) -> float:
            if not isinstance(obj, dict):
                return float('nan')
            for k in keys:
                if k in obj:
                    v = _safe_float(obj.get(k), float('nan'))
                    if v == v:
                        return float(v)
            return float('nan')

        num_keys_rsi = ('rsi', 'rsi14', 'rsi_14', 'rsi_val', 'ind_rsi', 'raw_rsi', 'last_good_rsi')
        num_keys_adx = ('adx', 'adx14', 'adx_14', 'adx_val', 'ind_adx', 'raw_adx', 'last_good_adx')

        rsi = _take(d, num_keys_rsi)
        adx = _take(d, num_keys_adx)

        nests = []
        for k in ('req', 'meta', 'extras', 'params', 'gate_eval'):
            v = d.get(k) if isinstance(d, dict) else None
            if isinstance(v, dict):
                nests.append(v)
        req = d.get('req') if isinstance(d, dict) else None
        if isinstance(req, dict):
            for k in ('meta', 'params', 'gate_eval'):
                v = req.get(k)
                if isinstance(v, dict):
                    nests.append(v)
            params = req.get('params')
            if isinstance(params, dict):
                for k in ('meta', 'extras', 'gate_eval', 'params'):
                    v = params.get(k)
                    if isinstance(v, dict):
                        nests.append(v)

        for obj in nests:
            if not (rsi == rsi):
                rsi = _take(obj, num_keys_rsi)
            if not (adx == adx):
                adx = _take(obj, num_keys_adx)
            if (rsi == rsi) and (adx == adx):
                break

        if (not (rsi == rsi)) or (not (adx == adx)):
            rsi_h, adx_h = _lookup_rsi_adx_at(coin, ts)
            if (rsi_h is not None) and (not (rsi == rsi)):
                rsi = float(rsi_h)
            if (adx_h is not None) and (not (adx == adx)):
                adx = float(adx_h)

        return ((float(rsi) if (rsi == rsi) else None), (float(adx) if (adx == adx) else None))
    except Exception:
        return (None, None)




def _extract_grid_layer_tag_from_row(row: Dict[str, Any]) -> str:
    """从动作行里提取网格层标记，返回 L1/L2...；取不到则返回空串。

    兼容来源：
    - 顶层：layer_tag / layer_no / level
    - req / meta / params / req.meta / req.params / req.params.params / grid_layer 等嵌套
    - layer_id 例如 BTC_VAR_SHORT_M9_L2
    """
    try:
        d = row if isinstance(row, dict) else {}

        def _walk(obj: Any):
            if isinstance(obj, dict):
                yield obj
                for v in obj.values():
                    if isinstance(v, dict):
                        yield from _walk(v)
                    elif isinstance(v, list):
                        for it in v:
                            if isinstance(it, dict):
                                yield from _walk(it)

        def _from_obj(obj: Dict[str, Any]) -> str:
            try:
                tag = str(obj.get('layer_tag') or obj.get('layer') or '').strip().upper()
                if re.fullmatch(r'L\d+', tag):
                    return tag
                lv = obj.get('layer_no')
                if lv is None:
                    lv = obj.get('level_no')
                if lv is None:
                    lv = obj.get('level')
                if lv is not None:
                    try:
                        iv = int(float(lv))
                        if iv > 0:
                            return f'L{iv}'
                    except Exception:
                        pass
                lid = str(obj.get('layer_id') or obj.get('grid_layer_id') or '').strip().upper()
                if lid:
                    m = re.search(r'(L\d+)(?![A-Z0-9])', lid)
                    if m:
                        return m.group(1)
            except Exception:
                return ''
            return ''

        for obj in _walk(d):
            tag = _from_obj(obj)
            if tag:
                return tag
    except Exception:
        pass
    return ''


def _decorate_summary_module_action(module: str, action: str, act_cn: str, row: Dict[str, Any]) -> str:
    """给摘要“模块/动作”补展示口径。

    目标：
    - 网格模块继续显示层号：M9/L1开、M9/L2平
    - M10 三步改成人话：M10/全平、M10/减亏、M10/回补开仓
    - 其余模块尽量保持原口径，避免影响你的观察习惯
    """
    try:
        module_s = str(module or '-').strip() or '-'
        action_s = str(action or '-').strip() or '-'
        act_cn_s = str(act_cn or '-').strip() or '-'
        module_u = module_s.upper()
        action_l = action_s.lower()

        def _disp_module(ms: str) -> str:
            mu = str(ms or '-').upper().strip() or '-'
            if mu.startswith('M10'):
                return 'M10'
            if mu in ('B2/M10', 'M10/B2', 'M10+B2', 'B2+M10', 'M10-B2', 'B2-M10'):
                return 'M10+B2'
            return ms

        disp_module = _disp_module(module_s)

        # ===== M10 口径人话显示 =====
        if module_u.startswith('M10'):
            m10_map = {
                'close_full': '全平',
                'close_full_win': '全平',
                'close_all': '全平',
                'close_partial': '减亏',
                'close_partial_notional': '减亏',
                'decrease': '减亏',
                'reduce': '减亏',
                'open_full': '回补开仓',
                'reopen': '回补开仓',
                'reopen_full': '回补开仓',
            }
            human = m10_map.get(action_l)
            if human:
                return f'{disp_module}/{human}'
            if act_cn_s and act_cn_s != '-':
                return f'{disp_module}/{act_cn_s}'
            return disp_module

        # ===== 网格层号显示 =====
        layer_tag = _extract_grid_layer_tag_from_row(row)
        is_grid = module_u in ('M9', 'W1') or ('GRID' in module_u)
        if layer_tag and is_grid:
            if act_cn_s == '开仓':
                act_part = f'{layer_tag}开'
            elif act_cn_s == '平仓':
                act_part = f'{layer_tag}平'
            else:
                act_part = f'{layer_tag}{act_cn_s}'
            if disp_module != '-':
                return f'{disp_module}/{act_part}'
            return act_part

        if is_grid and disp_module != '-':
            return f'{disp_module}/{act_cn_s}'

        if disp_module != '-' and action_s != '-':
            return f'{disp_module}/{action_s}/{act_cn_s}'
        if disp_module != '-':
            return f'{disp_module}/{act_cn_s}'
        return f'{action_s}/{act_cn_s}' if action_s != '-' else act_cn_s
    except Exception:
        pass
    if module != '-' and action != '-':
        return f'{module}/{action}/{act_cn}'
    if module != '-':
        return f'{module}/{act_cn}'
    return f'{action}/{act_cn}' if action != '-' else act_cn


def _rsi_direction_expectation(rsi_v: Any, dir0: str) -> str:
    """摘要方向校验：
    - RSI > 50 期望方向=long
    - RSI < 50 期望方向=short
    - RSI == 50 / 无方向 / 无 RSI 时返回 'neutral'
    - 不符合时返回 'mismatch'
    """
    try:
        if dir0 not in ('long', 'short'):
            return 'neutral'
        rv = _safe_float(rsi_v, None)
        if rv is None:
            return 'neutral'
        rv = float(rv)
        if rv > 50.0:
            return 'ok' if dir0 == 'long' else 'mismatch'
        if rv < 50.0:
            return 'ok' if dir0 == 'short' else 'mismatch'
        return 'neutral'
    except Exception:
        return 'neutral'

def _norm_platform(p: Any) -> str:
    """归一化平台名：EXT（平台E）/VAR（平台V）/PAIR（双平台成对）。"""
    try:
        s = str(p or "").strip().upper()
        if not s:
            return "-"
        # 常见别名
        if s in ("E", "EXT", "PLATFORME", "PLATFORM_E", "PLAT_E", "EXTE"):
            return "EXT"
        if s in ("V", "VAR", "PLATFORMV", "PLATFORM_V", "PLAT_V"):
            return "VAR"
        if s in ("PAIR", "BOTH", "DUAL", "2", "TWO"):
            return "PAIR"
        # 前缀兜底
        if s.startswith("EXT"):
            return "EXT"
        if s.startswith("VAR"):
            return "VAR"
        return s
    except Exception:
        return "-"


def _fmt_u(v: Any) -> str:
    """格式化 U（金額U）显示：大金额无小数，小金额保留两位。"""
    try:
        x = _safe_float(v, 0.0)
        if abs(x) >= 100:
            return f"{x:,.0f}U"
        return f"{x:,.2f}U"
    except Exception:
        return "0U"


def _fmt_pct(p: Any) -> str:
    """格式化百分比：若 p 在 [-1,1] 之间，视为比例并 *100。"""
    try:
        x = _safe_float(p, 0.0)
        if -1.0 <= x <= 1.0:
            x = x * 100.0
        return f"{x:.2f}%"
    except Exception:
        return "0.00%"




def _fmt_pct_plain(p: Any) -> str:
    """格式化百分比（不做 *100 推断，认为传入就是百分数）。"""
    try:
        x = _safe_float(p, 0.0)
        return f"{x:.2f}%"
    except Exception:
        return "0.00%"


def _fmt_pct100(p: Any) -> str:
    """格式化百分比（*100 口径）。
    - 若传入为 0.0123 这样的比例 -> 1.23%
    - 若传入已是 1.23 这样的百分数，也会输出 1.23%
    """
    try:
        x = _safe_float(p, 0.0)
        if -1.0 <= x <= 1.0:
            x = x * 100.0
        return f"{x:.2f}%"
    except Exception:
        return "0.00%"


# ===== 摘要去重/规范化 =====
_SUMMARY_SEEN: set[str] = set()

def _make_summary_sig(ts: float, coin: str, plat: str, act: str, side: str, amt: float) -> str:
    """构造摘要去重签名：按秒桶 + 核心字段 + 金额(2位)"""
    try:
        t = int(float(ts)) if ts else 0
    except Exception:
        t = 0
    c = (coin or "-").upper()
    p = (plat or "-").upper()
    a = (act or "-").lower()
    s = (side or "-").upper()
    try:
        am = round(float(amt), 2)
    except Exception:
        am = 0.0
    return f"{t}|{c}|{p}|{a}|{s}|{am:.2f}"

def _summary_seen(sig: str) -> bool:
    """返回 True 表示已出现过；否则记录并返回 False。"""
    if not sig:
        return False
    if sig in _SUMMARY_SEEN:
        return True
    _SUMMARY_SEEN.add(sig)
    if len(_SUMMARY_SEEN) > 5000:
        _SUMMARY_SEEN.clear()
    return False

def _norm_module(s: str) -> str:
    """规范化模块名，尽量避免出现空/None/杂字符。"""
    if s is None:
        return "-"
    t = str(s).strip()
    if not t:
        return "-"
    t = re.sub(r"\s+", " ", t)
    return t




def _fmt_px(v: Any) -> str:
    """格式化价格：按数量级自动决定小数位，避免过长挤爆表格。"""
    try:
        if v is None or v == "":
            return "-"
        x = _safe_float(v, float("nan"))
        if x != x or x in (float("inf"), float("-inf")):
            return "-"
        ax = abs(x)
        if ax >= 1000:
            s = f"{x:,.2f}"
        elif ax >= 100:
            s = f"{x:,.3f}"
        elif ax >= 10:
            s = f"{x:,.4f}"
        elif ax >= 1:
            s = f"{x:,.5f}"
        elif ax >= 0.1:
            s = f"{x:,.6f}"
        else:
            s = f"{x:,.8f}"
        # 去掉末尾多余 0
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s
    except Exception:
        return "-"

def _extract_amount_u_from_row(row: dict) -> float:
    """摘要金额兜底提取：尽量从多字段/嵌套/原始文本里取到真实的 USD(U) 金额。

    注意：amount/std/intent 等字段在不同版本可能是数量或 U，需要做轻量识别，避免 EXT（平台E）金额误判。
    """
    try:
        if not isinstance(row, dict):
            return 0.0

        def _pick(d: dict, ks):
            if not isinstance(d, dict):
                return 0.0

            # 若存在明确 U 口径字段，则不要再用 std/intent/amount 这类模糊字段兜底，避免误判。
            has_strong_u = any((k in d and d.get(k) is not None) for k in (
                "notional_u","usd","usd_u","usd_amount","value_u","value_usd","value_usd_u",
                "notional_usd","notionalUsd","notionalUSD","notional_usdt","usd_notional",
                "size_usd",
                # 常见金额字段（你 trades.jsonl 里大量用 amount_usd/amount_usd_intent）
                "amount_usd","amount_usd_intent","amountUsd","amountUsdIntent",
                "std_u","intent_u","add_u","reduce_u","open_u","close_u","trade_u","delta_u","amount_u",
            ))

            for k in ks:
                if k not in d or d.get(k) is None:
                    continue
                if k == "std_u" and d.get("intent_u") is not None:
                    try:
                        iu = abs(float(d.get("intent_u")))
                        su = abs(float(d.get("std_u") or 0.0))
                        if iu > 0 and su > 0 and iu >= su * 1.2:
                            continue
                    except Exception:
                        pass
                if k == "amount_usd" and d.get("amount_usd_intent") is not None:
                    try:
                        ai = abs(float(d.get("amount_usd_intent")))
                        ab = abs(float(d.get("amount_usd") or 0.0))
                        if ai > 0 and ab > 0 and ai >= ab * 1.2:
                            continue
                    except Exception:
                        pass

                v = _safe_float(d.get(k), 0.0)
                if abs(v) <= 0:
                    continue
                v = abs(v)

                if k in ("std", "intent"):
                    if has_strong_u:
                        continue
                    unit = str(d.get("unit") or d.get("currency") or d.get("ccy") or "").upper()
                    if ("U" in unit) or ("USD" in unit) or ("USDT" in unit):
                        return v
                    # 经验兜底：若金额满足最小下单 20U 且整十（你的系统取整十），大概率是 U
                    if v >= 20 and abs(v / 10 - round(v / 10)) < 1e-9:
                        return v
                    continue

                if k == "amount":
                    # amount 极易是数量/张数：只有在明确标注 amount_u 或 raw 中出现 amount_u= 才采纳
                    if "amount_u" in d and d.get("amount_u") is not None:
                        vv = _safe_float(d.get("amount_u"), 0.0)
                        if abs(vv) > 0:
                            return abs(vv)
                    raw2 = str(d.get("raw") or "")
                    if raw2 and ("amount_u" in raw2.lower()):
                        return v
                    continue

                return v

            return 0.0

        keys = [
            # 真实 USD(U) 名义优先（避免把 amount 当成数量/张数）
            "notional_u","usd","usd_u","usd_amount","value_u","value_usd","value_usd_u",
            "notional_usd","notionalUsd","notionalUSD","notional_usdt","notional","usd_notional",
            "size_usd",
            # 常见金额字段（trades.jsonl）
            "amount_usd_intent","amount_usd","amountUsdIntent","amountUsd",
            # 标准/意图金额（明确 _u 优先）
            "intent_u","std_u",
            # 动作金额（补腿/加仓/减仓/开平）
            "add_u","reduce_u","open_u","close_u","trade_u","delta_u",
            # 明确 amount_u
            "amount_u",
            # 其它可能字段
            "amt","qty_u",
            # 模糊字段（放后面，且做识别）
            "std","intent",
            # 最后兜底：amount
            "amount",
        ]

        v = _pick(row, keys)
        if v > 0:
            return v

        req = row.get("req") or {}
        if isinstance(req, dict):
            v = _pick(req, keys)
            if v > 0:
                return v

            # trades.jsonl 常见：金额/意图金额在 req.params / req.params.params 里
            params = req.get("params") or {}
            if isinstance(params, dict):
                v = _pick(params, keys)
                if v > 0:
                    return v
                params2 = params.get("params") or {}
                if isinstance(params2, dict):
                    v = _pick(params2, keys)
                    if v > 0:
                        return v

                # PAIR（双腿/成对）动作：legs 里可能带每条腿的 usd/amount。
                # 这里用于“金额兜底”（例如 trades.jsonl 顶层 notional_u 为空）。
                legs = params.get("legs")
                if not isinstance(legs, list):
                    legs = params2.get("legs") if isinstance(params2, dict) else None
                if isinstance(legs, list) and legs:
                    ssum = 0.0
                    for lg in legs:
                        if not isinstance(lg, dict):
                            continue
                        vv = _pick(lg, keys)
                        if vv > 0:
                            ssum += float(vv)
                    if ssum > 0:
                        return float(ssum)

        raw = str(row.get("raw") or "")
        if raw:
            for pat in (
                r"usd\W*=\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
                r"amount_u\W*=\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
                r"amount\W*=\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
                r"std_u\W*=\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
                r"intent_u\W*=\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
                r"std\W*=\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
                r"intent\W*=\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
                r"金额\W*=\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
                r"名义\W*\(?u\)?\W*=\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
            ):
                mm = re.search(pat, raw, flags=re.I)
                if mm:
                    vv = _safe_float(mm.group(1).replace(",", ""), 0.0)
                    if abs(vv) > 0:
                        # raw 里 std/intent 也可能是数量：按整十 + >=20U 的经验兜底
                        if ("std" in pat or "intent" in pat) and ("_u" not in pat):
                            vvv = abs(float(vv))
                            if vvv >= 20 and abs(vvv / 10 - round(vvv / 10)) < 1e-9:
                                return vvv
                            continue
                        return abs(float(vv))

        return 0.0
    except Exception:
        return 0.0






def _extract_summary_notional_u(row: dict) -> float:
    """摘要/面板金额唯一口径：只认 U 金额字段，禁止把 exec_u/amount 数量口径当成金额。"""
    try:
        if not isinstance(row, dict):
            return 0.0

        def _pick_u_only(d: dict, keys):
            if not isinstance(d, dict):
                return 0.0
            for k in keys:
                if k not in d or d.get(k) is None:
                    continue
                v = abs(float(_safe_float(d.get(k), 0.0) or 0.0))
                if v > 0:
                    return v
            return 0.0

        req = row.get('req') if isinstance(row.get('req'), dict) else {}
        params = req.get('params') if isinstance(req.get('params'), dict) else {}
        params2 = params.get('params') if isinstance(params.get('params'), dict) else {}
        meta = req.get('meta') if isinstance(req.get('meta'), dict) else {}

        keys = (
            'close_notional_u',
            'amount_u', 'notional_u',
            'amount_usd', 'amount_usd_intent', 'amountUsd', 'amountUsdIntent',
            'usd', 'usd_u', 'usd_amount',
            'value_u', 'value_usd', 'value_usd_u',
            'notional_usd', 'notionalUsd', 'notionalUSD', 'notional_usdt',
            'size_usd',
            'intent_u', 'std_u',
            'add_u', 'reduce_u', 'open_u', 'close_u', 'trade_u', 'delta_u',
        )

        for obj in (row, params2, params, meta, req):
            v = _pick_u_only(obj, keys)
            if v > 0:
                return v

        v = abs(float(_extract_amount_u_from_row(row) or 0.0))
        return v if v > 0 else 0.0
    except Exception:
        return 0.0

def _extract_price_from_row(row: dict) -> float | None:
    """摘要价格提取：优先用动作发生时的成交/触发/参考价。"""
    try:
        if not isinstance(row, dict):
            return None

        def _pick(obj, keys):
            if not isinstance(obj, dict):
                return None
            for k in keys:
                try:
                    v = _safe_float(obj.get(k), None)
                except Exception:
                    v = None
                if v is not None and float(v) > 0:
                    return float(v)
            return None

        req = row.get('req') if isinstance(row.get('req'), dict) else {}
        params = req.get('params') if isinstance(req.get('params'), dict) else {}
        params2 = params.get('params') if isinstance(params.get('params'), dict) else {}
        meta = req.get('meta') if isinstance(req.get('meta'), dict) else {}
        action = str(row.get('action') or row.get('action_type') or req.get('action_type') or req.get('action') or '').lower()

        # 价格口径：
        # - 先认“成交回执价”（filled/avg/fill/exec）
        # - 再认“动作当时参考价”（price/entry/open_ref）
        # - 最后才用 mark/last/cur 这类快照价（否则会出现你截图里 72595 -> 72564 这种偏差）
        _filled_keys = (
            'filled_price', 'filled_px', 'filledPrice',
            'avg_px', 'avg_price', 'avgPrice',
            'fill_px', 'fill_price', 'fillPrice',
            'exec_price', 'exec_px',
        )
        _ref_keys_open = ('price', 'entry_price', 'entry_px', 'open_ref_px')
        _ref_keys_close = ('price', 'close_price', 'close_px')
        _snap_keys = ('cur_px', 'mark_price', 'mark_px', 'mark', 'last_px', 'last')

        if action in ('decrease','reduce','close','tp','take_profit','grid_sell','grid_reduce'):
            px = (
                _pick(row, _filled_keys) or
                _pick(meta, _filled_keys) or
                _pick(params2, _filled_keys) or
                _pick(params, _filled_keys) or
                _pick(row, _ref_keys_close) or
                _pick(meta, _ref_keys_close) or
                _pick(params2, _ref_keys_close) or
                _pick(params, _ref_keys_close) or
                _pick(row, _snap_keys) or
                _pick(meta, _snap_keys) or
                _pick(params2, _snap_keys) or
                _pick(params, _snap_keys)
            )
            if px is not None:
                return float(px)

        px = (
            _pick(row, _filled_keys) or
            _pick(meta, _filled_keys) or
            _pick(params2, _filled_keys) or
            _pick(params, _filled_keys) or
            _pick(row, _ref_keys_open) or
            _pick(meta, _ref_keys_open) or
            _pick(params2, _ref_keys_open) or
            _pick(params, _ref_keys_open) or
            _pick(row, _snap_keys) or
            _pick(meta, _snap_keys) or
            _pick(params2, _snap_keys) or
            _pick(params, _snap_keys)
        )
        return float(px) if px is not None else None
    except Exception:
        return None


def _lookup_latest_price_live(out_dir: str | None, logs_dir: str | None, coin: str, plat: str | None = None) -> float | None:
    """价格兜底：当成交行里没有价格时，尽量用当前最新快照补一份。

    优先：positions.json -> m9_detail_last.json -> ind_cache.json。
    说明：这是“面板展示兜底价”，不是成交回执价；只在原始动作行没带 price 时使用。
    """
    try:
        from pathlib import Path as _Path
        coin_u = str(coin or '').upper().strip()
        plat_u = str(plat or '').upper().strip()
        cands = []
        for base in (out_dir, logs_dir):
            if base:
                bp = _Path(base)
                if bp.exists():
                    cands.append(bp)
        try:
            cands.append(_Path(__file__).resolve().parent / 'out')
        except Exception:
            pass
        seen = set()
        uniq = []
        for b in cands:
            sb = str(b)
            if sb in seen:
                continue
            seen.add(sb)
            uniq.append(b)

        def _match_plat(v: str) -> bool:
            if not plat_u:
                return True
            return str(v or '').upper().strip() == plat_u

        # A) positions.json：最靠谱的当前价
        for b in uniq:
            fp = b / 'positions.json'
            if not fp.exists():
                continue
            obj = _read_json(fp)
            plist = obj.get('positions') if isinstance(obj, dict) else None
            if not isinstance(plist, list):
                continue
            for it in plist:
                if not isinstance(it, dict):
                    continue
                if str(it.get('coin') or '').upper().strip() != coin_u:
                    continue
                if not _match_plat(it.get('platform') or it.get('plat') or ''):
                    continue
                px = _safe_float(it.get('px'), None)
                if px is None:
                    px = _safe_float(it.get('mark_px') or it.get('mark') or it.get('price') or it.get('last_price'), None)
                if px is not None and float(px) > 0:
                    return float(px)

        # B) m9_detail_last.json：策略快照里经常带当前价/标记价
        for b in uniq:
            fp = b / 'm9_detail_last.json'
            if not fp.exists():
                continue
            obj = _read_json(fp)
            ent = obj.get('entries') if isinstance(obj, dict) else None
            if not isinstance(ent, list):
                continue
            for it in ent:
                if not isinstance(it, dict):
                    continue
                if str(it.get('coin') or '').upper().strip() != coin_u:
                    continue
                if not _match_plat(it.get('platform') or it.get('plat') or ''):
                    continue
                for k in ('px', 'mark_px', 'mark', 'price', 'last_px', 'cur_px', 'entry_px'):
                    px = _safe_float(it.get(k), None)
                    if px is not None and float(px) > 0:
                        return float(px)

        # C) ind_cache.json：少量版本会把当前价格顺手缓存进去
        for b in uniq:
            fp = b / 'ind_cache.json'
            if not fp.exists():
                continue
            obj = _read_json(fp)
            entries = obj.get('entries') if isinstance(obj, dict) else None
            if isinstance(entries, list):
                for it in reversed(entries):
                    if not isinstance(it, dict):
                        continue
                    if str(it.get('coin') or '').upper().strip() != coin_u:
                        continue
                    if not _match_plat(it.get('platform') or it.get('plat') or ''):
                        continue
                    for k in ('px', 'mark_px', 'mark', 'price', 'last_px', 'cur_px'):
                        px = _safe_float(it.get(k), None)
                        if px is not None and float(px) > 0:
                            return float(px)
    except Exception:
        return None
    return None


def _extract_profit_from_row(row: dict) -> float | None:
    """摘要利润提取：优先读真实字段；若是 M9/W1 网格减仓则用开平价估算。"""
    try:
        if not isinstance(row, dict):
            return None

        def _pick(obj, keys):
            if not isinstance(obj, dict):
                return None
            for k in keys:
                try:
                    v = _safe_float(obj.get(k), None)
                except Exception:
                    v = None
                if v is not None:
                    return float(v)
            return None

        req = row.get('req') if isinstance(row.get('req'), dict) else {}
        params = req.get('params') if isinstance(req.get('params'), dict) else {}
        params2 = params.get('params') if isinstance(params.get('params'), dict) else {}
        meta = req.get('meta') if isinstance(req.get('meta'), dict) else {}

        val = None
        for _obj in (row, meta, params2, params):
            val = _pick(_obj, ('profit_u','realized_pnl_u','pnl_u','grid_profit_u','net_profit_u','profit_usd'))
            if val is not None:
                return float(val)

        module = str(row.get('module') or req.get('module') or '').upper()
        action = str(row.get('action') or row.get('action_type') or req.get('action_type') or req.get('action') or '').lower()
        if module in ('M9','W1') and action in ('decrease','reduce','close','tp','take_profit','grid_sell','grid_reduce'):
            side = str(row.get('dir') or row.get('side') or row.get('pos_side') or req.get('side') or params.get('side') or params2.get('side') or meta.get('side') or '').upper()
            if side in ('LONG','SHORT'):
                open_px = (
                    _pick(meta, ('open_ref_px','entry_price','entry_px')) or
                    _pick(params2, ('open_ref_px','entry_price','entry_px')) or
                    _pick(params, ('open_ref_px','entry_price','entry_px'))
                )
                close_px = _extract_price_from_row(row)
                notional_u = float(_extract_summary_notional_u(row) or 0.0)
                if open_px and close_px and notional_u and open_px > 0:
                    gross = float(notional_u) * ((float(close_px) - float(open_px)) / float(open_px))
                    if side == 'SHORT':
                        gross = -gross
                    fee_rate = 0.001
                    return float(gross - float(notional_u) * fee_rate * 2.0)
        return None
    except Exception:
        return None


def _decode_bytes(b: bytes) -> str:
    """尽量把 Windows 控制台输出解码为可读中文（utf-8/gbk 自动回退）。"""
    if b is None:
        return ""
    for enc in ("utf-8", "utf-8-sig", "gbk", "cp936", "latin-1"):
        try:
            return b.decode(enc, errors="strict")
        except Exception:
            continue
    # 最终兜底：忽略错误
    try:
        return b.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _read_text(fp: Optional[Path], max_chars: int = 40000) -> str:
    """读取文本文件尾部内容（优先 utf-8，其次 gbk/cp936），并截取尾部 max_chars。

    v12_5 自我优化：
    - 以前 read_bytes() 会把整个文件读进内存，日志大时会卡顿
    - 现在改为“尾部读取”（tail read），只读最后一段字节
    """
    try:
        if not fp or (not fp.exists()):
            return ""
        # 估算读取字节：中文等多字节编码下，max_chars 对应字节更大一些
        max_bytes = int(max_chars * 4)
        try:
            size = fp.stat().st_size
        except Exception:
            size = 0
        start = max(0, size - max_bytes)
        with open(fp, "rb") as f:
            try:
                if start:
                    f.seek(start)
            except Exception:
                pass
            raw = f.read()
        txt2 = _decode_bytes(raw)
        if len(txt2) > max_chars:
            return txt2[-max_chars:]
        return txt2
    except Exception:
        return ""

def _read_json(fp: Optional[Path]) -> Optional[dict]:
    """读取 JSON 文件并返回 dict；失败返回 None。
    说明：此函数被大量调用，因此保留原签名。更鲁棒的读取请用 _read_json_obj。
    """
    obj, _ = _read_json_obj(fp, want="dict", retries=2, sleep_s=0.03)
    return obj


def _read_json_obj(
    fp: Optional[Path],
    *,
    want: str = "dict",
    retries: int = 3,
    sleep_s: float = 0.06,
) -> Tuple[Optional[object], str]:
    """更鲁棒的 JSON 读取器：返回 (obj, err).

    - want="dict": 只接受 dict（最常见）
    - want="any": 允许 dict 或 list
    - want="list": 只接受 list
    处理场景：
    1) Bot 正在写入，面板读到半截 JSON（JSONDecodeError）
    2) 文件为空/不存在
    3) 根对象不是预期类型（例如 root 是 list）
    """
    if not fp:
        return None, "fp is None"
    cached = _json_cache_get(fp, want=want)
    if cached is not None:
        return cached, ""
    if (not fp.exists()) or fp.stat().st_size <= 0:
        return None, "file missing/empty"

    last_err = ""
    for k in range(max(1, int(retries))):
        try:
            raw0 = fp.read_text(encoding="utf-8", errors="ignore")
            raw = (raw0 or "").strip()
            # 兼容：UTF-8 BOM / 前缀垃圾字符 / 末尾残留（常见于非原子覆盖写入）
            try:
                raw = raw.lstrip()
                if raw.startswith("\ufeff"):
                    raw = raw.lstrip("\ufeff").lstrip()
                if raw and (raw[0] not in "{["):
                    p1 = raw.find("{")
                    p2 = raw.find("[")
                    ps = [p for p in (p1, p2) if p >= 0]
                    if ps:
                        raw = raw[min(ps):].lstrip()
                if raw and (raw[-1] not in "}]"):
                    j = max(raw.rfind("}"), raw.rfind("]"))
                    if j > 0:
                        raw = raw[:j+1].strip()
            except Exception:
                pass
            if not raw:
                last_err = "empty content"
                time.sleep(sleep_s)
                continue

            # 粗略判定“写到一半”——避免 json.loads 直接炸
            if (raw[0] in "{[") and (raw[-1] not in "}]"):
                last_err = "incomplete json (tail not closed)"
                time.sleep(sleep_s)
                continue

            obj = json.loads(raw)

            if want == "dict":
                if isinstance(obj, dict):
                    _json_cache_put(fp, obj, want="dict")
                    return obj, ""
                return None, f"type mismatch: want dict got {type(obj).__name__}"
            if want == "list":
                if isinstance(obj, list):
                    _json_cache_put(fp, obj, want="list")
                    return obj, ""
                return None, f"type mismatch: want list got {type(obj).__name__}"
            # want == "any"
            if isinstance(obj, (dict, list)):
                _json_cache_put(fp, obj, want="any")
                return obj, ""
            return None, f"type mismatch: got {type(obj).__name__}"
        except json.JSONDecodeError as e:
            last_err = f"JSONDecodeError line={e.lineno} col={e.colno}"
            time.sleep(sleep_s)
            continue
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(sleep_s)
            continue

    return None, (last_err or "unknown")
def _tail_lines(fp: Optional[Path], max_lines: int = 400) -> List[str]:
    """高性能读取文件尾部若干行（按字节读取，再做多编码回退）。"""
    try:
        if not fp or (not fp.exists()):
            return []
        size = fp.stat().st_size
        if size <= 0:
            return []
        with fp.open("rb") as f:
            block = 4096
            data = b""
            pos = size
            while pos > 0 and data.count(b"\n") <= max_lines:
                step = min(block, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + data
                block = min(block * 2, 1_048_576)
        lines_b = data.splitlines()[-max_lines:]
        return [_decode_bytes(ln) for ln in lines_b]
    except Exception:
        return []



def _find_log_path_fallback() -> Optional[Path]:
    """Bot 如果回退日志路径，允许写 TEMP/bot_panel_bridge/out/log_path.txt 指向真实日志。"""
    try:
        hint = Path(tempfile.gettempdir()) / "bot_panel_bridge" / "out" / "log_path.txt"
        if hint.exists():
            p = Path(hint.read_text(encoding="utf-8", errors="ignore").strip())
            if p.exists():
                return p
    except Exception:
        pass
    return None


def _ts_to_dt_str(ts: Any) -> Optional[str]:
    try:
        if ts is None:
            return None
        if isinstance(ts, (int, float)) and float(ts) > 0:
            return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
        s = str(ts).strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}$", s):
            return s
    except Exception:
        pass
    return None


def _format_log_with_ts(lines: List[str]) -> List[str]:
    """给日志行补一个“可解析的时间戳前缀”。

    重要：如果原始行里解析不出任何时间信息，**不要**用当前时间去“冒充”时间戳。
    否则面板每次刷新都会把同一条历史日志显示成“现在”，导致摘要/全量日志时间不断漂移。
    """
    out: List[str] = []
    if mode == "focus":
        src_lines = _compact_focus_log_lines(lines, max_keep=260)
    else:
        src_lines = lines

    for ln in src_lines:
        s = (ln or "").rstrip("\n")
        if not s:
            continue
        # 已有前缀：直接保留
        if re.match(r"^\[\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\]", s):
            out.append(s)
            continue

        dt = None
        # JSON 行：尝试从常见字段解析 ts
        if s.lstrip().startswith("{"):
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    raw_ts = (
                        obj.get("ts") or obj.get("time_ts") or obj.get("t") or obj.get("timestamp")
                        or obj.get("ts_ms") or obj.get("time_ms") or obj.get("ts_us") or obj.get("time_us")
                        or obj.get("time") or obj.get("datetime") or obj.get("dt") or obj.get("time_str")
                    )
                    try:
                        ts = _parse_ts_sec_any(raw_ts)
                        if ts > 0:
                            dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        dt = None
            except Exception:
                dt = None

        # 无法解析时间：不加“伪时间戳”，保留原文（这是避免时间漂移的关键）
        if not dt:
            out.append(s)
            continue

        out.append(f"[{dt}] {s}")
    return out

def _humanize_one_json(obj: dict) -> Optional[str]:
    """将一条 JSON 日志 human 化为一行/多行文本。返回 None 表示忽略。"""
    try:
        # 1) HUMAN 块：直接渲染 title + lines
        if str(obj.get("type")) == "HUMAN":
            title = str(obj.get("title") or "").strip()
            lines = obj.get("lines") or []
            if not isinstance(lines, list):
                lines = [lines]
            body = "\n".join(str(x) for x in lines if str(x).strip())
            if title and body:
                return f"【{title}】\n{body}"
            if title:
                return f"【{title}】"
            if body:
                return body

        # 2) 下单/成交类（如果 bot 有写）
        cat = str(obj.get("cat") or obj.get("category") or "").lower()
        action = str(obj.get("action") or obj.get("action_type") or "").lower()
        if cat in ("trade", "order", "execution") or action in ("open", "close", "add", "reduce", "increase", "hedge"):
            coin = obj.get("coin") or (obj.get("req") or {}).get("coin") or "-"
            plat = obj.get("platform") or (obj.get("req") or {}).get("platform") or "-"
            side = obj.get("side") or (obj.get("req") or {}).get("side") or "-"
            notional = obj.get("notional_u") or obj.get("usd") or (obj.get("req") or {}).get("amount") or "-"
            module = obj.get("module") or "-"
            act = obj.get("action_type") or obj.get("action") or "-"
            return f"动作 {act} | 平台 {plat} | 币种 {coin} | 方向 {side} | 金额 {notional}U | 模块 {module}"

        # 3) 级别/错误
        lvl = str(obj.get("level") or obj.get("lvl") or "").upper()
        msg = obj.get("msg") or obj.get("message") or obj.get("text") or obj.get("line") or ""
        title = str(obj.get("title") or "").strip()
        if lvl in ("ERROR", "ERR"):
            return f"错误 {title} {msg}".strip()
        if lvl in ("WARN", "WARNING"):
            return f"警告 {title} {msg}".strip()

        # 4) 进度/心跳/扫描类（尽量压缩）
        if "扫描" in str(msg) or "scan" in str(msg).lower():
            return str(msg).strip()
        if "心跳" in str(msg):
            return None  # 屏蔽心跳刷屏

        # 5) 兜底：压缩成一行
        s = str(msg).strip()
        if title and s:
            return f"{title} | {s}"
        if s:
            return s
    except Exception:
        return None
    return None


def _compact_focus_log_lines(lines: List[str], max_keep: int = 260) -> List[str]:
    """日志聚焦视图：压缩重复进度/噪声，只保留关键变化。"""
    try:
        if not isinstance(lines, list) or not lines:
            return []
        kept: List[str] = []
        last_by_key: Dict[str, str] = {}
        progress_keys = (
            '持仓进度', '扫描进度', '扫描完成', '预计下次扫描', 'M9_COOLDOWN_SET',
            'PIN', 'TRACE', 'PROGRESS', 'STATUS'
        )
        for ln in lines:
            s = str(ln or '').strip()
            if not s:
                continue
            sl = s.lower()
            if ('心跳' in s) or ('heartbeat' in sl):
                continue
            strong = any(k in s for k in (
                'EXEC_ACTION', 'EXEC_DONE', 'EXEC_OK', 'ORDER_FILLED', 'FILLED',
                'ERROR', 'Exception', 'Traceback', 'WARN', '警告', '错误',
                'M1_ACCEPT', 'ACCEPT', '[STANDBY]', 'module_disabled',
                '本次扫描', '下次扫描', '扫描开始', '扫描结束'
            ))
            if strong:
                kept.append(s)
                continue
            if any(k in s for k in progress_keys):
                m = re.search(r'\b(EXT|VAR)\b.*?\b(BTC|ETH|SOL|HYPE|BNB|XRP|SUI|ADA)\b', s, re.I)
                if m:
                    key = f"prog|{m.group(1).upper()}|{m.group(2).upper()}|{s.split('|')[0][:24]}"
                else:
                    key = 'prog|' + re.sub(r'\d+(?:\.\d+)?', '*', s[:80])
                last_by_key[key] = s
                continue
            if any(x in sl for x in ('deprecationwarning', 'resourcewarning', 'websocket', 'ws:')):
                continue
            kept.append(s)
        if last_by_key:
            kept.extend(last_by_key[k] for k in sorted(last_by_key.keys()))
        if len(kept) > max_keep:
            kept = kept[-max_keep:]
        return kept
    except Exception:
        return list(lines or [])[-max_keep:]


def _build_log_digest(lines: List[str], exec_rows: Optional[List[dict]] = None, scan_now: str = '-', scan_next: str = '-') -> str:
    """面板日志摘要：让你一眼看明白现在在干嘛。"""
    try:
        total = len(lines or [])
        errs = 0
        warns = 0
        accepts = 0
        exec_cnt = len(exec_rows or [])
        latest_exec = ''
        if exec_rows:
            try:
                r0 = sorted(exec_rows, key=lambda x: float(_safe_float(x.get('ts'), 0.0) or 0.0), reverse=True)[0]
                latest_exec = f"最近成交：{r0.get('time') or '--'} {r0.get('coin') or '-'} {r0.get('plat') or r0.get('platform') or '-'} {r0.get('module') or '-'} {r0.get('oc') or '-'} {float(_safe_float(r0.get('amount_u'),0.0) or 0.0):.2f}U"
            except Exception:
                latest_exec = ''
        for ln in (lines or [])[-400:]:
            s = str(ln or '')
            sl = s.lower()
            if any(x in sl for x in ('error', 'exception', 'traceback')) or ('错误' in s):
                errs += 1
            elif ('warn' in sl) or ('警告' in s):
                warns += 1
            if 'ACCEPT' in s:
                accepts += 1
        parts = [f"日志{total}行", f"成交{exec_cnt}条", f"入队{accepts}条", f"警告/报错 {warns}/{errs}"]
        if scan_now or scan_next:
            parts.append(f"扫描 {scan_now or '-'} → {scan_next or '-'}")
        if latest_exec:
            parts.append(latest_exec)
        return '｜'.join(parts)
    except Exception:
        return ''


def _humanize_and_filter_logs(lines: List[str], mode: str, keyword: str = "") -> List[str]:
    """
    mode:
      - all: 全部（轻度清洗）
      - focus: 聚焦关键动作/错误/扫描进度（默认更适合盯盘）
      - human: 仅 HUMAN 块
      - errors: 仅 错误/警告
      - actions: 仅 下单动作（开/平/加/减/补）
      - m1: 仅 M1 相关
      - scan: 仅 扫描相关
    """
    mode = (mode or "focus").lower().strip()
    kw = (keyword or "").strip().lower()

    out: List[str] = []

    def keep(s: str) -> bool:
        if not s:
            return False
        if kw and (kw not in s.lower()):
            return False
        return True

    for ln in lines:
        s = (ln or "").strip()
        if not s:
            continue

        # 带时间戳前缀的纯文本
        ts_prefix = ""
        m = re.match(r"^\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\]\s*(.*)$", s)
        if m:
            ts_prefix = f"[{m.group(1)}] "
            s_body = m.group(2).strip()
        else:
            s_body = s

        # 永久屏蔽心跳类刷屏（无论模式）
        if ("心跳" in s_body) or ("heartbeat" in s_body.lower()):
            continue

        rendered: Optional[str] = None

        # 尝试 JSON
        if s_body.startswith("{") and s_body.endswith("}"):
            try:
                obj = json.loads(s_body)
                if isinstance(obj, dict):
                    if mode == "human" and str(obj.get("type")) != "HUMAN":
                        continue
                    if mode == "errors":
                        lvl = str(obj.get("level") or obj.get("lvl") or "").upper()
                        if lvl not in ("ERROR", "ERR", "WARN", "WARNING"):
                            continue
                    if mode == "m1":
                        t = str(obj.get("title") or "") + " " + str(obj.get("msg") or obj.get("message") or "")
                        if "M1" not in t and "m1" not in t.lower():
                            continue
                    if mode == "scan":
                        t = str(obj.get("title") or "") + " " + str(obj.get("msg") or obj.get("message") or "") + " " + str(obj.get("text") or "")
                        if ("扫描" not in t) and ("scan" not in t.lower()):
                            continue
                    if mode == "actions":
                        cat = str(obj.get("cat") or obj.get("category") or "").lower()
                        act = str(obj.get("action") or obj.get("action_type") or "").lower()
                        if not (cat in ("trade", "order", "execution") or act in ("open", "close", "add", "reduce", "increase", "hedge")):
                            continue

                    rendered = _humanize_one_json(obj)
            except Exception:
                rendered = None

        # 非 JSON：做一些噪声过滤
        if rendered is None:
            # mode 限制
            if mode == "human":
                # human 视图允许展示 system_observe.log 里的纯文本人话日志，
                # 但只放白名单事件，避免把 all 视图里的机器噪音带进来。
                human_keys = (
                    "轮次开始", "轮次结束", "扫描开始", "扫描结果", "扫描", "网格汇总", "网格",
                    "决策开始", "决策结果", "决策", "执行开始", "执行结果", "执行",
                    "系统", "警告", "错误", "下轮预计开始", "预计间隔", "开始时间", "结束时间",
                    "本轮总耗时", "本轮链路", "候选", "入选", "未入选", "拦截", "通过", "开仓",
                    "平仓", "加仓", "减仓", "补仓", "止盈", "持仓进度"
                )
                lower_body = s_body.lower()
                noisy_human = (
                    "ulog_ready", "bot_console", "mirror-on", "trace_flow_summary", "round_reason_summary",
                    "round_priority_rank", "m9_reason_rank", "jsonl>", "csv>", "txt>", "observer_table_written",
                    "exists=", "size=", "mtime=", "deprecationwarning", "heartbeat", "websocket", "ws:",
                    "<c:\\", "<c:/"
                )
                if any(n in lower_body for n in noisy_human):
                    continue
                if not any(k in s_body for k in human_keys):
                    continue
            if mode == "errors":
                if not any(x in s_body.lower() for x in ("error", "warn", "警告", "错误", "exception", "traceback")):
                    continue
            if mode == "m1":
                if "m1" not in s_body.lower():
                    continue
            if mode == "scan":
                if ("扫描" not in s_body) and ("scan" not in s_body.lower()):
                    continue
            if mode == "actions":
                # 纯文本动作行通常包含 open/close/add/reduce
                if not any(x in s_body.lower() for x in (" open ", " close ", " add ", " reduce ", " increase ", " hedge ", "下单", "平仓", "开仓", "加仓", "减仓")):
                    continue

            # 轻度噪声去除
            noisy = ("DeprecationWarning", "DEBUG", "ping", "heartbeat", "心跳", "resource", "websocket", "ws:")
            if mode != "all" and any(n.lower() in s_body.lower() for n in noisy):
                continue

            rendered = s_body

        if not rendered:
            continue

        # 多行拼接时保留时间戳在第一行
        if "\n" in rendered:
            parts = rendered.splitlines()
            parts = [p.rstrip() for p in parts if p.strip()]
            if not parts:
                continue
            parts[0] = ts_prefix + parts[0]
            rendered = "\n".join(parts)
        else:
            rendered = ts_prefix + rendered

        if keep(rendered):
            out.append(rendered)

    # 防止太长：只取尾部
    if len(out) > 1200:
        out = out[-1200:]
    return out



def _extract_scan_times(raw_lines: List[str]) -> Tuple[str, str]:
    """从日志中提取本次/下次扫描时间（兼容中文/乱码/纯数字进度条等多种格式）。"""
    scan_now = "-"
    scan_next = "-"
    # 1) 优先：明确的中文标记
    for ln in reversed(raw_lines):
        s = (ln or "").strip()
        if not s:
            continue

        txt = s
        ts_dt = None

        if s.startswith("{"):
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    ts_dt = _ts_to_dt_str(obj.get("ts"))
                    txt = " ".join([str(obj.get(k) or "") for k in ("title","msg","message","text","line")]).strip() or s
            except Exception:
                txt = s

        m = re.search(r"预计下次扫描\s*[:=：]?\s*([0-9\-: ]{19})", txt)
        if m and scan_next == "-":
            scan_next = m.group(1).strip()

        m = re.search(r"下轮预计开始\s*[:=：]?\s*([0-9\-: ]{19})", txt)
        if m and scan_next == "-":
            scan_next = m.group(1).strip()

        if (("扫描开始" in txt) or ("轮次开始" in txt) or ("开始时间" in txt)) and scan_now == "-":
            if ts_dt and (("扫描开始" in txt) or ("轮次开始" in txt)):
                scan_now = ts_dt
            else:
                m2 = re.search(r"(?:开始时间\s*[:=：]?\s*)?(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", txt)
                scan_now = m2.group(1).strip() if m2 else scan_now

        if scan_now != "-" and scan_next != "-":
            break

    # 2) 回退：同一行出现两个 datetime（例如：[ts] 0% | ... | next_ts ...）
    if scan_now == "-" or scan_next == "-":
        dt2 = re.compile(r"\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\].*?(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")
        for ln in reversed(raw_lines):
            s = (ln or "").strip()
            m = dt2.search(s)
            if not m:
                continue
            if scan_now == "-":
                scan_now = m.group(1)
            if scan_next == "-":
                scan_next = m.group(2)
            break

    # 3) 兜底：仅识别到“0% |”这类起始行时，scan_now 用 [] 里的时间
    if scan_now == "-":
        dt1 = re.compile(r"\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\]\s*0%\s*\|")
        for ln in reversed(raw_lines):
            s = (ln or "").strip()
            m = dt1.search(s)
            if m:
                scan_now = m.group(1)
                break

    return scan_now, scan_next



def _slice_since_last_scan(raw_lines: List[str], max_lines: int = 2200) -> List[str]:
    """只取最近一次“扫描开始”之后的日志片段，用于 M1 运行情况去噪。"""
    if not raw_lines:
        return []
    key_marks = ("扫描开始", "0% | 扫描开始", "0%| 扫描开始", "SCAN_START", "scan start")
    idx = 0
    for i in range(len(raw_lines) - 1, -1, -1):
        s = str(raw_lines[i] or "")
        if any(k in s for k in key_marks):
            idx = i
            break
    sliced = raw_lines[idx:]
    if max_lines and len(sliced) > max_lines:
        sliced = sliced[-max_lines:]
    return sliced



def _slice_recent_rounds(raw_lines: List[str], max_rounds: int = LOG_MAX_ROUNDS_KEEP, fallback_lines: int = LOG_TAIL_FALLBACK_LINES) -> List[str]:
    """只保留最近 N 轮日志，避免面板长期运行时日志解析/渲染越来越重。

    口径：优先按“轮次开始”切片；若日志里暂无轮次标记，则回退为仅保留最后若干行。
    """
    if not raw_lines:
        return []
    try:
        rounds = 0
        start_idx = 0
        for i in range(len(raw_lines) - 1, -1, -1):
            s = str(raw_lines[i] or "")
            if "轮次开始" in s:
                rounds += 1
                if rounds >= int(max_rounds):
                    start_idx = i
                    break
        sliced = raw_lines[start_idx:] if rounds >= int(max_rounds) else list(raw_lines)
        if fallback_lines and len(sliced) > int(fallback_lines):
            sliced = sliced[-int(fallback_lines):]
        return sliced
    except Exception:
        return raw_lines[-int(fallback_lines):] if fallback_lines and len(raw_lines) > int(fallback_lines) else list(raw_lines)


def _count_today_scans(raw_lines: List[str]) -> int:
    """统计今日扫描次数：优先匹配中文“扫描开始”，否则匹配“[ts] 0% |”起始行。"""
    today = datetime.now().date()
    cnt = 0
    # A) 中文标记
    for ln in raw_lines:
        ss = (ln or "").strip()
        if not ss:
            continue
        txt = ss
        ts_dt = None
        if ss.startswith("{"):
            try:
                obj = json.loads(ss)
                if isinstance(obj, dict):
                    ts = obj.get("ts")
                    if ts is not None:
                        try:
                            ts_dt = datetime.fromtimestamp(float(ts))
                        except Exception:
                            ts_dt = None
                    txt = " ".join([str(obj.get(k) or "") for k in ("title","msg","message","text","line")]).strip() or ss
            except Exception:
                txt = ss

        if "扫描开始" in txt:
            if ts_dt and ts_dt.date() == today:
                cnt += 1
                continue
            m = re.search(r"(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}:\d{2}", txt)
            if m:
                try:
                    d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
                    if d == today:
                        cnt += 1
                except Exception:
                    pass
            else:
                cnt += 1

    if cnt > 0:
        return cnt

    # B) 回退：0% 起始行（更接近你命令行看到的）
    dt0 = re.compile(r"\[(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}:\d{2}\]\s*0%\s*\|")
    for ln in raw_lines:
        s = (ln or "").strip()
        m = dt0.search(s)
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            if d == today:
                cnt += 1
        except Exception:
            pass
    return cnt




def _parse_queue_items_from_lines(lines: List[str], max_items: int = 12) -> List[dict]:
    """从全量日志里解析“排队/入队”信息（尽量接近真实队列）。
    优先：队列快照块（若 bot 有打印）
    回退：最近 ENQUEUE/入队 行（近似展示）
    """
    if not lines:
        return []

    # 1) 尝试抓“队列快照”块（最接近真实排队）
    snap_keys = ("队列", "QUEUE", "M1_QUEUE", "排队")
    for i in range(len(lines) - 1, -1, -1):
        s = (lines[i] or "").strip()
        if not s:
            continue
        if any(k in s for k in snap_keys) and ("====" in s or "列表" in s or "SNAPSHOT" in s.upper()):
            items: List[dict] = []
            for j in range(i + 1, min(len(lines), i + 30)):
                ln = (lines[j] or "").strip()
                if not ln:
                    break
                coin = "-"
                mm = re.search(r"(?:coin|币种)[^=]*=\s*([A-Za-z0-9_\-]+)", ln, flags=re.I)
                if mm:
                    coin = mm.group(1).upper()
                module = "-"
                mm = re.search(r"(?:module|mod|模块)[^=]*=\s*([A-Za-z0-9_\-]+)", ln, flags=re.I)
                if mm:
                    module = mm.group(1).upper()
                else:
                    for k in ("M10", "M9", "B4", "B3", "B2", "B1", "M1"):
                        if k in ln:
                            module = k
                            break
                action = "-"
                mm = re.search(r"(?:action|动作)[^=]*=\s*([A-Za-z_]+)", ln, flags=re.I)
                if mm:
                    action = mm.group(1).lower()
                else:
                    if "平仓" in ln:
                        action = "close"
                    elif "开仓" in ln:
                        action = "open"
                    elif "补腿" in ln:
                        action = "hedge"
                    elif "补仓" in ln or "加仓" in ln:
                        action = "add"
                amt = 0.0
                mm = re.search(r"(?:usd|notional|amount|金额|intent|std)\D*=\s*([0-9][0-9,]*(?:\.[0-9]+)?)", ln, flags=re.I)
                if mm:
                    try:
                        amt = float(mm.group(1).replace(",", ""))
                    except Exception:
                        amt = 0.0
                if coin != "-" and (action != "-" or module != "-"):
                    items.append({"coin": coin, "module": module, "action": action, "notional_u": amt, "platform": "-", "side": "-", "time": "-"})
                if len(items) >= max_items:
                    break
            if items:
                return items[:max_items]

    # 2) 回退：抓最近入队事件
    items: List[dict] = []
    for ln in reversed(lines):
        s = (ln or "").strip()
        if not s:
            continue
        low = s.lower()
        if ("enqueue" not in low) and ("queued_wait" not in low) and ("mutex_wait" not in low) and ("入队" not in s) and ("m1_queue" not in low) and ("排队" not in s):
            continue

        t = "-"
        mt = re.search(r"\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\]", s)
        if mt:
            t = mt.group(1).split(" ")[1]

        coin = "-"
        mm = re.search(r"coin[^=]*=\s*([A-Za-z0-9_\-]+)", s)
        if mm:
            coin = mm.group(1).upper()

        module = "-"
        mm = re.search(r"(?:module|mod)[^=]*=\s*([A-Za-z0-9_\-]+)", s, flags=re.I)
        if mm:
            module = mm.group(1).upper()
        else:
            for k in ("M10", "M9", "B4", "B3", "B2", "B1", "M1"):
                if k in s:
                    module = k
                    break

        action = "-"
        mm = re.search(r"action[^=]*=\s*([A-Za-z_]+)", s, flags=re.I)
        if mm:
            action = mm.group(1).lower()
        else:
            if "平仓" in s:
                action = "close"
            elif "开仓" in s:
                action = "open"
            elif "补腿" in s:
                action = "hedge"
            elif "补仓" in s or "加仓" in s:
                action = "add"

        amt = 0.0
        mm = re.search(r"(?:usd|notional|amount|金额|intent|std)\D*=\s*([0-9][0-9,]*(?:\.[0-9]+)?)", s, flags=re.I)
        if mm:
            try:
                amt = float(mm.group(1).replace(",", ""))
            except Exception:
                amt = 0.0


        platform = "-"
        mp = re.search(r"(?:platform|plat)[^=]*=\s*([A-Za-z0-9_\-]+)", s, flags=re.I)
        if mp:
            platform = mp.group(1).upper()
        else:
            if re.search(r"\bEXT\b|EXT（平台）|^EXT\s", s):
                platform = "EXT"
            elif re.search(r"\bVAR\b|VAR（平台）|^VAR\s", s):
                platform = "VAR"
        if coin != "-" and (action != "-" or module != "-"):
            items.append({"coin": coin, "module": module, "action": action, "notional_u": amt, "platform": platform, "side": "-", "time": t})
        if len(items) >= max_items:
            break


    # 3) 回退二：从 [M1_ACCEPT] 里抓“排队等待”的请求（mutex_wait/queued_wait）
    # 说明：这些是“预计队列”，不代表已成交；用于顶部队首/下一条与 M1 预计队列展示
    if len(items) < max_items:
        for i in range(len(lines) - 1, -1, -1):
            s = (lines[i] or "").strip()
            if (not s) or ("[M1_ACCEPT]" not in s):
                continue
            if ("mutex_wait" not in s) and ("queued_wait" not in s) and ("queued" not in s):
                continue

            # 时间
            tm = "-"
            mt = re.search(r"\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\]", s)
            if mt:
                try:
                    tm = mt.group(1)[11:19]
                except Exception:
                    tm = "-"

            def _pick(pat: str) -> str:
                mm = re.search(pat, s)
                return (mm.group(1).strip() if mm else "")

            module_raw = _pick(r"\bmodule=([A-Za-z0-9_\-+/]+)")
            coin = (_pick(r"\bcoin=([A-Za-z0-9_\-]+)") or "-").upper()
            plat = (_pick(r"\bplatform=([A-Za-z0-9_\-]+)") or "-").upper()
            action = (_pick(r"\baction=([A-Za-z0-9_\-]+)") or "-").lower()
            side = (_pick(r"\bside=([A-Za-z]+)") or "").upper()
            intent = _safe_float((_pick(r"\bintent=([0-9][0-9,]*(?:\.[0-9]+)?)") or "").replace(",", ""), 0.0)
            std = _safe_float((_pick(r"\bstd=([0-9][0-9,]*(?:\.[0-9]+)?)") or "").replace(",", ""), 0.0)
            amt = std if std > 0 else intent

            mod = _norm_module(module_raw)
            if mod == "-" and module_raw:
                mod = module_raw

            if coin == "-" or plat == "-":
                continue

            items.append({"time": tm, "coin": coin, "platform": plat, "module": mod, "action": action, "amount": float(amt or 0.0), "side": side or "-"})
            if len(items) >= max_items:
                break


    return list(reversed(items))

def _case_get(d: dict, keys: List[str]) -> Any:
    """字典键大小写不敏感取值。"""
    if not isinstance(d, dict):
        return None
    lower_map = {str(k).lower(): k for k in d.keys()}
    for k in keys:
        kk = lower_map.get(str(k).lower())
        if kk is not None:
            return d.get(kk)
    return None



def _parse_balance_summary_from_text(lines: List[str]) -> Tuple[float, float, float, bool, str]:
    """从命令行镜像日志中解析余额汇总（EXT/VAR/TOTAL）。

    兼容两种情况：
    1) 有“余额汇总”块（推荐）
    2) 没有块标题，但出现了类似 'EXT Equity: 1234.56' / 'VAR Total : 2345.67' / 'TOTAL: 3580.23'
    """
    try:
        # 优先：最近一次“余额汇总”块
        idx = -1
        for i in range(len(lines) - 1, -1, -1):
            if "余额汇总" in (lines[i] or ""):
                idx = i
                break

        if idx >= 0:
            chunk = [ln for ln in lines[idx: idx + 120] if (ln or "").strip()]
        else:
            # 回退：在尾部窗口里直接搜关键数字
            chunk = [ln for ln in lines[-2000:] if (ln or "").strip()]

        joined = "\n".join(chunk)

        def _pick_float(pat: str) -> Optional[float]:
            m = re.search(pat, joined)
            if not m:
                return None
            try:
                return float(m.group(1))
            except Exception:
                return None

        e = _pick_float(r"EXT\s*Equity\s*:\s*([0-9]+(?:\.[0-9]+)?)")
        v = _pick_float(r"VAR\s*Total\s*: ?\s*([0-9]+(?:\.[0-9]+)?)")
        t = _pick_float(r"\bTOTAL\b\s*: ?\s*([0-9]+(?:\.[0-9]+)?)")

        # 若只拿到部分，用可推导关系补齐
        if t is None and (e is not None or v is not None):
            if e is None:
                e = 0.0
            if v is None:
                v = 0.0
            t = float(e) + float(v)
        if e is None and t is not None and v is not None:
            e = float(t) - float(v)
        if v is None and t is not None and e is not None:
            v = float(t) - float(e)

        if e is None and v is None and t is None:
            return 0.0, 0.0, 0.0, False, "balance numbers not found in stdout"

        e = float(e or 0.0)
        v = float(v or 0.0)
        t = float(t or (e + v))
        ok = (t > 0.0) or (e > 0.0) or (v > 0.0)
        return e, v, t, ok, "ok"
    except Exception as e:
        return 0.0, 0.0, 0.0, False, f"balance parse error: {e}"



def _find_last_block(lines: List[str], marker: str, max_follow: int = 120) -> List[str]:
    """找最近一次包含 marker 的块，并返回从 marker 行开始的后续若干行（到空行或下一个 ==== 块）。"""
    idx = -1
    for i in range(len(lines) - 1, -1, -1):
        if marker in (lines[i] or ""):
            idx = i
            break
    if idx < 0:
        return []
    out: List[str] = []
    for j in range(idx, min(len(lines), idx + max_follow)):
        s = (lines[j] or "").rstrip("\n")
        if not s.strip():
            # 连续空行视为块结束
            if out:
                break
            continue
        if j > idx and s.strip().startswith("===="):
            break
        out.append(s.strip())
    return out


def _parse_positions_tables_from_text(lines: List[str]) -> Tuple[Dict[str, dict], Dict[str, dict], bool, str]:
    """从命令行镜像日志中解析 EXT/VAR 持仓表。

    返回：
    - ext_positions: coin -> {side, notional_u, upnl_u, pct, price}
    - var_positions: coin -> ...
    """
    try:
        def parse_one(marker: str) -> Dict[str, dict]:
            block = _find_last_block(lines, marker, max_follow=220)
            if not block:
                return {}
            # 跳过标题行、表头
            rows = []
            for s in block[1:]:
                if "|" not in s:
                    continue
                # 过滤表头
                if "币种" in s and "名义" in s:
                    continue
                rows.append(s)
            out: Dict[str, dict] = {}
            for r in rows:
                parts = [x.strip() for x in r.split("|")]
                if len(parts) < 6:
                    continue
                coin = parts[0].upper()
                side = parts[1]
                notional = _safe_float(parts[2].replace(",", ""), 0.0)
                upnl = _safe_float(parts[3].replace(",", ""), 0.0)
                pct_txt = parts[4]
                pct = None
                try:
                    pct = float(pct_txt.replace("%", "").replace(",", ""))
                except Exception:
                    pct = None
                price = _safe_float(parts[5].replace(",", ""), 0.0)
                out[coin] = {
                    "coin": coin,
                    "side": side,
                    "notional_u": float(notional),
                    "upnl_u": float(upnl),
                    "pct": pct,
                    "price": float(price),
                }
            return out

        ext = parse_one("==== EXT 持仓列表")
        var = parse_one("==== VAR 持仓列表")
        ok = bool(ext or var)
        return ext, var, ok, ("ok" if ok else "positions blocks not found")
    except Exception as e:
        return {}, {}, False, f"parse error: {e}"


def _positions_to_notional_and_upnl(ext: Dict[str, dict], var: Dict[str, dict]) -> Tuple[float, float, float, Dict[str, float]]:
    e_notional = sum(float(v.get("notional_u") or 0.0) for v in ext.values())
    v_notional = sum(float(v.get("notional_u") or 0.0) for v in var.values())
    upnl_map: Dict[str, float] = {}
    for d in (ext, var):
        for coin, it in d.items():
            upnl_map[coin] = upnl_map.get(coin, 0.0) + float(it.get("upnl_u") or 0.0)
    return float(e_notional), float(v_notional), float(v_notional - e_notional), upnl_map


def _sum_positions_notional(x: Any) -> float:
    """对 EXT/VAR 持仓合计名义价值（U）。"""
    s = 0.0
    if isinstance(x, list):
        for it in x:
            if isinstance(it, dict):
                s += _safe_float(_case_get(it, ["notional_u", "notional", "value_usd", "usd"]), 0.0)
    elif isinstance(x, dict):
        for _, it in x.items():
            if isinstance(it, dict):
                s += _safe_float(_case_get(it, ["notional_u", "notional", "value_usd", "usd"]), 0.0)
    return float(s)


def _collect_upnl_by_coin(x: Any, out: Dict[str, float]) -> None:
    """按币种汇总 UPNL（未实现盈亏）"""
    if isinstance(x, list):
        for it in x:
            if not isinstance(it, dict):
                continue
            coin = str(_case_get(it, ["coin", "symbol", "asset"]) or "").upper().strip()
            if not coin:
                continue
            upnl = _safe_float(_case_get(it, ["upnl", "upnl_u", "upnl_usd", "u_pnl", "u_pnl_u", "unrealized_pnl", "unrealized_pnl_u", "unrealized_pnl_usd", "pnl_unrealized", "pnl_unrealized_u", "unrealizedPnl"]), 0.0)
            out[coin] = out.get(coin, 0.0) + float(upnl)
    elif isinstance(x, dict):
        for _, it in x.items():
            if not isinstance(it, dict):
                continue
            coin = str(_case_get(it, ["coin", "symbol", "asset"]) or "").upper().strip()
            if not coin:
                continue
            upnl = _safe_float(_case_get(it, ["upnl", "upnl_u", "upnl_usd", "u_pnl", "u_pnl_u", "unrealized_pnl", "unrealized_pnl_u", "unrealized_pnl_usd", "pnl_unrealized", "pnl_unrealized_u", "unrealizedPnl"]), 0.0)
            out[coin] = out.get(coin, 0.0) + float(upnl)


def _ls_side_tag(side: Any) -> str:
    """把 side/方向 归一为 long/short/unknown。

    兼容字段示例：
    - long/short
    - buy/sell
    - LONG/SHORT
    - positionSide=LONG/SHORT
    - 中文：多/空/买/卖/开多/开空
    """
    try:
        s = str(side or "").strip().lower()
        if not s:
            return "unknown"

        # 中文/常见别名先判定（避免后续子串误伤）
        if s in ("多", "多单", "多头", "买", "买入", "开多", "做多", "long", "l", "b", "buy"):
            return "long"
        if s in ("空", "空单", "空头", "卖", "卖出", "开空", "做空", "short", "s", "sh", "sell"):
            return "short"

        # 子串判定：LONG/SHORT、open_long/open_short 等
        if "long" in s or s == "buy" or s.startswith("buy_") or s.startswith("open_long") or "开多" in s or "做多" in s or "多" == s:
            return "long"
        if "short" in s or s == "sell" or s.startswith("sell_") or s.startswith("open_short") or "开空" in s or "做空" in s or "空" == s:
            return "short"

        # 常见：posSide=LONG/SHORT
        if s in ("l", "b"):
            return "long"
        if s in ("s", "sh"):
            return "short"
        return "unknown"
    except Exception:
        return "unknown"


def _calc_long_short_stats(x: Any) -> Tuple[float, float, int, int]:
    """统计某平台持仓的多/空总名义与笔数。

    - x 可能是 list[dict]（positions.json 常见）
    - 也可能是 dict[coin]->dict（stdout 解析后的 ext_pos2/var_pos2）
    """
    long_u = 0.0
    short_u = 0.0
    cnt_long = 0
    cnt_short = 0

    def _iter_items(y: Any):
        if isinstance(y, list):
            for it in y:
                if isinstance(it, dict):
                    yield it
        elif isinstance(y, dict):
            for _, it in y.items():
                if isinstance(it, dict):
                    yield it

    for it in _iter_items(x):
        side = _ls_side_tag(_case_get(it, ["side", "dir", "positionSide", "pos_side", "position_side"]))
        notional = _safe_float(_case_get(it, ["notional_u", "notional", "value_usd", "usd", "amount_u", "amt", "size_usd", "notionalUsd", "notionalUSD"]), 0.0)
        notional = abs(float(notional))
        if notional <= 0:
            continue
        if side == "long":
            long_u += notional
            cnt_long += 1
        elif side == "short":
            short_u += notional
            cnt_short += 1
        else:
            # unknown：不纳入多空比，避免把异常字段算进去污染比例
            continue

    return float(long_u), float(short_u), int(cnt_long), int(cnt_short)



def _calc_long_short_by_coin(x: Any) -> dict:
    """Per-coin long/short notional and counts.

    Returns dict like:
      {"BTC": {"long_u": 120.0, "short_u": 80.0, "cnt_long": 1, "cnt_short": 1}, ...}

    Accepts either list[dict] (positions.json style) or dict[coin]->dict (stdout parsed style).
    """
    out = {}

    def _iter_items(y: Any):
        if isinstance(y, list):
            for it in y:
                if isinstance(it, dict):
                    yield it
        elif isinstance(y, dict):
            for _, it in y.items():
                if isinstance(it, dict):
                    yield it

    def _pick_coin(it: dict) -> str:
        coin = _case_get(it, ["coin", "symbol", "inst", "instrument", "market", "pair", "base", "name"])
        if coin is None:
            return ""
        c = str(coin).strip().upper()
        # Clean common suffix/prefix like BTCUSDT -> BTC, BTC-PERP -> BTC
        c = re.sub(r'[^A-Z0-9]+', ' ', c).strip().split(' ')[0]
        # If looks like BTCUSDT or BTCUSD, try to split base
        for suf in ("USDT", "USD", "PERP", "SWAP"):
            if c.endswith(suf) and len(c) > len(suf):
                c = c[:-len(suf)]
                break
        return c

    for it in _iter_items(x):
        coin = _pick_coin(it)
        if not coin:
            continue
        side = _ls_side_tag(_case_get(it, ["side", "dir", "positionSide", "pos_side", "position_side"]))
        notional = _safe_float(_case_get(it, ["notional_u", "notional", "value_usd", "usd", "amount_u", "amt", "size_usd", "notionalUsd", "notionalUSD"]))
        notional = abs(float(notional))
        if notional <= 0:
            continue
        d = out.get(coin)
        if d is None:
            d = {"long_u": 0.0, "short_u": 0.0, "cnt_long": 0, "cnt_short": 0}
            out[coin] = d
        if side == "long":
            d["long_u"] += notional
            d["cnt_long"] += 1
        elif side == "short":
            d["short_u"] += notional
            d["cnt_short"] += 1
        else:
            continue

    return out


def _fmt_ls_ratio_line(name: str, long_u: float, short_u: float, cnt_long: int, cnt_short: int) -> str:
    """多空比展示行（口径：名义 Notional）。

    目标格式：
    EXT（平台E） | 多=xxxU（xx%） | 空=xxxU（xx%） | 多/空=…
    """
    total = max(0.0, float(long_u) + float(short_u))
    if total <= 0:
        return f"{name} | 多=- | 空=- | 多/空=-"

    lp = float(long_u) / total * 100.0
    sp = float(short_u) / total * 100.0

    if float(short_u) <= 0 and float(long_u) > 0:
        ls_txt = "INF"
    elif float(short_u) <= 0 and float(long_u) <= 0:
        ls_txt = "-"
    else:
        ls_txt = f"{(float(long_u) / float(short_u)):.2f}"

    return (
        f"{name} | 多={float(long_u):,.2f}U（{lp:.2f}%） | 空={float(short_u):,.2f}U（{sp:.2f}%） | 多/空={ls_txt}"
    )


@dataclass
class PanelConfig:
    bot_script: str = ""
    bot_dir: str = ""
    cdp_url: str = "http://127.0.0.1:9222"
    refresh_ms: int = REFRESH_MS

    def save(self) -> None:
        try:
            PANEL_CONFIG_FP.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    @staticmethod
    def load() -> "PanelConfig":
        try:
            if PANEL_CONFIG_FP.exists():
                obj = json.loads(PANEL_CONFIG_FP.read_text(encoding="utf-8", errors="ignore"))
                if isinstance(obj, dict):
                    cfg = PanelConfig(**{k: obj.get(k) for k in PanelConfig().__dict__.keys()})
                    cfg.bot_script = cfg.bot_script or ""
                    cfg.bot_dir = cfg.bot_dir or ""
                    cfg.cdp_url = cfg.cdp_url or "http://127.0.0.1:9222"
                    cfg.refresh_ms = int(cfg.refresh_ms or REFRESH_MS)
                    return cfg
        except Exception:
            pass
        return PanelConfig()


class BotProc:
    def __init__(self) -> None:
        self.p: Optional[subprocess.Popen] = None
        self.identity = None
        self.last_stop_reason = "not_requested"

    def is_alive(self) -> bool:
        try:
            return self.p is not None and self.p.poll() is None
        except Exception:
            return False

    @staticmethod
    def _query_identity(pid):
        # A failed query must remain unknown, never "process absent".
        try:
            pid = int(pid)
            if pid <= 0:
                return None
            command = (
                "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
                "$p=Get-CimInstance Win32_Process -Filter 'ProcessId = " + str(pid) + "' -ErrorAction Stop; "
                "if($null -eq $p){'{\"exists\":false}'}else{"
                "@{exists=$true;pid=[int]$p.ProcessId;created=$p.CreationDate.ToUniversalTime().Ticks.ToString();"
                "executable=$p.ExecutablePath;command_line=$p.CommandLine}|ConvertTo-Json -Compress}"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            )
            if result.returncode != 0:
                return None
            obj = json.loads(result.stdout.strip().lstrip("\ufeff"))
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    def start(self, bot_script: str, cdp_url: str, bot_dir: Path) -> bool:
        if not bot_script:
            return False
        if self.is_alive():
            return True
        try:
            script = Path(bot_script)
            if not script.is_absolute():
                script = bot_dir / script
            script = script.resolve()
            out_fp = bot_dir / "out" / "panel_spawn_stdout.txt"
            out_fp.parent.mkdir(parents=True, exist_ok=True)
            creationflags = (0x00000008 | 0x00000200) if os.name == "nt" else 0
            with open(out_fp, "a", encoding="utf-8", errors="ignore") as out:
                self.p = subprocess.Popen([sys.executable, str(script), "--cdp", cdp_url],
                    cwd=str(bot_dir), stdout=out, stderr=out, creationflags=creationflags)
            self.identity = None
            if os.name == "nt":
                observed = self._query_identity(self.p.pid)
                if observed and observed.get("exists"):
                    expected = dict(observed, script=str(script))
                    if _panel_state.process_identity_matches(expected, observed):
                        self.identity = expected
            if self.identity:
                (bot_dir / "out" / "panel_bot_pid.txt").write_text(
                    json.dumps(self.identity, ensure_ascii=False), encoding="utf-8")
            return True
        except Exception:
            # A launch may have succeeded before identity persistence failed.
            return self.is_alive()

    def _clear_stopped(self, bot_dir, expected):
        if bot_dir and isinstance(expected, dict):
            try:
                pid_fp = bot_dir / "out" / "panel_bot_pid.txt"
                stored = json.loads(pid_fp.read_text(encoding="utf-8"))
                if isinstance(stored, dict) and stored.get("pid") == expected.get("pid") and stored.get("created") == expected.get("created"):
                    pid_fp.unlink()
            except (OSError, ValueError):
                pass
        self.p = None
        self.identity = None

    def stop(self, bot_dir: Optional[Path] = None, reason: str = "user_close") -> bool:
        """Only report stopped after an owned process is positively confirmed gone."""
        expected = self.identity
        if self.p is not None and self.p.poll() is not None:
            self._clear_stopped(bot_dir, expected)
            self.last_stop_reason = "already_exited"
            return True
        if os.name != "nt":
            if self.p is None:
                self.last_stop_reason = "no_owned_process"
                return False
            try:
                self.p.terminate()
                self.p.wait(timeout=5)
            except Exception:
                self.last_stop_reason = "exit_not_confirmed"
                return False
            self._clear_stopped(bot_dir, expected)
            self.last_stop_reason = "exit_confirmed"
            return True
        if expected is None and self.p is not None:
            # A live Popen handle proves ownership; acquire a fresh identity if
            # its first query at launch failed. Never do this for a file-only PID.
            observed = self._query_identity(self.p.pid)
            args = getattr(self.p, "args", [])
            if observed and isinstance(args, (list, tuple)) and len(args) > 1:
                candidate = dict(observed, script=str(args[1]))
                if self.p.poll() is None and _panel_state.process_identity_matches(candidate, observed):
                    expected = candidate
        if expected is None and bot_dir:
            try:
                expected = json.loads((bot_dir / "out" / "panel_bot_pid.txt").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                expected = None
        if not isinstance(expected, dict):
            self.last_stop_reason = "unverified_or_legacy_pid"
            return False
        before = self._query_identity(expected.get("pid", 0))
        if isinstance(before, dict) and before.get("exists") is False:
            self._clear_stopped(bot_dir, expected)
            self.last_stop_reason = "already_exited"
            return True
        if not _panel_state.process_identity_matches(expected, before):
            self.last_stop_reason = "process_identity_unverified"
            return False
        try:
            result = subprocess.run(["taskkill", "/PID", str(int(expected["pid"])), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
            if result.returncode != 0:
                self.last_stop_reason = "taskkill_failed:" + str(result.returncode)
                return False
            for attempt in range(4):
                after = self._query_identity(expected["pid"])
                if _panel_state.stop_confirmed(expected, after, command_returncode=result.returncode):
                    self._clear_stopped(bot_dir, expected)
                    self.last_stop_reason = "exit_confirmed"
                    return True
                if attempt < 3:
                    time.sleep(0.2)
        except Exception:
            self.last_stop_reason = "stop_command_failed"
            return False
        self.last_stop_reason = "exit_not_confirmed"
        return False


class DataCache:
    """trades.jsonl 增量解析（用于摘要/交易额/下单提示音）"""
    def __init__(self) -> None:
        self.trades_offset = 0
        self.trade_total_u = 0.0
        self.trade_today_u = 0.0
        self.trade_today_date = date.today().isoformat()
        self.last_trade_ts = 0.0

        # 按平台拆分交易额（EXT/VAR），用于摘要上方小字展示
        self.trade_total_by_plat = {'EXT': 0.0, 'VAR': 0.0}
        self.trade_today_by_plat = {'EXT': 0.0, 'VAR': 0.0}
        # 按“本机时间”自然日累计（0-24点）
        # 用于：今日口径=当天累计；历史口径=每日叠加（即跨日累计）。
        self.trade_by_day = {}  # type: Dict[str, float]

        self.mod_cnt: Dict[str, int] = {}
        self.mod_pnl: Dict[str, float] = {}
        # 模块已使用预算（按当日累计成交额U，去重后累加）
        self.mod_used_u: Dict[str, float] = {}

        self.action_rows: List[dict] = []

        # 动作来源（trades.jsonl 或 命令行镜像）切换时需要重置 offset，避免摘要为空
        self._source_path: str = ""
        self._recent_action_hashes = deque(maxlen=600)
        self._recent_summary_sigs = deque(maxlen=SUMMARY_SIG_MAX)  # 摘要动作级去重（避免 ACCEPT/EXEC_DONE 双计）
        self._m10_partial_keys_seen = set()  # M10 step2 去重：已见 close_partial 键

        # 交易额“历史（累计）”口径：截至当前所有交易额之和。
        # 为避免只回溯尾部（例如 2MB）导致历史交易额偏小：
        # - 首次读取
        # - 来源切换
        # - 文件被截断
        # 触发一次全量扫描（只做一次，随后走增量）。
        self._need_full_scan = True

        # 最近的 M1_ACCEPT / TRACE ACCEPT 决策缓存：用于把 EXECUTOR（执行器）动作回绑到决策模块（M9/B13/M10/B2 等）
        # key=(coin, platform) -> {'ts': float, 'module': str, 'action': str, 'amount': float}
        self._recent_decisions = {}
        self._recent_decisions_order = deque(maxlen=400)

        # 最近的 EXEC(...) 命中缓存：作为二级回绑（当缺失 M1_ACCEPT 时用）
        # key=(coin, platform) -> {'ts': float, 'module': str, 'action': str, 'amount': float, 'side': str}
        self._recent_hits = {}
        self._recent_hits_order = deque(maxlen=600)


        # 最近的 EXECUTOR（执行器）下单意图缓存：用于补齐 EXEC_DONE 中缺失的方向/金额（只缓存，不直接入摘要）
        # key=(coin, platform) -> {'ts': float, 'amount': float, 'side': str, 'oc': str}
        self._recent_execu = {}
        self._recent_execu_order = deque(maxlen=600)
        # === 摘要增强：用于从纯文本日志推断口径止盈(M10)三步动作的方向/金额（否则会出现 GROUP 行金额=0）
        self._text_last_epoch_ts = 0.0  # 最近一次在命令行中看到的 epoch ts（用于给无 ts 的行定时）
        self._text_pos_cache = {}       # key=(coin, plat) -> {'side': 'LONG/SHORT/多/空', 'notional_u': float}
        self._text_m10_reopen_cache = {}  # key=coin -> reopen_u10(float)
        # 最近一次“双平/成对全平”缓存：用于在面板层过滤“先双平成功，后旧 M9 请求超时又被误标成功”的脏摘要。
        # key=coin -> {'ts': float, 'platforms': set({'EXT','VAR'}), 'module': str, 'action': str}
        self._recent_pair_close_by_coin = {}

        # 交易额严格口径：只认 trades.jsonl 真正落地成交；
        # executor_results / BOT_grid_full / 文本日志 只补摘要，不参与交易额累计。
        self.turnover_source = 'trades_jsonl_only'

    def _norm_trade_dir(self, *vals: Any) -> str:
        try:
            for v in vals:
                s = str(v or '').strip().lower()
                if s in ('long', 'buy', '多'):
                    return 'long'
                if s in ('short', 'sell', '空'):
                    return 'short'
            return ''
        except Exception:
            return ''

    def _m10_step2_dup_key(self, module: Any, action: Any, coin: Any, plat: Any, side: Any, amount_u: Any, ts: Any, dir_hint: Any = '') -> Optional[Tuple[int, str, str, str, float]]:
        try:
            mod_u = str(module or '').upper().strip()
            if 'M10' not in mod_u:
                return None
            act_l = str(action or '').lower().strip()
            if not act_l:
                return None
            if not (('close_partial' in act_l) or ('partial' in act_l) or (act_l in ('decrease', 'reduce'))):
                return None
            coin_u = str(coin or '').upper().strip()
            plat_u = _norm_platform(str(plat or '').strip())
            dir0 = self._norm_trade_dir(dir_hint, side)
            amt = round(float(abs(_safe_float(amount_u, 0.0) or 0.0)), 2)
            ts0 = float(_safe_float(ts, 0.0) or 0.0)
            bucket = int(ts0 // 10) if ts0 > 0 else 0
            return (bucket, coin_u, plat_u, dir0, amt)
        except Exception:
            return None

    def _should_skip_m10_step2_duplicate(self, module: Any, action: Any, coin: Any, plat: Any, side: Any, amount_u: Any, ts: Any, dir_hint: Any = '') -> bool:
        try:
            act_l = str(action or '').lower().strip()
            key = self._m10_step2_dup_key(module, action, coin, plat, side, amount_u, ts, dir_hint=dir_hint)
            if not key:
                return False
            if ('close_partial' in act_l) or ('partial' in act_l):
                self._m10_partial_keys_seen.add(key)
                return False
            if act_l in ('decrease', 'reduce') and key in self._m10_partial_keys_seen:
                return True
            return False
        except Exception:
            return False

    def _accumulate_turnover(self, notional_u: Any, plat: Any = '', dt: Optional[datetime] = None) -> None:
        """严格交易额累计：只在真正落地成交源上调用。"""
        try:
            amt = float(abs(_safe_float(notional_u, 0.0) or 0.0))
            if amt <= 0.0:
                return
            dkey = (dt.date().isoformat() if isinstance(dt, datetime) else date.today().isoformat())
            self.trade_total_u += amt
            self.trade_by_day[dkey] = self.trade_by_day.get(dkey, 0.0) + amt
            if dkey == self.trade_today_date:
                self.trade_today_u += amt
            plat_u = str(plat or '').upper().strip()
            if plat_u in ('EXT', 'VAR'):
                self.trade_total_by_plat[plat_u] = self.trade_total_by_plat.get(plat_u, 0.0) + amt
                if dkey == self.trade_today_date:
                    self.trade_today_by_plat[plat_u] = self.trade_today_by_plat.get(plat_u, 0.0) + amt
        except Exception:
            pass

    def bootstrap_actions_from_lines(self, lines: List[str], max_lines: int = 4200) -> int:
        """停用文本日志补摘要。

        最新口径：摘要只显示复检终态，不再从全量日志反推动作，
        彻底切断“请求/排队/待确认/半成品”混入摘要的旧链。
        """
        return 0

    def reset_day_if_needed(self) -> None:
        d = date.today().isoformat()
        if d != self.trade_today_date:
            self.trade_today_date = d
            self.trade_today_u = 0.0
            try:
                for k in self.trade_today_by_plat.keys():
                    self.trade_today_by_plat[k] = 0.0
            except Exception:
                self.trade_today_by_plat = {'EXT': 0.0, 'VAR': 0.0}
            # 按日清零模块已使用预算
            try:
                self.mod_used_u.clear()
            except Exception:
                self.mod_used_u = {}

    def _extract_pnl(self, obj: dict) -> Optional[float]:
        for k in ("realized_pnl_usd", "pnl_usd", "profit_usd", "pnl", "profit"):
            if k in obj:
                return float(_safe_float(obj.get(k), 0.0))
        req = obj.get("req") or {}
        if isinstance(req, dict):
            for k in ("realized_pnl_usd", "pnl_usd", "profit_usd", "pnl", "profit"):
                if k in req:
                    return float(_safe_float(req.get(k), 0.0))
        return None

    def _remember_pair_close(self, coin: Any, platforms: Any, ts: Any, module: Any = '', action: Any = '') -> None:
        """记录最近一次双平/成对全平，用于过滤后续漏清理旧请求造成的脏成功摘要。"""
        try:
            coin_u = str(coin or '').upper().strip()
            ts0 = float(_safe_float(ts, 0.0) or 0.0)
            if not coin_u or ts0 <= 0:
                return
            plats = set()
            if isinstance(platforms, (list, tuple, set)):
                for p in platforms:
                    pp = _norm_platform(str(p or '').upper().strip())
                    if pp in ('EXT', 'VAR'):
                        plats.add(pp)
            else:
                pp = _norm_platform(str(platforms or '').upper().strip())
                if pp in ('EXT', 'VAR'):
                    plats.add(pp)
            if not plats:
                return
            self._recent_pair_close_by_coin[coin_u] = {
                'ts': ts0,
                'platforms': plats,
                'module': str(module or '').upper().strip(),
                'action': str(action or '').lower().strip(),
            }
            if len(self._recent_pair_close_by_coin) > 128:
                old_keys = sorted(self._recent_pair_close_by_coin.items(), key=lambda kv: float((kv[1] or {}).get('ts') or 0.0))[:-96]
                for k, _ in old_keys:
                    self._recent_pair_close_by_coin.pop(k, None)
        except Exception:
            pass

    def _should_skip_stale_post_pair_close(self, obj: Dict[str, Any], *, coin: Any, plat: Any, module: Any, action: Any, side: Any, amount_u: Any, ts: Any) -> bool:
        """面板层去污：过滤“双平成功后，旧减仓请求超时却被误标成成功”的脏摘要。"""
        try:
            coin_u = str(coin or '').upper().strip()
            plat_u = _norm_platform(str(plat or '').upper().strip())
            if not coin_u or plat_u not in ('EXT', 'VAR'):
                return False
            act_l = str(action or '').lower().strip()
            mod_u = str(module or '').upper().strip()
            if mod_u == 'B2':
                return False
            if not (act_l.startswith('close') or ('close' in act_l) or ('decrease' in act_l) or ('reduce' in act_l)):
                return False
            rec = self._recent_pair_close_by_coin.get(coin_u) or {}
            if not rec:
                return False
            pair_ts = float(_safe_float(rec.get('ts'), 0.0) or 0.0)
            if pair_ts <= 0:
                return False
            if plat_u not in set(rec.get('platforms') or set()):
                return False
            ts0 = float(_safe_float(ts, 0.0) or 0.0)
            if ts0 <= 0 or ts0 < pair_ts or (ts0 - pair_ts) > 900.0:
                return False
            confirmation_state = str(obj.get('confirmation_state') or obj.get('status_label') or '').lower().strip()
            reason = str(obj.get('reason') or obj.get('error') or obj.get('message') or '').lower().strip()
            state = str(obj.get('status') or obj.get('state') or '').lower().strip()
            confirmed_success = bool(obj.get('confirmed_success')) or (str(obj.get('display_state') or '').upper().strip() == 'CONFIRMED')
            dirty = False
            if confirmation_state in ('submit_fail', 'fail', 'failed', 'reject', 'rejected', 'timeout', 'error', 'exception'):
                dirty = True
            if any(x in reason for x in ('timeout', 'submit_fail', 'worker_timeout', 'exception', 'traceback', 'error', 'reject')):
                dirty = True
            if state in ('fail', 'failed', 'error', 'exception', 'reject', 'rejected', 'timeout'):
                dirty = True
            if not (dirty and confirmed_success):
                return False
            req = obj.get('req') or {}
            req_meta = req.get('meta') if isinstance(req, dict) else {}
            req_params = req.get('params') if isinstance(req, dict) else {}
            params_meta = req_params.get('meta') if isinstance(req_params, dict) else {}
            ts_candidates = []
            for box in (obj, req if isinstance(req, dict) else {}, req_meta if isinstance(req_meta, dict) else {}, req_params if isinstance(req_params, dict) else {}, params_meta if isinstance(params_meta, dict) else {}):
                if not isinstance(box, dict):
                    continue
                for k in ('request_ts', 'enq_ts', 'deq_ts', 'queue_accept_ts', 'exec_begin_ts', 'ts'):
                    v = _safe_float(box.get(k), 0.0)
                    if v > 0:
                        ts_candidates.append(float(v))
            oldest_req_ts = min(ts_candidates) if ts_candidates else 0.0
            if oldest_req_ts <= 0 or oldest_req_ts >= pair_ts:
                return False
            amt = abs(float(_safe_float(amount_u, 0.0) or 0.0))
            if amt <= 0 or amt > 1500:
                return False
            return True
        except Exception:
            return False

    def _pick_pair_like_notional(self, req0: dict, plat_name: str) -> float:
        """给 B2/BOTH 这类父行，从 req / params / meta 中捞 EXT/VAR 对应金额。"""
        try:
            plat_key = str(plat_name or "").strip().lower()
            if not plat_key:
                return 0.0

            keys = (
                f"{plat_key}_notional_u",
                f"{plat_key}_val_u",
                f"{plat_key}_val",
                f"{plat_key}_value_u",
                f"{plat_key}_usd_u",
                f"{plat_key}_usd",
                f"{plat_key}_notional",
            )

            boxes = []
            if isinstance(req0, dict):
                boxes.append(req0)
                p0 = req0.get("params") or {}
                if isinstance(p0, dict):
                    boxes.append(p0)
                    p1 = p0.get("params") or {}
                    if isinstance(p1, dict):
                        boxes.append(p1)
                    m1 = p0.get("meta") or {}
                    if isinstance(m1, dict):
                        boxes.append(m1)
                meta0 = req0.get("meta") or {}
                if isinstance(meta0, dict):
                    boxes.append(meta0)

            for box in boxes:
                if not isinstance(box, dict):
                    continue
                for k in keys:
                    v = _safe_float(box.get(k), 0.0)
                    if v > 0:
                        return abs(float(v))
        except Exception:
            pass
        return 0.0

    def _pick_pair_like_side(self, coin: str, plat: str, fallback: str = "-") -> str:
        """优先从 _text_pos_cache 取 EXT/VAR 方向；取不到再用 fallback。"""
        try:
            c = str(coin or "").upper().strip()
            p = str(plat or "").upper().strip()
            meta = self._text_pos_cache.get((c, p), {}) if hasattr(self, "_text_pos_cache") else {}
            side = str(meta.get("side") or fallback or "-").upper().strip()
            if side == "多":
                return "LONG"
            if side == "空":
                return "SHORT"
            if side in ("LONG", "SHORT"):
                return side
        except Exception:
            pass

        fb = str(fallback or "-").upper().strip()
        if fb == "多":
            return "LONG"
        if fb == "空":
            return "SHORT"
        if fb in ("LONG", "SHORT"):
            return fb
        return "-"

    def _update_text_caches(self, line: str) -> None:
        """从命令行镜像日志里抓缓存，用于补齐摘要的方向/金额。"""
        try:
            s = (line or "").strip()
            if not s:
                return

            # 1) epoch ts 缓存：形如 [1770794193.930848] ...
            mt = re.search(r"\[(\d{10,16}(?:\.\d+)?)\]", s)
            if mt:
                try:
                    ts = float(mt.group(1))
                    # ms/us 归一到秒
                    if ts > 1e15:
                        ts = ts / 1e6
                    elif ts > 1e12:
                        ts = ts / 1e3
                    if ts > 1e9:
                        self._text_last_epoch_ts = ts
                except Exception:
                    pass

            # 2) M10 口径 reopen 金额缓存：用于 step3/open_full 金额
            if "[M10_REOPEN]" in s:
                mm = re.search(r"\bcoin\s*=\s*([A-Za-z0-9_\-]+)", s, flags=re.I)
                mu = re.search(r"\breopen_u10\s*=\s*([0-9][0-9,]*(?:\.[0-9]+)?)", s, flags=re.I)
                if mm and mu:
                    try:
                        coin = mm.group(1).upper()
                        u10 = float(mu.group(1).replace(',', ''))
                        if u10 > 0:
                            self._text_m10_reopen_cache[coin] = u10
                    except Exception:
                        pass


            # 2.5) 从“持仓进度”行抓 EXT/VAR 的 side/notional（能补齐口径止盈 step1/step2 方向/金额）
            if "持仓进度" in s and "notional_u=" in s and "side=" in s:
                mp = re.search(r"持仓进度\s+(EXT|VAR)\s+([A-Za-z0-9_\-]+)\s*\|.*?\bside=([A-Za-z]+)\b.*?\bnotional_u=([0-9][0-9,]*(?:\.[0-9]+)?)", s, flags=re.I)
                if mp:
                    try:
                        plat = mp.group(1).upper()
                        coin = mp.group(2).upper()
                        side = mp.group(3).upper()
                        notional = float(mp.group(4).replace(',', ''))
                        if notional >= 0:
                            self._text_pos_cache[(coin, plat)] = {'side': side, 'notional_u': float(notional)}
                    except Exception:
                        pass

            # 3) 从 B2/trend_tp TRACE 里抓 EXT/VAR 的 side/val（最稳定）
            if "[TRACE]" in s and "coin=" in s and "ext_side" in s and "var_side" in s:
                try:
                    mcoin = re.search(r"\bcoin\s*=\s*([A-Za-z0-9_\-]+)", s, flags=re.I)
                    coin = (mcoin.group(1).upper() if mcoin else "-")
                    js0 = s[s.find('{'):] if '{' in s else ''
                    if js0 and coin != "-":
                        obj = json.loads(js0)
                        ext_side = str(obj.get('ext_side') or '').upper()
                        var_side = str(obj.get('var_side') or '').upper()
                        ext_val = _safe_float(obj.get('ext_val') or obj.get('ext_val_u') or 0.0, 0.0)
                        var_val = _safe_float(obj.get('var_val') or obj.get('var_val_u') or 0.0, 0.0)
                        if ext_val > 0 and ext_side in ('LONG','SHORT','多','空'):
                            self._text_pos_cache[(coin, 'EXT')] = {'side': ext_side, 'notional_u': float(ext_val)}
                        if var_val > 0 and var_side in ('LONG','SHORT','多','空'):
                            self._text_pos_cache[(coin, 'VAR')] = {'side': var_side, 'notional_u': float(var_val)}
                except Exception:
                    pass

            # 4) 从 B3 missing_leg PIN 里抓 notional（big/sml）及 need=SIDE@PLAT（可补 side）
            if "[PIN][B3]" in s and "big=" in s and "sml=" in s and "coin=" in s:
                try:
                    mcoin = re.search(r"\bcoin\s*=\s*([A-Za-z0-9_\-]+)", s, flags=re.I)
                    coin = (mcoin.group(1).upper() if mcoin else "-")
                    mb = re.search(r"\bbig\s*=\s*(EXT|VAR)\s*:\s*([0-9][0-9,]*(?:\.[0-9]+)?)", s, flags=re.I)
                    ms2 = re.search(r"\bsml\s*=\s*(EXT|VAR)\s*:\s*([0-9][0-9,]*(?:\.[0-9]+)?)", s, flags=re.I)
                    mneed = re.search(r"\bneed\s*=\s*(LONG|SHORT)@?(EXT|VAR)", s, flags=re.I)
                    if coin != "-":
                        if mb:
                            p = mb.group(1).upper(); v = float(mb.group(2).replace(',', ''))
                            if v > 0:
                                cur = self._text_pos_cache.get((coin, p), {})
                                self._text_pos_cache[(coin, p)] = {'side': cur.get('side', '-'), 'notional_u': v}
                        if ms2:
                            p = ms2.group(1).upper(); v = float(ms2.group(2).replace(',', ''))
                            if v > 0:
                                cur = self._text_pos_cache.get((coin, p), {})
                                self._text_pos_cache[(coin, p)] = {'side': cur.get('side', '-'), 'notional_u': v}
                        if mneed:
                            side = mneed.group(1).upper(); plat = mneed.group(2).upper()
                            cur = self._text_pos_cache.get((coin, plat), {})
                            self._text_pos_cache[(coin, plat)] = {'side': side, 'notional_u': float(cur.get('notional_u') or 0.0)}
                except Exception:
                    pass

        except Exception:
            pass

    def _parse_action_from_text_line(self, line: str) -> Optional[dict]:
        """从命令行镜像（纯文本）解析“摘要动作”。

        目标：
        - 抓到真实下单/平单（EXECUTOR（执行器）层）并补齐：金额U、开/平、方向、盈利（若可得）
        - 用 M1_ACCEPT/EXEC(...) 做回绑，让摘要显示决策模块（M9/B13/B4/M10/B2 等），而不是执行器

        注意：本函数允许解析失败（返回 None），宁可少记也不乱记。
        """
        s = (line or "").strip()
        # 先更新缓存（即使本行不是动作，也可能包含 ts/仓位/口径信息）
        self._update_text_caches(line)

        # === 口径止盈三步：用 STEP_EXTRACT + 缓存仓位/REOPEN 金额，把 GROUP 动作拆成 EXT/VAR 多行 ===
        # 这样摘要就能展示：动作/方向/金额（而不是 platform=GROUP 金额=0 的占位行）
        if "TREND_TP_STEP_EXTRACT" in s and "coin=" in s and "win_plat" in s and "loss_plat" in s:
            try:
                mcoin = re.search(r"\bcoin\s*=\s*([A-Za-z0-9_\-]+)", s, flags=re.I)
                mwin = re.search(r"\bwin_plat\s*=\s*(EXT|VAR)", s, flags=re.I)
                mloss = re.search(r"\bloss_plat\s*=\s*(EXT|VAR)", s, flags=re.I)
                mclose = re.search(r"\bclose_notional\s*=\s*([0-9][0-9,]*(?:\.[0-9]+)?)", s, flags=re.I)
                if mcoin and mwin and mloss:
                    coin0 = mcoin.group(1).upper()
                    winp = mwin.group(1).upper()
                    lossp = mloss.group(1).upper()
                    close_u = float(mclose.group(1).replace(',', '')) if mclose else 0.0

                    def _side_cn(side_raw: str) -> str:
                        x = (side_raw or '').upper()
                        if x in ('LONG', '多'):
                            return '多'
                        if x in ('SHORT', '空'):
                            return '空'
                        return '-'

                    win_meta = self._text_pos_cache.get((coin0, winp), {}) if hasattr(self, '_text_pos_cache') else {}
                    loss_meta = self._text_pos_cache.get((coin0, lossp), {}) if hasattr(self, '_text_pos_cache') else {}
                    win_side = _side_cn(str(win_meta.get('side') or '-'))
                    loss_side = _side_cn(str(loss_meta.get('side') or '-'))
                    win_notional = float(_safe_float(win_meta.get('notional_u'), 0.0))
                    reopen_u10 = float(_safe_float(self._text_m10_reopen_cache.get(coin0), 0.0)) if hasattr(self, '_text_m10_reopen_cache') else 0.0

                    ts0 = float(_safe_float(getattr(self, '_text_last_epoch_ts', 0.0), 0.0))
                    # fallback：如果本行没带 epoch ts，用最近一次 epoch ts
                    if ts0 <= 0:
                        ts0 = 0.0

                    out = []
                    # step1 close_full@WIN
                    if win_notional > 0:
                        out.append({'ts': ts0, 'coin': coin0, 'platform': winp, 'module': 'M10_KOUJING_A', 'action': 'close_full', 'side': win_side, 'oc': '平仓', 'notional_u': win_notional})
                    # step2 close_partial_notional@LOSS
                    if close_u > 0:
                        out.append({'ts': ts0, 'coin': coin0, 'platform': lossp, 'module': 'M10_KOUJING_A', 'action': 'close_partial', 'side': loss_side, 'oc': '平仓', 'notional_u': close_u})
                    # step3 open_full@WIN（金额优先用 reopen_u10，否则回退 win_notional）
                    opu = reopen_u10 if reopen_u10 > 0 else win_notional
                    if opu > 0:
                        out.append({'ts': ts0, 'coin': coin0, 'platform': winp, 'module': 'M10_KOUJING_A', 'action': 'open_full', 'side': win_side, 'oc': '开仓', 'notional_u': opu})

                    if out:
                        return out
            except Exception:
                pass

        if not s:
            return None

                # 1) 时间戳（必须是“发生时刻”，解析不到就留空；禁止用当前时间伪造）
        ts = 0.0
        raw_s = s  # 保留原始行；后续 coin/module/action 解析可能会使用 s（可能被 json msg 覆盖）
        try:
            # 1.0) 兼容 jsonl：{"ts":..., "msg":"..."}（full_all.jsonl / module_xxx.jsonl 常见）
            if raw_s.startswith("{") and ("\"ts\"" in raw_s or "'ts'" in raw_s):
                try:
                    obj = json.loads(raw_s)
                    ts = float(_parse_ts_sec_any(obj.get("ts") or obj.get("ts_ms") or obj.get("timestamp") or obj.get("time") or 0.0) or 0.0)
                    # 用 msg 作为“正文”继续解析 coin/module/action 等字段（不影响 ts）
                    msg = obj.get("msg")
                    if isinstance(msg, str) and msg.strip():
                        s = msg.strip()
                except Exception:
                    pass

            # 1.1) 兼容 [2026-02-11 09:45:30] 这种
            if ts <= 0:
                m_ts = re.search(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", raw_s)
                if m_ts:
                    ts = datetime.strptime(m_ts.group(1), "%Y-%m-%d %H:%M:%S").timestamp()

            # 1.2) 关键补丁：兼容 [1770780768.265832] 这种 epoch 秒（浮点/整数）
            if ts <= 0:
                m_ep = re.search(r"^\s*\[(\d{10,}(?:\.\d+)?)\]\s*", raw_s)
                if m_ep:
                    ts = float(_parse_ts_sec_any(m_ep.group(1)) or 0.0)

            # 1.3) 兜底：行首就是 epoch（没带 []）
            if ts <= 0:
                m_ep2 = re.search(r"^\s*(\d{10,}(?:\.\d+)?)\b", raw_s)
                if m_ep2:
                    ts = float(_parse_ts_sec_any(m_ep2.group(1)) or 0.0)

            # 1.4) 兼容只有 HH:MM:SS 的情况（例如：11:00:01）
            if ts <= 0:
                m_hms = re.search(r"^\s*(\d{2}:\d{2}:\d{2})\b", raw_s)
                if m_hms:
                    today = date.today().isoformat()
                    ts = datetime.strptime(f"{today} {m_hms.group(1)}", "%Y-%m-%d %H:%M:%S").timestamp()

        except Exception:
            ts = 0.0

        dt = (datetime.fromtimestamp(ts) if ts > 0 else None)

        # 2) 解析 helpers
        def _m(re_pat: str) -> Optional[str]:
            mm = re.search(re_pat, s, flags=re.I)
            return mm.group(1) if mm else None

        def _norm_side(x: str) -> str:
            x = (x or "").strip().lower()
            if x in ("buy", "long", "l", "多", "longer", "bull"):
                return "多"
            if x in ("sell", "short", "s", "空", "bear"):
                return "空"
            if x in ("long", "short"):  # 兜底
                return "多" if x == "long" else "空"
            # 来自日志的 LONG/SHORT
            if x.upper() == "LONG":
                return "多"
            if x.upper() == "SHORT":
                return "空"
            return "-"

        def _infer_platform() -> str:
            # 兼容编码乱码：优先 platform=XXX，其次直接找 EXT/VAR
            p = _m(r"(?:plat(?:form)?|tform)\W*=\s*([A-Z]{2,4})")
            if p:
                return p.upper()
            m2 = re.search(r"\b(EXT|VAR)\b", s)
            return m2.group(1).upper() if m2 else "-"

        def _infer_coin() -> str:
            c = _m(r"coin\W*=\s*([A-Za-z0-9_\-]+)")
            if c:
                return c.upper()
            # 兼容 Coin:BTC / Coin（币种）：BTC
            c2 = _m(r"Coin[^A-Za-z0-9]*([A-Za-z]{2,8})")
            return c2.upper() if c2 else "-"

        def _infer_amount() -> Optional[float]:
            # 常见字段：usd= / intent= / std= / amount=
            for pat in (
                r"usd（金额）\W*=\s*([\d,]+(?:\.\d+)?)",
                r"usd\(金额\)\W*=\s*([\d,]+(?:\.\d+)?)",
                r"usd\W*=\s*([\d,]+(?:\.\d+)?)",
                r"金额\W*=\s*([\d,]+(?:\.\d+)?)",
                r"amount\W*=\s*([\d,]+(?:\.\d+)?)",
                r"intent_u\W*=\s*([\d,]+(?:\.\d+)?)",
                r"std_u\W*=\s*([\d,]+(?:\.\d+)?)",
                r"notional_u\W*=\s*([\d,]+(?:\.\d+)?)",
                # 常见 U 字段（补腿/加仓/减仓/开平）
                r"add_u\W*=\s*([\d,]+(?:\.\d+)?)",
                r"reduce_u\W*=\s*([\d,]+(?:\.\d+)?)",
                r"open_u\W*=\s*([\d,]+(?:\.\d+)?)",
                r"close_u\W*=\s*([\d,]+(?:\.\d+)?)",
                r"trade_u\W*=\s*([\d,]+(?:\.\d+)?)",
                r"std\W*=\s*([\d,]+(?:\.\d+)?)",
                r"intent\W*=\s*([\d,]+(?:\.\d+)?)",
                r"value\W*=\s*([\d,]+(?:\.\d+)?)",
            ):
                v = _m(pat)
                if v:
                    try:
                        return float(v.replace(",", ""))
                    except Exception:
                        pass
            return None

        def _infer_pnl() -> Optional[float]:
            for pat in (r"pnl[_u]*\s*=?\s*([-+]?\d+(?:\.\d+)?)", r"profit[_u]*\s*=?\s*([-+]?\d+(?:\.\d+)?)", r"realized[_ ]?pnl[_u]*\s*=?\s*([-+]?\d+(?:\.\d+)?)"):
                v = _m(pat)
                if v:
                    try:
                        return float(v)
                    except Exception:
                        pass
            return None

        # 3) 上游决策信息：M1_ACCEPT / EXEC(...) 命中行 —— 只做缓存，不直接进入摘要
        if "M1_ACCEPT" in s and "module=" in s and "coin=" in s:
            try:
                coin = _infer_coin()
                plat = _infer_platform()
                module = (_m(r"module\W*=\s*([A-Za-z0-9_+\-/]+)") or "-").strip()
                act = (_m(r"action\W*=\s*([A-Za-z0-9_+\-/]+)") or "-").strip()
                amt = _infer_amount()
                side = _norm_side(_m(r"side\W*=\s*([A-Za-z]+)") or "-")
                self._recent_decisions[(coin, plat)] = {"ts": ts, "module": module, "action": act, "amount": amt, "side": side}
                self._recent_decisions_order.append((coin, plat))
            except Exception:
                pass
            return None

        if "EXEC(" in s and "module=" in s and "coin=" in s:
            try:
                coin = _infer_coin()
                plat = _infer_platform()
                module = (_m(r"module\W*=\s*([A-Za-z0-9_+\-/]+)") or "-").strip()
                act = (_m(r"action\W*=\s*([A-Za-z0-9_+\-/]+)") or "-").strip()
                amt = _infer_amount()
                # intent_u=xxx
                iu = _m(r"intent[_u]*\s*=\s*([\d,]+(?:\.\d+)?)")
                if iu:
                    try:
                        amt = float(iu.replace(",", ""))
                    except Exception:
                        pass
                side = _norm_side(_m(r"side\W*=\s*([A-Za-z]+)") or "-")
                self._recent_hits[(coin, plat)] = {"ts": ts, "module": module, "action": act, "amount": amt, "side": side}
                self._recent_hits_order.append((coin, plat, ts))
                # 同步到 decisions 作为兜底
                self._recent_decisions[(coin, plat)] = {"ts": ts, "module": module, "action": act, "amount": amt, "side": side}
                self._recent_decisions_order.append((coin, plat))
            except Exception:
                pass
            return None

        # EXEC_DONE：作为“实际执行完成”的信号；若找得到模块/币/平台则进摘要（金额从缓存兜底）
        if "EXEC_DONE" in s and "module=" in s and "coin=" in s:
            # 成交/失败信号（执行器汇总）
            # - ok_cnt>0：认为“实际成交”
            # - ok_cnt<=0 且 fail_cnt>0：认为“执行失败/未成交”（用户仍希望在摘要中可见）
            ok_cnt = _m(r"ok_cnt\W*=\s*(\d+)")
            fail_cnt = _m(r"fail_cnt\W*=\s*(\d+)")
            ok_i = 0
            fail_i = 0
            try:
                ok_i = int(ok_cnt) if ok_cnt is not None else 0
            except Exception:
                ok_i = 0
            try:
                fail_i = int(fail_cnt) if fail_cnt is not None else 0
            except Exception:
                fail_i = 0

            status = "OK" if ok_i > 0 else ("FAIL" if fail_i > 0 else "")
            if not status:
                return None
            coin = _infer_coin()
            plat = _infer_platform()
            module = (_m(r"module\W*=\s*([A-Za-z0-9_+\-/]+)") or "-").strip()
            act = (_m(r"action\W*=\s*([A-Za-z0-9_+\-/]+)") or "-").strip()
            amt = _infer_amount()
            pnl = _infer_pnl()
            side = _norm_side(_m(r"side\W*=\s*([A-Za-z]+)") or "-")
            # 失败原因（仅 FAIL 时展示在摘要里；OK 时保持为空）
            err = (_m(r"err\W*=\s*([A-Za-z0-9_+\-./]+)") or _m(r"reason\W*=\s*([A-Za-z0-9_+\-./]+)") or "").strip()
            if status == "OK":
                err = ""

            # 用缓存补齐金额/方向
            key = (coin, plat)
            if (amt is None) or (amt <= 0):
                dec = self._recent_execu.get(key) or self._recent_hits.get(key) or self._recent_decisions.get(key) or {}
                if isinstance(dec, dict):
                    # 金额/方向/动作兜底：EXEC_DONE 行可能 action='-' 或缺字段（尤其失败场景）
                    amt = float(_safe_float(dec.get("amount"), 0.0)) or amt
                    if (not act) or (str(act).strip() in ("-", "none", "null")):
                        try:
                            act = str(dec.get("action") or act).strip() or act
                        except Exception:
                            pass
                    if side == "-":
                        side = dec.get("side") or side

            # 开/平推断：close/close_xxx -> 平仓；其它默认开仓/加仓
            oc = "平仓" if act.lower().startswith("close") or "close" in act.lower() else "开仓"
            return {
                "ts": float(ts),
                "time": (datetime.fromtimestamp(ts).strftime('%H:%M:%S') if ts > 0 else '--'),
                "coin": coin,
                "platform": plat,
                "module": module,
                "action": act,
                "oc": oc,
                "pnl": pnl,
                "side": side,
                "notional_u": float(_safe_float(amt, 0.0)) if amt else 0.0,
                "status": status,
                "err": err,
                "raw": s[:320],
            }


        # 4) submit ok（提交成功）行：部分 Bot 版本没有 EXECUTOR/EXEC_DONE
        # 例如：[B13][REPAIR] submit ok | coin=HYPE plat=EXT side=SHORT add_u=100.00 ...
        # 这里把它当作“已执行”近似，确保摘要与交易额不再为 0（尤其是 EXT（平台E））。
        if ("submit ok" in s.lower()) and ("coin=" in s.lower()) and re.search(r"(add_u|reduce_u|open_u|close_u|usd|amount|std|intent)\s*=", s, flags=re.I):
            try:
                coin = _infer_coin()
                plat = _infer_platform()
                if coin == "-":
                    return None
                # module：取第一个 [XXX]
                mm_mod = re.search(r"^\[([A-Za-z0-9_+\-/]+)\]", s)
                module = (mm_mod.group(1) if mm_mod else "-").strip()

                # 金额：优先 add_u/reduce_u/open_u/close_u，其次 std/intent/usd
                amt = _infer_amount()

                # 方向：side=SHORT/LONG
                side = _norm_side(_m(r"side\W*=\s*([A-Za-z]+)") or "-")
                if side == "-":
                    side = _norm_side(_m(r"\b(LONG|SHORT)\b") or "-")

                # 动作推断
                sl = s.lower()
                if "close_u" in sl or "close" in sl or "平" in sl:
                    act = "close"
                    oc = "平仓"
                elif "reduce_u" in sl or "decrease" in sl or "reduce" in sl or "减" in sl:
                    act = "decrease"
                    oc = "平仓"  # 口径上属于减仓/平仓
                elif "add_u" in sl or "add" in sl or "increase" in sl or "补" in sl or "加" in sl:
                    act = "add_leg" if ("leg" in sl or "补腿" in sl or module.upper() in ("B3", "B13")) else "increase"
                    oc = "开仓"
                else:
                    act = "open"
                    oc = "开仓"

                # submit ok 行：不直接入摘要，先缓存供后续 EXEC_DONE 补齐金额/方向（避免重复计数）

                try:

                    key = (coin, plat)

                    aa = float(_safe_float(amt, 0.0)) if amt else 0.0

                    if aa > 0:

                        # submit ok 只作为低优先级兜底：放进 recent_decisions（决策缓存），不要抢 recent_execu（执行器缓存）
                        self._recent_decisions[key] = {"ts": float(ts), "module": module, "action": act, "amount": aa, "side": side, "oc": oc}
                        self._recent_decisions_order.append(key)
                        while len(self._recent_decisions_order) > 400:
                            k0 = self._recent_decisions_order.popleft()
                            if k0 != key:
                                # 仅当 dict 里还是旧引用才删（避免同 key 更新后误删）
                                self._recent_decisions.pop(k0, None)
                except Exception:

                    pass

                return None
            except Exception:
                return None

        # 4) 执行器下单/平单（核心）
        # 需要出现 Open/Close（或中文 开仓/平仓）+ coin + 金额
        if ("EXECUTOR" not in s) and ("执行器" not in s) and ("Open" not in s) and ("Close" not in s) and ("开仓" not in s) and ("平仓" not in s):
            return None

        coin = _infer_coin()
        plat = _infer_platform()
        if coin == "-":
            return None

        # 开/平
        oc = "-"
        if re.search(r"\bOpen\b|开仓|开单", s, flags=re.I):
            oc = "开仓"
        elif re.search(r"\bClose\b|平仓|平单", s, flags=re.I):
            oc = "平仓"

        if oc == "-":
            return None

        amt = _infer_amount()
        pnl = _infer_pnl()

        side = _norm_side(_m(r"side\W*=\s*([A-Za-z]+)") or "-")
        if side == "-":
            # 有些行用 SHORT/LONG
            side = _norm_side(_m(r"\b(LONG|SHORT)\b") or "-")

        # 只做“执行器层”缓存：用于给 EXEC_DONE 补齐方向/金额；不把未确认成交的动作直接写进摘要/预算/模块计数
        try:
            key = (coin, plat)
            aa = float(_safe_float(amt, 0.0)) if amt else 0.0
            self._recent_execu[key] = {"ts": float(ts), "amount": aa, "side": side, "oc": oc}
            self._recent_execu_order.append((coin, plat, float(ts)))
            # 控制缓存体积
            while len(self._recent_execu_order) > 600:
                _c, _p, _t = self._recent_execu_order.popleft()
                if (_c, _p) in self._recent_execu:
                    self._recent_execu.pop((_c, _p), None)
        except Exception:
            pass
        return None

    def _norm_decision_module(self, module: str) -> str:
        m = str(module or '').strip().upper()
        if not m or m == '-':
            return '-'
        if m in ('B2/M10', 'M10/B2', 'B2+M10', 'M10+B2', 'B2-M10', 'M10-B2'):
            return 'M10+B2'
        return m

    def _parse_m1_accept_line(self, s: str) -> Optional[dict]:
        if 'M1_ACCEPT' not in (s or ''):
            return None
        ts = 0.0
        # 支持 [1770555422.5523] 这类 epoch 秒/毫秒/微秒（含数字字符串）
        mn = re.search(r'\[(\d{10,16}(?:\.\d+)?)\]', s)
        if mn:
            try:
                ts = float(_parse_ts_sec_any(mn.group(1)) or 0.0)
            except Exception:
                ts = 0.0
        mt = None
        if ts <= 0:
            mt = re.search(r'\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\]', s)
        if mt:
            try:
                ts = datetime.strptime(mt.group(1), '%Y-%m-%d %H:%M:%S').timestamp()
            except Exception:
                ts = 0.0

        def _pick(pat: str) -> str:
            mm = re.search(pat, s)
            return (mm.group(1).strip() if mm else '')

        module_raw = _pick(r'\bmodule=([A-Za-z0-9_\-+/]+)')
        coin = _pick(r'\bcoin=([A-Za-z0-9_\-]+)').upper() or '-'
        platform = _pick(r'\bplatform=([A-Za-z0-9_\-]+)').upper() or '-'
        action = _pick(r'\baction=([A-Za-z0-9_\-]+)').lower() or '-'

        side = (_pick(r'\bside=([A-Za-z]+)') or _pick(r'\bposSide=([A-Za-z]+)') or '').upper().strip() or '-'
        intent = _safe_float(_pick(r'\bintent=([0-9][0-9,]*(?:\.[0-9]+)?)').replace(',', ''), 0.0)
        std = _safe_float(_pick(r'\bstd=([0-9][0-9,]*(?:\.[0-9]+)?)').replace(',', ''), 0.0)
        amt = std if std > 0 else intent

        module = self._norm_decision_module(module_raw)
        if coin == '-' or platform == '-' or module == '-':
            return None
        return {'ts': ts, 'coin': coin, 'platform': platform, 'module': module, 'action': action, 'amount': float(amt or 0.0), 'side': side}

    def _parse_trace_accept_line(self, s: str) -> Optional[dict]:
        if ('[TRACE]' not in (s or '')) or ('ACCEPT' not in s):
            return None
        mj = re.search(r'ACCEPT\s+(\{.*\})\s*$', s)
        if not mj:
            return None
        try:
            obj = json.loads(mj.group(1))
            if not isinstance(obj, dict):
                return None
        except Exception:
            return None

        ts = 0.0
        # 支持 [1770555422.5523] 这类 epoch 秒/毫秒/微秒（含数字字符串）
        mn = re.search(r'\[(\d{10,16}(?:\.\d+)?)\]', s)
        if mn:
            try:
                ts = float(_parse_ts_sec_any(mn.group(1)) or 0.0)
            except Exception:
                ts = 0.0
        mt = None
        if ts <= 0:
            mt = re.search(r'\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\]', s)
        if mt:
            try:
                ts = datetime.strptime(mt.group(1), '%Y-%m-%d %H:%M:%S').timestamp()
            except Exception:
                ts = 0.0

        module = self._norm_decision_module(obj.get('module') or '')
        coin = str(obj.get('coin') or '-').upper()
        platform = str(obj.get('platform') or '-').upper()
        action = str(obj.get('action_type') or obj.get('action') or '-').lower()
        amt = _safe_float(obj.get('std_amount') or obj.get('intent_amount') or obj.get('amount') or 0.0, 0.0)

        if coin == '-' or platform == '-' or module == '-':
            return None
        # 方向（若可得）：兼容不同字段名
        side_raw = (obj.get('side') or obj.get('side_hint') or obj.get('position_side') or obj.get('pos_side')
                    or (obj.get('req') or {}).get('side') or (obj.get('req') or {}).get('side_hint') or '-')
        side_s = str(side_raw or '').upper().strip()
        if side_s in ('BUY', 'LONG', 'B'):
            side_s = 'LONG'
        elif side_s in ('SELL', 'SHORT', 'S'):
            side_s = 'SHORT'
        elif side_s not in ('LONG', 'SHORT'):
            side_s = '-'

        return {'ts': ts, 'coin': coin, 'platform': platform, 'module': module, 'action': action, 'amount': float(amt or 0.0), 'side': side_s}

    def _remember_decision(self, dec: dict) -> None:
        try:
            coin = str(dec.get('coin') or '-').upper()
            platform = str(dec.get('platform') or '-').upper()
            if coin == '-' or platform == '-':
                return
            key = (coin, platform)
            self._recent_decisions[key] = dec
            self._recent_decisions_order.append(key)

            # 轻量清理：超过 1 小时的决策缓存丢弃
            now_ts = time.time()
            cutoff = now_ts - 3600.0
            # 只做少量抽样清理，避免每行都全表扫描
            if len(self._recent_decisions_order) > 200:
                for _ in range(20):
                    try:
                        k = self._recent_decisions_order[0]
                    except Exception:
                        break
                    d = self._recent_decisions.get(k) or {}
                    dts = _safe_float(d.get('ts'), 0.0)
                    if dts > 0 and dts < cutoff:
                        try:
                            self._recent_decisions_order.popleft()
                        except Exception:
                            break
                        try:
                            # 若还是同一个引用，再删（避免同 key 被更新后误删）
                            cur = self._recent_decisions.get(k) or {}
                            if _safe_float(cur.get('ts'), 0.0) == dts:
                                self._recent_decisions.pop(k, None)
                        except Exception:
                            pass
                    else:
                        break
        except Exception:
            return

    def _bind_action_to_decision(self, parsed: dict, window_sec: float = 600.0) -> dict:
        """把执行器（EXECUTOR（执行器））层的动作回绑到上游决策模块。

        绑定优先级：
        1) 最近的 M1_ACCEPT / TRACE ACCEPT（最权威）
        2) 最近的 EXEC(...) 命中（作为二级回绑，防止摘要只显示执行器）
        """
        try:
            if not isinstance(parsed, dict):
                return parsed

            module0 = str(parsed.get("module") or "-")
            m0u = module0.upper()

            # 只回绑“执行器口径”的行；其他模块行保持原样
            if ("EXECUTOR" not in m0u) and ("执行器" not in module0):
                return parsed

            coin = str(parsed.get("coin") or "-").upper()
            platform = str(parsed.get("platform") or "-").upper()
            if coin == "-" or platform == "-":
                return parsed

            ts = float(_safe_float(parsed.get("ts"), 0.0))

            def _apply(d: dict) -> dict:
                if not isinstance(d, dict):
                    return parsed
                # module/action
                if d.get("module"):
                    parsed["module"] = d.get("module")
                if d.get("action"):
                    parsed["action"] = d.get("action")
                # amount/side（如果解析行自身缺失）
                if (parsed.get("notional_u") is None or float(_safe_float(parsed.get("notional_u"), 0.0)) <= 0) and d.get("amount") is not None:
                    parsed["notional_u"] = float(_safe_float(d.get("amount"), 0.0))
                if (not parsed.get("side") or parsed.get("side") == "-") and d.get("side"):
                    parsed["side"] = d.get("side")
                return parsed

            # 1) 决策缓存
            dec = self._recent_decisions.get((coin, platform))
            if isinstance(dec, dict) and float(_safe_float(dec.get("ts"), 0.0)) > 0:
                if ts <= 0 or abs(ts - float(dec.get("ts"))) <= float(window_sec):
                    return _apply(dec)

            # 2) 二级回绑：命中缓存
            hit = getattr(self, "_recent_hits", {}).get((coin, platform))
            if isinstance(hit, dict) and float(_safe_float(hit.get("ts"), 0.0)) > 0:
                if ts <= 0 or abs(ts - float(hit.get("ts"))) <= float(window_sec):
                    return _apply(hit)

            return parsed
        except Exception:
            return parsed


    def ingest_trades_incremental(self, trades_fp: Optional[Path]) -> int:
        """增量读取 trades.jsonl 或命令行镜像日志，生成摘要/交易额/模块计数用的 action_rows。

        说明：这里调用模块级实现函数，避免误缩进导致的方法缺失。
        """
        return _ingest_trades_incremental_impl(self, trades_fp)

def _ingest_trades_incremental_impl(self, trades_fp: Optional[Path]) -> int:
        """增量读取 trades.jsonl 或命令行镜像日志，生成摘要用的 action_rows。

        设计目标：
        - trades.jsonl 存在：按结构化 JSON 解析（最准确）
        - trades.jsonl 缺失：从全量日志文本里“够用地”抓动作（避免摘要为空）
        - 切换来源/文件截断：从文件尾部回溯一段，避免一次性扫全文件卡死
        """
        self.reset_day_if_needed()
        added = 0
        turnover_enabled = False
        try:
            turnover_enabled = bool(trades_fp) and (Path(trades_fp).name.lower() == 'trades.jsonl')
        except Exception:
            turnover_enabled = False

        # 爸爸最新口径：历史真实成交摘要只认 trades.jsonl 里的真实成交。
        # 任何 ACCEPT / EXEC_DONE / 文本补摘要 / executor_results 回填，一律不再进入该区块。
        if not turnover_enabled:
            return 0

        def _is_filled_trade_json(obj: dict, msg_txt: str, typ: str, act_l: str) -> bool:
            """只统计“已成交/已执行完成”的动作（过滤准备/意图/排队）。

            口径（更严格，宁可漏不要假）：
            - 显式失败（ok=False/status=failed/包含 error 字段） -> 不计
            - filled_cnt/ok_cnt/filled_size 等 > 0 或 filled=True -> 计
            - type/event/msg 含 EXEC_DONE/EXEC_OK/ORDER_FILLED/FILLED/EXECUTED -> 计
            - 仅出现 *_done/done 之类字样不足以计入（避免“意图完成/计划完成”误判）
            """
            try:
                if not isinstance(obj, dict):
                    return False

                # 0) 显式失败优先
                for k in ("ok", "success", "succeeded", "is_ok", "status_ok"):
                    v = obj.get(k)
                    if isinstance(v, bool):
                        if v is False:
                            return False

                st = str(obj.get("status") or obj.get("state") or "").lower().strip()
                if st in ("fail", "failed", "error", "err", "exception", "rejected", "reject", "canceled", "cancelled"):
                    return False

                # 错误字段兜底：出现 error/err/exc/traceback 直接判失败
                for k in ("error", "err", "exc", "exception", "traceback"):
                    v = obj.get(k)
                    if v:
                        return False

                # 1) trades.jsonl 真成交快速判定：
                # 只认成交/订单完成标识，不再因为 req_id 这种“请求号”提前入账。
                for k in ("exec_u", "amount_u", "notional_u"):
                    if k in obj:
                        try:
                            amt0 = float(obj.get(k) or 0.0)
                        except Exception:
                            amt0 = 0.0
                        if amt0 <= 0.0:
                            continue
                        if obj.get("trade_id") or obj.get("fill_id") or obj.get("order_id") or obj.get("exchange_order_id"):
                            return True
                        st0 = str(obj.get("status") or obj.get("state") or "").lower().strip()
                        if st0 in ("filled", "fill", "executed", "done", "closed", "success", "ok"):
                            return True

                # 1b) M10 / GROUP 三步口径动作：父行本身 often 无 amount，但 req/meta.extras.steps 已带真实执行腿
                try:
                    req0 = obj.get("req") or {}
                    steps = None
                    if isinstance(req0, dict):
                        p0 = req0.get("params") or {}
                        if isinstance(p0, dict):
                            steps = p0.get("steps")
                            if not (isinstance(steps, list) and steps):
                                p0_meta = p0.get("meta") or {}
                                if isinstance(p0_meta, dict):
                                    p0_extras = p0_meta.get("extras") or {}
                                    if isinstance(p0_extras, dict):
                                        steps = p0_extras.get("steps")
                        if not (isinstance(steps, list) and steps):
                            req_meta = req0.get("meta") or {}
                            if isinstance(req_meta, dict):
                                req_extras = req_meta.get("extras") or {}
                                if isinstance(req_extras, dict):
                                    steps = req_extras.get("steps")
                    if isinstance(steps, list) and steps:
                        for stp in steps:
                            if not isinstance(stp, dict):
                                continue
                            splat = str(stp.get("platform") or "").upper().strip()
                            if splat not in ("EXT", "VAR"):
                                continue
                            for k in ("target_notional_u", "notional_u", "notional_usd", "usd", "amount_usd", "amount_u", "value_usd", "value_u", "close_notional_u", "close_notional"):
                                try:
                                    if float(stp.get(k) or 0.0) > 0.0:
                                        return True
                                except Exception:
                                    pass
                            meta = stp.get("meta") or {}
                            if isinstance(meta, dict):
                                for k in ("win_notional_u", "loss_notional_u", "target_notional_u", "m10_step_std_amt_final", "m1_std_amt_final", "tp_step_std_amt_final"):
                                    try:
                                        if float(meta.get(k) or 0.0) > 0.0:
                                            return True
                                    except Exception:
                                        pass
                except Exception:
                    pass

                # 1c) PAIR / BOTH 成对动作：父行无顶层金额，但 legs 或 ext_notional_u / var_notional_u 已带真实金额
                try:
                    req0 = obj.get("req") or {}
                    pair_like_plat = str(
                        obj.get("platform")
                        or (req0.get("platform") if isinstance(req0, dict) else "")
                        or (req0.get("plat") if isinstance(req0, dict) else "")
                        or ""
                    ).upper().strip()
                    module0 = str(obj.get("module") or (req0.get("module") if isinstance(req0, dict) else "") or "").upper().strip()
                    action0 = str(obj.get("action_type") or obj.get("action") or (req0.get("action") if isinstance(req0, dict) else "") or "").lower().strip()
                    if pair_like_plat in ("PAIR", "BOTH", "GROUP") or (module0 == "B2") or (action0 in ("open_pair", "close_pair", "koujing_a_3step")):
                        legs = None
                        if isinstance(req0, dict):
                            p0 = req0.get("params") or {}
                            if isinstance(p0, dict):
                                legs = p0.get("legs")
                                if not isinstance(legs, list):
                                    p1 = p0.get("params") or {}
                                    if isinstance(p1, dict):
                                        legs = p1.get("legs")
                        if isinstance(legs, list) and legs:
                            for lg in legs:
                                if not isinstance(lg, dict):
                                    continue
                                splat = str(lg.get("platform") or "").upper().strip()
                                if splat not in ("EXT", "VAR"):
                                    continue
                                for k in ("notional_u", "usd", "usd_u", "amount_usd", "amount_u", "value_usd", "value_u", "close_notional_u", "close_notional"):
                                    try:
                                        if float(lg.get(k) or 0.0) > 0.0:
                                            return True
                                    except Exception:
                                        pass
                        def _pair_meta_amount(box: Any, prefix: str) -> float:
                            if not isinstance(box, dict):
                                return 0.0
                            for k in (f"{prefix}_notional_u", f"{prefix}_val_u", f"{prefix}_val", f"{prefix}_value_u", f"{prefix}_usd_u", f"{prefix}_usd", f"{prefix}_notional"):
                                try:
                                    v = float(box.get(k) or 0.0)
                                except Exception:
                                    v = 0.0
                                if v > 0.0:
                                    return v
                            return 0.0
                        boxes = []
                        if isinstance(req0, dict):
                            boxes.append(req0)
                            p0 = req0.get("params") or {}
                            if isinstance(p0, dict):
                                boxes.append(p0)
                                p1 = p0.get("params") or {}
                                if isinstance(p1, dict):
                                    boxes.append(p1)
                                m1 = p0.get("meta") or {}
                                if isinstance(m1, dict):
                                    boxes.append(m1)
                            m0 = req0.get("meta") or {}
                            if isinstance(m0, dict):
                                boxes.append(m0)
                        for box in boxes:
                            if _pair_meta_amount(box, "ext") > 0.0 or _pair_meta_amount(box, "var") > 0.0:
                                return True
                except Exception:
                    pass

                # 1) 显式计数类：ok_cnt / filled_cnt / filled_size
                for k in ("ok_cnt", "filled_cnt", "fill_cnt", "filled_size", "fill_size", "filled_qty", "fill_qty", "filled_amount"):
                    if k in obj:
                        try:
                            if float(obj.get(k) or 0.0) > 0:
                                return True
                        except Exception:
                            pass

                # 布尔型 filled
                if isinstance(obj.get("filled"), bool) and obj.get("filled") is True:
                    return True

                # 2) 事件/类型类：EXEC_DONE / ORDER_FILLED 等
                t = str(typ or obj.get("type") or obj.get("event") or obj.get("category") or "").upper().strip()
                if t:
                    if any(x in t for x in ("EXEC_DONE", "EXEC_OK", "EXECUTED", "ORDER_FILLED", "TRADE_FILLED", "FILLED")):
                        return True
                    # 明确的“意图/排队/请求/接受”类：不计入摘要
                    if any(x in t for x in ("INTENT", "PREPARE", "QUEUE", "PENDING", "SUBMIT", "REQUEST", "ACCEPT", "ENQUEUE")):
                        return False

                # 3) 文本兜底：包含“已成交/成交成功/filled/executed”才算；出现“准备/意图/排队/入队/未成交/接受”则排除
                mt = str(msg_txt or obj.get("msg") or obj.get("message") or "")
                mt_l = mt.lower()
                if any(x in mt_l for x in ("intent", "prepare", "queue", "pending", "accept", "enqueue", "enqueued", "submit", "request"))                     or ("意图" in mt) or ("排队" in mt) or ("入队" in mt) or ("未成交" in mt) or ("接受" in mt):
                    return False
                if ("已成交" in mt) or ("成交" in mt) or ("执行成功" in mt) or ("下单成功" in mt)                     or any(x in mt_l for x in ("filled", "executed", "execute ok", "exec ok", "order filled", "trade filled")):
                    return True

                # 4) act/action 名称兜底（极保守）
                a = str(act_l or obj.get("action") or "").lower()
                if any(x in a for x in ("exec_done", "exec_ok", "order_filled", "trade_filled")):
                    return True

                return False
            except Exception:
                return False





        def _is_budget_act(a: str) -> bool:
            a = (a or '').strip().lower()
            if not a:
                return False
            # 预算只统计“开/加仓类”，不统计平仓
            if a.startswith('close') or 'close' in a or 'reduce' in a or 'decrease' in a:
                return False
            return any(k in a for k in ('open', 'increase', 'add', 'add_leg', 'grid', 'first'))

        def _merge_dicts(*objs: Any) -> Dict[str, Any]:
            out: Dict[str, Any] = {}
            for obj in objs:
                if isinstance(obj, dict):
                    try:
                        out.update(obj)
                    except Exception:
                        pass
            return out

        def _build_action_row(base: Optional[Dict[str, Any]] = None, extra: Optional[Dict[str, Any]] = None, **overrides: Any) -> Dict[str, Any]:
            row = _merge_dicts(base, extra)
            try:
                row.update(overrides)
            except Exception:
                pass
            return row

        def _drop_recent_provisional_accept(coin: str, plat: str, act: str, side: str, amt_u: float, ts_filled: float, window_sec: float = 12.0) -> None:
            """当真实成交/执行完成行出现时，删除最近的“ACCEPT/尝试”占位行，避免摘要一笔单显示两次。

            设计原则：
            - ACCEPT 没有订单 ID，无法 100% 精确回绑，因此采用“同币/同平台/同动作/同方向/同金额 + 时间窗”的软匹配
            - 只影响摘要展示，不影响真实成交统计（成交统计本来也不认 ACCEPT）
            """
            try:
                if not getattr(self, 'action_rows', None):
                    return
                coin_u = str(coin or '').upper().strip()
                plat_u = _norm_platform(str(plat or '').upper().strip())
                act_l = str(act or '').lower().strip()
                side_u = str(side or '').upper().strip()
                amt_key = round(abs(float(amt_u or 0.0)), 2)
                tf = float(ts_filled or 0.0)
                if not (coin_u and plat_u and act_l and side_u and amt_key > 0 and tf > 0):
                    return

                # 只扫尾部，避免 O(n) 扫全表
                start = max(0, len(self.action_rows) - 120)
                to_del = []
                for i in range(start, len(self.action_rows)):
                    r = self.action_rows[i]
                    if not isinstance(r, dict):
                        continue
                    if not (r.get('is_provisional') or str(r.get('status') or '').upper() == 'ACCEPT'):
                        continue
                    if str(r.get('coin') or '').upper().strip() != coin_u:
                        continue
                    if _norm_platform(str(r.get('platform') or '').upper().strip()) != plat_u:
                        continue
                    if str(r.get('action') or r.get('action_type') or '').lower().strip() != act_l:
                        continue
                    if str(r.get('side') or '').upper().strip() != side_u:
                        continue
                    amt_r = round(abs(float(_safe_float(r.get('amount_u') or r.get('notional_u') or 0.0, 0.0) or 0.0)), 2)
                    if amt_r != amt_key:
                        continue
                    tr = float(_safe_float(r.get('ts') or 0.0, 0.0) or 0.0)
                    if tr <= 0:
                        continue
                    if abs(tf - tr) <= float(window_sec or 12.0):
                        to_del.append(i)

                if to_del:
                    for idx in reversed(to_del):
                        try:
                            self.action_rows.pop(idx)
                        except Exception:
                            pass
            except Exception:
                return

        try:
            if not trades_fp:
                return 0
            fp = Path(trades_fp)
            if not fp.exists():
                # 缺文件不报错：交给上层去选择别的来源
                return 0

            try:
                src = str(fp.resolve())
            except Exception:
                src = str(fp)

            size = fp.stat().st_size

            # 来源切换 or 文件被截断：重置 offset。
            # 注意：历史交易额口径要求“截至当前所有交易额之和”，不能只回溯尾部。
            # 因此这里标记需要做一次全量扫描（只做一次，随后继续增量）。
            if (src != getattr(self, "_source_path", "")) or (self.trades_offset > size):
                self._source_path = src
                self.trades_offset = 0
                self.trade_total_u = 0.0
                self.trade_today_u = 0.0
                self.trade_total_by_plat = {'EXT': 0.0, 'VAR': 0.0}
                self.trade_by_day = {}  # type: Dict[str, float]
                self.trade_today_by_plat = {'EXT': 0.0, 'VAR': 0.0}
                self.last_trade_ts = 0.0
                self._need_full_scan = True
                self.mod_cnt.clear()
                self.mod_pnl.clear()
                try:
                    self.mod_used_u.clear()
                except Exception:
                    self.mod_used_u = {}
                self.action_rows.clear()
                try:
                    self._m10_partial_keys_seen.clear()
                except Exception:
                    pass
                try:
                    self._recent_action_hashes.clear()
                except Exception:
                    pass
                try:
                    self._recent_decisions.clear()
                    self._recent_decisions_order.clear()
                except Exception:
                    pass

            with fp.open("r", encoding="utf-8", errors="ignore") as f:
                # 首次/切换/截断后：全量扫描一次，用于准确累计历史交易额
                if getattr(self, "_need_full_scan", False):
                    self.trades_offset = 0
                    # 为确保全量扫描结果一致，清理去重缓存（否则可能误跳过）
                    try:
                        self._recent_action_hashes.clear()
                    except Exception:
                        pass
                    f.seek(0)
                elif self.trades_offset > 0:
                    f.seek(self.trades_offset)
                    # 跳过可能的半行
                    _ = f.readline()
                else:
                    f.seek(0)

                for raw in f:
                    line = (raw or "").strip()
                    if not line:
                        continue

                    # 心跳/噪声过滤（无论 JSON 还是文本都先滤一层）
                    if ("心跳" in line) or ("heartbeat" in line.lower()):
                        continue

                    # 增量去重：避免一个动作多次被写入不同格式而重复计数
                    try:
                        h = hashlib.md5(line.encode("utf-8", errors="ignore")).hexdigest()
                        if h in self._recent_action_hashes:
                            continue
                        self._recent_action_hashes.append(h)
                    except Exception:
                        pass

                    # 决策回绑（优先）：抓取 M1_ACCEPT / TRACE ACCEPT，供后续 EXECUTOR（执行器）动作回绑 module/action
                    try:
                        dec = self._parse_m1_accept_line(line)
                        if dec:
                            self._remember_decision(dec)
                            # 爸爸最新口径：历史真实成交摘要只认真实成交，不再把 ACCEPT/尝试单混进来。
                            continue
                        dec2 = self._parse_trace_accept_line(line)
                        if dec2:
                            self._remember_decision(dec2)
                            # TRACE ACCEPT 仅作决策缓存，不入摘要（摘要只显示真实下单/成交/执行成功）
                            continue
                            continue
                    except Exception:
                        pass

                    obj = None
                    try:
                        obj = json.loads(line)
                        if not isinstance(obj, dict):
                            obj = None
                    except Exception:
                        obj = None

                    if isinstance(obj, dict):
                        # === A：全量日志（BOT_grid_full.jsonl）结构化动作回退 ===
                        # 该文件每行是 JSON：大多是进度/状态，但也会出现 executor_done（执行完成）事件。
                        # 这些事件包含 request/legs，可等价视为 trades.jsonl 的 *_done，直接生成摘要与交易额。
                        try:
                            ev = str(obj.get('event') or '').strip().lower()
                            if ev == 'executor_done':
                                ex = obj.get('executed_request_check') or {}
                                req = ex.get('request') if isinstance(ex, dict) else None
                                if isinstance(req, dict):
                                    def _parse_ts_any(v):
                                        try:
                                            if v is None:
                                                return 0.0
                                            if isinstance(v, (int, float)):
                                                return float(v)
                                            s = str(v).strip()
                                            if not s:
                                                return 0.0
                                            # 2026-01-18T05:45:59Z / 2026-01-18T05:44:36.751+00:00
                                            if 'T' in s and ('Z' in s or '+' in s or '-' in s[10:]):
                                                ss = s.replace('Z', '+00:00')
                                                try:
                                                    return datetime.fromisoformat(ss).timestamp()
                                                except Exception:
                                                    pass
                                            # 2026-01-18 13:44:36
                                            try:
                                                return datetime.strptime(s[:19], '%Y-%m-%d %H:%M:%S').timestamp()
                                            except Exception:
                                                pass
                                            return 0.0
                                        except Exception:
                                            return 0.0
                                    tsx = _parse_ts_any(obj.get('ts') or req.get('ts') or (req.get('params') or {}).get('ts_submit'))
                                    obj = {
                                        'ts': tsx,
                                        'module': req.get('module') or (req.get('meta') or {}).get('module') or 'EXECUTOR',
                                        'coin': req.get('coin') or '-',
                                        'platform': req.get('platform') or '-',
                                        'action_type': req.get('action_type') or req.get('action') or '-',
                                        'type': 'EXEC_DONE',
                                        'reason': 'executor_done',
                                        'req': req,
                                    }
                        except Exception:
                            pass

                        # 只把“交易动作”计入交易额与摘要（避免把扫描/心跳/HUMAN 等 JSON 也当成交易）
                        msg_txt = str(obj.get("msg") or obj.get("message") or obj.get("text") or "")
                        if ("心跳" in msg_txt) or ("heartbeat" in msg_txt.lower()):
                            continue
                        typ = str(
                            obj.get("type")
                            or obj.get("cat")
                            or obj.get("kind")
                            or obj.get("category")
                            or obj.get("event")
                            or ""
                        ).upper().strip()
                        if typ in ("HUMAN", "SCAN", "PROGRESS", "STATUS"):
                            continue

                        act_raw = obj.get("action_type") or obj.get("action") or (obj.get("req") or {}).get("action") or ""
                        act = str(act_raw or "").strip()
                        act_l = act.lower()
                        # 允许 increase/decrease 等英文动作；是否为“真成交/真执行”由 _is_filled_trade_json 决定
                        if not act_l:
                            continue
                            continue

                        # 只统计“已成交/已执行完成”的记录（过滤准备/意图/排队）
                        if not _is_filled_trade_json(obj, msg_txt=msg_txt, typ=typ, act_l=act_l):
                            continue

                        ts = _safe_float(obj.get("ts"), 0.0)
                        if ts > 0:
                            self.last_trade_ts = max(self.last_trade_ts, ts)

                        notional = _safe_float(
                            obj.get("notional_u")
                            or obj.get("usd")
                            or obj.get("usd_u")
                            or (obj.get("req") or {}).get("usd")
                            or (obj.get("req") or {}).get("notional_u")
                            or obj.get("value_usd")
                            or obj.get("value_u")
                            or obj.get("std_u")
                            or obj.get("intent_u")
                            ,
                            0.0,
                        )
                        # EXT（平台E）有时只写 std/intent（标准金额/意图金额）或把 amount 写成数量/张数：
                        # 这里统一用 _extract_amount_u_from_row 再兜底一次（其中 amount 放在最后）。
                        if notional <= 0:
                            try:
                                notional = float(_extract_amount_u_from_row(obj) or 0.0)
                            except Exception:
                                notional = 0.0
                        if notional < 0:
                            notional = abs(float(notional))
                        # === 关键修复：add/add_leg 金额优先使用 intent/标准金额（避免把目标金额当成已成交金额）===
                        try:
                            if (('add_leg' in act_l) or (act_l == 'add') or act_l.startswith('add_')):
                                req0 = obj.get('req') or {}
                                cand = 0.0
                                if isinstance(req0, dict):
                                    p0 = req0.get('params') or {}
                                    if isinstance(p0, dict):
                                        cand = _safe_float(p0.get('amount_usd_intent'), 0.0)
                                        if cand <= 0:
                                            meta0 = p0.get('meta') or {}
                                            if isinstance(meta0, dict):
                                                cand = _safe_float(meta0.get('m1_std_amt_final') or meta0.get('m10_step_std_amt_final') or meta0.get('m1_std_amt_raw'), 0.0)
                                        if cand <= 0:
                                            p00 = p0.get('params') or {}
                                            if isinstance(p00, dict):
                                                cand = _safe_float(p00.get('amount_usd_intent'), 0.0)
                                                if cand <= 0:
                                                    meta00 = p00.get('meta') or {}
                                                    if isinstance(meta00, dict):
                                                        cand = _safe_float(meta00.get('m1_std_amt_final') or meta00.get('m10_step_std_amt_final') or meta00.get('m1_std_amt_raw'), 0.0)
                                if cand > 0:
                                    notional = abs(float(cand))
                        except Exception:
                            pass


                        def _clean_str(v: Any) -> str:
                            s = str(v or "").strip()
                            return s if s else "-"

                        module = _clean_str(obj.get("module") or (obj.get("req") or {}).get("module") or "-")
                        coin = _clean_str(
                            obj.get("coin")
                            or obj.get("symbol")
                            or obj.get("asset")
                            or (obj.get("req") or {}).get("coin")
                            or "-"
                        ).upper()
                        plat = _clean_str(
                            obj.get("platform")
                            or obj.get("exchange")
                            or obj.get("ex")
                            or (obj.get("req") or {}).get("platform")
                            or "-"
                        )
                        plat = _norm_platform(plat)
                        side = _clean_str(
                            obj.get("side")
                            or obj.get("side_hint")
                            or (obj.get("req") or {}).get("side")
                            or (obj.get("req") or {}).get("side_hint")
                            or "-"
                        ).upper()

                        dt = datetime.fromtimestamp(ts) if ts > 0 else None

                        # 没有可解析 ts 的行：不要用当前时间冒充下单时间（否则刷新会漂移）
                        # === 关键修复：M10_KOUJING_A（口径A）steps 动作拆成 EXT/VAR 多条腿 ===
                        # 典型：action_type=koujing_a_3step, platform=GROUP, 金额藏在 req.params.steps[*].meta 或 target_notional_u
                        try:
                            req0 = obj.get('req') or {}
                            steps = None
                            if isinstance(req0, dict):
                                p0 = req0.get('params') or {}
                                if isinstance(p0, dict):
                                    steps = p0.get('steps')
                                    if not (isinstance(steps, list) and steps):
                                        p0_meta = p0.get('meta') or {}
                                        if isinstance(p0_meta, dict):
                                            p0_extras = p0_meta.get('extras') or {}
                                            if isinstance(p0_extras, dict):
                                                steps = p0_extras.get('steps')
                                if not (isinstance(steps, list) and steps):
                                    req_meta = req0.get('meta') or {}
                                    if isinstance(req_meta, dict):
                                        req_extras = req_meta.get('extras') or {}
                                        if isinstance(req_extras, dict):
                                            steps = req_extras.get('steps')
                            if isinstance(steps, list) and steps:
                                pnl = self._extract_pnl(obj)
                                if pnl is not None:
                                    self.mod_pnl[module] = self.mod_pnl.get(module, 0.0) + float(pnl)

                                for stp in steps:
                                    if not isinstance(stp, dict):
                                        continue
                                    s_plat = _norm_platform(str(stp.get('platform') or '-'))
                                    if s_plat not in ('EXT', 'VAR'):
                                        continue
                                    s_act = str(stp.get('action') or act).strip()
                                    s_side = _clean_str(stp.get('side') or stp.get('side_hint') or side or '-').upper()
                                    # side 兜底：有些 step 不带 side，但 raw/执行器文本里会有 side=LONG/SHORT 或 buy/sell
                                    if (not s_side) or (s_side in ('-', 'NONE')):
                                        try:
                                            rawx = str(stp.get('raw') or obj.get('raw') or '')
                                            mm_side = re.search(r"\bside\s*=?\s*(LONG|SHORT)\b", rawx, flags=re.I)
                                            if mm_side:
                                                s_side = mm_side.group(1).upper()
                                            else:
                                                low = rawx.lower()
                                                # executor 兼容：side（方向）=buy / sell
                                                if ("方向）=buy" in rawx) or ("side=buy" in low) or (" side = buy" in low):
                                                    s_side = "LONG"
                                                elif ("方向）=sell" in rawx) or ("side=sell" in low) or (" side = sell" in low):
                                                    s_side = "SHORT"
                                        except Exception:
                                            pass


                                    amt_from_meta = False
                                    s_amt = 0.0
                                    for k in ('target_notional_u','notional_u','notional_usd','usd','amount_usd','amount_u','value_usd','value_u'):
                                        if k in stp and stp.get(k) is not None:
                                            s_amt = abs(_safe_float(stp.get(k), 0.0))
                                            if s_amt > 0:
                                                break
                                    if s_amt <= 0:
                                        meta = stp.get('meta') or {}
                                        if isinstance(meta, dict):
                                            for k in ('win_notional_u','loss_notional_u','target_notional_u','m10_step_std_amt_final','m1_std_amt_final'):
                                                if k in meta and meta.get(k) is not None:
                                                    s_amt = abs(_safe_float(meta.get(k), 0.0))
                                                    if s_amt > 0:
                                                        amt_from_meta = True
                                                    if s_amt > 0:
                                                        break
                                    # 金额兜底：有些 step 只带标准化金额（例如 meta.m1_std_amt_final=100），但真实下单金额在父行 req/raw（例如 intent_u=330）
                                    try:
                                        parent_amt = abs(float(_extract_amount_u_from_row(obj) or 0.0))
                                    except Exception:
                                        parent_amt = 0.0
                                    # 仅在 step 金额缺失或明显来自 meta 且偏小时，用父行金额覆盖（摘要去重会避免重复计数）
                                    if parent_amt > 0:
                                        if s_amt <= 0:
                                            s_amt = parent_amt
                                        elif amt_from_meta and (parent_amt >= s_amt * 1.2):
                                            s_amt = parent_amt

                                    if s_amt <= 0:
                                        try:
                                            s_amt = abs(float(_extract_amount_u_from_row(stp) or 0.0))
                                        except Exception:
                                            s_amt = 0.0

                                    # 摘要必须有真实金额，否则会出现动作/方向/金额不对（例如 GROUP steps_std）
                                    if s_amt <= 0:
                                        continue

                                    if self._should_skip_m10_step2_duplicate(module, s_act, coin, s_plat, s_side, s_amt, ts):
                                        continue

                                    # 交易额只认 trades.jsonl；其它来源只补摘要
                                    if s_amt > 0 and turnover_enabled:
                                        self._accumulate_turnover(s_amt, s_plat, dt)
                                        if (dt.date().isoformat() if dt else date.today().isoformat()) == self.trade_today_date and _is_budget_act(str(s_act)):
                                            self.mod_used_u[module] = self.mod_used_u.get(module, 0.0) + abs(float(s_amt))

                                    self.mod_cnt[module] = self.mod_cnt.get(module, 0) + 1

                                    sig = _make_summary_sig(ts if ts > 0 else dt.timestamp(), coin, s_plat, s_act, s_side, float(s_amt) if s_amt else 0.0)


                                    if _summary_seen(sig):


                                        continue


                                    # 若此前已写入过同一笔“ACCEPT/尝试”占位行，这里用真实成交行替换掉
                                    _drop_recent_provisional_accept(coin, s_plat, s_act, s_side, s_amt, ts)

                                    self.action_rows.append(_build_action_row(
                                        obj,
                                        stp,
                                        ts=ts if ts > 0 else 0.0,
                                        time=(datetime.fromtimestamp(ts).strftime('%H:%M:%S') if ts > 0 else '--'),
                                        coin=coin,
                                        platform=s_plat,
                                        module=module,
                                        action=s_act,
                                        pnl=pnl,
                                        side=s_side,
                                        oc=('平仓' if (str(s_act).lower().startswith('close') or 'close' in str(s_act).lower()) else '开仓'),
                                        notional_u=float(s_amt) if s_amt else 0.0,
                                        amount_u=float(s_amt) if s_amt else 0.0,
                                        req=req0,
                                    ))
                                    added += 1

                                if len(self.action_rows) > ACTION_ROWS_MAX_KEEP:
                                    self.action_rows = self.action_rows[-ACTION_ROWS_MAX_KEEP:]
                                # steps 已拆分，跳过父行，避免重复计数
                                continue
                        except Exception:
                            pass

                        # === 关键修复：PAIR / BOTH（成对/双腿）动作拆成 EXT/VAR 两条腿 ===
                        # 典型：
                        # - action_type=open_pair / close_pair, platform=PAIR
                        # - B2 close_pair, platform=BOTH
                        # 这些父行顶层 notional_u 常为空，真实金额在 req.params.legs 或 ext_notional_u / var_notional_u 里。
                        try:
                            req0 = obj.get("req") or {}
                            legs = None
                            if isinstance(req0, dict):
                                p0 = req0.get("params") or {}
                                if isinstance(p0, dict):
                                    legs = p0.get("legs")
                                    if not isinstance(legs, list):
                                        p00 = p0.get("params") or {}
                                        if isinstance(p00, dict):
                                            legs = p00.get("legs")

                            pair_like_plat = str(plat or "").upper().strip()
                            act_u = str(act or "").lower().strip()
                            mod_u = str(module or "").upper().strip()

                            if pair_like_plat in ("PAIR", "BOTH", "GROUP"):
                                synthetic_legs = []

                                # A. 优先走原生 legs 拆分
                                if isinstance(legs, list) and legs:
                                    for lg in legs:
                                        if not isinstance(lg, dict):
                                            continue
                                        lplat = _norm_platform(str(lg.get("platform") or "-"))
                                        if lplat not in ("EXT", "VAR"):
                                            continue

                                        lside = _clean_str(
                                            lg.get("side")
                                            or lg.get("side_hint")
                                            or self._pick_pair_like_side(coin, lplat, side)
                                            or "-"
                                        ).upper()
                                        if lside == "多":
                                            lside = "LONG"
                                        elif lside == "空":
                                            lside = "SHORT"

                                        lnotional = _safe_float(
                                            lg.get("notional_u")
                                            or lg.get("usd")
                                            or lg.get("usd_u")
                                            or lg.get("amount_usd")
                                            or lg.get("amount_u")
                                            or lg.get("value_usd")
                                            or lg.get("value_u"),
                                            0.0,
                                        )
                                        if lnotional <= 0:
                                            try:
                                                lnotional = float(_extract_amount_u_from_row(lg) or 0.0)
                                            except Exception:
                                                lnotional = 0.0
                                        if lnotional < 0:
                                            lnotional = abs(float(lnotional))

                                        if lnotional > 0:
                                            synthetic_legs.append({
                                                "platform": lplat,
                                                "side": lside,
                                                "notional_u": float(lnotional),
                                                "leg_obj": lg,
                                            })

                                # B. 没有 legs 时，兼容 B2/BOTH：从 ext_notional_u / var_notional_u 合成两条腿
                                if (not synthetic_legs) and (mod_u == "B2" or act_u == "close_pair" or pair_like_plat == "BOTH"):
                                    ext_u = self._pick_pair_like_notional(req0, "ext")
                                    var_u = self._pick_pair_like_notional(req0, "var")

                                    if ext_u > 0:
                                        ext_side = self._pick_pair_like_side(coin, "EXT", side)
                                        synthetic_legs.append({
                                            "platform": "EXT",
                                            "side": ext_side,
                                            "notional_u": float(ext_u),
                                            "leg_obj": {"platform": "EXT", "side": ext_side, "notional_u": float(ext_u)},
                                        })
                                    if var_u > 0:
                                        var_side = self._pick_pair_like_side(coin, "VAR", side)
                                        synthetic_legs.append({
                                            "platform": "VAR",
                                            "side": var_side,
                                            "notional_u": float(var_u),
                                            "leg_obj": {"platform": "VAR", "side": var_side, "notional_u": float(var_u)},
                                        })

                                if synthetic_legs:
                                    if mod_u == "B2" or act_u == "close_pair":
                                        self._remember_pair_close(coin, [x.get("platform") for x in synthetic_legs], ts, module=module, action=act)

                                    pnl = self._extract_pnl(obj)
                                    if pnl is not None:
                                        self.mod_pnl[module] = self.mod_pnl.get(module, 0.0) + float(pnl)

                                    for item in synthetic_legs:
                                        lplat = str(item.get("platform") or "-").upper().strip()
                                        lside = str(item.get("side") or "-").upper().strip()
                                        if lside == "多":
                                            lside = "LONG"
                                        elif lside == "空":
                                            lside = "SHORT"
                                        lnotional = float(_safe_float(item.get("notional_u"), 0.0))
                                        lg = item.get("leg_obj") or {}

                                        if lplat not in ("EXT", "VAR") or lnotional <= 0:
                                            continue

                                        if self._should_skip_m10_step2_duplicate(module, act, coin, lplat, lside, lnotional, ts):
                                            continue

                                        # 交易额/预算统计（按腿）
                                        if turnover_enabled:
                                            self._accumulate_turnover(lnotional, lplat, dt)
                                            if (dt.date().isoformat() if dt else date.today().isoformat()) == self.trade_today_date and _is_budget_act(str(act)):
                                                self.mod_used_u[module] = self.mod_used_u.get(module, 0.0) + abs(float(lnotional))

                                        self.mod_cnt[module] = self.mod_cnt.get(module, 0) + 1

                                        # 动作名：保持开/平语义，避免 UI 分类失真
                                        a0 = str(act).strip().lower()
                                        if a0.startswith("open"):
                                            act_leg = "open_leg"
                                        elif a0.startswith("close") or mod_u == "B2":
                                            act_leg = "close_leg"
                                        else:
                                            act_leg = act

                                        # 若此前已写入过同一笔“ACCEPT/尝试”占位行，这里用真实成交行替换掉
                                        _drop_recent_provisional_accept(coin, lplat, act_leg, lside, lnotional, ts)

                                        self.action_rows.append(_build_action_row(
                                            obj,
                                            lg,
                                            ts=ts if ts > 0 else 0.0,
                                            time=(datetime.fromtimestamp(ts).strftime('%H:%M:%S') if ts > 0 else '--'),
                                            coin=coin,
                                            platform=lplat,
                                            module=module,
                                            action=act_leg,
                                            pnl=pnl,
                                            side=lside,
                                            oc=("平仓" if (str(act_leg).lower().startswith("close") or "close" in str(act_leg).lower() or mod_u == "B2") else "开仓"),
                                            notional_u=float(lnotional) if lnotional else 0.0,
                                            amount_u=float(lnotional) if lnotional else 0.0,
                                            req=req0,
                                        ))
                                        added += 1

                                    if len(self.action_rows) > ACTION_ROWS_MAX_KEEP:
                                        self.action_rows = self.action_rows[-ACTION_ROWS_MAX_KEEP:]

                                    # 父行已拆成 EXT/VAR，多腿动作不再继续走下面的 notional<=0 过滤逻辑
                                    continue
                        except Exception:
                            # 拆分失败时，不影响原逻辑
                            pass

                        # 摘要必须有真实金额（否则动作/方向/金额不可靠）
                        if notional <= 0:
                            continue

                        if self._should_skip_stale_post_pair_close(
                            obj,
                            coin=coin,
                            plat=plat,
                            module=module,
                            action=act,
                            side=side,
                            amount_u=notional,
                            ts=ts,
                        ):
                            continue

                        if self._should_skip_m10_step2_duplicate(module, act, coin, plat, side, notional, ts):
                            continue

                        if notional > 0 and turnover_enabled:
                            self._accumulate_turnover(notional, plat, dt)
                        
                            # 预算已使用（按日累计成交额，去重后本函数已保证行级去重）
                            if (dt.date().isoformat() if dt else date.today().isoformat()) == self.trade_today_date and _is_budget_act(str(act)):
                                self.mod_used_u[module] = self.mod_used_u.get(module, 0.0) + abs(float(notional))

                        self.mod_cnt[module] = self.mod_cnt.get(module, 0) + 1
                        pnl = self._extract_pnl(obj)
                        if pnl is not None:
                            self.mod_pnl[module] = self.mod_pnl.get(module, 0.0) + float(pnl)

                        # 若此前已写入过同一笔“ACCEPT/尝试”占位行，这里用真实成交行替换掉
                        _drop_recent_provisional_accept(coin, plat, act, side, notional, ts)

                        self.action_rows.append(_build_action_row(
                            obj,
                            ts=ts if ts > 0 else 0.0,
                            time=(datetime.fromtimestamp(ts).strftime('%H:%M:%S') if ts > 0 else '--'),
                            coin=coin,
                            platform=plat,
                            module=module,
                            action=act,
                            pnl=pnl,
                            side=side,
                            oc=("平仓" if (str(act).lower().startswith("close") or "close" in str(act).lower()) else "开仓"),
                            notional_u=float(notional) if notional else 0.0,
                            amount_u=float(notional) if notional else 0.0,
                        ))
                        added += 1
                    else:
                        parsed_any = self._parse_action_from_text_line(line)
                        if not parsed_any:
                            continue
                        parsed_list = parsed_any if isinstance(parsed_any, list) else [parsed_any]
                        for parsed in parsed_list:
                            if not isinstance(parsed, dict):
                                continue
                            parsed = self._bind_action_to_decision(parsed)

                            act = str(parsed.get("action") or "-")
                            coin = str(parsed.get("coin") or "-").upper()
                            plat = _norm_platform(str(parsed.get("platform") or "-"))
                            parsed["platform"] = plat
                            module = str(parsed.get("module") or "-")
                            ts = _safe_float(parsed.get("ts"), 0.0)
                            notional = _safe_float(parsed.get("notional_u"), 0.0)
                            if notional <= 0:
                                try:
                                    notional = float(_extract_amount_u_from_row(parsed) or 0.0)
                                except Exception:
                                    notional = 0.0
                            if notional < 0:
                                notional = abs(float(notional))
                            if notional <= 0:
                                # 没有真实金额的行不入摘要（否则会出现 金额=0、动作/方向缺失 的错觉）
                                continue
                            side = str(parsed.get("side") or "-").upper()

                            # 必须具备：动作 + 币种（否则不入摘要，避免误判）
                            if (act == "-") or (coin == "-"):
                                continue

                            if self._should_skip_m10_step2_duplicate(module, act, coin, plat, side, notional, ts):
                                continue

                            dt = datetime.fromtimestamp(ts) if ts > 0 else None

                            # 文本/日志只补摘要；交易额只认 trades.jsonl 真成交
                            if turnover_enabled:
                                try:
                                    self._accumulate_turnover(notional, plat, dt)
                                except Exception:
                                    pass

                            self.mod_cnt[module] = self.mod_cnt.get(module, 0) + 1

                            # 若此前已写入过同一笔“ACCEPT/尝试”占位行，这里用真实动作行替换掉
                            _drop_recent_provisional_accept(coin, plat, act, side, notional, ts)

                            self.action_rows.append(_build_action_row(
                                parsed,
                                ts=ts if ts > 0 else 0.0,
                                time=(datetime.fromtimestamp(ts).strftime('%H:%M:%S') if ts > 0 else '--'),
                                coin=coin,
                                platform=plat,
                                module=module,
                                action=act,
                                pnl=None,
                                side=side,
                                oc=("平仓" if (str(act).lower().startswith("close") or "close" in str(act).lower()) else "开仓"),
                                notional_u=float(notional),
                                amount_u=float(notional),
                            ))
                            added += 1

                    if len(self.action_rows) > ACTION_ROWS_MAX_KEEP:
                        self.action_rows = self.action_rows[-ACTION_ROWS_MAX_KEEP:]

                self.trades_offset = f.tell()
                # 全量扫描只需要做一次
                try:
                    self._need_full_scan = False
                except Exception:
                    pass
        except Exception:
            return 0

        return added

class PanelModel:
    def __init__(self, cfg: PanelConfig) -> None:
        self.cfg = cfg
        self.cache = DataCache()

        self.bot_dir: Optional[Path] = None
        self.out_dir: Optional[Path] = None
        self.logs_dir: Optional[Path] = None

        self.fp_equity: Optional[Path] = None
        self.fp_positions: Optional[Path] = None
        self.fp_trades: Optional[Path] = None
        self.fp_control: Optional[Path] = None
        self.fp_log: Optional[Path] = None
        self.fp_err: Optional[Path] = None
        # Bot 若输出面板专用结构化文件，这里优先读取
        self.fp_status: Optional[Path] = None
        self.fp_queue: Optional[Path] = None
        self.fp_module_status: Optional[Path] = None
        self.fp_budget_status: Optional[Path] = None
        self.fp_risk: Optional[Path] = None

        self.runtime_fp: Optional[Path] = None
        self.trade_daily_fp: Optional[Path] = None  # 面板侧：每日交易额累计落盘（历史=每日叠加）
        self.profit_daily_fp: Optional[Path] = None  # 面板侧：盈利口径改为“每日0点余额法”落盘
        self._trade_daily_cache: Dict[str, float] = {}
        self._profit_daily_cache: Dict[str, Any] = {}
        self.runtime_state = {"today": date.today().isoformat(), "today_run_sec": 0.0, "total_run_sec": 0.0, "last_tick_ts": 0.0, "init_total": None, "yday_total_close": None, "last_total": None}

        self.last_err_mtime = 0.0
        self.data_status = {"env": "bad", "equity": "bad", "positions": "bad", "reason": "", "paths": {}}

        # 全量日志尾部缓存：避免每次刷新都读 6000 行导致卡顿
        self._full_log_cache = {
            "path": None,
            "mtime": 0.0,
            "size": 0,
            "display_lines": [],
            "parse_lines": [],
        }

        self._init_paths(initial=True)
        self._load_runtime()
    def _choose_full_log_fp(self) -> Optional[Path]:
        """人类可观测日志：固定优先读 system_observe.log。"""
        cands: List[Path] = []
        try:
            bd = self.bot_dir
            if bd:
                cands += [
                    bd / "out" / HUMAN_OBSERVE_LOG_BASENAME,
                    bd / "out" / "panel_spawn_stdout.txt",
                    bd / "out" / "bot_console.log",
                    bd / "out" / "bot_console.txt",
                ]
            if self.out_dir:
                cands += [
                    self.out_dir / HUMAN_OBSERVE_LOG_BASENAME,
                    self.out_dir / "panel_spawn_stdout.txt",
                    self.out_dir / "bot_console.log",
                    self.out_dir / "bot_console.txt",
                ]
            if self.fp_log:
                cands.append(self.fp_log)
        except Exception:
            cands = []

        exist: List[Path] = []
        for p in cands:
            try:
                if p and p.exists() and p.is_file() and p.stat().st_size > 0:
                    exist.append(p)
            except Exception:
                continue
        if not exist:
            return self.fp_log
        exist.sort(key=lambda p: (0 if p.name == HUMAN_OBSERVE_LOG_BASENAME else 1, -p.stat().st_mtime))
        return exist[0]

    def _choose_actions_fp(self, fp_full: Optional[Path]) -> Optional[Path]:
        """选择‘摘要/交易额’动作来源（实时跟随 Bot 落盘更新）。

        选择原则：
        - 优先结构化来源：
          1) trades.jsonl（若 Bot 有写）
          2) executor_results.jsonl（执行器结果）
          3) BOT_grid_full.jsonl（全量结构化日志，含 executor_done）
        - 文本镜像（bot_console.log / stdout）只作为兜底
        - 如果 out 目录存在老文件但不再更新，自动切回 logs 的活跃文件
        """
        cands: List[Path] = []
        try:
            bd = self.bot_dir
            if bd:
                cands += [
                    bd / 'out' / 'trades.jsonl',
                    bd / 'out' / 'bot_console.log',
                    bd / 'logs' / MACHINE_DEBUG_LOG_BASENAME,
                    bd / 'out' / 'trades.jsonl',
                    bd / 'logs' / 'executor_results.jsonl',
                ]
            if self.out_dir:
                cands += [
                    self.out_dir / 'trades.jsonl',
                    self.out_dir / 'bot_console.log',
                ]
            if self.logs_dir:
                cands += [
                    self.logs_dir / MACHINE_DEBUG_LOG_BASENAME,
                    self.logs_dir / 'executor_results.jsonl',
                    self.logs_dir / 'BOT_grid_full.jsonl',
                ]
            if fp_full:
                cands.append(fp_full)
            if self.fp_log:
                cands.append(self.fp_log)
            if self.fp_trades:
                cands.append(self.fp_trades)
        except Exception:
            cands = []

        # 过滤存在且非空
        exist: List[Path] = []
        seen = set()
        for p in cands:
            try:
                if not p:
                    continue
                sp = str(p)
                if sp in seen:
                    continue
                seen.add(sp)
                if p.exists() and p.is_file() and p.stat().st_size > 0:
                    exist.append(p)
            except Exception:
                continue
        if not exist:
            return fp_full or self.fp_log

        def _weight(p: Path) -> int:
            name = p.name.lower()
            # 结构化优先
            if name == 'trades.jsonl':
                return 100
            if name == 'executor_results.jsonl':
                return 95
            if name == 'bot_grid_full.jsonl':
                return 90
            # 文本镜像兜底
            if 'console' in name or 'stdout' in name:
                return 70
            return 60

        # 评分：权重优先，其次 mtime（修改时间），再 size（大小）
        scored: List[tuple] = []
        for p in exist:
            try:
                st = p.stat()
                scored.append((_weight(p), float(st.st_mtime), int(st.st_size), p))
            except Exception:
                continue
        if not scored:
            return fp_full or self.fp_log
        scored.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
        pick = scored[0][3]

        # === trades.jsonl 活跃性强化：避免“文件被 touch 但无新增内容”导致来回切源 ===
        try:
            now = time.time()
            cur_live = getattr(self, '_actions_live', None) or {}
            cur_path = str(cur_live.get('path') or '')
            # 记录每个候选的上次 size/mtime（用于判断是否真实增长）
            hist = getattr(self, '_actions_hist', None)
            if not isinstance(hist, dict):
                hist = {}
            # 关键修复：先保留“上一轮”的 size/mtime，再写入当前轮。
            # 旧代码先覆盖后比较，导致 trades.jsonl 的 size 增长永远看不出来，
            # 面板会误切到 executor_results.jsonl，进而丢掉价格/利润/RSI/ADX。
            prev_hist = {k: (dict(v) if isinstance(v, dict) else {}) for k, v in hist.items()}
            for _w, _mt, _sz, _pp in scored:
                try:
                    sp = str(_pp)
                    last = prev_hist.get(sp, {}) if isinstance(prev_hist.get(sp), dict) else {}
                    hist[sp] = {
                        'mtime': float(_mt),
                        'size': int(_sz),
                        'last_size': int(last.get('size', int(_sz))),
                        'last_mtime': float(last.get('mtime', float(_mt))),
                    }
                except Exception:
                    continue
            self._actions_hist = hist

            def _is_live(pp: Path, w: int, mt: float, sz: int) -> bool:
                nm = pp.name.lower()
                sp = str(pp)
                h = hist.get(sp, {}) if isinstance(hist.get(sp), dict) else {}
                last_sz = int(h.get('last_size', sz))
                last_mt = float(h.get('last_mtime', mt))
                # trades.jsonl 只要最近在写，且 size/mtime 任一前进，就优先视为 live；
                # 因为它才是摘要唯一真值，包含价格/利润/指标。
                if nm == 'trades.jsonl' and now - float(mt) <= 90.0 and int(sz) > 0:
                    if int(sz) > int(last_sz) or float(mt) > float(last_mt) or str(pp) != str(cur_path or ''):
                        return True
                # 其它结构化来源：近 90 秒有更新且 size 有增长，算 live
                if now - float(mt) <= 90.0 and int(sz) > int(last_sz):
                    return True
                # executor_results / BOT_grid_full 允许只看 mtime（有时同大小追加会触发延迟）
                if nm in ('executor_results.jsonl', 'bot_grid_full.jsonl') and now - float(mt) <= 90.0:
                    return True
                return False

            # 若当前 pick 是 trades.jsonl 但不 live，尝试改为 live 的结构化来源
            try:
                if pick.name.lower() == 'trades.jsonl':
                    stp = pick.stat()
                    if not _is_live(pick, 100, float(stp.st_mtime), int(stp.st_size)):
                        best_live = None
                        best_score = None
                        for w, mt, sz, pp in scored:
                            nm = pp.name.lower()
                            if nm not in ('executor_results.jsonl', 'bot_grid_full.jsonl'):
                                continue
                            if _is_live(pp, w, float(mt), int(sz)):
                                sc = (w, mt, sz)
                                if (best_score is None) or (sc > best_score):
                                    best_score = sc
                                    best_live = pp
                        if best_live is not None:
                            pick = best_live
            except Exception:
                pass
        except Exception:
            pass

        # === 活跃性检测：避免 out 目录老文件“存在但不更新”导致摘要卡死 ===
        try:
            now = time.time()
            cur = getattr(self, '_actions_live', None) or {}
            cur_path = str(cur.get('path') or '')
            cur_m = float(cur.get('mtime') or 0.0)
            cur_s = int(cur.get('size') or 0)
            stale_n = int(cur.get('stale_n') or 0)

            stp = pick.stat()
            pm, ps = float(stp.st_mtime), int(stp.st_size)

            # 如果沿用同一文件但 mtime/size 都不变，计为 stale
            if str(pick) == cur_path and (pm == cur_m) and (ps == cur_s):
                stale_n += 1
            else:
                stale_n = 0

            # 若 stale 3 轮以上，尝试在候选中找“最近 90 秒内更新过”的结构化日志并切换
            if stale_n >= 3:
                best_live = None
                best_score = None
                for w, mt, sz, pp in scored:
                    try:
                        # 只切结构化来源
                        nm = pp.name.lower()
                        if nm not in ('trades.jsonl', 'executor_results.jsonl', 'bot_grid_full.jsonl'):
                            continue
                        if now - float(mt) <= 90.0:
                            sc = (w, mt, sz)
                            if (best_score is None) or (sc > best_score):
                                best_score = sc
                                best_live = pp
                    except Exception:
                        continue
                if best_live is not None:
                    pick = best_live
                    stp2 = pick.stat()
                    pm, ps = float(stp2.st_mtime), int(stp2.st_size)
                    stale_n = 0

            self._actions_live = {
                'path': str(pick),
                'mtime': pm,
                'size': ps,
                'stale_n': stale_n,
            }
        except Exception:
            pass

        return pick



    def _get_full_log_tails_cached(self, fp_full: Optional[Path], display_lines: int = 2000, parse_lines: int = 6000) -> Tuple[List[str], List[str]]:
        """读取全量日志尾部（展示/解析两份），并基于 mtime/size 做缓存，降低刷新卡顿。"""
        try:
            if not fp_full or (not fp_full.exists()):
                return [], []
            st = fp_full.stat()
            cache = getattr(self, "_full_log_cache", None) or {}
            if cache.get("path") == str(fp_full) and cache.get("mtime") == float(st.st_mtime) and cache.get("size") == int(st.st_size):
                return list(cache.get("display_lines") or []), list(cache.get("parse_lines") or [])
            disp = _slice_recent_rounds(_tail_lines(fp_full, max_lines=int(display_lines)))
            pars = _slice_recent_rounds(_tail_lines(fp_full, max_lines=int(parse_lines)))
            self._full_log_cache = {
                "path": str(fp_full),
                "mtime": float(st.st_mtime),
                "size": int(st.st_size),
                "display_lines": disp,
                "parse_lines": pars,
            }
            return disp, pars
        except Exception:
            return [], []


    def _render_full_log_lines(self, fp: Optional[Path], raw_lines: List[str]) -> List[str]:
        """渲染面板左侧日志。

        - human/system_observe.log：尽量保留原始人话播报
        - machine_debug_full.jsonl：提取模块/阶段/原因，压成可读文本
        """
        if not raw_lines:
            return []
        # 1) 控制台文本：原样（仅去心跳）
        try:
            if fp and (fp.suffix.lower() in (".log", ".txt") or "stdout" in fp.name.lower()):
                out = []
                for ln in raw_lines:
                    s = (ln or "").rstrip("\n")
                    if not s:
                        continue
                    # 心跳/倒计时刷屏过滤（既要兼容中文“心跳”，也要兼容乱码/纯英文输出）
                    if ("心跳" in s) or ("heartbeat" in s.lower()):
                        continue
                    if ("next=" in s) and re.search(r"next=\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", s) and re.search(r"\b\d+\s*s\b", s):
                        continue
                    out.append(s)
                return out[-1400:]
        except Exception:
            pass

        # 2) jsonl：尽量还原为一行文本（不额外加前缀）
        out: List[str] = []
        for ln in raw_lines:
            s = (ln or "").strip()
            if not s:
                continue
            if ("心跳" in s) or ("heartbeat" in s.lower()):
                continue
            if s.startswith("{") and s.endswith("}"):
                try:
                    obj = json.loads(s)
                    if isinstance(obj, dict):
                        # 最贴近命令行的字段（如果 bot 写了）
                        cand = obj.get("line") or obj.get("text") or obj.get("msg") or obj.get("message")
                        if cand:
                            ss = str(cand).rstrip("\n")
                            if ss and ("心跳" not in ss) and ("heartbeat" not in ss.lower()):
                                out.append(ss)
                                continue
                        # 退一步：title + msg/text
                        title = str(obj.get("title") or "").strip()
                        msg = str(obj.get("msg") or obj.get("message") or obj.get("text") or "").strip()
                        if title and msg:
                            out.append(f"{title} | {msg}")
                            continue
                        if msg:
                            out.append(msg)
                            continue
                except Exception:
                    pass
            # 兜底：原行
            out.append(s)
        return out[-1400:]


    def _init_paths(self, initial: bool = False) -> None:
        # 1) bot_dir 优先来自 bot_script；否则使用 config.bot_dir；否则用面板自身目录（自动探测）
        bot_dir = None
        if self.cfg.bot_script:
            bot_dir = Path(self.cfg.bot_script).resolve().parent
        elif self.cfg.bot_dir:
            bot_dir = Path(self.cfg.bot_dir).resolve()
        else:
            bot_dir = Path(__file__).resolve().parent  # 自动探测（把面板放在 bot 目录也能跑）

        self.bot_dir = bot_dir

        # 2) 自动探测 out/logs（可随时更新）
        self.out_dir, self.logs_dir, reason = self._discover_dirs(self.bot_dir)
        self.data_status["reason"] = reason

        if self.out_dir:
            out_dirs = self._iter_out_dir_candidates()
            # 只挑“能解析成功”的文件，避免误读旧目录/半截文件导致 positions.json JSONDecodeError
            self.fp_equity = self._pick_parsable_json(out_dirs, "equity.json", want="dict")
            self.fp_positions = self._pick_parsable_json(out_dirs, "positions.json", want="dict")
            # trades.jsonl 是 JSONL（逐行 JSON），不能按“整文件 JSON”校验，否则会被误判为不可解析 -> 摘要空白
            self.fp_trades = None
            for _d in out_dirs:
                try:
                    _p = _d / "trades.jsonl"
                    if _p.exists() and _p.stat().st_size > 0:
                        self.fp_trades = _p
                        break
                except Exception:
                    pass
            if self.fp_trades is None:
                for _d in out_dirs:
                    try:
                        _p = _d / "trades.jsonl"
                        if _p.exists():
                            self.fp_trades = _p
                            break
                    except Exception:
                        pass
            self.fp_control = self.out_dir / "control.json"
            self.fp_err = self.out_dir / "bot_error.txt"
            # 运行态插针：记录面板最终使用的 trades 文件路径（便于排查“有记录但摘要空”）
            try:
                if isinstance(self.runtime_state, dict):
                    self.runtime_state.setdefault("paths", {})
                    self.runtime_state["paths"]["trades_fp"] = str(self.fp_trades) if self.fp_trades else ""
                    self.runtime_state["paths"]["out_dir"] = str(self.out_dir) if self.out_dir else ""
                    self.runtime_state["paths"]["logs_dir"] = str(self.logs_dir) if self.logs_dir else ""
            except Exception:
                pass
            # 面板专用结构化输出（若 Bot 有写，则优先读取以提升稳定性）
            self.fp_status = self.out_dir / "status.json"
            self.fp_queue = self.out_dir / "queue.json"
            self.fp_module_status = self.out_dir / "module_status.json"
            self.fp_budget_status = self.out_dir / "budget_status.json"
            self.fp_risk = self.out_dir / "risk.json"
            self.runtime_fp = self.out_dir / "panel_runtime.json"
            self.trade_daily_fp = self.out_dir / "panel_trade_daily.json"
            self.profit_daily_fp = self.out_dir / "panel_profit_daily.json"
            self.fp_anchor_state = self.out_dir / "state" / "anchor_state.json"
        else:
            self.fp_equity = self.fp_positions = self.fp_trades = self.fp_control = self.fp_err = None
            self.fp_status = self.fp_queue = self.fp_module_status = self.fp_budget_status = self.fp_risk = None
            self.runtime_fp = None
            self.trade_daily_fp = None
            self.profit_daily_fp = None
            self.fp_anchor_state = None

        self.fp_log = None
        if self.logs_dir:
            self.fp_log = self._pick_latest(self.logs_dir, [MACHINE_DEBUG_LOG_BASENAME, "BOT_grid_full.jsonl", "bot_console.log", "bot_run.jsonl"])

        # trades 回退：如果 out/trades.jsonl 不存在，则用主日志里可解析的交易动作
        if (not self.fp_trades) or (not self.fp_trades.exists()):
            if self.fp_log and self.fp_log.exists():
                self.fp_trades = self.fp_log

        # 3) 允许 hint 指向真实 log
        fb = _find_log_path_fallback()
        if fb and fb.exists():
            self.fp_log = fb

        # 4) TEMP 回退：找最新 jsonl
        if not self.fp_log or (not self.fp_log.exists()):
            self.fp_log = self._find_recent_temp_log()

        self.data_status["paths"] = {
            "bot_dir": str(self.bot_dir) if self.bot_dir else "",
            "out_dir": str(self.out_dir) if self.out_dir else "",
            "logs_dir": str(self.logs_dir) if self.logs_dir else "",
            "equity": str(self.fp_equity) if self.fp_equity else "",
            "positions": str(self.fp_positions) if self.fp_positions else "",
            "trades": str(self.fp_trades) if self.fp_trades else "",
            "log": str(self.fp_log) if self.fp_log else "",
            "err": str(self.fp_err) if self.fp_err else "",
            "control": str(self.fp_control) if self.fp_control else "",
            "anchor_state": str(self.fp_anchor_state) if getattr(self, "fp_anchor_state", None) else "",
        }

    def _read_anchor_panel_view(self, eq_obj: Optional[dict] = None) -> Dict[str, float]:
        """读取 ANCHOR 展示真值：优先 state/anchor_state.json，其次 equity/status/log 推断。"""
        out = {
            "anchor_u": 0.0,
            "next_trigger_u": 0.0,
            "takeprofit_pct": 0.01,
        }
        try:
            eq_obj = eq_obj if isinstance(eq_obj, dict) else {}
            state_obj = _read_json_safe(getattr(self, 'fp_anchor_state', None))
            if isinstance(state_obj, dict) and state_obj:
                out["anchor_u"] = _safe_float(state_obj.get("anchor_u"), out["anchor_u"])
            out["anchor_u"] = _safe_float(
                eq_obj.get("anchor_current_u"),
                _safe_float(eq_obj.get("anchor_u"), out["anchor_u"]),
            )
            out["next_trigger_u"] = _safe_float(
                eq_obj.get("anchor_next_trigger_u"),
                _safe_float(eq_obj.get("next_trigger_u"), out["next_trigger_u"]),
            )
            out["takeprofit_pct"] = _safe_float(eq_obj.get("anchor_takeprofit_pct"), out["takeprofit_pct"])

            # 从最近日志里提取“当前锚点/下一触发价”，比固定 2000U 更接近 bot 真相。
            try:
                fp = self.fp_log if (self.fp_log and self.fp_log.exists()) else None
                tail = _tail_lines(fp, max_lines=240) if fp else []
                for ln in reversed(tail):
                    s = str(ln or '')
                    m = re.search(r"当前锚点\s*=\s*([0-9.]+).*?(?:下一触发价|next_trigger_u)\s*=\s*([0-9.]+)", s)
                    if not m:
                        m = re.search(r"anchor_u\s*=\s*([0-9.]+).*?(?:next_trigger_u|下一触发价)\s*=\s*([0-9.]+)", s)
                    if m:
                        out["anchor_u"] = _safe_float(m.group(1), out["anchor_u"])
                        out["next_trigger_u"] = _safe_float(m.group(2), out["next_trigger_u"])
                        break
            except Exception:
                pass

            # 若已知锚点和触发点，反推出真实比例；否则按默认 1% 计算。
            if out["anchor_u"] > 0 and out["next_trigger_u"] > 0:
                pct = (out["next_trigger_u"] / out["anchor_u"]) - 1.0
                if pct >= 0:
                    out["takeprofit_pct"] = pct
            elif out["anchor_u"] > 0:
                out["next_trigger_u"] = out["anchor_u"] * (1.0 + max(0.0, float(out["takeprofit_pct"])))
        except Exception:
            pass
        return out

    # === 交易额口径：本机时间 0-24 点（今日），历史=每日叠加（跨日累计） ===
    def _load_trade_daily_store(self) -> Dict[str, float]:
        try:
            if not getattr(self, 'trade_daily_fp', None):
                return {}
            fp = self.trade_daily_fp
            if (not fp) or (not fp.exists()):
                return {}
            obj = _read_json_safe(fp)
            if not isinstance(obj, dict):
                return {}
            days = obj.get('days') or {}
            if not isinstance(days, dict):
                return {}
            out: Dict[str, float] = {}
            for k, v in days.items():
                ks = str(k).strip()
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", ks):
                    continue
                try:
                    out[ks] = float(v)
                except Exception:
                    continue
            return out
        except Exception:
            return {}

    def _save_trade_daily_store(self, days: Dict[str, float]) -> None:
        try:
            if not getattr(self, 'trade_daily_fp', None):
                return
            fp = self.trade_daily_fp
            if not fp:
                return
            fp.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                'updated_local': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'days': {k: float(v) for k, v in sorted(days.items())},
            }
            fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass

    def _compute_trade_today_total_local(self) -> Tuple[float, float]:
        """返回（today_u, total_u）。today=本机自然日 0-24 点；total=每日叠加。"""
        try:
            td = date.today().isoformat()

            # 1) 取本轮解析出的“按日累计”
            cur = dict(getattr(self.cache, 'trade_by_day', {}) or {})
            cur_days: Dict[str, float] = {}
            for k, v in cur.items():
                ks = str(k).strip()
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", ks):
                    continue
                try:
                    fv = float(v)
                except Exception:
                    continue
                if fv < 0:
                    fv = abs(fv)
                cur_days[ks] = fv

            # 2) Bot 真值提示：优先用于“今日”，避免切源/半行时把今日交易额抹成 0
            bot_today = _safe_float(getattr(self, '_bot_trade_today_hint', 0.0), 0.0)
            bot_total = _safe_float(getattr(self, '_bot_trade_total_hint', 0.0), 0.0)

            if not getattr(self, '_trade_daily_cache', None):
                self._trade_daily_cache = self._load_trade_daily_store()
            merged = dict(self._trade_daily_cache or {})

            # 3) 合并当前解析结果，但禁止用“空解析/0”把已有非 0 今日覆盖掉
            for d, amt in cur_days.items():
                prev = _safe_float(merged.get(d), 0.0)
                if amt > 0:
                    merged[d] = float(max(prev, amt))
                elif d not in merged:
                    merged[d] = 0.0

            prev_today = _safe_float(merged.get(td), 0.0)
            cur_today = _safe_float(cur_days.get(td), 0.0)
            merged[td] = float(max(prev_today, cur_today, bot_today))

            # 4) 总历史优先保留较大值；若 bot 给了更大的累计，也向上修正
            total_local = float(sum(float(x) for x in merged.values()))
            if bot_total > total_local and bot_total > 0:
                merged[td] = float(max(_safe_float(merged.get(td), 0.0), bot_today) + (bot_total - total_local))
                total_local = float(sum(float(x) for x in merged.values()))

            if merged != (self._trade_daily_cache or {}):
                self._trade_daily_cache = dict(merged)
                self._save_trade_daily_store(self._trade_daily_cache)

            today_u = float((self._trade_daily_cache or {}).get(td, 0.0))
            total_u = float(sum(float(x) for x in (self._trade_daily_cache or {}).values()))
            return today_u, total_u
        except Exception:
            return float(max(_safe_float(getattr(self, '_bot_trade_today_hint', 0.0), 0.0), getattr(self.cache, 'trade_today_u', 0.0) or 0.0)), float(max(_safe_float(getattr(self, '_bot_trade_total_hint', 0.0), 0.0), getattr(self.cache, 'trade_total_u', 0.0) or 0.0))

    def _load_profit_daily_store(self) -> Dict[str, Any]:
        """读取面板侧盈利日账本：每日记录 0 点余额，历史盈利按日盈利累计。"""
        try:
            fp = getattr(self, 'profit_daily_fp', None)
            if not fp:
                return {'days': {}}
            fp = Path(fp)
            if (not fp.exists()) or fp.stat().st_size <= 0:
                return {'days': {}}
            obj = _read_json_safe(fp)
            if not isinstance(obj, dict):
                return {'days': {}}
            days_raw = obj.get('days') if isinstance(obj.get('days'), dict) else {}
            days: Dict[str, Dict[str, Any]] = {}
            for k, v in days_raw.items():
                ks = str(k).strip()
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", ks):
                    continue
                rec = v if isinstance(v, dict) else {}
                try:
                    eq0 = float(rec.get('equity_0am', 0.0) or 0.0)
                except Exception:
                    eq0 = 0.0
                try:
                    day_profit = float(rec.get('day_profit', 0.0) or 0.0)
                except Exception:
                    day_profit = 0.0
                try:
                    last_total = float(rec['last_total']) if rec.get('last_total') is not None else eq0 + day_profit
                except Exception:
                    last_total = eq0 + day_profit
                days[ks] = {
                    'equity_0am': eq0,
                    'day_profit': day_profit,
                    'last_total': last_total,
                    'settled': bool(rec.get('settled', False)),
                    'updated_local': str(rec.get('updated_local') or ''),
                    'created_local': str(rec.get('created_local') or ''),
                    'close_total': (float(rec.get('close_total')) if rec.get('close_total') not in (None, '') else None),
                    'baseline_source': str(rec.get('baseline_source') or 'legacy'),
                    'baseline_day': str(rec.get('baseline_day') or ''),
                }
            return {'updated_local': str(obj.get('updated_local') or ''), 'days': days}
        except Exception:
            return {'days': {}}

    def _save_profit_daily_store(self, store: Dict[str, Any]) -> None:
        try:
            fp = getattr(self, 'profit_daily_fp', None)
            if not fp:
                return
            fp = Path(fp)
            fp.parent.mkdir(parents=True, exist_ok=True)
            days = store.get('days') if isinstance(store, dict) else {}
            if not isinstance(days, dict):
                days = {}
            payload_days: Dict[str, Dict[str, Any]] = {}
            for day in sorted(days.keys()):
                rec = days.get(day) if isinstance(days.get(day), dict) else {}
                payload_days[str(day)] = {
                    'equity_0am': round(float(rec.get('equity_0am', 0.0) or 0.0), 6),
                    'day_profit': round(float(rec.get('day_profit', 0.0) or 0.0), 6),
                    'last_total': round(float(rec.get('last_total', 0.0) or 0.0), 6),
                    'settled': bool(rec.get('settled', False)),
                    'updated_local': str(rec.get('updated_local') or ''),
                    'created_local': str(rec.get('created_local') or ''),
                    'close_total': (round(float(rec.get('close_total')), 6) if rec.get('close_total') is not None else None),
                    'baseline_source': str(rec.get('baseline_source') or 'legacy'),
                    'baseline_day': str(rec.get('baseline_day') or ''),
                }
            payload = {
                'updated_local': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'mode': 'observed_equity_change_carry_forward',
                'days': payload_days,
            }
            fp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass

    def _compute_equity_change_today_hist_local(self, total_usd: Optional[float] = None, eq_ok: bool = False) -> Tuple[float, float]:
        """权益变动包含浮盈亏与出入金；既有历史基准不会重算。"""
        if not getattr(self, '_profit_daily_cache', None):
            self._profit_daily_cache = self._load_profit_daily_store()
        store, today_change, history_change, changed = _panel_state.advance_equity_days(
            self._profit_daily_cache, today=date.today().isoformat(),
            observed_local=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            total=total_usd, valid=eq_ok,
        )
        if changed:
            self._profit_daily_cache = store
            self._save_profit_daily_store(store)
        return today_change, history_change


    def _discover_dirs(self, base: Path) -> Tuple[Optional[Path], Optional[Path], str]:
        """探测 out/logs（按文件存在性打分）。

        关键修复：趋势观察/选币排名主要读取 status.json / m9_detail_last.json。
        旧逻辑只认 equity/positions/trades，遇到 bot 刚启动、仓位快照暂缺、
        或只写出了结构化状态文件时，会把 out 误判成不存在，导致这两页一直空白。
        现在把结构化状态文件也纳入打分，只要状态文件在，就先接上。"""
        candidates_out = [
            base / "out",
            base / "OUT",
            base / "output" / "out",
            base.parent / "out",
            Path(tempfile.gettempdir()) / "bot_panel_bridge" / "out",
        ]
        candidates_logs = [
            base / "logs",
            base / "LOGS",
            base.parent / "logs",
            Path(tempfile.gettempdir()) / "bot_panel_bridge" / "logs",
            Path(tempfile.gettempdir()) / "bot_logs",
        ]

        best_out = None
        best_score = -1
        for d in candidates_out:
            score = 0
            if d.exists() and d.is_dir():
                if (d / "status.json").exists(): score += 4
                if (d / "m9_detail_last.json").exists(): score += 3
                if (d / "equity.json").exists(): score += 3
                if (d / "positions.json").exists(): score += 3
                if (d / "trades.jsonl").exists(): score += 2
                if (d / "control.json").exists(): score += 1
                if (d / "module_status.json").exists(): score += 1
                if (d / "budget_status.json").exists(): score += 1
            if score > best_score:
                best_score = score
                best_out = d if score > 0 else None

        best_logs = None
        best_lscore = -1
        for d in candidates_logs:
            score = 0
            if d.exists() and d.is_dir():
                if (d / "m9_detail_last.json").exists(): score += 4
                if (d / "BOT_grid_full.jsonl").exists(): score += 3
                if (d / "round_priority_rank_last.json").exists(): score += 2
                try:
                    if any(p.suffix == ".jsonl" for p in d.glob("*.jsonl")): score += 1
                except Exception:
                    pass
            if score > best_lscore:
                best_lscore = score
                best_logs = d if score > 0 else None

        reason = f"discover out_score={best_score} logs_score={best_lscore}"
        return best_out, best_logs, reason


    def _pick_latest(self, d: Path, names: List[str]) -> Optional[Path]:
        for n in names:
            p = d / n
            if p.exists():
                return p
        # fallback: 按后缀挑最新
        try:
            cand = [p for p in d.glob("*.json") if p.name in names]
            cand += [p for p in d.glob("*.jsonl") if p.name in names]
            if cand:
                cand.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                return cand[0]
        except Exception:
            pass
        return None

    
    def _iter_out_dir_candidates(self) -> List[Path]:
        """尽可能找到 Bot 的 out 目录（面板常见误读旧 out/positions.json 导致 JSONDecodeError）。
        规则：从当前 out_dir / bot_dir / 日志路径 / 临时桥接目录 / bot_console.txt 里推导并去重。
        """
        cands: List[Path] = []
        try:
            if self.out_dir:
                cands.append(self.out_dir)
                # out_dir 上一级可能就是 bot 根目录
                cands.append(self.out_dir.parent / "out")
        except Exception:
            pass
        try:
            # 面板自身目录
            if getattr(self, "panel_dir", None):
                cands.append(self.panel_dir / "out")
        except Exception:
            pass
        try:
            # bot_dir（若配置的是根目录）
            if self.bot_dir:
                bd = Path(self.bot_dir)
                if bd.name.lower() != "out":
                    cands.append(bd / "out")
                else:
                    cands.append(bd)
        except Exception:
            pass
        try:
            # 从日志推导：...\logs\BOT_grid_full.jsonl => 根目录=logs.parent => out=根目录/out
            if self.fp_log and self.fp_log.exists():
                root = self.fp_log.parent.parent
                cands.append(root / "out")
        except Exception:
            pass
        try:
            # TEMP 桥接目录
            cands.append(Path(tempfile.gettempdir()) / "bot_panel_bridge" / "out")
        except Exception:
            pass
        # 解析 bot_console.txt / bot_console.log 里的 out 路径（最稳）
        for probe in [
            ("bot_console.txt", True),
            ("bot_console.log", True),
            ("bot_console.txt", False),
            ("bot_console.log", False),
        ]:
            name, in_out = probe
            try:
                base = self.out_dir if (in_out and self.out_dir) else getattr(self, "panel_dir", None)
                if not base:
                    continue
                fp = Path(base) / name
                if not fp.exists():
                    continue
                s = fp.read_text(encoding="utf-8", errors="ignore")
                m = re.search(r"see\s+(.+?\\out\\grid_ui_url\.txt)", s)
                if m:
                    out_fp = Path(m.group(1)).parent
                    cands.append(out_fp)
            except Exception:
                continue
        # 去重（按出现顺序）
        uniq: List[Path] = []
        seen = set()
        for p in cands:
            try:
                p2 = p
                if not p2:
                    continue
                k = str(p2).lower()
                if k in seen:
                    continue
                seen.add(k)
                uniq.append(p2)
            except Exception:
                continue
        return uniq

    def _iter_status_like_files(self) -> List[Path]:
        """收集趋势观察/选币排名可读取的状态文件候选。"""
        cands: List[Path] = []

        def _add(p: Optional[Path]) -> None:
            try:
                if p:
                    cands.append(Path(p))
            except Exception:
                pass

        try:
            _add(getattr(self, 'fp_status', None))
        except Exception:
            pass
        try:
            if getattr(self, 'out_dir', None):
                _add(Path(self.out_dir) / 'status.json')
                _add(Path(self.out_dir) / 'm9_detail_last.json')
        except Exception:
            pass
        try:
            if getattr(self, 'logs_dir', None):
                _add(Path(self.logs_dir) / 'status.json')
                _add(Path(self.logs_dir) / 'm9_detail_last.json')
                _add(Path(self.logs_dir) / 'round_priority_rank_last.json')
        except Exception:
            pass
        try:
            if getattr(self, 'bot_dir', None):
                _add(Path(self.bot_dir) / 'out' / 'status.json')
                _add(Path(self.bot_dir) / 'out' / 'm9_detail_last.json')
                _add(Path(self.bot_dir) / 'logs' / 'm9_detail_last.json')
                _add(Path(self.bot_dir) / 'logs' / 'round_priority_rank_last.json')
        except Exception:
            pass
        try:
            for _d in self._iter_out_dir_candidates():
                _add(_d / 'status.json')
                _add(_d / 'm9_detail_last.json')
        except Exception:
            pass

        out: List[Path] = []
        seen = set()
        for p in cands:
            try:
                sp = str(p.resolve())
            except Exception:
                sp = str(p)
            if sp in seen:
                continue
            seen.add(sp)
            try:
                if p.exists() and p.is_file() and p.stat().st_size > 0:
                    out.append(p)
            except Exception:
                continue
        return out

    def _load_panel_status_payload(self, base_status: Optional[Dict[str, Any]] = None, required_keys: Optional[List[str]] = None) -> Tuple[Dict[str, Any], str]:
        """给趋势观察/选币排名装配可用状态，并返回实际命中的来源文件路径。"""
        req = [str(x) for x in list(required_keys or []) if str(x or '').strip()]
        base = dict(base_status) if isinstance(base_status, dict) else {}

        def _non_empty(v: Any) -> bool:
            return v not in (None, '', [], {}, ())

        def _hit_count(obj: Dict[str, Any]) -> int:
            if not isinstance(obj, dict):
                return 0
            if req:
                return sum(1 for k in req if _non_empty(obj.get(k)))
            return sum(1 for _k, _v in obj.items() if _non_empty(_v))

        best_payload = dict(base) if isinstance(base, dict) else {}
        best_path = str(getattr(self, 'fp_status', '-') or '-')
        best_score = _hit_count(best_payload) * 100
        candidates: List[Tuple[int, float, str, Dict[str, Any]]] = []

        for fp in self._iter_status_like_files():
            try:
                data = _read_json_safe(fp)
            except Exception:
                data = {}
            if not isinstance(data, dict) or not data:
                continue
            hit = _hit_count(data)
            try:
                mtime = float(fp.stat().st_mtime)
            except Exception:
                mtime = 0.0
            name_bonus = 15 if fp.name.lower() == 'status.json' else (10 if fp.name.lower() == 'm9_detail_last.json' else 0)
            score = hit * 100 + name_bonus
            candidates.append((score, mtime, str(fp), data))
            if hit > 0 and (score > best_score or (score == best_score and mtime > 0)):
                best_payload = dict(data)
                best_path = str(fp)
                best_score = score

        if req and _hit_count(best_payload) < len(req):
            candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
            merged = dict(best_payload)
            for _score, _mtime, _path, data in candidates:
                for k in req:
                    if not _non_empty(merged.get(k)) and _non_empty(data.get(k)):
                        merged[k] = data.get(k)
                if _hit_count(merged) >= len(req):
                    break
            best_payload = merged
        elif not best_payload and candidates:
            candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
            best_payload = dict(candidates[0][3])
            best_path = candidates[0][2]

        if isinstance(base, dict):
            for k, v in base.items():
                if _non_empty(v) or k not in best_payload:
                    best_payload[k] = v

        return (best_payload if isinstance(best_payload, dict) else {}), (best_path or '-')


    def _pick_parsable_json(self, out_dirs: List[Path], filename: str, *, want: str = "dict") -> Optional[Path]:
        """在多个 out 目录候选中，挑一个能被 json.loads 成功解析的文件路径。"""
        for d in out_dirs:
            try:
                p = Path(d) / filename
                obj, err = _read_json_obj(p, want=want, retries=2, sleep_s=0.03)
                if obj is not None:
                    return p
            except Exception:
                continue
        # fallback：返回第一个存在的
        for d in out_dirs:
            try:
                p = Path(d) / filename
                if p.exists():
                    return p
            except Exception:
                continue
        return None

    def recover_fp_positions(self) -> Tuple[Optional[Path], str]:
        """当 positions.json 解析失败时，尝试自动切换到正确的 out 目录与文件。"""
        try:
            out_dirs = self._iter_out_dir_candidates()
            p = self._pick_parsable_json(out_dirs, "positions.json", want="dict")
            if p and p.exists():
                self.fp_positions = p
                return p, ""
            return None, "no parsable positions.json in candidates"
        except Exception as e:
            return None, f"recover exception: {e}"


    def _find_recent_temp_log(self) -> Optional[Path]:
        try:
            td = Path(tempfile.gettempdir())
            # 优先 bot_logs
            for d in [td / "bot_logs", td]:
                if d.exists() and d.is_dir():
                    logs = []
                    for p in d.glob("*.jsonl"):
                        if p.stat().st_size > 0:
                            logs.append(p)
                    if logs:
                        logs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                        return logs[0]
        except Exception:
            pass
        return None

    def set_bot_script(self, bot_script: str) -> None:
        self.cfg.bot_script = bot_script or ""
        self.cfg.bot_dir = str(Path(bot_script).resolve().parent) if bot_script else ""
        self.cfg.save()
        self.cache = DataCache()
        self._init_paths()
        self._load_runtime()

    def choose_data_dir(self, folder: str) -> None:
        if not folder:
            return
        self.cfg.bot_dir = folder
        if not self.cfg.bot_script:
            self.cfg.bot_script = ""
        self.cfg.save()
        self.cache = DataCache()
        self._init_paths()

    def _load_runtime(self) -> None:
        try:
            if self.runtime_fp and self.runtime_fp.exists():
                obj = json.loads(self.runtime_fp.read_text(encoding="utf-8", errors="ignore"))
                if isinstance(obj, dict):
                    self.runtime_state.update(obj)
        except Exception:
            pass

    def _save_runtime(self) -> None:
        try:
            if not self.runtime_fp:
                return
            self.runtime_fp.parent.mkdir(parents=True, exist_ok=True)
            self.runtime_fp.write_text(json.dumps(self.runtime_state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _env_ok(self, eq_ok: bool, pos_ok: bool) -> bool:
        if not (eq_ok and pos_ok):
            return False
        if self.fp_equity and self.fp_equity.exists():
            try:
                if time.time() - self.fp_equity.stat().st_mtime > STALE_SEC:
                    return False
            except Exception:
                pass
        return True

    def read_control_state(self) -> dict:
        st = {"run_state": "stopped", "cmd": None, "mode": None, "updated_ts": 0.0}
        try:
            if self.fp_control and self.fp_control.exists():
                obj = _read_json(self.fp_control) or {}
                cmd = obj.get("cmd") or obj.get("command") or obj.get("action") or obj.get("act")
                run_state = obj.get("run_state") or obj.get("state")
                mode_raw = obj.get("mode") or obj.get("profile") or obj.get("run_mode")
                # 兼容：Bot 只写 work_mode/sleep_mode 布尔值时，避免 mode=True 导致面板误判为 work
                sleep_flag = obj.get("sleep_mode") if ("sleep_mode" in obj) else obj.get("sleep")
                work_flag = obj.get("work_mode") if ("work_mode" in obj) else obj.get("work")
                mode = mode_raw
                try:
                    def _as_bool(v: Any) -> Optional[bool]:
                        if isinstance(v, bool):
                            return v
                        if v is None:
                            return None
                        s2 = str(v).strip().lower()
                        if s2 in ("true","1","yes","y","on"):
                            return True
                        if s2 in ("false","0","no","n","off"):
                            return False
                        return None
                    sb = _as_bool(sleep_flag)
                    wb = _as_bool(work_flag)
                    if isinstance(mode_raw, bool) or str(mode_raw).strip().lower() in ("true","false","1","0"):
                        if sb is True:
                            mode = "sleep"
                        elif wb is True:
                            mode = "work"
                        elif mode_raw is True:
                            mode = "sleep"
                        else:
                            mode = "work"
                except Exception:
                    pass
                if cmd:
                    st["cmd"] = str(cmd)
                if run_state:
                    st["run_state"] = str(run_state)
                if mode is not None and str(mode).strip():
                    st["mode"] = str(mode)


            c = (st.get("cmd") or "").lower().strip()
            if c == "pause":
                st["run_state"] = "paused"
            elif c in ("resume", "start"):
                st["run_state"] = "running"
            elif c in ("stop", "exit", "quit"):
                st["run_state"] = "stopped"
        except Exception:
            pass
        return st

    def write_control_cmd(self, cmd: str) -> bool:
        """多路径 + 多字段兼容写入"""
        try:
            if not self.bot_dir:
                return False
            out_dirs = []
            if self.out_dir:
                out_dirs.append(self.out_dir)
            out_dirs.append(self.bot_dir / "out")
            out_dirs.append(self.bot_dir)
            out_dirs.append(Path(tempfile.gettempdir()) / "bot_panel_bridge" / "out")

            ok_any = False
            now = time.time()
            payload = {
                "cmd": cmd, "command": cmd, "action": cmd,
                "updated_ts": now,
                "seq": int(now * 1000),
                "run_state": "running" if cmd in ("start", "resume") else ("paused" if cmd == "pause" else "stopped"),
                "start": cmd == "start",
                "pause": cmd == "pause",
                "resume": cmd == "resume",
                "exit": cmd in ("exit", "quit", "stop"),
            }
            for d in out_dirs:
                try:
                    d.mkdir(parents=True, exist_ok=True)
                    (d / "control.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                    (d / "control_cmd.txt").write_text(cmd, encoding="utf-8")
                    ok_any = True
                except Exception:
                    continue
            return ok_any
        except Exception:
            return False


    def write_profile(self, profile: str, params: Optional[dict] = None) -> bool:
        """写入 profile（形态）到 control.json（多路径），Bot 会轮询读取并应用。"""
        try:
            if not self.bot_dir:
                return False
            out_dirs = []
            if self.out_dir:
                out_dirs.append(self.out_dir)
            out_dirs.append(self.bot_dir / "out")
            out_dirs.append(self.bot_dir)
            out_dirs.append(Path(tempfile.gettempdir()) / "bot_panel_bridge" / "out")

            now = time.time()
            payload = {
                "cmd": "profile",
                "updated_ts": now,
                "seq": int(now * 1000),
                "profile": str(profile or "normal").strip().lower(),
                "params": params if isinstance(params, dict) else {},
            }
            ok_any = False
            txt = json.dumps(payload, ensure_ascii=False, indent=2)
            for od in out_dirs:
                try:
                    od.mkdir(parents=True, exist_ok=True)
                    fp = od / "control.json"
                    fp.write_text(txt, encoding="utf-8")
                    ok_any = True
                except Exception:
                    continue
            return ok_any
        except Exception:
            return False


    def write_control_mode(self, mode: str) -> bool:
        """写入模式切换指令：work/sleep。"""
        try:
            if not self.bot_dir:
                return False
            m = (mode or "").strip().lower()
            if m not in ("work", "sleep"):
                m = "work"
            mode_cn = "睡眠" if m == "sleep" else "工作"
            payload = {
                "ts": time.time(),
                "cmd": "mode",
                "mode": mode_cn,
                "mode_en": m,
                "sleep_mode": True if m == "sleep" else False,
                "work_mode": True if m == "work" else False,
            }

            out_dirs = []
            if self.out_dir:
                out_dirs.append(self.out_dir)
            out_dirs.append(self.bot_dir / "out")
            out_dirs.append(self.bot_dir)
            out_dirs.append(Path(tempfile.gettempdir()) / "bot_panel_bridge" / "out")

            ok_any = False
            for d in out_dirs:
                try:
                    d.mkdir(parents=True, exist_ok=True)
                    (d / "control.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                    (d / "control_cmd.txt").write_text("mode", encoding="utf-8")
                    ok_any = True
                except Exception:
                    continue
            return ok_any
        except Exception:
            return False
    def read_equity(self) -> Tuple[float, float, float, bool, str]:
        return _panel_state.equity_values(_read_json(self.fp_equity), now=time.time(),
            e_keys=E_KEY_CANDIDATES, v_keys=V_KEY_CANDIDATES, total_keys=T_KEY_CANDIDATES)

    def read_positions(self) -> Tuple[float, float, float, bool, Dict[str, float], str]:
        fp = self.fp_positions
        obj = _read_json(fp)
        if not obj:
            return 0.0, 0.0, 0.0, False, {}, "positions.json missing/invalid"

        ext = obj.get("EXT")
        var = obj.get("VAR")
        if ext is None:
            ext = _case_get(obj, ["ext"])
        if var is None:
            var = _case_get(obj, ["var"])

        e = _sum_positions_notional(ext)
        v = _sum_positions_notional(var)

        upnl_map: Dict[str, float] = {}
        _collect_upnl_by_coin(ext, upnl_map)
        _collect_upnl_by_coin(var, upnl_map)

        ok, reason = _panel_state.snapshot_status(obj, now=time.time(), kind="positions")
        return float(e), float(v), float(v - e), ok, upnl_map, reason

    def _tick_runtime(self, eq_ok: bool, ctl: dict, total_usd: float) -> None:
        try:
            today = date.today().isoformat()
            # 初始化历史基准
            if self.runtime_state.get("init_total") is None and eq_ok and _panel_state.finite_number(total_usd) is not None:
                self.runtime_state["init_total"] = float(total_usd)

            # 日切：把上一日最后一次的 total 记录为 yday_total_close（截止 24:00 的近似）
            if self.runtime_state.get("today") != today:
                # yesterday close = last_total
                last_total = self.runtime_state.get("last_total")
                if last_total is not None:
                    self.runtime_state["yday_total_close"] = float(last_total)
                self.runtime_state["today"] = today
                self.runtime_state["today_run_sec"] = 0.0

            # 记录本轮 total
            if eq_ok and _panel_state.finite_number(total_usd) is not None:
                self.runtime_state["last_total"] = float(total_usd)

            now = time.time()
            last = _safe_float(self.runtime_state.get("last_tick_ts"), 0.0)
            if last <= 0:
                self.runtime_state["last_tick_ts"] = now
                self._save_runtime()
                return
            dt = max(0.0, now - last)
            self.runtime_state["last_tick_ts"] = now

            is_running = (ctl.get("run_state") == "running")
            if is_running and eq_ok:
                self.runtime_state["today_run_sec"] = _safe_float(self.runtime_state.get("today_run_sec"), 0.0) + dt
                self.runtime_state["total_run_sec"] = _safe_float(self.runtime_state.get("total_run_sec"), 0.0) + dt

            if int(now) % 5 == 0:
                self._save_runtime()
        except Exception:
            return

    def check_error_change(self) -> str:
        """检测 bot_error.txt 是否有更新；若有更新且内容非空，返回错误文本（用于弹窗）。"""
        try:
            if not self.fp_err or (not self.fp_err.exists()):
                return ""
            st = self.fp_err.stat()
            if st.st_mtime <= self.last_err_mtime:
                return ""
            txt = _read_text(self.fp_err, max_chars=4000).strip()
            self.last_err_mtime = st.st_mtime
            return txt or ""
        except Exception:
            return ""

    def read_error_current(self, max_chars: int = 2000) -> str:
        """读取当前错误文本（不要求变更），用于把状态灯从 running 拉到 error。"""
        try:
            if not self.fp_err or (not self.fp_err.exists()):
                return ""
            return (_read_text(self.fp_err, max_chars=max_chars) or "").strip()
        except Exception:
            return ""

    def build_m1_view(self, raw_tail: List[str], action_rows: Optional[List[dict]] = None, queue_rows: Optional[List[dict]] = None) -> str:
        """本轮 M1 运行情况视图。

        优先：解析 Bot 输出的 HUMAN 块（结构化）。
        回退：若未发现 HUMAN 块，则从纯文本日志中抓取包含 M1（排除 M10/M11）的关键行。
        """

        # 0) 优先用结构化动作/队列生成中文视图（更稳定、更适配面板）
        try:
            if action_rows or queue_rows:
                def _cn_mod(m: str) -> str:
                    m = (m or "").strip()
                    mp = {
                        "M9": "网格(M9)",
                        "B1": "首次开仓(B1)",
                        "B3": "补腿(B3)",
                        "B13": "补腿(B1+B3)",
                        "B4": "亏损补仓(B4)",
                        "M10": "口径A止盈(M10)",
                        "B2": "双平止盈(B2)",
                        "M10+B2": "止盈组合(M10+B2)",
                    }
                    return mp.get(m, m or "-")

                def _cn_act(a: str) -> str:
                    a = (a or "").strip().lower()
                    if a in ("open", "open_full", "open_leg", "first_open"):
                        return "下单/开仓"
                    if a in ("close", "close_full", "close_part", "close_leg"):
                        return "平单/平仓"
                    if a in ("add", "add_layer", "increase", "grid_add"):
                        return "加仓"
                    if a in ("reduce", "decrease"):
                        return "减仓"
                    if a in ("add_leg", "hedge_fix", "b3"):
                        return "补腿"
                    if a in ("b4", "loss_add"):
                        return "补仓"
                    return a or "-"

                lines_out: List[str] = []
                lines_out.append("本轮 M1 运行情况：")
                # 已执行（下单/平单）
                if action_rows:
                    lines_out.append("已执行（下单/平单）：")
                    for r in list(action_rows)[-20:]:
                        if not isinstance(r, dict):
                            continue
                        tm = str(r.get("time") or "")[:8] or "-"
                        mod = _cn_mod(str(r.get("module") or ""))
                        act = _cn_act(str(r.get("action") or ""))
                        plat = str(r.get("platform") or "-")
                        coin = str(r.get("coin") or "-")
                        side = str(r.get("side") or "")
                        nu = float(r.get("notional_u") or 0.0)
                        lines_out.append(f"{tm}｜{mod}｜{act}｜{plat}｜{coin}｜{nu:.0f}U｜{side}")
                # 预计队列
                if queue_rows:
                    lines_out.append("")
                    lines_out.append("预计队列（下单/平单）：")
                    for r in list(queue_rows)[:20]:
                        if not isinstance(r, dict):
                            continue
                        tm = str(r.get("time") or "")[:8] or "-"
                        mod = _cn_mod(str(r.get("module") or ""))
                        act = _cn_act(str(r.get("action") or ""))
                        plat = str(r.get("platform") or "-")
                        coin = str(r.get("coin") or "-")
                        side = str(r.get("side") or "")
                        nu = float(r.get("notional_u") or 0.0)
                        code = str(r.get("code") or "")
                        lines_out.append(f"{tm}｜{mod}｜{act}｜{plat}｜{coin}｜{nu:.0f}U｜{side}｜{code}")
                return "\n".join(lines_out).strip()
        except Exception:
            pass

        blocks: List[str] = []

        # 1) 结构化 HUMAN 块
        for ln in reversed(raw_tail):
            s = (ln or "").strip()
            if not s.startswith("{"):
                continue
            try:
                obj = json.loads(s)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            if str(obj.get("type")) != "HUMAN":
                continue
            title = str(obj.get("title") or "")
            if title not in ("模块决策执行情况", "M1 路由决策（结构优先）", "M1 路由入队汇总"):
                continue
            ts = _ts_to_dt_str(obj.get("ts")) or "-"
            lines = obj.get("lines") or []
            if not isinstance(lines, list):
                continue
            head = f"[{ts}] {title}"
            body = "\\n".join(str(x) for x in lines)
            blocks.append(head + "\\n" + body)
            if len(blocks) >= 3:
                break
        if blocks:
            return "\\n\\n".join(reversed(blocks))

        # 2) 文本回退：抓取 M1 关键行（避免误判 M10/M11）
        key_lines: List[str] = []
        m1_re = re.compile(r"(?<![A-Za-z0-9])M1(?![0-9])")
        for ln in reversed(raw_tail):
            s = (ln or "").rstrip("\\n")
            if not s:
                continue
            if "M10" in s or "M11" in s or "M12" in s:
                continue
            if not (m1_re.search(s) or "M1_" in s or "[M1" in s or "M1（" in s):
                continue
            if "心跳" in s or "倒计时" in s or "等待下次扫描" in s:
                continue
            key_lines.append(s)
            if len(key_lines) >= 18:
                break

        if key_lines:
            key_lines = list(reversed(key_lines))
            return "本轮 M1 运行情况（回退解析：未发现 HUMAN 块）：\n" + "\n".join(key_lines)

        # 3) 文本回退2：命令行常见的 DEBUG 表 / TRACE 段（有时不含显式 M1 字样）
        for mk in ("==== DEBUG", "coin | EXT(", "[TRACE]"):
            blk = _find_last_block(raw_tail, mk, max_follow=180)
            if blk:
                return "本轮 M1 运行情况（回退解析：DEBUG/TRACE）：\n" + "\n".join(blk[:180])

        return "本轮 M1 运行情况：未解析到明细（未发现 HUMAN 块，也未发现 M1/DEBUG/TRACE 段）\n请确认 Bot 全量日志里有打印：逐币逐模块判定、拒绝原因、入队结果、TRACE 等。"

    def refresh(self) -> dict:
        # 趋势观察页等后续渲染会直接读取这些局部变量；
        # 必须先给默认值，避免 refresh 前段任意异常导致局部变量未定义。
        panel_status: Dict[str, Any] = {}
        panel_queue: Dict[str, Any] = {}
        panel_module_status: Dict[str, Any] = {}
        panel_budget_status: Dict[str, Any] = {}
        # 每轮刷新重新探测一次（解决“改版后路径变了没数据”）
        self._init_paths()

        ctl = self.read_control_state()

        eq = _read_json_safe(self.fp_equity)
        e_usd, v_usd, total_usd, eq_ok, eq_reason = self.read_equity()
        e_notional, v_notional, hedge_diff, pos_ok, upnl_map, pos_reason = self.read_positions()

        # 全量日志展示：优先选“命令行镜像”来源，尽量与用户看到一致
        fp_full = self._choose_full_log_fp()

        # 摘要动作来源：必须选“活跃写入”的结构化落盘文件，否则摘要不会跟随更新
        fp_actions = self._choose_actions_fp(fp_full)
        try:
            self.data_status["paths"] = self.data_status.get("paths", {})
            self.data_status["paths"]["actions"] = str(fp_actions) if fp_actions else ""
        except Exception:
            pass
        try:
            grid_fp = None
            for _p in [
                (self.out_dir / GRID_DATA_BASENAME) if self.out_dir else None,
                (self.bot_dir / "out" / GRID_DATA_BASENAME) if self.bot_dir else None,
            ]:
                if _p and _p.exists():
                    grid_fp = _p
                    break
            meta = f"人类日志：{str(fp_full) if fp_full else "-"} | 机器日志：{str(fp_actions) if fp_actions else "-"} | 网格数据：{str(grid_fp) if grid_fp else "-"}"
            if hasattr(self, "var_log_meta"):
                self.var_log_meta.set(meta)
            self.data_status["paths"]["human_log"] = str(fp_full) if fp_full else ""
            self.data_status["paths"]["machine_log"] = str(fp_actions) if fp_actions else ""
            self.data_status["paths"]["grid_data"] = str(grid_fp) if grid_fp else ""
        except Exception:
            pass

        # 动作源切换/全量扫描标记：用于抑制“切源抖动”导致的瞎滴滴
        prev_actions_fp = getattr(self, "_last_actions_fp", None)
        actions_src_switched = (prev_actions_fp is not None) and (str(prev_actions_fp) != str(fp_actions))
        setattr(self, "_last_actions_fp", fp_actions)
        actions_full_scan = bool(getattr(self.cache, "_need_full_scan", False)) or bool(actions_src_switched)
        added_actions = self.cache.ingest_trades_incremental(fp_actions)
        self._tick_runtime(eq_ok, ctl, total_usd)

        raw_tail = _slice_recent_rounds(_tail_lines(self.fp_log, max_lines=1400))

        # 全量日志尾部（展示/解析）走缓存：避免每次刷新都读 6000 行导致卡顿
        raw_tail_full_display, raw_tail_full_parse = self._get_full_log_tails_cached(fp_full, display_lines=2000, parse_lines=6000)

        full_log_lines = self._render_full_log_lines(fp_full, raw_tail_full_display)

        # 解析来源优先级：能拿到命令行镜像就用它来解析（否则经常显示 '-'）
        parse_lines = raw_tail_full_parse if raw_tail_full_parse else raw_tail


        # 爸爸最新口径：历史真实成交摘要只认 trades.jsonl 真成交。
        # 因此这里不再用全量日志/bootstrap 去“补摘要”，避免 ACCEPT / EXEC_DONE / 文本动作重新混入真实成交区。
        # parse_lines 只保留给队列/观测解析，不再参与摘要 action_rows 构建。

        # 解析排队/入队信息（用于顶部队首/下一条）
        queue_rows = _parse_queue_items_from_lines(parse_lines)

        # 面板专用结构化文件（若 Bot 有写，则优先读取以提升稳定性）
        panel_status = _read_json_safe(self.fp_status)
        panel_queue = _read_json_safe(self.fp_queue)
        panel_module_status = _read_json_safe(self.fp_module_status)
        panel_budget_status = _read_json_safe(self.fp_budget_status)

        # 1) 队列：优先使用 queue.json
        try:
            if isinstance(panel_queue, dict) and panel_queue:
                items = panel_queue.get("items") or panel_queue.get("queue") or panel_queue.get("rows") or []
                norm: List[dict] = []
                if isinstance(items, list):
                    for it in items[:20]:
                        if not isinstance(it, dict):
                            continue
                        def _fnum(x) -> float:
                            try:
                                return float(x)
                            except Exception:
                                try:
                                    return float(str(x).replace(",", ""))
                                except Exception:
                                    return 0.0
                        # 时间优先级：ts/time_ts/t -> timestamp/dt/datetime -> time(HH:MM:SS) -> now
                        raw_ts = (it.get("ts") or it.get("time_ts") or it.get("t") or it.get("timestamp")
                                  or it.get("ts_ms") or it.get("time_ms") or it.get("ts_us") or it.get("time_us")
                                  or it.get("dt") or it.get("datetime") or it.get("time_str"))
                        ts = _parse_ts_sec_any(raw_ts)
                        if ts <= 0:
                            # 尝试用日期字段拼接（若存在）
                            d0 = str(it.get("date") or it.get("day") or it.get("ymd") or "").strip()
                            t0 = str(it.get("time") or it.get("tm") or it.get("hhmmss") or "").strip()
                            if d0 and t0 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d0[:10]) and re.fullmatch(r"\d{2}:\d{2}:\d{2}", t0[:8]):
                                ts = _parse_ts_sec_any(f"{d0[:10]} {t0[:8]}")
                            else:
                                ts = _parse_ts_sec_any(it.get("dt") or it.get("datetime") or "")
                        if ts <= 0:
                            ts = 0.0
                        tm = str(it.get("time") or it.get("tm") or it.get("hhmmss") or "")[:8]
                        if not tm:
                            ds = str(it.get("dt") or it.get("datetime") or "")
                            ds2 = ds.replace("T", " ")
                            if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", ds2):
                                tm = ds2[11:19]
                        # time 展示：优先用源字段，其次用 ts 推导；都没有就显示 "--"（禁止用当前时间伪造）
                        if ts <= 0 and tm and re.fullmatch(r"\d{2}:\d{2}:\d{2}", tm):
                            try:
                                ts = _parse_ts_sec_any(f"{date.today().isoformat()} {tm}")
                            except Exception:
                                ts = 0.0
                        if not tm:
                            if ts > 0:
                                try:
                                    tm = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
                                except Exception:
                                    tm = "--"
                            else:
                                tm = "--"
                        norm.append({
                            "ts": ts,
                            "time": tm,
                            "coin": str(it.get("coin") or it.get("symbol") or it.get("c") or "-"),
                            "platform": str(it.get("platform") or it.get("plat") or it.get("p") or "-"),
                            "module": str(it.get("module") or it.get("mod") or it.get("m") or "-"),
                            "action": str(it.get("action") or it.get("act") or it.get("a") or "-"),
                            "side": str(it.get("side") or it.get("dir") or ""),
                            "notional_u": _fnum(it.get("notional_u") or it.get("usd") or it.get("amount_u") or it.get("amt") or 0.0),
                            "code": str(it.get("code") or ""),
                            "reason": str(it.get("reason") or ""),
                        })
                if norm:
                    queue_rows = norm
        except Exception:
            pass

        # 2) 模式：优先使用 status.json
        mode_txt_override = ""
        try:
            if isinstance(panel_status, dict) and panel_status:
                mode_txt_override = str(panel_status.get("mode_cn") or panel_status.get("mode") or panel_status.get("bucket") or "").strip()
        except Exception:
            mode_txt_override = ""

        # 2.1) 工作/睡眠时间档（用于面板显示倒计时）：默认 08:00-20:00，可从 status.json / control.json 覆盖
        work_start_h = 8
        work_end_h = 20
        try:
            if isinstance(panel_status, dict) and panel_status:
                ws = panel_status.get("work_start_hour") or panel_status.get("WORK_START_HOUR") or panel_status.get("work_start_h")
                we = panel_status.get("work_end_hour") or panel_status.get("WORK_END_HOUR") or panel_status.get("work_end_h")
                if ws is not None:
                    work_start_h = int(ws)
                if we is not None:
                    work_end_h = int(we)
        except Exception:
            pass
        try:
            if isinstance(ctl, dict) and ctl:
                ws = ctl.get("work_start_hour") or ctl.get("WORK_START_HOUR") or ctl.get("work_start_h")
                we = ctl.get("work_end_hour") or ctl.get("WORK_END_HOUR") or ctl.get("work_end_h")
                if ws is not None:
                    work_start_h = int(ws)
                if we is not None:
                    work_end_h = int(we)
        except Exception:
            pass

        # 3) 模块状态/预算：保留原逻辑，同时把结构化数据透传给 UI 以优先展示
        budget_status = panel_budget_status if isinstance(panel_budget_status, dict) else {}
        module_status = panel_module_status if isinstance(panel_module_status, dict) else {}
        # 资金/持仓只采用带时间戳的结构化快照；历史stdout仍展示，但不能覆盖有效性。

        # 额外候选：有些版本把“扫描开始/预计下次扫描”写在 bot_run.jsonl 或 TEMP 回退日志里
        extra_logs: List[Path] = []
        try:
            if self.logs_dir:
                # logs 目录下最新 jsonl
                cand = sorted([p for p in self.logs_dir.glob("*.jsonl")], key=lambda p: p.stat().st_mtime, reverse=True)
                extra_logs += cand[:3]
        except Exception:
            pass
        try:
            td = Path(tempfile.gettempdir())
            for d in [td / "bot_logs", td]:
                if d.exists() and d.is_dir():
                    cand = sorted([p for p in d.glob("*.jsonl")], key=lambda p: p.stat().st_mtime, reverse=True)
                    extra_logs += cand[:3]
        except Exception:
            pass
        # 指标缓存更新（用于摘要逐笔 RSI/ADX）
        try:
            _md = getattr(self, "model", None)
            _ensure_ind_sources_loaded(getattr(_md, "out_dir", None), getattr(_md, "logs_dir", None))
        except Exception:
            pass

        # 去重
        seen = set()
        extra_logs2: List[Path] = []
        for p in extra_logs:
            sp = str(p)
            if sp in seen:
                continue
            seen.add(sp)
            extra_logs2.append(p)

        # 从 parse_lines + 额外候选里优先找出扫描时间
        scan_now, scan_next = _extract_scan_times(parse_lines)
        scan_src = str(fp_full) if (raw_tail_full_parse and (scan_now != "-" or scan_next != "-")) else (str(self.fp_log) if self.fp_log else "")
        if scan_now == "-" and scan_next == "-":
            # 若命令行镜像/主日志都没找到，再去额外候选找
            for p in extra_logs2:
                lines = _slice_recent_rounds(_tail_lines(p, max_lines=1400))
                sn, sx = _extract_scan_times(lines)
                if sn != "-" or sx != "-":
                    scan_now, scan_next = sn, sx
                    scan_src = str(p)
                    break

        scan_today_cnt = _count_today_scans(parse_lines)

        # M1 运行情况：只取“本轮扫描开始”后的片段，避免多轮混杂导致太乱
        m1_lines = _slice_since_last_scan(parse_lines)
        m1_view = self.build_m1_view(m1_lines, action_rows=list(self.cache.action_rows[-80:]), queue_rows=queue_rows)

        # UPNL top3
        top3 = sorted(upnl_map.items(), key=lambda kv: kv[1], reverse=True)[:3]

        # 多空比（long/short ratio，多空比例）：优先用 stdout 解析（更贴近命令行），否则回退 positions.json
        ls_ratio_text = "-"
        ls_small_text = "-"
        ls_small_text_ext = "-"
        ls_small_text_var = "-"
        try:
            ext_src: Any = None
            var_src: Any = None
            if pos2_ok and (ext_pos2 or var_pos2):
                ext_src = ext_pos2
                var_src = var_pos2
            else:
                pobj = _read_json(self.fp_positions) or {}

                # 兼容 positions.json 结构：
                # 1) {"EXT": [...], "VAR": [...]} 或 {"ext": [...], "var": [...]}  
                # 2) {"positions": [...]}（每行含 platform 字段）
                # 3) 直接就是 list[dict]
                ext_src = None
                var_src = None
                if isinstance(pobj, dict):
                    ext_src = pobj.get("EXT")
                    var_src = pobj.get("VAR")
                    if ext_src is None:
                        ext_src = _case_get(pobj, ["ext", "ext_positions", "ext_pos", "positions_ext"])
                    if var_src is None:
                        var_src = _case_get(pobj, ["var", "var_positions", "var_pos", "positions_var"])

                    # {"positions": [...]} 形式
                    if ext_src is None and var_src is None:
                        maybe_list = _case_get(pobj, ["positions", "data", "items", "rows"]) 
                        if isinstance(maybe_list, list):
                            ext_list = []
                            var_list = []
                            for it in maybe_list:
                                if not isinstance(it, dict):
                                    continue
                                plat = str(_case_get(it, ["platform", "plat", "p", "exchange"]) or "").upper()
                                if "EXT" in plat or plat in ("E", "PLAT_E"):
                                    ext_list.append(it)
                                elif "VAR" in plat or plat in ("V", "PLAT_V"):
                                    var_list.append(it)
                            ext_src = ext_list if ext_list else None
                            var_src = var_list if var_list else None

                # list 形式
                if ext_src is None and var_src is None and isinstance(pobj, list):
                    ext_list = []
                    var_list = []
                    for it in pobj:
                        if not isinstance(it, dict):
                            continue
                        plat = str(_case_get(it, ["platform", "plat", "p", "exchange"]) or "").upper()
                        if "EXT" in plat or plat in ("E", "PLAT_E"):
                            ext_list.append(it)
                        elif "VAR" in plat or plat in ("V", "PLAT_V"):
                            var_list.append(it)
                    ext_src = ext_list if ext_list else None
                    var_src = var_list if var_list else None

            el, es, ecl, ecs = _calc_long_short_stats(ext_src)
            vl, vs, vcl, vcs = _calc_long_short_stats(var_src)
            al, a_s, acl, acs = (el + vl), (es + vs), (ecl + vcl), (ecs + vcs)
            # 小字：展示“所有持仓多/空名义价值”（用于顶部对冲健康卡片红框位置）
            if (al + a_s) > 0:
                ls_small_text = f"总多: {al:.2f}U    总空: {a_s:.2f}U"
                if (el + es) > 0:
                    ls_small_text_ext = f"EXT  多: {el:.2f}U    空: {es:.2f}U"
                if (vl + vs) > 0:
                    ls_small_text_var = f"VAR  多: {vl:.2f}U    空: {vs:.2f}U"

            # 如果完全没有可识别的方向数据，则保持 '-'
            if (el + es + vl + vs) > 0:
                ls_ratio_text = "\n".join([
                    _fmt_ls_ratio_line("EXT（平台E）", el, es, ecl, ecs),
                    _fmt_ls_ratio_line("VAR（平台V）", vl, vs, vcl, vcs),
                    _fmt_ls_ratio_line("合计（两平台）", al, a_s, acl, acs),
                ])
        except Exception:
            ls_ratio_text = "-"
            ls_small_text = "-"
            ls_small_text_ext = "-"
            ls_small_text_var = "-"


        # 多空比明细（替代原 M1 区域展示用）：平台汇总 + 分币种汇总
        ls_ratio_detail_text = "-"
        try:
            # 基于上面已经解析好的 ext_src/var_src
            el, es, ecl, ecs = _calc_long_short_stats(ext_src)
            vl, vs, vcl, vcs = _calc_long_short_stats(var_src)
            al, a_s, acl, acs = (el + vl), (es + vs), (ecl + vcl), (ecs + vcs)

            lines = []
            if (el + es + vl + vs) > 0:
                lines.append(_fmt_ls_ratio_line("EXT（平台E）", el, es, ecl, ecs))
                lines.append(_fmt_ls_ratio_line("VAR（平台V）", vl, vs, vcl, vcs))
                lines.append(_fmt_ls_ratio_line("合计（两平台）", al, a_s, acl, acs))
                lines.append("")

                # per-coin (aggregate ext+var)
                ed = _calc_long_short_by_coin(ext_src)
                vd = _calc_long_short_by_coin(var_src)
                coins = sorted(set(ed.keys()) | set(vd.keys()))

                if coins:
                    lines.append("币种多空比（名义 Notional 口径）")

                    # 表头（固定宽度，便于一眼对齐）
                    lines.append("币种  | 合计 多U        | 合计 空U        | 多%    | 空%    | 多/空 | 笔数(多/空) | EXT多/空 | VAR多/空")
                    lines.append("----- | ------------- | ------------- | ------ | ------ | ----- | ---------- | ------- | -------")

                    rows = []
                    for coin in coins:
                        e = ed.get(coin, {"long_u": 0.0, "short_u": 0.0, "cnt_long": 0, "cnt_short": 0})
                        v = vd.get(coin, {"long_u": 0.0, "short_u": 0.0, "cnt_long": 0, "cnt_short": 0})
                        el_u = float(e.get("long_u", 0.0))
                        es_u = float(e.get("short_u", 0.0))
                        vl_u = float(v.get("long_u", 0.0))
                        vs_u = float(v.get("short_u", 0.0))
                        tl = el_u + vl_u
                        ts = es_u + vs_u
                        total = tl + ts
                        if total <= 0:
                            continue
                        lp = tl / total * 100.0
                        sp = ts / total * 100.0

                        if ts <= 0 and tl > 0:
                            rtxt = "INF"
                        elif ts <= 0 and tl <= 0:
                            rtxt = "-"
                        else:
                            rtxt = f"{(tl / ts):.2f}"

                        # 分平台多/空比（用于快速定位是哪边偏了）
                        def _r(a: float, b: float) -> str:
                            if b <= 0 and a > 0:
                                return "INF"
                            if b <= 0 and a <= 0:
                                return "-"
                            return f"{(a / b):.2f}"

                        ext_r = _r(el_u, es_u)
                        var_r = _r(vl_u, vs_u)

                        cnt_l = int(e.get("cnt_long", 0)) + int(v.get("cnt_long", 0))
                        cnt_s = int(e.get("cnt_short", 0)) + int(v.get("cnt_short", 0))

                        rows.append((total, f"{coin:<5} | {tl:>13,.2f}U | {ts:>13,.2f}U | {lp:>5.2f}% | {sp:>5.2f}% | {rtxt:>5} | {cnt_l:>4}/{cnt_s:<4}     | {ext_r:>6} | {var_r:>6}"))

                    # 按该币总名义从大到小排序（你现在只有 8 个币，默认全展示）
                    rows.sort(key=lambda x: x[0], reverse=True)
                    max_show = len(rows)
                    for _, line in rows[:max_show]:
                        lines.append(line)


                ls_ratio_detail_text = "\n".join(lines) if lines else "-"
        except Exception:
            ls_ratio_detail_text = "-"

        # 多空比结构化数据（用于更好看的表格展示）：三行总览 + 按币种明细
        ls_ratio_summary_lines = []
        ls_ratio_coin_rows = []
        try:
            if (el + es + vl + vs) > 0:
                ls_ratio_summary_lines = [
                    _fmt_ls_ratio_line("EXT（平台E）", el, es, ecl, ecs),
                    _fmt_ls_ratio_line("VAR（平台V）", vl, vs, vcl, vcs),
                    _fmt_ls_ratio_line("合计（两平台）", al, a_s, acl, acs),
                ]

                ed = _calc_long_short_by_coin(ext_src)
                vd = _calc_long_short_by_coin(var_src)
                coins = sorted(set(ed.keys()) | set(vd.keys()))
                rows = []
                def _ratio(a: float, b: float) -> str:
                    if b <= 0 and a > 0:
                        return "INF"
                    if b <= 0 and a <= 0:
                        return "-"
                    return f"{(a / b):.2f}"

                for coin in coins:
                    e = ed.get(coin, {"long_u": 0.0, "short_u": 0.0, "cnt_long": 0, "cnt_short": 0})
                    v = vd.get(coin, {"long_u": 0.0, "short_u": 0.0, "cnt_long": 0, "cnt_short": 0})
                    el_u = float(e.get("long_u", 0.0))
                    es_u = float(e.get("short_u", 0.0))
                    vl_u = float(v.get("long_u", 0.0))
                    vs_u = float(v.get("short_u", 0.0))
                    tl = el_u + vl_u
                    ts = es_u + vs_u
                    total = tl + ts
                    if total <= 0:
                        continue
                    lp = (tl / total) * 100.0
                    sp = (ts / total) * 100.0
                    cnt_l = int(e.get("cnt_long", 0)) + int(v.get("cnt_long", 0))
                    cnt_s = int(e.get("cnt_short", 0)) + int(v.get("cnt_short", 0))

                    latest_rsi, latest_adx = _lookup_latest_rsi_adx_live(getattr(self, "out_dir", None), getattr(self, "logs_dir", None), str(coin))
                    rows.append({
                        "coin": str(coin),
                        "total_u": total,
                        "long_u": tl,
                        "short_u": ts,
                        "long_pct": lp,
                        "short_pct": sp,
                        "ls_ratio": _ratio(tl, ts),
                        "cnt": f"{cnt_l}/{cnt_s}",
                        "ext_ratio": _ratio(el_u, es_u),
                        "var_ratio": _ratio(vl_u, vs_u),
                        "latest_rsi": latest_rsi,
                        "latest_adx": latest_adx,
                    })

                rows.sort(key=lambda r: float(r.get("total_u", 0.0)), reverse=True)
                ls_ratio_coin_rows = rows
        except Exception:
            ls_ratio_summary_lines = []
            ls_ratio_coin_rows = []

        env_ok = self._env_ok(eq_ok, pos_ok)
        self.data_status["env"] = "ok" if env_ok else "bad"
        self.data_status["equity"] = "ok" if eq_ok else "bad"
        self.data_status["positions"] = "ok" if pos_ok else "bad"
        self.data_status["reason"] = f"equity={eq_reason}; positions={pos_reason}; {self.data_status.get('reason','')}"
        self.data_status["paths"] = self.data_status.get("paths", {})

        # Bot 报错监测：优先以 bot_error.txt 判定（避免 bot 已崩溃但 run_state 仍显示 running）
        err_changed_txt = self.check_error_change()
        err_current = self.read_error_current()

        # 运行状态：以 control.json 的 paused 为准；其余情况以“日志活跃度”推断 running/stopped，
        # 这样即使 error.txt 没清掉，跑马灯也不会熄灭
        base_state = str(ctl.get("run_state", "stopped") or "").lower().strip()
        bot_active = False
        try:
            fp_active = None
            if fp_full and fp_full.exists():
                fp_active = fp_full
            elif self.fp_log and self.fp_log.exists():
                fp_active = self.fp_log
            if fp_active:
                age = time.time() - float(fp_active.stat().st_mtime)
                if age < 30:
                    bot_active = True
        except Exception:
            pass

        # 额外兜底：若 scan_now 很新，也认为在 running（某些环境下 mtime 不及时）
        try:
            if (not bot_active) and (scan_now and scan_now != "-"):
                dt_scan = None
                sn = str(scan_now)
                if re.match(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", sn):
                    dt_scan = datetime.strptime(sn, "%Y-%m-%d %H:%M:%S")
                elif re.match(r"\d{2}:\d{2}:\d{2}", sn):
                    dt_scan = datetime.strptime(date.today().isoformat() + " " + sn, "%Y-%m-%d %H:%M:%S")
                if dt_scan and abs((datetime.now() - dt_scan).total_seconds()) <= 360:
                    bot_active = True
        except Exception:
            pass

        # === 稳定性兜底：避免摘要“有时有/有时没”的闪烁 ===
        # 典型原因：
        # - 动作源文件在 trades.jsonl / BOT_grid_full.jsonl 之间来回切换，触发 cache 清空
        # - Windows 写入时出现半行/文件锁，导致本轮解析得到 0 行
        # 处理：若本轮解析为空，则沿用上一轮的“最后一次非空摘要/交易额”
        try:
            if getattr(self.cache, 'action_rows', None):
                if len(self.cache.action_rows) > 0:
                    self._last_good_action_rows = list(self.cache.action_rows)
                    today_u_local, total_u_local = self._compute_trade_today_total_local()
                    self._last_good_trades = {
                        'today_u': float(today_u_local),
                        'total_u': float(total_u_local),
                        'today_by_plat': dict(getattr(self.cache, 'trade_today_by_plat', {}) or {}),
                        'total_by_plat': dict(getattr(self.cache, 'trade_total_by_plat', {}) or {}),
                    }
        except Exception:
            pass

        use_fallback = False
        try:
            if (not getattr(self.cache, 'action_rows', None)) or (len(self.cache.action_rows) == 0):
                lg = getattr(self, '_last_good_action_rows', None)
                if isinstance(lg, list) and len(lg) > 0:
                    use_fallback = True
        except Exception:
            pass

        if use_fallback:
            try:
                self.data_status['actions_fallback'] = 'using_last_good'
            except Exception:
                pass

        if base_state == "paused":
            run_state = "paused"
        else:
            run_state = "running" if bot_active else "stopped"

        anchor_view = self._read_anchor_panel_view(eq_obj=eq)
        eq_trade_today_hint = _safe_float((eq or {}).get("today_trade_total_u"), _safe_float((panel_status or {}).get("today_trade_total_u"), 0.0))
        eq_trade_total_hint = _safe_float((eq or {}).get("hist_trade_total_u"), _safe_float((panel_status or {}).get("hist_trade_total_u"), 0.0))
        self._bot_trade_today_hint = float(eq_trade_today_hint)
        self._bot_trade_total_hint = float(eq_trade_total_hint)
        today_u_local, total_u_local = self._compute_trade_today_total_local()
        equity_change_today, equity_change_hist = self._compute_equity_change_today_hist_local(total_usd=total_usd, eq_ok=eq_ok)
        return {
            "equity": {
                "e": e_usd, "v": v_usd, "t": total_usd, "ok": eq_ok,
                "anchor_current_u": _safe_float(anchor_view.get("anchor_u"), 0.0),
                "anchor_next_trigger_u": _safe_float(anchor_view.get("next_trigger_u"), 0.0),
                "anchor_takeprofit_pct": _safe_float(anchor_view.get("takeprofit_pct"), 0.01),
                "today_trade_total_u": float(eq_trade_today_hint),
                "hist_trade_total_u": float(eq_trade_total_hint),
            },
            "anchor": {
                "diff": total_usd - ANCHOR_U,
                "pct": ((total_usd - ANCHOR_U) / ANCHOR_U * 100.0) if ANCHOR_U else 0.0,
                "anchor_u": _safe_float(anchor_view.get("anchor_u"), 0.0),
                "next_trigger_u": _safe_float(anchor_view.get("next_trigger_u"), 0.0),
                "takeprofit_pct": _safe_float(anchor_view.get("takeprofit_pct"), 0.01),
            },
            "hedge": {"e_notional": e_notional, "v_notional": v_notional, "diff": hedge_diff, "ok": pos_ok},
            "runtime": {"today_sec": float(self.runtime_state.get("today_run_sec", 0.0)), "total_sec": float(self.runtime_state.get("total_run_sec", 0.0)), "profit_today": float(equity_change_today), "profit_hist": float(equity_change_hist)},
            "lights": {"run_state": run_state},
            "ctl": dict(ctl),
            "env": {"ok": env_ok},
            "trades": {
                "today_u": (getattr(self, "_last_good_trades", {}) or {}).get("today_u", 0.0) if use_fallback else float(max(today_u_local, eq_trade_today_hint)),
                "total_u": (getattr(self, "_last_good_trades", {}) or {}).get("total_u", 0.0) if use_fallback else float(max(total_u_local, eq_trade_total_hint)),
            },
            "trades_by_plat": {"ext_today_u": ((getattr(self, "_last_good_trades", {}).get("today_by_plat", {}).get('EXT', 0.0)) if use_fallback else self.cache.trade_today_by_plat.get('EXT', 0.0)), "var_today_u": ((getattr(self, "_last_good_trades", {}).get("today_by_plat", {}).get('VAR', 0.0)) if use_fallback else self.cache.trade_today_by_plat.get('VAR', 0.0)), "ext_total_u": ((getattr(self, "_last_good_trades", {}).get("total_by_plat", {}).get('EXT', 0.0)) if use_fallback else self.cache.trade_total_by_plat.get('EXT', 0.0)), "var_total_u": ((getattr(self, "_last_good_trades", {}).get("total_by_plat", {}).get('VAR', 0.0)) if use_fallback else self.cache.trade_total_by_plat.get('VAR', 0.0))},
            "action_rows": (list(getattr(self, "_last_good_action_rows", [])[-ACTION_ROWS_MAX_KEEP:]) if use_fallback else list(self.cache.action_rows[-ACTION_ROWS_MAX_KEEP:])),
            "queue_rows": queue_rows,
            "mode_txt": mode_txt_override,
            "work_start_h": work_start_h,
            "work_end_h": work_end_h,
            "budget_status": budget_status,
            "module_status": module_status,
            "log_full": full_log_lines,
            "m1_view": m1_view,
            "scan": {"now": scan_now, "next": scan_next, "src": scan_src, "today_cnt": int(scan_today_cnt)},
            "events": {
                "added_actions": added_actions,
                "error_changed": bool(err_changed_txt),
                "error_text": (err_changed_txt or err_current),
                "actions_src": (str(fp_actions) if fp_actions else ""),
                "actions_src_switched": bool(actions_src_switched),
                "actions_full_scan": bool(actions_full_scan),
            },
            "upnl_top3": top3,
            "ls_overview": {
                "total_long_u": float(al), "total_short_u": float(a_s), "total_long_cnt": int(acl), "total_short_cnt": int(acs),
                "ext_long_u": float(el), "ext_short_u": float(es), "ext_long_cnt": int(ecl), "ext_short_cnt": int(ecs),
                "var_long_u": float(vl), "var_short_u": float(vs), "var_long_cnt": int(vcl), "var_short_cnt": int(vcs),
            },
            "ls_ratio_text": ls_ratio_text,
            "ls_small_text": ls_small_text,
            "ls_small_text_ext": ls_small_text_ext,
            "ls_small_text_var": ls_small_text_var,
            "ls_ratio_detail_text": ls_ratio_detail_text,
            "ls_ratio_summary_lines": ls_ratio_summary_lines,
            "ls_ratio_coin_rows": ls_ratio_coin_rows,
            "status": dict(self.data_status),
            "browser_recovery_alert": str((panel_status or {}).get('browser_recovery_alert') or ''),
            "funds_imbalance_alert": str((panel_status or {}).get('funds_imbalance_alert') or ''),
        }


class PanelUI:
    def __init__(self, root: tk.Tk, model: PanelModel) -> None:
        self.root = root
        self.model = model
        self.bot = BotProc()

        self.root.title(APP_NAME)
        self.root.geometry("1500x900")
        self.root.minsize(1300, 760)

        # 置顶功能：按钮控制是否始终居于最前
        self._topmost_enabled = False
        self._topmost_btn_var = tk.StringVar(value="置顶：关")
        self._topmost_windows = []  # type: ignore
        self._topmost_job = None

        # 运行时长追踪：
        # - _run_start_ts：本次连续运行开始时刻
        # - _run_session_sec：本次连续运行已累计秒数（暂停/停止时冻结，恢复时重置）
        # 口径：
        # - “运行”= 本次打开机器人后最近一次 start/resume 到现在的连续运行时间
        # - “历史”= 面板累计总运行时长（total_run_sec）
        self._run_start_ts = None  # type: Optional[float]
        self._run_session_sec = 0.0
        self._last_run_state = None  # type: Optional[str]

        # 摘要时间稳定缓存：key=(plat,coin,act,amt,side,oc,module) -> {'ts': float, 'time': str}
        # 用途：如果上游数据源把 time/ts 写成“当前时间”（每次刷新都变），这里会冻结为首次看到/首次拿到稳定 ts 的时间
        self._stable_time_cache = {}  # type: dict
        self._stable_time_order = deque()  # type: deque
        self._summary_time_sort_desc = True

        # 小挂件置顶定时器
        self._mini_topmost_job = None
        self._init_style()
        self._build_shell()
        self._build_layout()
        # 置顶注册：把主窗口纳入置顶管理，并启动定时“抬升”防抢焦点
        try:
            self._register_topmost(self.root)
            self._sync_topmost_button_text()
            self._schedule_topmost_tick()
        except Exception:
            pass


        # 关闭窗口（右上角 X）时：自动杀 Bot 再退出面板（老板要求）
        try:
            self.root.protocol("WM_DELETE_WINDOW", self.on_exit)
        except Exception:
            pass

        # 启动即点亮边框跑马灯：避免首次数据刷新前边框不显示
        try:
            self._edge_set_mode("work")
        except Exception:
            pass

        self.mini_win: Optional[tk.Toplevel] = None
        self.mini_vars: Dict[str, tk.StringVar] = {}
        # 防止重复弹窗刷屏：同一错误文本只弹一次（按 hash 去重）
        self._last_err_popup_hash: str = ""
        self._last_panel_exc_hash: str = ""


        self._last_order_beep_ts = 0.0
        self._last_error_beep_ts = 0.0
        self._last_order_beep_key = ""

        self._queue_rows: List[dict] = []
        self._queue_base_ts: Optional[float] = None
        self._queue_last_sig: str = ""

        # 状态灯跑马灯（运行态）
        self._light_mode: str = "stopped"
        self._light_anim_idx: int = 0
        self._light_anim_job = None

        self._refresh_loop()

    def _init_style(self) -> None:
        self.root.configure(bg="#111214")
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background="#111214", foreground="#E6E6E6", fieldbackground="#111214")
        style.configure("Card.TFrame", background="#17181B", relief="flat")
        # Notebook（插页）样式：尽量贴近暗色主题
        try:
            style.configure("TNotebook", background="#111214", borderwidth=0, tabmargins=(0, 0, 0, 0))
            style.configure("TNotebook.Tab", background="#1B1C20", foreground="#E6E6E6", padding=(14, 8, 14, 8))
            style.map("TNotebook.Tab",
                      background=[("selected", "#17181B"), ("active", "#23242A"), ("!selected", "#1B1C20")],
                      foreground=[("selected", "#FFFFFF"), ("active", "#FFFFFF"), ("!selected", "#E6E6E6")],
                      padding=[("selected", (14, 8, 14, 8)), ("active", (14, 8, 14, 8)), ("!selected", (14, 8, 14, 8))],
                      expand=[("selected", (0, 0, 0, 0)), ("active", (0, 0, 0, 0)), ("!selected", (0, 0, 0, 0))])
        except Exception:
            pass

        style.configure("CardTitle.TLabel", background="#17181B", foreground="#B9C0C9", font=("Segoe UI", 10, "bold"))
        style.configure("CardValue.TLabel", background="#17181B", foreground="#E6E6E6", font=("Segoe UI", 16, "bold"))
        style.configure("SubValue.TLabel", background="#17181B", foreground="#E6E6E6", font=("Segoe UI", 11))
        style.configure("Hint.TLabel", background="#17181B", foreground="#9AA5B1", font=("Segoe UI", 9))
        style.configure("UpCardValue.TLabel", background="#17181B", foreground="#FF6B6B", font=("Segoe UI", 16, "bold"))
        style.configure("DownCardValue.TLabel", background="#17181B", foreground="#39D98A", font=("Segoe UI", 16, "bold"))
        style.configure("NeutralCardValue.TLabel", background="#17181B", foreground="#E6E6E6", font=("Segoe UI", 11, "bold"))
        style.configure("UpSubValue.TLabel", background="#17181B", foreground="#FF9B9B", font=("Segoe UI", 11, "bold"))
        style.configure("DownSubValue.TLabel", background="#17181B", foreground="#74E6AE", font=("Segoe UI", 11, "bold"))
        style.configure("NeutralSubValue.TLabel", background="#17181B", foreground="#C7CDD4", font=("Segoe UI", 11, "bold"))
        style.configure("Banner.TLabel", background="#0F1013", foreground="#E6E6E6", font=("Segoe UI", 10, "bold"), padding=8)
        style.configure("Btn.TButton", font=("Segoe UI", 10, "bold"), padding=7)
        style.configure("Treeview", background="#101114", fieldbackground="#101114", foreground="#E6E6E6", borderwidth=0)
        style.configure("Treeview.Heading", background="#17181B", foreground="#B9C0C9", font=("Segoe UI", 10, "bold"))
        style.map("Treeview", background=[("selected", "#2A2D33")])

        # 摘要表专用样式：对齐你给的参考图（更紧凑/黄字/暗底），不影响其它表
        try:
            style.configure(
                "Summary.Treeview",
                background="#0F1013",
                fieldbackground="#0F1013",
                foreground="#F6D365",
                borderwidth=0,
                font=("Consolas", 9, "bold"),
                rowheight=20,
            )
            style.configure(
                "Summary.Treeview.Heading",
                background="#17181B",
                foreground="#B9C0C9",
                font=("Microsoft YaHei UI", 9, "bold"),
            )
            style.map("Summary.Treeview", background=[("selected", "#2A2D33")], foreground=[("selected", "#F6D365")])
        except Exception:
            pass


    def _set_window_topmost(self, win: tk.Misc, enabled: bool, lift_win: bool = False) -> None:
        try:
            if (not win) or (not win.winfo_exists()):
                return
            try:
                st = str(getattr(win, 'state', lambda: '')())
                if st == 'withdrawn':
                    return
            except Exception:
                pass
            try:
                win.attributes('-topmost', bool(enabled))
            except Exception:
                try:
                    win.wm_attributes('-topmost', bool(enabled))
                except Exception:
                    pass
            if enabled and lift_win:
                try:
                    win.lift()
                except Exception:
                    pass
        except Exception:
            pass

    def _sync_topmost_button_text(self) -> None:
        try:
            self._topmost_btn_var.set('置顶：开' if getattr(self, '_topmost_enabled', False) else '置顶：关')
        except Exception:
            pass

    def on_toggle_topmost(self) -> None:
        try:
            self._topmost_enabled = not bool(getattr(self, '_topmost_enabled', False))
            self._sync_topmost_button_text()
            wins = list(getattr(self, '_topmost_windows', []) or [])
            extra = getattr(self, 'mini_win', None)
            if extra is not None:
                wins.append(extra)
            seen = set()
            for w in wins:
                try:
                    if (not w) or (not w.winfo_exists()):
                        continue
                    wid = id(w)
                    if wid in seen:
                        continue
                    seen.add(wid)
                    self._set_window_topmost(w, self._topmost_enabled, lift_win=self._topmost_enabled)
                except Exception:
                    pass
            if getattr(self, '_topmost_job', None):
                try:
                    self.root.after_cancel(self._topmost_job)
                except Exception:
                    pass
                self._topmost_job = None
            if getattr(self, '_mini_topmost_job', None):
                try:
                    self.root.after_cancel(self._mini_topmost_job)
                except Exception:
                    pass
                self._mini_topmost_job = None
            if self._topmost_enabled:
                self._schedule_topmost_tick()
                if getattr(self, 'mini_win', None) is not None:
                    self._schedule_mini_topmost_tick()
        except Exception:
            pass

    def _apply_topmost_now(self, win: tk.Misc) -> None:
        """对指定窗口立刻执行置顶+抬升（尽量温和）。"""
        try:
            if (not win) or (not win.winfo_exists()):
                return
            if not getattr(self, '_topmost_enabled', False):
                self._set_window_topmost(win, False, lift_win=False)
                return
            self._set_window_topmost(win, True, lift_win=True)
        except Exception:
            pass

    def _register_topmost(self, win: tk.Misc) -> None:
        """把窗口纳入置顶管理（主窗、小挂件、参数弹窗等）。"""
        try:
            if (not win) or (not win.winfo_exists()):
                return
            if win not in self._topmost_windows:
                self._topmost_windows.append(win)
            self._apply_topmost_now(win)
            try:
                win.bind('<FocusIn>', lambda e, w=win: self._apply_topmost_now(w))
            except Exception:
                pass
            try:
                win.bind('<Map>', lambda e, w=win: self._apply_topmost_now(w))
            except Exception:
                pass
        except Exception:
            pass

    def _schedule_topmost_tick(self) -> None:
        try:
            if self._topmost_job is not None:
                try:
                    self.root.after_cancel(self._topmost_job)
                except Exception:
                    pass
                self._topmost_job = None
            if not getattr(self, '_topmost_enabled', False):
                return
            self._topmost_job = self.root.after(1200, self._topmost_tick)
        except Exception:
            self._topmost_job = None

    def _apply_mini_topmost_now(self) -> None:
        """小挂件跟随全局置顶开关。"""
        try:
            win = getattr(self, 'mini_win', None)
            if (not win) or (not win.winfo_exists()):
                return
            self._set_window_topmost(win, bool(getattr(self, '_topmost_enabled', False)), lift_win=bool(getattr(self, '_topmost_enabled', False)))
        except Exception:
            pass

    def _mini_topmost_tick(self) -> None:
        """定时保持小挂件在最前，避免被系统/其他窗口抢层级。"""
        try:
            self._apply_mini_topmost_now()
        except Exception:
            pass
        try:
            win = getattr(self, 'mini_win', None)
            if win and win.winfo_exists():
                self._mini_topmost_job = self.root.after(800, self._mini_topmost_tick)
            else:
                self._mini_topmost_job = None
        except Exception:
            self._mini_topmost_job = None

    def _schedule_mini_topmost_tick(self) -> None:
        """启动/重置小挂件置顶定时器。"""
        try:
            if getattr(self, '_mini_topmost_job', None):
                try:
                    self.root.after_cancel(self._mini_topmost_job)
                except Exception:
                    pass
            if not getattr(self, '_topmost_enabled', False):
                self._mini_topmost_job = None
                return
            self._mini_topmost_job = self.root.after(800, self._mini_topmost_tick)
        except Exception:
            self._mini_topmost_job = None

    def _topmost_tick(self) -> None:
        try:
            if not getattr(self, '_topmost_enabled', False):
                return
            wins = list(getattr(self, '_topmost_windows', []) or [])
            for w in wins:
                try:
                    if w and w.winfo_exists():
                        self._apply_topmost_now(w)
                except Exception:
                    pass
        finally:
            self._schedule_topmost_tick()


    def _summary_time_to_sec(self, s: str) -> int:
        try:
            t = str(s or '').strip()
            hh, mm, ss = t.split(':')[:3]
            return int(hh) * 3600 + int(mm) * 60 + int(ss)
        except Exception:
            return -1

    def _sync_summary_time_heading(self) -> None:
        try:
            arrow = '↓' if getattr(self, '_summary_time_sort_desc', True) else '↑'
            self.tree.heading('time', text=f'时间 {arrow}', command=self._toggle_summary_time_sort)
        except Exception:
            pass

    def _resort_summary_tree(self) -> None:
        try:
            kids = list(self.tree.get_children(''))
            ts_map = getattr(self, '_summary_item_ts', None)
            if not isinstance(ts_map, dict):
                ts_map = {}
                self._summary_item_ts = ts_map
            rows = []
            for idx, iid in enumerate(kids):
                vals = self.tree.item(iid, 'values') or ()
                tm = vals[0] if len(vals) > 0 else ''
                ts_real = float(_safe_float(ts_map.get(iid), 0.0) or 0.0)
                # 关键修复：不能只按 HH:MM:SS 排序。跨天后 23:xx 会永远压在 02:xx 前面。
                # 优先按真实 ts 排；只有拿不到 ts 时才回退到显示时间。
                sort_key = ts_real if ts_real > 0 else float(self._summary_time_to_sec(tm))
                rows.append((sort_key, idx, iid))
            rows.sort(key=lambda x: (x[0], x[1]), reverse=bool(getattr(self, '_summary_time_sort_desc', True)))
            for pos, (_sec, _idx, iid) in enumerate(rows):
                self.tree.move(iid, '', pos)
        except Exception:
            pass

    def _dedupe_summary_tree_conflicts(self) -> None:
        """强制清理摘要表内重复。

        1）显示值完全一致：只保留优先级更高 / 时间更新的一条。
        2）同一业务动作（请求/复检弱冲突）：同样只保留更高优先级的一条。

        这样即使重复行是跨刷新轮次残留进来的，也能在表内再次收口。
        """
        try:
            kids = list(self.tree.get_children(''))
            if not kids:
                return
            ts_map = getattr(self, '_summary_item_ts', None)
            if not isinstance(ts_map, dict):
                ts_map = {}
                self._summary_item_ts = ts_map
            pr_map = getattr(self, '_summary_item_priority', None)
            if not isinstance(pr_map, dict):
                pr_map = {}
                self._summary_item_priority = pr_map
            ck_map = getattr(self, '_summary_item_conflict_key', None)
            if not isinstance(ck_map, dict):
                ck_map = {}
                self._summary_item_conflict_key = ck_map

            def _score(iid: str, vals: tuple, idx: int):
                pri = int(_safe_float(pr_map.get(iid), 0) or 0)
                ts_real = float(_safe_float(ts_map.get(iid), 0.0) or 0.0)
                if ts_real <= 0:
                    try:
                        ts_real = float(self._summary_time_to_sec(vals[0] if len(vals) > 0 else ''))
                    except Exception:
                        ts_real = 0.0
                return (pri, ts_real, idx)

            keep_by_vis = {}
            keep_by_conflict = {}
            delete_ids = set()

            for idx, iid in enumerate(kids):
                try:
                    vals = tuple(self.tree.item(iid, 'values') or ())
                except Exception:
                    vals = ()
                if not vals:
                    continue
                vis_sig = self._summary_visible_sig(vals) if hasattr(self, '_summary_visible_sig') else '|'.join(map(str, vals[:8]))
                conflict_sig = str(ck_map.get(iid) or '')
                if not conflict_sig:
                    try:
                        conflict_sig = self._summary_conflict_sig_from_vals(vals) if hasattr(self, '_summary_conflict_sig_from_vals') else ''
                    except Exception:
                        conflict_sig = ''
                    if conflict_sig:
                        ck_map[iid] = conflict_sig
                score = _score(iid, vals, idx)

                old_vis = keep_by_vis.get(vis_sig)
                if old_vis is None or score > old_vis[0]:
                    if old_vis is not None:
                        delete_ids.add(old_vis[1])
                    keep_by_vis[vis_sig] = (score, iid)
                else:
                    delete_ids.add(iid)
                    continue

                if conflict_sig:
                    old_conf = keep_by_conflict.get(conflict_sig)
                    if old_conf is None or score > old_conf[0]:
                        if old_conf is not None:
                            delete_ids.add(old_conf[1])
                        keep_by_conflict[conflict_sig] = (score, iid)
                    else:
                        delete_ids.add(iid)

            for iid in list(delete_ids):
                try:
                    self.tree.delete(iid)
                except Exception:
                    pass
                try:
                    ts_map.pop(iid, None)
                except Exception:
                    pass
                try:
                    pr_map.pop(iid, None)
                except Exception:
                    pass
                try:
                    ck_map.pop(iid, None)
                except Exception:
                    pass
        except Exception:
            pass

    def _toggle_summary_time_sort(self) -> None:
        try:
            self._summary_time_sort_desc = not bool(getattr(self, '_summary_time_sort_desc', True))
            self._sync_summary_time_heading()
            self._resort_summary_tree()
            self._dedupe_summary_tree_conflicts()
        except Exception:
            pass

    def _build_shell(self) -> None:
        """在主界面外包一层“边框跑马灯”。

        - 工作：淡红色边框 + 亮段沿边缘跑动
        - 睡眠：淡绿色边框 + 亮段沿边缘跑动

        只改外壳，不动内部主界面布局。
        """
        self._edge_thick = 18
        self._edge_tick_ms = 120
        self._edge_seg = 270  # 亮段长度（放大3倍）
        self._edge_pos = 0
        self._edge_job = None
        self._edge_mode = "off"  # off/work/sleep
        self._edge_dim = "#111214"
        self._edge_bright = "#111214"

        self._shell = tk.Frame(self.root, bg="#111214", highlightthickness=0, bd=0)
        self._shell.pack(fill="both", expand=True)

        # 四边画布（淡色底）
        self._edge_top = tk.Canvas(self._shell, height=self._edge_thick, bg=self._edge_dim, highlightthickness=0, bd=0)
        self._edge_bottom = tk.Canvas(self._shell, height=self._edge_thick, bg=self._edge_dim, highlightthickness=0, bd=0)
        self._edge_left = tk.Canvas(self._shell, width=self._edge_thick, bg=self._edge_dim, highlightthickness=0, bd=0)
        self._edge_right = tk.Canvas(self._shell, width=self._edge_thick, bg=self._edge_dim, highlightthickness=0, bd=0)

        self._body = tk.Frame(self._shell, bg="#111214", highlightthickness=0, bd=0)

        self._shell.grid_rowconfigure(1, weight=1)
        self._shell.grid_columnconfigure(1, weight=1)

        self._edge_top.grid(row=0, column=0, columnspan=3, sticky="nsew")
        self._edge_left.grid(row=1, column=0, sticky="nsew")
        self._body.grid(row=1, column=1, sticky="nsew")
        self._edge_right.grid(row=1, column=2, sticky="nsew")
        self._edge_bottom.grid(row=2, column=0, columnspan=3, sticky="nsew")

        # 允许窗口 resize 时边框跟随
        try:
            self.root.bind("<Configure>", lambda _e: self._edge_redraw())
        except Exception:
            pass

    def _edge_set_mode(self, mode_txt: str) -> None:
        """根据工作/睡眠更新边框跑马灯颜色并启动动画。

        规则：
        - work/工作 -> 绿色边框跑马灯
        - sleep/睡眠 -> 黄色边框跑马灯
        - 识别不到 -> 默认 work（避免边框熄灭）
        """
        raw = (mode_txt or "").strip()
        s = raw.lower()
        if ("sleep" in s) or ("睡" in raw):
            new_mode = "sleep"
        elif ("work" in s) or ("工" in raw):
            new_mode = "work"
        else:
            new_mode = "work"

        if new_mode == getattr(self, "_edge_mode", "off"):
            return

        self._edge_mode = new_mode

        # work=绿，sleep=黄（风险异常红在 _edge_set_risk 里覆盖）
        if new_mode == "work":
            self._edge_dim = "#17311F"      # 暗绿底
            self._edge_bright = "#2FE06B"   # 亮绿段
        else:
            self._edge_dim = "#2F2A12"      # 暗黄底
            self._edge_bright = "#E6C94A"   # 亮黄段

        for c in (self._edge_top, self._edge_bottom, self._edge_left, self._edge_right):
            try:
                c.configure(bg=self._edge_dim)
            except Exception:
                pass

        if getattr(self, "_edge_risk_mode", "normal") != "abnormal":
            self._edge_start()

    def _edge_set_risk(self, risk_txt: str) -> None:
        """根据风险状态更新边框跑马灯颜色并启动动画。

        规则：
        - 异常/高危/错误/失败 -> 红色跑马灯（优先级最高）
        - 正常/空 -> 不覆盖模式色（work=绿，sleep=黄）
        """
        raw = (risk_txt or "").strip()
        s = raw.lower()

        abnormal = False
        if raw:
            if ("异常" in raw) or ("高危" in raw) or ("错误" in raw) or ("失败" in raw):
                abnormal = True
            if ("error" in s) or ("fail" in s) or ("abnormal" in s) or ("red" in s):
                abnormal = True

        new_mode = "abnormal" if abnormal else "normal"
        if new_mode == getattr(self, "_edge_risk_mode", "normal"):
            return
        self._edge_risk_mode = new_mode

        if new_mode == "abnormal":
            self._edge_dim = "#311717"
            self._edge_bright = "#FF4D4D"
            for c in (self._edge_top, self._edge_bottom, self._edge_left, self._edge_right):
                try:
                    c.configure(bg=self._edge_dim)
                except Exception:
                    pass
            self._edge_start()
        else:
            # 回到模式色
            self._edge_set_mode(self.var_mode_txt.get() if hasattr(self, "var_mode_txt") else "")

    def _edge_start(self) -> None:
        if self._edge_job is not None:
            return
        self._edge_pos = 0
        self._edge_job = self.root.after(self._edge_tick_ms, self._edge_tick)

    def _edge_stop(self) -> None:
        if self._edge_job is not None:
            try:
                self.root.after_cancel(self._edge_job)
            except Exception:
                pass
        self._edge_job = None

    def _edge_redraw(self) -> None:
        """重绘当前亮段（不推进位置）。"""
        try:
            self._edge_draw(self._edge_pos)
        except Exception:
            pass

    def _edge_tick(self) -> None:
        try:
            # 每 tick 推进一点，形成 360° 跑马灯
            self._edge_pos += 18
            self._edge_draw(self._edge_pos)
        except Exception:
            pass
        self._edge_job = self.root.after(self._edge_tick_ms, self._edge_tick)

    def _edge_draw(self, pos: int) -> None:
        """在四边画布上绘制一段亮色矩形沿边缘移动。"""
        if getattr(self, "_edge_mode", "off") == "off":
            for c in (self._edge_top, self._edge_bottom, self._edge_left, self._edge_right):
                try:
                    c.delete("all")
                except Exception:
                    pass
            return

        w = max(1, int(self._body.winfo_width()))
        h = max(1, int(self._body.winfo_height()))
        seg = int(getattr(self, '_edge_seg', 270))  # 亮段长度
        L = 2 * (w + h)
        if L <= 0:
            return
        p = int(pos) % L

        # 清除旧亮段
        for c in (self._edge_top, self._edge_bottom, self._edge_left, self._edge_right):
            try:
                c.delete("all")
            except Exception:
                pass

        # Top: [0, w)
        if p < w:
            x0 = p
            x1 = min(w, p + seg)
            self._edge_top.create_rectangle(x0, 0, x1, self._edge_thick, fill=self._edge_bright, outline="")
            return

        # Right: [w, w+h)
        p2 = p - w
        if p2 < h:
            y0 = p2
            y1 = min(h, p2 + seg)
            self._edge_right.create_rectangle(0, y0, self._edge_thick, y1, fill=self._edge_bright, outline="")
            return

        # Bottom: [w+h, w+h+w)
        p3 = p2 - h
        if p3 < w:
            x1 = w - p3
            x0 = max(0, x1 - seg)
            self._edge_bottom.create_rectangle(x0, 0, x1, self._edge_thick, fill=self._edge_bright, outline="")
            return

        # Left: [w+h+w, L)
        p4 = p3 - w
        y1 = h - p4
        y0 = max(0, y1 - seg)
        self._edge_left.create_rectangle(0, y0, self._edge_thick, y1, fill=self._edge_bright, outline="")
        return

    # ------------------------------
    # 小挂件（mini（小窗））边框跑马灯（与主面板一致：工作淡红/睡眠淡绿）
    # ------------------------------
    def _mini_edge_init(self) -> None:
        self._mini_edge_thick = int(getattr(self, "_edge_thick", 18))
        self._mini_edge_seg = int(getattr(self, "_edge_seg", 270))
        self._mini_edge_tick_ms = int(getattr(self, "_edge_tick_ms", 120))
        self._mini_edge_pos = 0
        self._mini_edge_job = None
        self._mini_edge_mode = "off"
        self._mini_edge_dim = "#111214"
        self._mini_edge_bright = "#111214"

    def _mini_edge_set_mode(self, mode_txt: str) -> None:
        if not (self.mini_win and self.mini_win.winfo_exists()):
            return
        raw = (mode_txt or "").strip()
        s = raw.lower()
        if ("sleep" in s) or ("睡" in raw):
            new_mode = "sleep"
        elif ("work" in s) or ("工" in raw):
            new_mode = "work"
        else:
            new_mode = "work"

        if new_mode == getattr(self, "_mini_edge_mode", "off"):
            return
        self._mini_edge_mode = new_mode

        if new_mode == "work":
            self._mini_edge_dim = "#17311F"
            self._mini_edge_bright = "#2FE06B"
        else:
            self._mini_edge_dim = "#2F2A12"
            self._mini_edge_bright = "#E6C94A"

        for c in (self._mini_edge_top, self._mini_edge_bottom, self._mini_edge_left, self._mini_edge_right):
            try:
                c.configure(bg=self._mini_edge_dim)
            except Exception:
                pass

        if getattr(self, "_mini_edge_risk_mode", "normal") != "abnormal":
            self._mini_edge_start()

    def _mini_edge_set_risk(self, risk_txt: str) -> None:
        if not (self.mini_win and self.mini_win.winfo_exists()):
            return
        raw = (risk_txt or "").strip()
        s = raw.lower()

        abnormal = False
        if raw:
            if ("异常" in raw) or ("高危" in raw) or ("错误" in raw) or ("失败" in raw):
                abnormal = True
            if ("error" in s) or ("fail" in s) or ("abnormal" in s) or ("red" in s):
                abnormal = True

        new_mode = "abnormal" if abnormal else "normal"
        if new_mode == getattr(self, "_mini_edge_risk_mode", "normal"):
            return
        self._mini_edge_risk_mode = new_mode

        if new_mode == "abnormal":
            self._mini_edge_dim = "#311717"
            self._mini_edge_bright = "#FF4D4D"
            for c in (self._mini_edge_top, self._mini_edge_bottom, self._mini_edge_left, self._mini_edge_right):
                try:
                    c.configure(bg=self._mini_edge_dim)
                except Exception:
                    pass
            self._mini_edge_start()
        else:
            self._mini_edge_set_mode(self.var_mode_txt.get() if hasattr(self, "var_mode_txt") else "")

    def _mini_edge_start(self) -> None:
        self._mini_edge_stop()
        self._mini_edge_pos = 0
        self._mini_edge_tick()

    def _mini_edge_stop(self) -> None:
        try:
            if self._mini_edge_job is not None and self.mini_win and self.mini_win.winfo_exists():
                self.mini_win.after_cancel(self._mini_edge_job)
        except Exception:
            pass
        self._mini_edge_job = None

    def _mini_edge_redraw(self) -> None:
        if not (self.mini_win and self.mini_win.winfo_exists()):
            return
        self._mini_edge_draw(self._mini_edge_pos)

    def _mini_edge_tick(self) -> None:
        if not (self.mini_win and self.mini_win.winfo_exists()):
            return
        self._mini_edge_draw(self._mini_edge_pos)
        # 每 tick 前进一小段
        self._mini_edge_pos = int(self._mini_edge_pos + 18)  # 大边框配合更显眼
        self._mini_edge_job = self.mini_win.after(self._mini_edge_tick_ms, self._mini_edge_tick)

    def _mini_edge_draw(self, pos: int) -> None:
        if not (self.mini_win and self.mini_win.winfo_exists()):
            return
        if getattr(self, "_mini_edge_mode", "off") == "off":
            for c in (self._mini_edge_top, self._mini_edge_bottom, self._mini_edge_left, self._mini_edge_right):
                try:
                    c.delete("all")
                except Exception:
                    pass
            return

        try:
            w = max(1, int(self._mini_body.winfo_width()))
            h = max(1, int(self._mini_body.winfo_height()))
        except Exception:
            w, h = 1, 1

        seg = int(getattr(self, "_mini_edge_seg", 270))
        L = 2 * (w + h)
        if L <= 0:
            return
        p = int(pos % L)

        for c in (self._mini_edge_top, self._mini_edge_bottom, self._mini_edge_left, self._mini_edge_right):
            try:
                c.delete("all")
            except Exception:
                pass

        # Top: [0, w)
        if p < w:
            x1 = p
            x0 = max(0, x1 - seg)
            self._mini_edge_top.create_rectangle(x0, 0, x1, self._mini_edge_thick, fill=self._mini_edge_bright, outline="")
            return

        # Right: [w, w+h)
        p2 = p - w
        if p2 < h:
            y1 = p2
            y0 = max(0, y1 - seg)
            self._mini_edge_right.create_rectangle(0, y0, self._mini_edge_thick, y1, fill=self._mini_edge_bright, outline="")
            return

        # Bottom: [w+h, w+h+w)
        p3 = p2 - h
        if p3 < w:
            x1 = w - p3
            x0 = max(0, x1 - seg)
            self._mini_edge_bottom.create_rectangle(x0, 0, x1, self._mini_edge_thick, fill=self._mini_edge_bright, outline="")
            return

        # Left: [w+h+w, L)
        p4 = p3 - w
        y1 = h - p4
        y0 = max(0, y1 - seg)
        self._mini_edge_left.create_rectangle(0, y0, self._mini_edge_thick, y1, fill=self._mini_edge_bright, outline="")
        return


    def _card(self, parent: tk.Widget, title: str) -> Tuple[ttk.Frame, tk.StringVar, tk.StringVar]:
        fr = ttk.Frame(parent, style="Card.TFrame")
        ttk.Label(fr, text=title, style="CardTitle.TLabel").pack(anchor="w", padx=12, pady=(10, 0))
        v1 = tk.StringVar(value="-")
        v2 = tk.StringVar(value="")
        ttk.Label(fr, textvariable=v1, style="CardValue.TLabel", justify="left", wraplength=220).pack(anchor="w", padx=12, pady=(2, 0))
        ttk.Label(fr, textvariable=v2, style="Hint.TLabel", justify="left", wraplength=220).pack(anchor="w", padx=12, pady=(2, 10))
        return fr, v1, v2

    def _card_triplet(self, parent: tk.Widget, title: str) -> Tuple[ttk.Frame, tk.StringVar, tk.StringVar, tk.StringVar]:
        fr = ttk.Frame(parent, style="Card.TFrame")
        ttk.Label(fr, text=title, style="CardTitle.TLabel").pack(anchor="w", padx=12, pady=(10, 0))
        v1 = tk.StringVar(value="-")
        v2 = tk.StringVar(value="")
        v3 = tk.StringVar(value="")
        ttk.Label(fr, textvariable=v1, style="CardValue.TLabel", justify="left", wraplength=220).pack(anchor="w", padx=12, pady=(2, 0))
        ttk.Label(fr, textvariable=v2, style="Hint.TLabel", justify="left", wraplength=220).pack(anchor="w", padx=12, pady=(3, 0))
        ttk.Label(fr, textvariable=v3, style="Hint.TLabel", justify="left", wraplength=220).pack(anchor="w", padx=12, pady=(1, 10))
        return fr, v1, v2, v3

    def _card_ls_overview(self, parent: tk.Widget) -> ttk.Frame:
        fr = ttk.Frame(parent, style="Card.TFrame")
        ttk.Label(fr, text="多空概览", style="CardTitle.TLabel").pack(anchor="w", padx=12, pady=(10, 0))

        self.var_ls_overview = tk.StringVar(value="-")
        self.var_ls_overview_bias = tk.StringVar(value="")
        self.var_ls_overview_sub1 = tk.StringVar(value="")
        self.var_ls_overview_sub2 = tk.StringVar(value="")

        self.lbl_ls_overview = ttk.Label(fr, textvariable=self.var_ls_overview, style="NeutralCardValue.TLabel", justify="left", wraplength=220)
        self.lbl_ls_overview.pack(anchor="w", padx=12, pady=(2, 0))
        self.lbl_ls_overview_bias = ttk.Label(fr, textvariable=self.var_ls_overview_bias, style="NeutralSubValue.TLabel", justify="left", wraplength=220)
        self.lbl_ls_overview_bias.pack(anchor="w", padx=12, pady=(2, 0))
        self.lbl_ls_overview_sub1 = ttk.Label(fr, textvariable=self.var_ls_overview_sub1, style="Hint.TLabel", justify="left", wraplength=220)
        self.lbl_ls_overview_sub1.pack(anchor="w", padx=12, pady=(2, 0))
        self.lbl_ls_overview_sub2 = ttk.Label(fr, textvariable=self.var_ls_overview_sub2, style="Hint.TLabel", justify="left", wraplength=220)
        self.lbl_ls_overview_sub2.pack(anchor="w", padx=12, pady=(1, 10))
        return fr

    def _apply_main_pane_ratio(self) -> None:
        try:
            pw = getattr(self, "_main_pane", None)
            if not pw:
                return
            total = int(pw.winfo_width() or self.root.winfo_width() or 1400)
            if total <= 200:
                self.root.after(120, self._apply_main_pane_ratio)
                return
            left_w = max(460, min(int(total * 0.43), total - 680))
            pw.sashpos(0, left_w)
        except Exception:
            pass

    def _summary_visible_sig(self, vals: tuple) -> str:
        try:
            time_s = str(vals[0] or '--')[:8]
            coin = str(vals[1] or '').upper()
            plat = str(vals[2] or '').upper()
            module_action = str(vals[3] or '')
            action = str(vals[4] or '')
            dir_cn = str(vals[5] or '')
            amt = f"{float(_safe_float(vals[6], 0.0) or 0.0):.2f}"
            price = str(vals[7] or '-')
            return f"{time_s}|{coin}|{plat}|{module_action}|{action}|{dir_cn}|{amt}|{price}"
        except Exception:
            return '|'.join(str(x) for x in vals[:8])

    def _build_existing_summary_visible_sigs(self) -> set:
        out = set()
        try:
            for iid in self.tree.get_children(""):
                vals = tuple(self.tree.item(iid, 'values') or ())
                if vals:
                    out.add(self._summary_visible_sig(vals))
        except Exception:
            pass
        return out

    def _build_existing_summary_visible_map(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        try:
            for iid in self.tree.get_children(""):
                vals = tuple(self.tree.item(iid, 'values') or ())
                if not vals:
                    continue
                vis_sig = self._summary_visible_sig(vals)
                if vis_sig and vis_sig not in out:
                    out[vis_sig] = str(iid)
        except Exception:
            pass
        return out

    def _summary_conflict_sig_from_vals(self, vals: tuple) -> str:
        """摘要弱冲突签名：用于“请求行/复检行”二选一。

        口径比 _summary_visible_sig 更宽：
        - 保留 时间(秒) / 币种 / 平台 / 模块 / 动作 / 方向
        - 故意忽略 金额/价格

        这样当同一次动作先写出请求行、后写出复检/真成交行，
        即使金额或价格被后链修正，也会被识别为同一业务动作，
        从而只保留优先级更高的那条（通常是复检/真成交）。
        """
        try:
            if not vals:
                return ''
            time_s = str(vals[0] or '--')[:8]
            coin = str(vals[1] or '').upper().strip()
            plat = str(vals[2] or '').upper().strip()
            module_action = str(vals[3] or '').strip()
            action = str(vals[4] or '').strip()
            dir_cn = str(vals[5] or '').strip()
            if not (time_s and coin and plat and module_action and action and dir_cn):
                return ''
            return f"{time_s}|{coin}|{plat}|{module_action}|{action}|{dir_cn}"
        except Exception:
            return ''

    def _build_existing_summary_conflict_map(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        try:
            conflict_map = getattr(self, '_summary_item_conflict_key', None)
            if not isinstance(conflict_map, dict):
                conflict_map = {}
                self._summary_item_conflict_key = conflict_map
            for iid in self.tree.get_children(""):
                vals = tuple(self.tree.item(iid, 'values') or ())
                if not vals:
                    continue
                sig = str(conflict_map.get(iid) or '')
                if not sig:
                    sig = self._summary_conflict_sig_from_vals(vals)
                    if sig:
                        conflict_map[iid] = sig
                if sig and sig not in out:
                    out[sig] = str(iid)
        except Exception:
            pass
        return out

    def _summary_conflict_sig_from_row(self, r: dict, vals: Optional[tuple] = None) -> str:
        try:
            if vals:
                sig = self._summary_conflict_sig_from_vals(vals)
                if sig:
                    return sig
            if not isinstance(r, dict):
                return ''
            ts0 = float(_safe_float(r.get('ts'), 0.0) or 0.0)
            time_s = str(r.get('time') or '')[:8]
            if (not time_s) or (time_s in ('--', '-', '')):
                if ts0 > 0:
                    try:
                        time_s = datetime.fromtimestamp(ts0).strftime('%H:%M:%S')
                    except Exception:
                        time_s = '--'
            coin = str(r.get('coin') or '').upper().strip()
            plat = str(r.get('plat') or r.get('platform') or r.get('platf') or '').upper().strip()
            module_action = str(r.get('module') or '-').strip() or '-'
            action = str(r.get('action') or r.get('action_type') or '-').strip() or '-'
            dir0 = str(r.get('dir') or r.get('side') or r.get('pos_side') or '').strip().lower()
            dir_cn = '多' if dir0 in ('long', 'buy', '多') else ('空' if dir0 in ('short', 'sell', '空') else '')
            if not (time_s and coin and plat and module_action and action and dir_cn):
                return ''
            return f"{time_s}|{coin}|{plat}|{module_action}|{action}|{dir_cn}"
        except Exception:
            return ''

    def _summary_row_priority(self, r: dict) -> int:
        """摘要同屏冲突时的保留优先级：复检/真成交 > 普通结构化 > 请求/尝试。"""
        try:
            if not isinstance(r, dict):
                return 0
            score = 0
            src = str(r.get('source') or '').strip().lower()
            status = str(r.get('status') or '').strip().upper()
            text_blob = ' | '.join(
                str(r.get(k) or '')
                for k in ('source', 'status', 'event', 'type', 'raw', 'msg', 'message', 'reason', 'stage', 'phase')
            ).lower()

            if status == 'ACCEPT':
                score -= 300

            request_like_tokens = (
                'accept', 'request', 'submit', 'queue', 'pending', 'enqueue', 'intent', 'prepare',
                '尝试', '请求', '排队', '入队', '计划',
            )
            confirm_like_tokens = (
                'postcheck', '复检', 'exec_done', 'executor_done', 'exec_ok', 'executed',
                'filled', 'order_filled', 'trade_filled', '成交', '执行成功', '下单成功',
            )

            if src.startswith('structured'):
                score += 120
            elif src in ('log_full', 'text', 'parsed_log'):
                score += 20

            if any(tok in text_blob for tok in request_like_tokens):
                score -= 120
            if any(tok in text_blob for tok in confirm_like_tokens):
                score += 140

            if any(str(r.get(k) or '').strip() for k in ('trade_id', 'fill_id', 'order_id', 'exchange_order_id')):
                score += 120
            if str(r.get('req_id') or '').strip():
                score += 10
            if _safe_float(r.get('profit_u'), None) is not None:
                score += 12
            if _safe_float(r.get('price'), None) is not None:
                score += 6
            if _safe_float(r.get('rsi'), None) is not None:
                score += 2
            if _safe_float(r.get('adx'), None) is not None:
                score += 2
            return int(score)
        except Exception:
            return 0

    def _build_layout(self) -> None:
        rootp = getattr(self, "_body", self.root)

        # 顶部横幅：仅保留风险（去掉：模式、形态、队首、下一条）
        banner = ttk.Frame(rootp)
        banner.pack(fill="x", padx=12, pady=(10, 6))

        self.var_banner1_right = tk.StringVar(value="风险状态：-")

        row1 = ttk.Frame(banner)
        row1.pack(fill="x", pady=(0, 4))
        self.lbl_risk = ttk.Label(row1, textvariable=self.var_banner1_right, style="Banner.TLabel")
        self.lbl_risk.pack(side="left", fill="x", expand=True)


        # 顶部卡片行

        top = ttk.Frame(rootp)
        top.pack(fill="x", padx=12, pady=8)
        c1, self.var_total, self.var_total_sub = self._card(top, "总资金")
        c2, self.var_anchor, self.var_anchor_sub = self._card(top, "ANCHOR")
        c3, self.var_hedge, self.var_hedge_sub = self._card(top, "对冲健康")
        c4 = self._card_ls_overview(top)

        c5 = ttk.Frame(top, style="Card.TFrame")
        ttk.Label(c5, text="运行状态", style="CardTitle.TLabel").pack(anchor="w", padx=12, pady=(10, 0))
        self.var_runtime = tk.StringVar(value="运行：-")
        self.var_runtime_scan = tk.StringVar(value="扫描次数：-")
        self.var_runtime_sub = tk.StringVar(value="历史：-")
        ttk.Label(c5, textvariable=self.var_runtime, style="CardValue.TLabel", justify="left", wraplength=220).pack(anchor="w", padx=12, pady=(2, 0))
        ttk.Label(c5, textvariable=self.var_runtime_scan, style="SubValue.TLabel", justify="left", wraplength=220).pack(anchor="w", padx=12, pady=(1, 0))
        ttk.Label(c5, textvariable=self.var_runtime_sub, style="Hint.TLabel", justify="left", wraplength=220).pack(anchor="w", padx=12, pady=(1, 8))

        c6 = ttk.Frame(top, style="Card.TFrame")
        ttk.Label(c6, text="状态灯 / 盈利", style="CardTitle.TLabel").pack(anchor="w", padx=12, pady=(10, 6))
        row = ttk.Frame(c6, style="Card.TFrame")
        row.pack(fill="x", padx=12, pady=(0, 10))
        # 卡片内改成上下排列：上面状态灯，下面盈利，避免字显示不全
        lights_box = ttk.Frame(row, style="Card.TFrame")
        lights_box.pack(fill="x", anchor="w", pady=(0, 6))
        self.canvas_lights = tk.Canvas(lights_box, width=150, height=44, bg="#17181B", highlightthickness=0)
        self.canvas_lights.pack(anchor="w")
        self._set_lights_mode("stopped")

        profit_box = ttk.Frame(row, style="Card.TFrame")
        profit_box.pack(fill="x", anchor="w")
        self.var_profit_today = tk.StringVar(value="今日权益变动：-")
        self.var_profit_hist = tk.StringVar(value="历史权益变动：-")
        ttk.Label(profit_box, textvariable=self.var_profit_today, style="SubValue.TLabel", justify="left", wraplength=220).pack(anchor="w", pady=(2, 0))
        ttk.Label(profit_box, textvariable=self.var_profit_hist, style="SubValue.TLabel", justify="left", wraplength=220).pack(anchor="w", pady=(2, 0))
        ttk.Label(profit_box, text=_panel_state.EQUITY_CHANGE_BASIS, style="SubValue.TLabel", justify="left", wraplength=220).pack(anchor="w", pady=(2, 0))

        for i, card in enumerate([c1, c2, c3, c4, c5, c6]):
            card.grid(row=0, column=i, padx=8, sticky="nsew")
            top.grid_columnconfigure(i, weight=1)

        # 按钮行 + 右侧横向信息条
        mid = ttk.Frame(rootp)
        mid.pack(fill="x", padx=12, pady=(0, 10))

        btns = ttk.Frame(mid)
        btns.pack(side="left", fill="x", expand=True)
        self.btn_topmost = ttk.Button(btns, textvariable=self._topmost_btn_var, style="Btn.TButton", command=self.on_toggle_topmost)
        self.btn_topmost.pack(side="left", padx=6)
        ttk.Button(btns, text="开始", style="Btn.TButton", command=self.on_start).pack(side="left", padx=6)
        ttk.Button(btns, text="关闭", style="Btn.TButton", command=self.on_close).pack(side="left", padx=6)
        ttk.Button(btns, text="选择Bot", style="Btn.TButton", command=self.on_choose_bot).pack(side="left", padx=6)

        ttk.Button(btns, text="缩小挂件", style="Btn.TButton", command=self.on_toggle_mini).pack(side="left", padx=6)

        tk.Button(btns, text="输出日报", command=self.on_generate_daily_report, bg="#d9534f", fg="white", activebackground="#c9302c", activeforeground="white", relief="raised", bd=1, padx=10, pady=4).pack(side="left", padx=6)
        info = ttk.Frame(mid, style="Card.TFrame")
        info.pack(side="right")
        self.var_info = tk.StringVar(value="环境：- | 交易额：- | 扫描：-")
        ttk.Label(info, textvariable=self.var_info, style="Hint.TLabel").pack(anchor="e", padx=10, pady=8)
        self.var_browser_recovery_alert = tk.StringVar(value='')
        self.var_funds_imbalance_alert = tk.StringVar(value='')
        ttk.Label(rootp, textvariable=self.var_browser_recovery_alert, foreground='#FFB454',
                  font=('Microsoft YaHei UI', 11, 'bold'), wraplength=1250,
                  justify='left').pack(fill='x', padx=12, pady=(0, 4))
        ttk.Label(rootp, textvariable=self.var_funds_imbalance_alert, foreground='#FFB454',
                  font=('Microsoft YaHei UI', 11, 'bold'), wraplength=1250,
                  justify='left').pack(fill='x', padx=12, pady=(0, 4))

        # 主体区域：左侧全量日志，右侧总览/网格。
        # 改成可调分栏，并默认给右侧更多空间，避免“左日志太宽把右边挤没”。
        main = ttk.Panedwindow(rootp, orient=tk.HORIZONTAL)
        main.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        left = ttk.Frame(main, style="Card.TFrame")
        right = ttk.Frame(main, style="Card.TFrame")
        main.add(left, weight=3)
        main.add(right, weight=5)
        self._main_pane = main
        # 右侧插页：总览（保持原布局） + 网格（新增）
        self.nb_right = ttk.Notebook(right)
        self.tab_overview = ttk.Frame(self.nb_right)
        self.tab_grid = ttk.Frame(self.nb_right)
        self.tab_grid_book = ttk.Frame(self.nb_right)
        self.tab_trend_observe = ttk.Frame(self.nb_right)
        self.tab_selection_observe = ttk.Frame(self.nb_right)
        self.nb_right.add(self.tab_overview, text="总览")
        self.nb_right.add(self.tab_grid, text="网格")
        self.nb_right.add(self.tab_grid_book, text="网格账本")
        self.nb_right.add(self.tab_trend_observe, text="趋势观察")
        self.nb_right.add(self.tab_selection_observe, text="选币排名")
        self.nb_right.pack(fill="both", expand=True, padx=0, pady=0)
        try:
            self.root.after(120, self._apply_main_pane_ratio)
        except Exception:
            pass

        # 兼容旧代码：后续所有右侧组件继续使用变量 right 作为父容器
        right = self.tab_overview
        # 左：全量日志（占用剩余空间）
        log_box = ttk.Frame(left, style="Card.TFrame")
        log_box.pack(fill="both", expand=True, padx=10, pady=(10, 6))
        ttk.Label(log_box, text="全量日志", style="CardTitle.TLabel").pack(anchor="w", padx=10, pady=(8, 2))
        self.var_log_meta = tk.StringVar(value="来源：等待日志...")
        ttk.Label(log_box, textvariable=self.var_log_meta, style="Hint.TLabel", wraplength=560, justify="left").pack(anchor="w", padx=10, pady=(0, 4))

        # 过滤/汉化（human）工具条
        bar = ttk.Frame(log_box, style="Card.TFrame")
        bar.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(bar, text="视图", style="Hint.TLabel").pack(side="left")
        self.var_log_mode = tk.StringVar(value="human")
        cmb = ttk.Combobox(bar, textvariable=self.var_log_mode, values=["focus", "all", "human", "errors", "actions", "m1", "scan"], width=10, state="readonly")
        cmb.pack(side="left", padx=(6, 12))
        ttk.Label(bar, text="关键词", style="Hint.TLabel").pack(side="left")
        self.var_log_kw = tk.StringVar(value="")
        ent = ttk.Entry(bar, textvariable=self.var_log_kw, width=24)
        ent.pack(side="left", padx=(6, 0))
        self.var_log_digest = tk.StringVar(value="")
        ttk.Label(log_box, textvariable=self.var_log_digest, style="Hint.TLabel").pack(anchor="w", padx=10, pady=(0, 6))
        self.txt_full = tk.Text(
            log_box, height=14,
            bg="#0E0F12", fg="#E6E6E6",
            insertbackground="#E6E6E6",
            relief="flat", wrap="char",
            font=("Consolas", 9)
        )
        self._log_manual_scroll_until = 0.0
        self._log_manual_scroll_secs = 180.0
        self._log_scroll_debounce_ms = 180
        self._log_restore_job = None
        self._log_last_render_ts = 0.0
        self._log_auto_follow_eps = 0.003

        # 全量日志高亮：
        # 1) 开仓/加仓/补仓/B1 首开 → 红色加粗
        # 2) 平仓/减仓/双平/M10 减亏 → 绿色加粗
        # 3) B2/M10/M9/B1 相关但未直接判断开平 → 黄色加粗
        try:
            import tkinter.font as tkfont
            self._font_log_bold = tkfont.Font(font=self.txt_full["font"])
            self._font_log_bold.configure(weight="bold")
            self.txt_full.tag_configure("tag_red_bold", foreground="#E74C3C", font=self._font_log_bold)
            self.txt_full.tag_configure("tag_green_bold", foreground="#2ECC71", font=self._font_log_bold)
            self.txt_full.tag_configure("tag_yellow_bold", foreground="#F1C40F", font=self._font_log_bold)
            self.txt_full.tag_configure("tag_blue_bold", foreground="#5DADE2", font=self._font_log_bold)
            self.txt_full.tag_configure("tag_gray", foreground="#8E9AA7")
        except Exception:
            pass

        def _log_clear_manual_scroll() -> None:
            try:
                self._log_manual_scroll_until = 0.0
                if getattr(self, "_log_restore_job", None):
                    try:
                        self.after_cancel(self._log_restore_job)
                    except Exception:
                        pass
                self._log_restore_job = None
            except Exception:
                pass

        def _log_mark_manual_scroll(*_args):
            try:
                import time as _time
                self._log_manual_scroll_until = _time.time() + float(getattr(self, "_log_manual_scroll_secs", 180.0))
                if getattr(self, "_log_restore_job", None):
                    try:
                        self.after_cancel(self._log_restore_job)
                    except Exception:
                        pass
                delay = int(float(getattr(self, "_log_manual_scroll_secs", 180.0)) * 1000)
                self._log_restore_job = self.after(delay, _log_clear_manual_scroll)
            except Exception:
                pass

        def _log_is_near_bottom() -> bool:
            try:
                _y0, _y1 = self.txt_full.yview()
                return float(_y1) >= (0.999 - float(getattr(self, "_log_auto_follow_eps", 0.003) or 0.003))
            except Exception:
                return True

        def _log_sync_follow_state() -> None:
            try:
                if _log_is_near_bottom():
                    _log_clear_manual_scroll()
                else:
                    _log_mark_manual_scroll()
            except Exception:
                pass

        def _yview_with_manual(*args):
            try:
                ret = self.txt_full.yview(*args)
            except Exception:
                ret = None
            try:
                self.root.after_idle(_log_sync_follow_state)
            except Exception:
                _log_sync_follow_state()
            return ret

        def _xview_with_manual(*args):
            try:
                ret = self.txt_full.xview(*args)
            except Exception:
                ret = None
            try:
                self.root.after_idle(_log_sync_follow_state)
            except Exception:
                _log_sync_follow_state()
            return ret

        scy = ttk.Scrollbar(log_box, orient="vertical", command=_yview_with_manual)
        scx = ttk.Scrollbar(log_box, orient="horizontal", command=_xview_with_manual)
        self.txt_full.configure(yscrollcommand=scy.set, xscrollcommand=scx.set)
        self.txt_full.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=(0, 10))
        scy.pack(side="right", fill="y", padx=(0, 10), pady=(0, 10))
        # 全量日志改为自动换行后，不再需要横向滚动条。
        # 保留对象但不显示，避免其它旧引用失效。
        try:
            scx.pack_forget()
        except Exception:
            pass

        # 去掉全量日志右下角滚动条交汇处的小方块（只遮角，不动其它布局）
        try:
            _sb_w = int(scy.winfo_reqwidth() or 16)
        except Exception:
            _sb_w = 16
        try:
            _sb_h = int(scx.winfo_reqheight() or 16)
        except Exception:
            _sb_h = 16
        try:
            _corner_bg = self.txt_full.cget("background")
        except Exception:
            _corner_bg = "#0b0b0c"
        self._log_scroll_corner_mask = tk.Frame(log_box, bd=0, highlightthickness=0, bg=_corner_bg)
        self._log_scroll_corner_mask.place(relx=1.0, rely=1.0, x=-10, y=-10, anchor="se", width=_sb_w, height=_sb_h)
        for _ev in ("<MouseWheel>", "<Button-4>", "<Button-5>", "<Shift-MouseWheel>", "<ButtonPress-1>", "<B1-Motion>", "<Key-Prior>", "<Key-Next>", "<Home>", "<End>"):
            try:
                self.txt_full.bind(_ev, lambda _e: (self.root.after_idle(_log_sync_follow_state), None)[1], add="+")
            except Exception:
                pass

        # 右：上半摘要、下半 多空比（为保证 8 币全可见，给下半区固定最小高度，避免被挤压）
        top_r = ttk.Frame(right, style="Card.TFrame")
        bot_r = ttk.Frame(right, style="Card.TFrame", height=360)
        top_r.pack(fill="both", expand=True, padx=(10, 10), pady=(10, 2))
        bot_r.pack(fill="x", expand=False, padx=(10, 10), pady=(0, 8))
        try:
            bot_r.pack_propagate(False)
        except Exception:
            pass

        # 摘要区标题行："历史真实成交摘要" + 今日 EXT/VAR 交易额（放在标题后面，且明确这不是本轮统计）
        # 关键调整：把标题行和表体都放进同一层内边距容器，
        # 这样“摘要”左侧位置不变，同时右侧边界与下方多空比列表完全一致。
        self.var_trade_by_plat = tk.StringVar(value="")
        self.var_summary_hint = tk.StringVar(value="（本轮统计看左侧日志/状态）")
        sum_wrap = ttk.Frame(top_r, style="Card.TFrame")
        sum_wrap.pack(fill="both", expand=True, padx=(10, 10), pady=(8, 10))

        hdr_sum = ttk.Frame(sum_wrap, style="Card.TFrame")
        hdr_sum.pack(fill="x", padx=0, pady=(0, 4))
        ttk.Label(hdr_sum, text="历史真实成交摘要", style="CardTitle.TLabel").pack(side="left")
        ttk.Label(hdr_sum, textvariable=self.var_trade_by_plat, style="Hint.TLabel").pack(side="left", padx=(12, 0))
        ttk.Label(hdr_sum, textvariable=self.var_summary_hint, style="Hint.TLabel").pack(side="left", padx=(12, 0))

        summary_body = ttk.Frame(sum_wrap, style="Card.TFrame")
        summary_body.pack(fill="both", expand=True, padx=0, pady=0)
        # 摘要表（参考你给的图：时间/币种/平台/模块动作/动作/方向/金额U）
        cols = ("time", "coin", "platform", "module_action", "action", "dir", "amount_u", "price", "grid_gap", "profit_u", "rsi", "adx")
        self.tree = ttk.Treeview(summary_body, columns=cols, show="headings", height=12, style="Summary.Treeview")
        headings = {
            "time": "时间",
            "coin": "币种",
            "platform": "平台",
            "module_action": "模块",
            "action": "动作",
            "dir": "方向",
            "amount_u": "金额",
            "price": "价",
            "grid_gap": "网格间距",
            "profit_u": "利",
            "rsi": "RSI",
            "adx": "ADX",
        }
        for c in cols:
            if c == 'time':
                self.tree.heading(c, text='时间 ↓', command=self._toggle_summary_time_sort)
            else:
                self.tree.heading(c, text=headings[c])
            # 关键：禁用 Treeview 自动“压缩列宽”（否则右侧 RSI/ADX 会被挤到只剩 RS / A）
            self.tree.column(c, anchor="center", stretch=False)

        # 列宽（尽量在常见窗口宽度下完整显示；不足则可横向滚动）
        self.tree.column("time", width=68, minwidth=60, anchor="center", stretch=False)
        self.tree.column("coin", width=48, minwidth=42, anchor="center", stretch=False)
        self.tree.column("platform", width=62, minwidth=56, anchor="center", stretch=False)
        self.tree.column("module_action", width=160, minwidth=138, anchor="w", stretch=True)
        self.tree.column("action", width=42, minwidth=36, anchor="center", stretch=False)
        self.tree.column("dir", width=38, minwidth=34, anchor="center", stretch=False)
        self.tree.column("amount_u", width=60, minwidth=54, anchor="e", stretch=False)
        self.tree.column("price", width=70, minwidth=62, anchor="e", stretch=False)
        self.tree.column("grid_gap", width=86, minwidth=78, anchor="e", stretch=False)
        self.tree.column("profit_u", width=58, minwidth=52, anchor="e", stretch=False)
        self.tree.column("rsi", width=44, minwidth=38, anchor="e", stretch=False)
        self.tree.column("adx", width=44, minwidth=38, anchor="e", stretch=False)

        # 纵向/横向滚动条（解决列多导致 RSI/ADX 标题和内容被截断）
        scy2 = ttk.Scrollbar(summary_body, orient="vertical", command=self.tree.yview)
        scx2 = ttk.Scrollbar(summary_body, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scy2.set, xscrollcommand=scx2.set)
        # 用 grid 而不是 pack：右边界由同一个容器统一控制，
        # 可避免摘要区因为横向滚动条/竖滚动条的 pack 顺序导致右侧视觉更宽。
        summary_body.grid_columnconfigure(0, weight=1)
        summary_body.grid_rowconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(0, 0), pady=(0, 0))
        scy2.grid(row=0, column=1, sticky="ns", padx=(0, 0), pady=(0, 0))
        # 横滚动条右侧对齐到竖滚动条外缘；左侧仍保持不动。
        scx2.grid(row=1, column=0, columnspan=2, sticky="ew", padx=(0, 0), pady=(0, 0))
        self._sync_summary_time_heading()

        try:
            # 前景色（买红卖绿） + 轻背景（更好看也不刺眼）
            self.tree.tag_configure("tag_buy", foreground="#ff4d4f", background="#0F1013")
            self.tree.tag_configure("tag_sell", foreground="#52c41a", background="#0F1013")
            # 中性/信息行
            self.tree.tag_configure("tag_neutral", foreground="#F6D365", background="#0F1013")
            self.tree.tag_configure("tag_blue_mismatch", foreground="#4DA3FF", background="#0F1013")
            # 斑马纹（用于没有买卖色的行/备注行）
            self.tree.tag_configure("tag_row_odd", background="#0F1013")
            self.tree.tag_configure("tag_row_even", background="#0F1013")
        except Exception:
            pass

        # 摘要颜色规则：开仓/加仓=红，平仓=绿，M10/B2=黄
        try:
            import tkinter.font as tkfont
            self._font_tree_bold = tkfont.Font(family="Segoe UI", size=10, weight="bold")
            self.tree.tag_configure("tag_red", foreground="#E74C3C")
            self.tree.tag_configure("tag_green", foreground="#2ECC71")
            self.tree.tag_configure("tag_yellow", foreground="#F1C40F")
        except Exception:
            try:
                self.tree.tag_configure("tag_red", foreground="#E74C3C")
                self.tree.tag_configure("tag_green", foreground="#2ECC71")
                self.tree.tag_configure("tag_yellow", foreground="#F1C40F")
            except Exception:
                pass


        ratio_wrap = ttk.Frame(bot_r, style="Card.TFrame")
        ratio_wrap.pack(fill="both", expand=True, padx=(10, 10), pady=(0, 6))
        ttk.Label(ratio_wrap, text="多空比列表", style="CardTitle.TLabel").pack(anchor="w", padx=0, pady=(0, 0))
        ratio_body = ttk.Frame(ratio_wrap, style="Card.TFrame")
        ratio_body.pack(fill="both", expand=True, padx=0, pady=(0, 0))
        # 按币种明细表（Treeview 比 Text 更整齐）
        cols_ls = ("coin", "total", "long", "short", "pct", "ratio", "cnt", "rsi", "adx")
        self.tree_ls = ttk.Treeview(ratio_body, columns=cols_ls, show="headings", height=8)
        heads_ls = {
            "coin": "币种",
            "total": "总名义U",
            "long": "多U",
            "short": "空U",
            "pct": "多/空%",
            "ratio": "多/空",
            "cnt": "笔数多/空",
            "rsi": "RSI",
            "adx": "ADX",
        }
        for c in cols_ls:
            self.tree_ls.heading(c, text=heads_ls[c])
            self.tree_ls.column(c, anchor="center")

        # 列宽（右侧半屏：尽量紧凑）
        self.tree_ls.column("coin", width=70)
        self.tree_ls.column("total", width=110)
        self.tree_ls.column("long", width=95)
        self.tree_ls.column("short", width=95)
        self.tree_ls.column("pct", width=90)
        self.tree_ls.column("ratio", width=70)
        self.tree_ls.column("cnt", width=85)
        self.tree_ls.column("rsi", width=70)
        self.tree_ls.column("adx", width=70)

        # 行颜色标签（红绿提示：偏多/偏空）
        # 说明：只影响显示，不影响任何交易逻辑
        try:
            self.tree_ls.tag_configure('healthy', foreground='#2ECC71')
            self.tree_ls.tag_configure('unhealthy', foreground='#E74C3C')
            self.tree_ls.tag_configure('rsi_red', foreground='#E74C3C')
            self.tree_ls.tag_configure('rsi_green', foreground='#2ECC71')
            self.tree_ls.tag_configure('rsi_neutral', foreground='#E6E6E6')
        except Exception:
            pass

        scy3 = ttk.Scrollbar(ratio_body, orient="vertical", command=self.tree_ls.yview)
        self.tree_ls.configure(yscrollcommand=scy3.set)
        ratio_body.grid_columnconfigure(0, weight=1)
        ratio_body.grid_rowconfigure(0, weight=1)
        self.tree_ls.grid(row=0, column=0, sticky="nsew", padx=(0, 0), pady=(0, 0))
        scy3.grid(row=0, column=1, sticky="ns", padx=(0, 0), pady=(0, 0))

        
        # =========================
        # 网格插页（只展示，不影响 Bot）
        # =========================
        try:
            self._build_grid_tab(self.tab_grid)
        except Exception:
            # 网格插页失败不影响主面板
            pass

        # 关键修复：之前只创建了“网格账本”tab 容器，但没有真正构建里面的控件，
        # 所以点击“网格账本”时会看到一整块空白黑页。
        # 这里必须像网格 tab 一样，在启动阶段把账本页的 UI 真正建出来。
        try:
            self._build_grid_book_tab(self.tab_grid_book)
        except Exception:
            # 账本插页失败不影响主面板，其它地方会把异常写到状态文本里
            pass
        try:
            self._build_trend_observe_tab(self.tab_trend_observe)
        except Exception:
            pass
        try:
            self._build_selection_observe_tab(self.tab_selection_observe)
        except Exception:
            pass

# 底部状态栏：数据状态+原因+路径
        self.var_status = tk.StringVar(value="数据状态：-")
        ttk.Label(self.root, textvariable=self.var_status, background="#111214", foreground="#9AA5B1", font=("Segoe UI", 9)).pack(fill="x", padx=14, pady=(0, 8))


    def _build_grid_tab(self, parent: tk.Widget) -> None:
        """网格插页：展示 EXT/VAR 各币种网格状态（只读）"""
        # 头部状态
        hdr = ttk.Frame(parent, style="Card.TFrame")
        hdr.pack(fill="x", padx=12, pady=(12, 8))

        self.var_grid_status = tk.StringVar(value="网格状态：等待数据…")
        ttk.Label(hdr, text="网格状态", style="CardTitle.TLabel").pack(anchor="w", padx=10, pady=(8, 2))
        ttk.Label(hdr, textvariable=self.var_grid_status, style="SubValue.TLabel").pack(anchor="w", padx=10, pady=(0, 10))

        # 表格
        box = ttk.Frame(parent, style="Card.TFrame")
        box.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        ttk.Label(box, text="网格明细（实时快照）", style="CardTitle.TLabel").pack(anchor="w", padx=10, pady=(8, 4))

        # 网格表（压缩列宽 + 横向滚动 + 左右滑动按钮）
        cols = (
            "plat", "coin", "side", "status",
            "px", "invest_u", "layers", "run_cnt",
            "grid_profit_u", "total_pnl_u", "pnl_pct",
            "next_buy_px", "next_sell_tp_px",
            "buy_gap_pct", "sell_gap_pct",
            "note"
        )

        table_wrap = ttk.Frame(box)
        table_wrap.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        tree_wrap = ttk.Frame(table_wrap)
        tree_wrap.pack(fill="both", expand=True)

        self.grid_tree = ttk.Treeview(tree_wrap, columns=cols, show="headings", height=18)
        heads = {
            "plat": "平台",
            "coin": "币种",
            "side": "方向",
            "status": "状态",
            "px": "现价",
            "invest_u": "投入(U)",
            "layers": "OPEN层",
            "run_cnt": "运行次数",
            "grid_profit_u": "网格利润(U)",
            "total_pnl_u": "总盈亏(U)",
            "pnl_pct": "总盈亏%",
            "next_buy_px": "下一买入价",
            "next_sell_tp_px": "下一卖出价",
            "buy_gap_pct": "买距%",
            "sell_gap_pct": "卖距%",
            "note": "备注",
        }
        widths = {
            "plat": 56, "coin": 56, "side": 52, "status": 66,
            "px": 78, "invest_u": 82, "layers": 60, "run_cnt": 68,
            "grid_profit_u": 92, "total_pnl_u": 92, "pnl_pct": 72,
            "next_buy_px": 92, "next_sell_tp_px": 92,
            "buy_gap_pct": 60, "sell_gap_pct": 60,
            "note": 180,
        }
        stretch_cols = {"note"}
        for c in cols:
            self.grid_tree.heading(c, text=heads.get(c, c))
            self.grid_tree.column(c, width=widths.get(c, 80), minwidth=widths.get(c, 80), anchor="center", stretch=(c in stretch_cols))

        try:
            import tkinter.font as tkfont
            _bf = tkfont.Font(family="Segoe UI", size=10, weight="bold")
            self.grid_tree.tag_configure("tag_red", foreground="#E74C3C", font=_bf)
            self.grid_tree.tag_configure("tag_green", foreground="#2ECC71", font=_bf)
            self.grid_tree.tag_configure("tag_yellow", foreground="#F1C40F", font=_bf)
        except Exception:
            try:
                self.grid_tree.tag_configure("tag_red", foreground="#E74C3C")
                self.grid_tree.tag_configure("tag_green", foreground="#2ECC71")
                self.grid_tree.tag_configure("tag_yellow", foreground="#F1C40F")
            except Exception:
                pass

        scy = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.grid_tree.yview)
        scx = ttk.Scrollbar(table_wrap, orient="horizontal", command=self.grid_tree.xview)
        self.grid_tree.configure(yscrollcommand=scy.set, xscrollcommand=scx.set)
        self.grid_tree.pack(side="left", fill="both", expand=True)
        scy.pack(side="right", fill="y")
        scx.pack(fill="x", pady=(6, 0))

        btn_bar = ttk.Frame(table_wrap)
        btn_bar.pack(fill="x", pady=(6, 0))

        def _grid_xscroll_units(units: int) -> None:
            try:
                self.grid_tree.xview_scroll(int(units), "units")
            except Exception:
                pass

        ttk.Button(btn_bar, text="← 左滑", command=lambda: _grid_xscroll_units(-6)).pack(side="left")
        ttk.Button(btn_bar, text="→ 右滑", command=lambda: _grid_xscroll_units(6)).pack(side="left", padx=(8, 0))

        # 右键复制（可选）
        try:
            self.grid_tree.bind("<Button-3>", lambda _e: None)
        except Exception:
            pass


    def _open_path_or_url(self, target: str) -> None:
        """打开本地文件或 URL。"""
        try:
            s = str(target or '').strip()
            if not s:
                return
            if s.startswith(('http://', 'https://', 'file:///')):
                try:
                    webbrowser.open(s)
                    return
                except Exception:
                    pass
            try:
                if os.name == 'nt':
                    os.startfile(s)  # type: ignore[attr-defined]
                    return
            except Exception:
                pass
            try:
                webbrowser.open(Path(s).resolve().as_uri())
            except Exception:
                pass
        except Exception:
            pass

    def _grid_book_pick_file(self, *names: str) -> Optional[Path]:
        try:
            cands = []
            od = getattr(self.model, 'out_dir', None)
            if od:
                cands.append(Path(od))
            try:
                cands.append(Path(__file__).resolve().parent / 'out')
            except Exception:
                pass
            try:
                cands.append(Path(tempfile.gettempdir()) / 'bot_panel_bridge' / 'out')
            except Exception:
                pass
            seen = set()
            for base in cands:
                try:
                    sb = str(base)
                    if sb in seen:
                        continue
                    seen.add(sb)
                    for nm in names:
                        fp = Path(base) / nm
                        if fp.exists() and fp.stat().st_size > 0:
                            return fp
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _grid_book_guess_url(self) -> str:
        try:
            fp = self._grid_book_pick_file('grid_ui_url.txt')
            if fp:
                s = fp.read_text(encoding='utf-8', errors='ignore').strip()
                if s:
                    return s
        except Exception:
            pass
        try:
            st = _read_json_safe(self._grid_book_pick_file('grid_book_observe_status.json'))
            for k in ('preferred_url', 'panel_entry_url', 'portal_http_url', 'html_http_url', 'portal_html_uri', 'html_uri'):
                v = st.get(k)
                if v:
                    return str(v)
        except Exception:
            pass
        try:
            fp = self._grid_book_pick_file('grid_book_observe.html', 'm9_grid_book_observe_last.html')
            if fp:
                return fp.resolve().as_uri()
        except Exception:
            pass
        return ''

    def _grid_book_sort_key(self, d: Dict[str, Any]) -> Tuple[int, int, int]:
        try:
            plat = str(d.get('platform') or d.get('plat') or '-').upper()
            coin = str(d.get('coin') or '-').upper()
            side = str(d.get('side') or d.get('direction') or '-').upper()
            plat_order = {'EXT': 0, 'VAR': 1}
            coin_order = {'BTC': 0, 'ETH': 1, 'SOL': 2, 'HYPE': 3, 'BNB': 4, 'XRP': 5, 'SUI': 6, 'ADA': 7}
            side_order = {'LONG': 0, 'SHORT': 1}
            return (plat_order.get(plat, 9), coin_order.get(coin, 99), side_order.get(side, 9))
        except Exception:
            return (99, 99, 99)

    def _grid_book_dist_pct_value(self, d: Optional[Dict[str, Any]], *keys: str) -> Optional[float]:
        try:
            if not isinstance(d, dict):
                return None
            for k in keys:
                if k in d and d.get(k) not in (None, ''):
                    return float(_safe_float(d.get(k), 0.0))
        except Exception:
            pass
        return None

    def _grid_book_is_near_trigger(self, d: Optional[Dict[str, Any]]) -> bool:
        try:
            if not isinstance(d, dict):
                return False
            action = str(d.get('current_action') or d.get('action') or '').strip()
            if ('应止盈' in action) or ('应加仓' in action):
                return False
            tp = self._grid_book_dist_pct_value(d, 'dist_tp_pct', 'tp_gap_pct', 'distance_to_tp_pct')
            ad = self._grid_book_dist_pct_value(d, 'dist_add_pct', 'add_gap_pct', 'distance_to_add_pct')
            vals = [abs(v) for v in (tp, ad) if isinstance(v, (int, float))]
            return bool(vals) and min(vals) <= 0.005
        except Exception:
            return False

    def _grid_book_action_icon(self, d: Optional[Dict[str, Any]]) -> str:
        try:
            if not isinstance(d, dict):
                return '· 无'
            action = str(d.get('current_action') or d.get('action') or '无网格动作').strip() or '无网格动作'
            if '应止盈' in action:
                return '🟢 止盈'
            if '应加仓' in action:
                return '🔴 加仓'
            if self._grid_book_is_near_trigger(d):
                return '🔵 接近'
            if action in ('无网格动作', '-'):
                return '· 无'
            return '· 持有'
        except Exception:
            return '· -'

    def _grid_book_row_tag(self, d: Dict[str, Any]) -> str:
        try:
            action = str(d.get('current_action') or d.get('action') or '').strip()
            if '应止盈' in action:
                return 'tag_tp'
            if '应加仓' in action:
                return 'tag_add'
            if self._grid_book_is_near_trigger(d):
                return 'tag_near'
            uv = float(_safe_float(d.get('upnl_u') or d.get('floating_pnl_u') or d.get('float_pnl_u') or 0.0, 0.0) or 0.0)
            if uv > 0:
                return 'tag_green'
            if uv < 0:
                return 'tag_red'
        except Exception:
            pass
        return ''

    def _grid_book_cmp_tag(self, ext: Optional[Dict[str, Any]], var: Optional[Dict[str, Any]]) -> str:
        try:
            ext_action = str((ext or {}).get('current_action') or (ext or {}).get('action') or '').strip()
            var_action = str((var or {}).get('current_action') or (var or {}).get('action') or '').strip()
            ext_tp = '应止盈' in ext_action
            var_tp = '应止盈' in var_action
            ext_add = '应加仓' in ext_action
            var_add = '应加仓' in var_action
            ext_near = self._grid_book_is_near_trigger(ext)
            var_near = self._grid_book_is_near_trigger(var)
            if (ext_tp and (var_add or var_tp)) or (var_tp and (ext_add or ext_tp)) or (ext_add and var_add):
                return 'tag_mix'
            if ext_tp or var_tp:
                return 'tag_tp'
            if ext_add or var_add:
                return 'tag_add'
            if ext_near or var_near:
                return 'tag_near'
            vals = []
            for d in (ext, var):
                if isinstance(d, dict):
                    vals.append(float(_safe_float(d.get('upnl_u') or d.get('floating_pnl_u') or d.get('float_pnl_u') or 0.0, 0.0) or 0.0))
            if any(v > 0 for v in vals):
                return 'tag_green'
            if any(v < 0 for v in vals):
                return 'tag_red'
        except Exception:
            pass
        return ''

    def _grid_book_build_compare_rows(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        try:
            coin_order = ['BTC', 'ETH', 'SOL', 'HYPE', 'BNB', 'XRP', 'SUI', 'ADA']
            plat_side_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
            for d in rows or []:
                plat = str(d.get('platform') or d.get('plat') or '-').upper()
                coin = str(d.get('coin') or '-').upper()
                if not plat or not coin or coin == '-':
                    continue
                plat_side_map[(coin, plat)] = d
            out: List[Dict[str, Any]] = []
            for coin in coin_order:
                ext = plat_side_map.get((coin, 'EXT'))
                var = plat_side_map.get((coin, 'VAR'))
                def _side_cn(d: Optional[Dict[str, Any]]) -> str:
                    s = str((d or {}).get('side') or (d or {}).get('direction') or '-').upper()
                    return '多' if s == 'LONG' else ('空' if s == 'SHORT' else '-')
                def _layers(d: Optional[Dict[str, Any]]) -> str:
                    return str(int(_safe_float((d or {}).get('current_layers') or (d or {}).get('layers') or 0.0, 0.0) or 0))
                def _action(d: Optional[Dict[str, Any]]) -> str:
                    return self._grid_book_action_icon(d)
                def _px(d: Optional[Dict[str, Any]], *keys: str) -> str:
                    if not isinstance(d, dict):
                        return '-'
                    for k in keys:
                        v = d.get(k)
                        if v not in (None, ''):
                            return _fmt_px(v)
                    return '-'
                def _gap(d: Optional[Dict[str, Any]], *keys: str) -> str:
                    if not isinstance(d, dict):
                        return '-'
                    for k in keys:
                        if k in d and d.get(k) not in (None, ''):
                            return self._fmt_gap_pct_for_book(d.get(k))
                    return '-'
                out.append({
                    'coin': coin,
                    'ext': ext,
                    'var': var,
                    'values': (
                        coin,
                        _side_cn(ext), _layers(ext), _action(ext), _px(ext, 'px', 'mark_px', 'current_px'), _px(ext, 'target_tp_px', 'tp_px', 'next_tp_px'), _gap(ext, 'dist_tp_pct', 'tp_gap_pct', 'distance_to_tp_pct'), _px(ext, 'next_add_px', 'add_px', 'next_buy_px'), _gap(ext, 'dist_add_pct', 'add_gap_pct', 'distance_to_add_pct'),
                        _side_cn(var), _layers(var), _action(var), _px(var, 'px', 'mark_px', 'current_px'), _px(var, 'target_tp_px', 'tp_px', 'next_tp_px'), _gap(var, 'dist_tp_pct', 'tp_gap_pct', 'distance_to_tp_pct'), _px(var, 'next_add_px', 'add_px', 'next_buy_px'), _gap(var, 'dist_add_pct', 'add_gap_pct', 'distance_to_add_pct'),
                    )
                })
            return out
        except Exception:
            return []

    def _build_grid_book_compare_detail_text(self, row: Dict[str, Any]) -> str:
        try:
            coin = str(row.get('coin') or '-').upper()
            ext = row.get('ext') if isinstance(row.get('ext'), dict) else None
            var = row.get('var') if isinstance(row.get('var'), dict) else None
            lines = [f'{coin} 双腿对照', '']
            for plat, d in (('EXT', ext), ('VAR', var)):
                lines.append(f'[{plat}]')
                if not isinstance(d, dict):
                    lines.append('无网格账本数据')
                    lines.append('')
                    continue
                lines.append(self._build_grid_book_detail_text(d))
                lines.append('')
            return '\n'.join(lines).strip()
        except Exception as e:
            return f'账本对照渲染失败：{e}'

    def _on_grid_book_cmp_select(self, _event=None) -> None:
        try:
            if not hasattr(self, 'grid_book_cmp_tree'):
                return
            sel = self.grid_book_cmp_tree.selection()
            if not sel:
                return
            iid = sel[0]
            idx = self.grid_book_cmp_tree.index(iid)
            rows = getattr(self, '_grid_book_cmp_rows', []) or []
            if idx < 0 or idx >= len(rows):
                return
            txt = self._build_grid_book_compare_detail_text(rows[idx])
            self.txt_grid_book.delete('1.0', 'end')
            self.txt_grid_book.insert('1.0', txt)
        except Exception:
            pass

    def _summary_next_tp_map(self) -> Dict[Tuple[str, str, str], str]:
        out: Dict[Tuple[str, str, str], str] = {}
        try:
            rows, _fp, _err = self._read_grid_book_entries()
            for d in rows:
                plat = str(d.get('platform') or d.get('plat') or '-').upper().strip()
                coin = str(d.get('coin') or '-').upper().strip()
                side = str(d.get('side') or d.get('direction') or '-').upper().strip()
                if not plat or not coin or not side:
                    continue
                tp = _fmt_px(d.get('target_tp_px') or d.get('tp_px') or d.get('next_tp_px'))
                if tp and tp != '-':
                    out[(plat, coin, side)] = tp
        except Exception:
            pass
        return out

    def _summary_extract_grid_gap_str(self, row: Dict[str, Any]) -> str:
        """摘要表“网格间距”列：显示“层号 | 本次成交对应网格间距”。"""
        if not isinstance(row, dict):
            return '-'

        module = str(row.get('module') or '').upper().strip()
        if module not in ('M9', 'W1'):
            req = row.get('req') if isinstance(row.get('req'), dict) else {}
            req_mod = str(req.get('module') or '').upper().strip()
            if req_mod not in ('M9', 'W1'):
                return '-'

        def _walk_get(obj, path):
            cur = obj
            for k in path:
                if not isinstance(cur, dict):
                    return None
                cur = cur.get(k)
            return cur

        def _sf_none(x):
            try:
                if x is None or x == '':
                    return None
                return float(x)
            except Exception:
                try:
                    sx = str(x).strip().replace(',', '')
                    if not sx:
                        return None
                    return float(sx)
                except Exception:
                    return None

        layer_txt = ''
        for x in [
            row.get('layer_no'), row.get('layer'), row.get('grid_layer_no'), row.get('grid_level'),
            _walk_get(row, ['grid_layer', 'layer_no']),
            _walk_get(row, ['grid_layer', 'layer']),
            _walk_get(row, ['req', 'grid_layer', 'layer_no']),
            _walk_get(row, ['req', 'grid_layer', 'layer']),
            _walk_get(row, ['req', 'params', 'grid_layer', 'layer_no']),
            _walk_get(row, ['req', 'params', 'grid_layer', 'layer']),
            _walk_get(row, ['req', 'params', 'params', 'grid_layer', 'layer_no']),
            _walk_get(row, ['req', 'params', 'params', 'grid_layer', 'layer']),
            _walk_get(row, ['req', 'meta', 'grid_layer', 'layer_no']),
            _walk_get(row, ['req', 'meta', 'grid_layer', 'layer']),
            _walk_get(row, ['req', 'meta', 'extras', 'grid_layer', 'layer_no']),
            _walk_get(row, ['req', 'meta', 'extras', 'grid_layer', 'layer']),
        ]:
            if x is None or x == '':
                continue
            sx = str(x).strip().upper()
            if not sx:
                continue
            if sx.startswith('L'):
                layer_txt = sx
                break
            fx = _sf_none(x)
            if fx is not None and fx > 0:
                layer_txt = f"L{int(fx)}"
                break
            if sx.isdigit():
                layer_txt = f"L{sx}"
                break

        gap_val = None
        for x in [
            row.get('close_grid_gap_pct'),
            row.get('last_partial_close_grid_gap_pct'),
            _walk_get(row, ['req', 'close_grid_gap_pct']),
            _walk_get(row, ['req', 'last_partial_close_grid_gap_pct']),
            _walk_get(row, ['req', 'params', 'close_grid_gap_pct']),
            _walk_get(row, ['req', 'params', 'last_partial_close_grid_gap_pct']),
            _walk_get(row, ['req', 'params', 'params', 'close_grid_gap_pct']),
            _walk_get(row, ['req', 'params', 'params', 'last_partial_close_grid_gap_pct']),
            _walk_get(row, ['req', 'meta', 'close_grid_gap_pct']),
            _walk_get(row, ['req', 'meta', 'last_partial_close_grid_gap_pct']),
            _walk_get(row, ['req', 'meta', 'extras', 'close_grid_gap_pct']),
            _walk_get(row, ['req', 'meta', 'extras', 'last_partial_close_grid_gap_pct']),
            _walk_get(row, ['grid_layer', 'grid_pct']),
            _walk_get(row, ['grid_layer', 'grid_gap_pct']),
            _walk_get(row, ['req', 'params', 'grid_layer', 'grid_pct']),
            _walk_get(row, ['req', 'params', 'grid_layer', 'grid_gap_pct']),
            _walk_get(row, ['req', 'params', 'params', 'grid_layer', 'grid_pct']),
            _walk_get(row, ['req', 'params', 'params', 'grid_layer', 'grid_gap_pct']),
            _walk_get(row, ['req', 'meta', 'grid_layer', 'grid_pct']),
            _walk_get(row, ['req', 'meta', 'grid_layer', 'grid_gap_pct']),
            _walk_get(row, ['req', 'meta', 'extras', 'grid_layer', 'grid_pct']),
            _walk_get(row, ['req', 'meta', 'extras', 'grid_layer', 'grid_gap_pct']),
            _walk_get(row, ['req', 'grid_layer', 'grid_pct']),
            _walk_get(row, ['req', 'grid_layer', 'grid_gap_pct']),
            row.get('grid_gap_pct'),
            row.get('gap_pct_final'),
            row.get('gap_truth_pct'),
            row.get('gap_pct_used'),
            row.get('final_gap_pct'),
            row.get('step_pct'),
            row.get('grid_pct'),
            row.get('final_grid_pct'),
            _walk_get(row, ['req', 'params', 'gap_truth_pct']),
            _walk_get(row, ['req', 'params', 'gap_pct_final']),
            _walk_get(row, ['req', 'params', 'step_pct']),
            _walk_get(row, ['req', 'params', 'final_gap_pct']),
            _walk_get(row, ['req', 'params', 'params', 'gap_truth_pct']),
            _walk_get(row, ['req', 'params', 'params', 'gap_pct_final']),
            _walk_get(row, ['req', 'params', 'params', 'step_pct']),
            _walk_get(row, ['req', 'params', 'params', 'final_gap_pct']),
            _walk_get(row, ['req', 'meta', 'gap_truth_pct']),
            _walk_get(row, ['req', 'meta', 'gap_pct_final']),
            _walk_get(row, ['req', 'meta', 'step_pct']),
            _walk_get(row, ['req', 'meta', 'final_gap_pct']),
            _walk_get(row, ['req', 'meta', 'extras', 'gap_truth_pct']),
            _walk_get(row, ['req', 'meta', 'extras', 'gap_pct_final']),
            _walk_get(row, ['req', 'meta', 'extras', 'step_pct']),
            _walk_get(row, ['req', 'meta', 'extras', 'final_gap_pct']),
            _walk_get(row, ['req', 'params', 'grid_layer', 'meta', 'grid_decision_obs', 'gap_pct_final']),
            _walk_get(row, ['req', 'params', 'params', 'meta', 'grid_decision_obs', 'gap_pct_final']),
            _walk_get(row, ['req', 'meta', 'grid_layer', 'meta', 'grid_decision_obs', 'gap_pct_final']),
            _walk_get(row, ['req', 'grid_layer', 'meta', 'grid_decision_obs', 'gap_pct_final']),
            _walk_get(row, ['req', 'params', 'meta', 'grid_decision_obs', 'gap_pct_final']),
        ]:
            fx = _sf_none(x)
            if fx is not None and fx > 0:
                gap_val = float(fx)
                break

        gap_txt = _fmt_pct100(gap_val) if (gap_val is not None and gap_val > 0) else '-'
        if layer_txt and gap_txt != '-':
            return f"{layer_txt} | {gap_txt}"
        if layer_txt:
            return layer_txt
        return gap_txt

    def _build_trend_observe_tab(self, parent: tk.Widget) -> None:
        """趋势观察插页：把 bot 输出的主趋势/候补/退出/普通分组直接显示在面板中。"""
        hdr = ttk.Frame(parent, style='Card.TFrame')
        hdr.pack(fill='x', padx=12, pady=(12, 8))
        self.var_trend_observe_status = tk.StringVar(value='趋势观察：等待数据…')
        self.var_trend_observe_meta = tk.StringVar(value='趋势更新时间：- | 状态文件：- | 分组条数：-')
        ttk.Label(hdr, text='趋势观察', style='CardTitle.TLabel').pack(anchor='w', padx=10, pady=(8, 2))
        ttk.Label(hdr, textvariable=self.var_trend_observe_status, style='SubValue.TLabel').pack(anchor='w', padx=10, pady=(0, 4))
        ttk.Label(hdr, textvariable=self.var_trend_observe_meta, style='Hint.TLabel', wraplength=1200, justify='left').pack(anchor='w', padx=10, pady=(0, 8))

        body = ttk.Frame(parent, style='Card.TFrame')
        body.pack(fill='both', expand=True, padx=12, pady=(0, 12))
        grid = ttk.Frame(body, style='Card.TFrame')
        grid.pack(fill='both', expand=True, padx=10, pady=10)
        for c in range(2):
            grid.grid_columnconfigure(c, weight=1)
        for r in range(2):
            grid.grid_rowconfigure(r, weight=1)

        self._trend_section_vars = {}
        self._trend_section_texts = {}
        self._trend_section_line_meta = {}
        sections = [
            ('alpha', '主趋势币'),
            ('candidate', '候补趋势币'),
            ('exit_only', '退出态币'),
            ('normal', '普通币'),
        ]
        for idx0, (key, title) in enumerate(sections):
            row, col = divmod(idx0, 2)
            box = ttk.Frame(grid, style='Card.TFrame')
            box.grid(row=row, column=col, sticky='nsew', padx=6, pady=6)
            var = tk.StringVar(value=f'{title}[0]')
            self._trend_section_vars[key] = var
            ttk.Label(box, textvariable=var, style='CardTitle.TLabel').pack(anchor='w', padx=10, pady=(8, 4))
            txt = tk.Text(box, height=11, bg='#0E0F12', fg='#E6E6E6', insertbackground='#E6E6E6', relief='flat', wrap='word', font=('Consolas', 10))
            sy = ttk.Scrollbar(box, orient='vertical', command=txt.yview)
            txt.configure(yscrollcommand=sy.set)
            txt.pack(side='left', fill='both', expand=True, padx=(10,0), pady=(0,10))
            sy.pack(side='right', fill='y', padx=(0,10), pady=(0,10))
            try:
                txt.configure(state='disabled')
            except Exception:
                pass
            try:
                txt.bind('<Double-Button-1>', lambda e, kk=key: self._on_trend_section_double_click(kk, e))
            except Exception:
                pass
            self._trend_section_texts[key] = txt
            self._trend_section_line_meta[key] = []

    def _set_text_widget(self, widget: tk.Text, content: str) -> None:
        try:
            widget.configure(state='normal')
            widget.delete('1.0', 'end')
            widget.insert('1.0', content or '-')
            widget.configure(state='disabled')
        except Exception:
            pass

    def _show_trend_detail_popup(self, title: str, content: str) -> None:
        try:
            win = tk.Toplevel(self)
            win.title(title or '趋势详情')
            try:
                win.transient(self)
            except Exception:
                pass
            try:
                win.geometry('760x420')
            except Exception:
                pass
            wrap = ttk.Frame(win, style='Card.TFrame')
            wrap.pack(fill='both', expand=True, padx=10, pady=10)
            ttk.Label(wrap, text=title or '趋势详情', style='CardTitle.TLabel').pack(anchor='w', padx=8, pady=(8, 4))
            txt = tk.Text(wrap, bg='#0E0F12', fg='#E6E6E6', insertbackground='#E6E6E6', relief='flat', wrap='word', font=('Consolas', 10))
            sy = ttk.Scrollbar(wrap, orient='vertical', command=txt.yview)
            txt.configure(yscrollcommand=sy.set)
            txt.pack(side='left', fill='both', expand=True, padx=(8,0), pady=(0,8))
            sy.pack(side='right', fill='y', padx=(0,8), pady=(0,8))
            txt.insert('1.0', content or '暂无详情')
            try:
                txt.configure(state='disabled')
            except Exception:
                pass
        except Exception as e:
            try:
                messagebox.showinfo(title or '趋势详情', content or f'打开详情失败：{e}')
            except Exception:
                pass

    def _on_trend_section_double_click(self, key: str, event: Optional[tk.Event] = None) -> None:
        try:
            widget = self._trend_section_texts.get(key)
            if widget is None:
                return
            idx = widget.index(f'@{getattr(event, "x", 0)},{getattr(event, "y", 0)}') if event is not None else widget.index('insert')
            line_no = max(1, int(str(idx).split('.')[0]))
            line_txt = str(widget.get(f'{line_no}.0', f'{line_no}.end') or '').strip()
            meta_list = self._trend_section_line_meta.get(key) or []
            meta = meta_list[line_no - 1] if 0 <= line_no - 1 < len(meta_list) else {}
            detail = ''
            if isinstance(meta, dict):
                detail = str(meta.get('detail') or '')
            if not detail:
                detail = line_txt or '暂无详情'
            try:
                self.clipboard_clear()
                self.clipboard_append(line_txt or detail)
            except Exception:
                pass
            title = str((self._trend_section_vars.get(key).get() if key in self._trend_section_vars else key) or key)
            title = f'{title} · 详情'
            self._show_trend_detail_popup(title, detail)
            try:
                if hasattr(self, 'var_trend_observe_status'):
                    self.var_trend_observe_status.set(f'趋势观察：已复制并打开详情 -> {line_txt or key}')
            except Exception:
                pass
        except Exception as e:
            try:
                if hasattr(self, 'var_trend_observe_status'):
                    self.var_trend_observe_status.set(f'趋势观察：详情打开失败：{e}')
            except Exception:
                pass

    def _render_trend_observe_tab(self, panel_status: Optional[Dict[str, Any]] = None) -> None:
        try:
            if getattr(self, 'model', None) and hasattr(self.model, '_load_panel_status_payload'):
                st, fp_status_txt = self.model._load_panel_status_payload(panel_status, required_keys=['trend_observe', 'trend_panel_blocks', 'trend_panel_sections_text'])
            else:
                st = panel_status if isinstance(panel_status, dict) else {}
                fp_status_txt = '-'
            trend_update_txt = '-'
            try:
                cand_ts = None
                if isinstance(st, dict):
                    for k in ('trend_updated_at', 'trend_observe_updated_at', 'trend_last_update_ts', 'trend_update_ts', 'updated_at', 'status_ts', 'ts'):
                        v = st.get(k)
                        if v not in (None, '', 0, '0'):
                            cand_ts = v
                            break
                    if cand_ts in (None, '', 0, '0'):
                        obs0 = st.get('trend_observe') or {}
                        if isinstance(obs0, dict):
                            for k in ('updated_at', 'ts', 'timestamp', 'last_update_ts'):
                                v = obs0.get(k)
                                if v not in (None, '', 0, '0'):
                                    cand_ts = v
                                    break
                if cand_ts not in (None, '', 0, '0'):
                    trend_update_txt = _fmt_report_dt(cand_ts)
                elif fp_status_txt not in ('', '-') and Path(fp_status_txt).exists():
                    trend_update_txt = _fmt_report_dt(Path(fp_status_txt).stat().st_mtime)
            except Exception:
                trend_update_txt = '-'
            groups_txt = {}
            groups_meta = {'alpha': [], 'candidate': [], 'exit_only': [], 'normal': []}
            sections = (((st.get('trend_panel_sections_text') if isinstance(st, dict) else {}) or {}) if isinstance(st, dict) else {})
            if isinstance(sections, dict) and sections:
                groups_txt = {k: str(sections.get(k) or '') for k in ('alpha','candidate','exit_only','normal')}
            if not any(groups_txt.values()) and isinstance(st, dict):
                blocks = st.get('trend_panel_blocks') or []
                if isinstance(blocks, list):
                    for b in blocks:
                        if not isinstance(b, dict):
                            continue
                        key = str(b.get('key') or '')
                        lines = b.get('lines') or []
                        title = str(b.get('title') or '')
                        if key:
                            groups_txt[key] = '\n'.join([title] + [str(x) for x in lines]) if lines else title
                            groups_meta[key] = [{'detail': str(x)} for x in lines]
            if not any(groups_txt.values()) and isinstance(st, dict):
                obs = st.get('trend_observe') or {}
                groups = obs.get('groups') if isinstance(obs, dict) else {}
                counts = obs.get('group_counts') if isinstance(obs, dict) else {}
                if isinstance(groups, dict):
                    name_map = {'alpha':'主趋势币','candidate':'候补趋势币','exit_only':'退出态币','normal':'普通币'}
                    for key in ('alpha','candidate','exit_only','normal'):
                        items = groups.get(key) or []
                        title = f"{name_map.get(key,key)}[{int((counts or {}).get(key, len(items)) if isinstance(counts, dict) else len(items))}]"
                        lines = []
                        if isinstance(items, list):
                            for it in items:
                                if isinstance(it, dict):
                                    line = str(it.get('line') or it.get('text') or it.get('summary') or '')
                                    if not line:
                                        coin = str(it.get('coin') or '').upper()
                                        side = str(it.get('trend_side') or it.get('side') or '')
                                        state = str(it.get('trend_state') or '')
                                        reason = str(it.get('trend_reason') or '')
                                        line = '|'.join([x for x in (coin, side, state, reason) if x])
                                    if line:
                                        lines.append(line)
                                        detail_parts = [line]
                                        for kk, lab in (('coin','币种'),('trend_side','趋势方向'),('trend_state','趋势状态'),('trend_reason','趋势原因'),('opp_side_frozen','对侧冻结'),('runner_protect','runner保护'),('b2_guard','B2保护'),('m10_guard','M10保护')):
                                            vv = it.get(kk) if isinstance(it, dict) else None
                                            if vv not in (None, '', [], {}):
                                                detail_parts.append(f'{lab}: {vv}')
                                        groups_meta[key].append({'detail': '\n'.join(detail_parts)})
                                elif str(it).strip():
                                    txt0 = str(it).strip()
                                    lines.append(txt0)
                                    groups_meta[key].append({'detail': txt0})
                        groups_txt[key] = '\n'.join([title] + lines) if lines else title
            name_map = {'alpha':'主趋势币','candidate':'候补趋势币','exit_only':'退出态币','normal':'普通币'}
            total_items = 0
            has_any_group = any(str(groups_txt.get(k) or '').strip() for k in ('alpha','candidate','exit_only','normal'))
            empty_hint = '暂无趋势分组数据\n请先运行 bot，并等待状态文件写入 trend_observe / trend_panel_blocks / trend_panel_sections_text。'
            for key in ('alpha','candidate','exit_only','normal'):
                raw = str(groups_txt.get(key) or '').strip()
                title = name_map.get(key, key)
                lines = []
                if raw:
                    ls = raw.splitlines()
                    if ls:
                        head = ls[0].strip()
                        body = [x for x in ls[1:] if str(x).strip()]
                        if not head.startswith(title):
                            head = title if body else f'{title}[0]'
                        lines = body
                        try:
                            if key in self._trend_section_vars:
                                self._trend_section_vars[key].set(head)
                        except Exception:
                            pass
                    total_items += len(lines)
                    try:
                        self._trend_section_line_meta[key] = list(groups_meta.get(key) or [])
                    except Exception:
                        pass
                    self._set_text_widget(self._trend_section_texts.get(key), '\n'.join(lines) if lines else '暂无该分组数据')
                else:
                    try:
                        if key in self._trend_section_vars:
                            self._trend_section_vars[key].set(f'{title}[0]')
                    except Exception:
                        pass
                    try:
                        self._trend_section_line_meta[key] = []
                    except Exception:
                        pass
                    self._set_text_widget(self._trend_section_texts.get(key), '暂无该分组数据' if has_any_group else empty_hint)
            if hasattr(self, 'var_trend_observe_status'):
                self.var_trend_observe_status.set(f'趋势观察：已加载分组，共 {total_items} 条' if total_items > 0 else '趋势观察：未发现趋势分组数据')
            if hasattr(self, 'var_trend_observe_meta'):
                self.var_trend_observe_meta.set(f'趋势更新时间：{trend_update_txt} | 状态文件：{fp_status_txt} | 分组条数：{total_items}')
        except Exception as e:
            try:
                if hasattr(self, 'var_trend_observe_status'):
                    self.var_trend_observe_status.set(f'趋势观察：面板渲染异常：{e}')
                if hasattr(self, 'var_trend_observe_meta'):
                    self.var_trend_observe_meta.set(f'趋势更新时间：- | 状态文件：{str(locals().get("fp_status_txt", "-") or "-")} | 分组条数：-')
            except Exception:
                pass

    def _build_grid_book_tab(self, parent: tk.Widget) -> None:
        """网格账本插页：改成“总览 + 曲线 + 十六腿对照 + 明细表 + 详情”的结构。"""
        wrap = ttk.Frame(parent, style='Card.TFrame')
        wrap.pack(fill='both', expand=True, padx=0, pady=0)

        hdr = ttk.Frame(wrap, style='Card.TFrame')
        hdr.pack(fill='x', padx=12, pady=(12, 8))
        self.var_grid_book_status = tk.StringVar(value='网格账本：等待数据…')
        ttk.Label(hdr, text='网格账本总览', style='CardTitle.TLabel').pack(anchor='w', padx=10, pady=(8, 2))
        ttk.Label(hdr, textvariable=self.var_grid_book_status, style='SubValue.TLabel').pack(anchor='w', padx=10, pady=(0, 8))

        bar = ttk.Frame(hdr, style='Card.TFrame')
        bar.pack(fill='x', padx=10, pady=(0, 8))
        ttk.Button(bar, text='打开账本网页', style='Btn.TButton', command=lambda: self._open_path_or_url(self._grid_book_guess_url())).pack(side='left', padx=(0, 8))
        ttk.Button(bar, text='打开账本文本', style='Btn.TButton', command=lambda: self._open_path_or_url(str(self._grid_book_pick_file('m9_grid_book_observe_last.txt') or ''))).pack(side='left', padx=(0, 8))
        ttk.Button(bar, text='打开账本JSON', style='Btn.TButton', command=lambda: self._open_path_or_url(str(self._grid_book_pick_file('m9_grid_book_observe_last.json') or ''))).pack(side='left', padx=(0, 8))
        self.var_grid_book_meta = tk.StringVar(value='')
        ttk.Label(bar, textvariable=self.var_grid_book_meta, style='Hint.TLabel').pack(side='left', padx=(12, 0))

        main = ttk.Panedwindow(wrap, orient=tk.VERTICAL)
        main.pack(fill='both', expand=True, padx=12, pady=(0, 12))
        overview_box = ttk.Frame(main, style='Card.TFrame')
        cmp_box = ttk.Frame(main, style='Card.TFrame')
        top = ttk.Frame(main, style='Card.TFrame')
        bot = ttk.Frame(main, style='Card.TFrame')
        main.add(overview_box, weight=2)
        main.add(cmp_box, weight=2)
        main.add(top, weight=3)
        main.add(bot, weight=2)

        ttk.Label(overview_box, text='账本概览', style='CardTitle.TLabel').pack(anchor='w', padx=10, pady=(8, 6))
        kpi_wrap = ttk.Frame(overview_box, style='Card.TFrame')
        kpi_wrap.pack(fill='x', padx=10, pady=(0, 8))
        self.grid_book_kpi_vars = {}
        kpi_defs = [
            ('已实现', '0.00U'), ('浮盈亏', '0.00U'), ('总权益', '-'), ('累计收益率', '-'),
            ('近24h收益率', '-'), ('近24h APR', '-'), ('运行 APR', '-'), ('7日滚动APR', '-'),
            ('30日滚动APR', '-'), ('更新时间', '-'),
        ]
        for i, (title, init) in enumerate(kpi_defs):
            card = ttk.Frame(kpi_wrap, style='Card.TFrame')
            card.grid(row=0, column=i, sticky='nsew', padx=(0 if i == 0 else 6, 0), pady=0)
            kpi_wrap.grid_columnconfigure(i, weight=1)
            ttk.Label(card, text=title, style='Hint.TLabel').pack(anchor='w', padx=10, pady=(8, 2))
            var = tk.StringVar(value=init)
            ttk.Label(card, textvariable=var, style='SubValue.TLabel').pack(anchor='w', padx=10, pady=(0, 8))
            self.grid_book_kpi_vars[title] = var

        charts = ttk.Frame(overview_box, style='Card.TFrame')
        charts.pack(fill='both', expand=True, padx=10, pady=(0, 10))
        charts.grid_columnconfigure(0, weight=1)
        charts.grid_columnconfigure(1, weight=1)
        charts.grid_rowconfigure(0, weight=1)

        eq_box = ttk.Frame(charts, style='Card.TFrame')
        rt_box = ttk.Frame(charts, style='Card.TFrame')
        eq_box.grid(row=0, column=0, sticky='nsew', padx=(0, 6), pady=0)
        rt_box.grid(row=0, column=1, sticky='nsew', padx=(6, 0), pady=0)
        ttk.Label(eq_box, text='总权益曲线（末点含浮盈亏）', style='CardTitle.TLabel').pack(anchor='w', padx=10, pady=(8, 4))
        ttk.Label(rt_box, text='收益率曲线（末点含浮盈亏）', style='CardTitle.TLabel').pack(anchor='w', padx=10, pady=(8, 4))
        self.canvas_grid_equity = tk.Canvas(eq_box, height=220, bg='#0E0F12', highlightthickness=0, bd=0)
        self.canvas_grid_return = tk.Canvas(rt_box, height=220, bg='#0E0F12', highlightthickness=0, bd=0)
        self.canvas_grid_equity.pack(fill='both', expand=True, padx=10, pady=(0, 8))
        self.canvas_grid_return.pack(fill='both', expand=True, padx=10, pady=(0, 8))
        self.var_grid_equity_hint = tk.StringVar(value='总权益曲线：等待数据…')
        self.var_grid_return_hint = tk.StringVar(value='收益率曲线：等待数据…')
        ttk.Label(eq_box, textvariable=self.var_grid_equity_hint, style='Hint.TLabel').pack(anchor='w', padx=10, pady=(0, 8))
        ttk.Label(rt_box, textvariable=self.var_grid_return_hint, style='Hint.TLabel').pack(anchor='w', padx=10, pady=(0, 8))

        ttk.Label(cmp_box, text='八币十六腿对照', style='CardTitle.TLabel').pack(anchor='w', padx=10, pady=(8, 4))
        cmp_inner = ttk.Frame(cmp_box, style='Card.TFrame')
        cmp_inner.pack(fill='both', expand=True, padx=0, pady=(0, 0))
        cmp_cols = ('coin','ext_side','ext_layers','ext_action','ext_px','ext_tp','ext_tp_gap','ext_add','ext_add_gap','var_side','var_layers','var_action','var_px','var_tp','var_tp_gap','var_add','var_add_gap')
        self.grid_book_cmp_tree = ttk.Treeview(cmp_inner, columns=cmp_cols, show='headings', height=8)
        cmp_heads = {
            'coin':'币种',
            'ext_side':'EXT方向','ext_layers':'EXT层','ext_action':'EXT状态','ext_px':'EXT现价','ext_tp':'EXT止盈','ext_tp_gap':'EXT距止盈','ext_add':'EXT加仓','ext_add_gap':'EXT距加仓',
            'var_side':'VAR方向','var_layers':'VAR层','var_action':'VAR状态','var_px':'VAR现价','var_tp':'VAR止盈','var_tp_gap':'VAR距止盈','var_add':'VAR加仓','var_add_gap':'VAR距加仓',
        }
        cmp_widths = {'coin':56,'ext_side':62,'ext_layers':56,'ext_action':96,'ext_px':82,'ext_tp':82,'ext_tp_gap':82,'ext_add':82,'ext_add_gap':82,'var_side':62,'var_layers':56,'var_action':96,'var_px':82,'var_tp':82,'var_tp_gap':82,'var_add':82,'var_add_gap':82}
        for c in cmp_cols:
            self.grid_book_cmp_tree.heading(c, text=cmp_heads.get(c, c))
            self.grid_book_cmp_tree.column(c, width=cmp_widths.get(c, 80), anchor='center', stretch=False)
        try:
            self.grid_book_cmp_tree.tag_configure('tag_red', foreground='#E74C3C')
            self.grid_book_cmp_tree.tag_configure('tag_green', foreground='#2ECC71')
            self.grid_book_cmp_tree.tag_configure('tag_yellow', foreground='#F1C40F')
            self.grid_book_cmp_tree.tag_configure('tag_add', foreground='#FF6B6B', background='#151010')
            self.grid_book_cmp_tree.tag_configure('tag_tp', foreground='#7CFC8A', background='#0F1510')
            self.grid_book_cmp_tree.tag_configure('tag_mix', foreground='#F6D365', background='#17130B')
            self.grid_book_cmp_tree.tag_configure('tag_near', foreground='#8FD3FF', background='#0D131A')
        except Exception:
            pass
        cmp_sy = ttk.Scrollbar(cmp_inner, orient='vertical', command=self.grid_book_cmp_tree.yview)
        cmp_sx = ttk.Scrollbar(cmp_inner, orient='horizontal', command=self.grid_book_cmp_tree.xview)
        self.grid_book_cmp_tree.configure(yscrollcommand=cmp_sy.set, xscrollcommand=cmp_sx.set)
        cmp_inner.grid_columnconfigure(0, weight=1)
        cmp_inner.grid_rowconfigure(0, weight=1)
        self.grid_book_cmp_tree.grid(row=0, column=0, sticky='nsew', padx=(10,0), pady=(0,0))
        cmp_sy.grid(row=0, column=1, sticky='ns', padx=(0,10), pady=(0,0))
        cmp_sx.grid(row=1, column=0, columnspan=2, sticky='ew', padx=(10,10), pady=(0,10))

        ttk.Label(top, text='网格明细表', style='CardTitle.TLabel').grid(row=0, column=0, sticky='w', padx=10, pady=(8, 4))
        cols = ('plat','coin','side','layers','action','px','tp_px','add_px','tp_gap','add_gap','realized','upnl','reason','grid_gap','grid_formula')
        self.grid_book_tree = ttk.Treeview(top, columns=cols, show='headings', height=12)
        heads = {
            'plat':'平台','coin':'币种','side':'方向','layers':'当前层数','action':'当前动作','px':'现价',
            'tp_px':'目标止盈价','add_px':'下一加仓价','tp_gap':'距止盈','add_gap':'距加仓',
            'realized':'已实现利润','upnl':'浮盈亏','reason':'原因','grid_gap':'网格间距','grid_formula':'网格间距计算方式'
        }
        widths = {'plat':60,'coin':60,'side':55,'layers':70,'action':82,'px':88,'tp_px':96,'add_px':96,'tp_gap':86,'add_gap':86,'realized':88,'upnl':88,'reason':180,'grid_gap':84,'grid_formula':340}
        for c in cols:
            self.grid_book_tree.heading(c, text=heads.get(c,c))
            self.grid_book_tree.column(c, width=widths.get(c, 90), anchor='center', stretch=(c in ('reason','grid_formula')))
        try:
            self.grid_book_tree.tag_configure('tag_red', foreground='#E74C3C')
            self.grid_book_tree.tag_configure('tag_green', foreground='#2ECC71')
            self.grid_book_tree.tag_configure('tag_yellow', foreground='#F1C40F')
            self.grid_book_tree.tag_configure('tag_add', foreground='#FF6B6B', background='#151010')
            self.grid_book_tree.tag_configure('tag_tp', foreground='#7CFC8A', background='#0F1510')
            self.grid_book_tree.tag_configure('tag_near', foreground='#8FD3FF', background='#0D131A')
        except Exception:
            pass
        sx = ttk.Scrollbar(top, orient='horizontal', command=self.grid_book_tree.xview)
        sy = ttk.Scrollbar(top, orient='vertical', command=self.grid_book_tree.yview)
        self.grid_book_tree.configure(xscrollcommand=sx.set, yscrollcommand=sy.set)
        top.grid_columnconfigure(0, weight=1)
        top.grid_rowconfigure(1, weight=1)
        self.grid_book_tree.grid(row=1, column=0, sticky='nsew', padx=(10,0), pady=(0,0))
        sy.grid(row=1, column=1, sticky='ns', padx=(0,10), pady=(0,0))
        sx.grid(row=2, column=0, columnspan=2, sticky='ew', padx=(10,10), pady=(0,10))

        ttk.Label(bot, text='账本详情', style='CardTitle.TLabel').pack(anchor='w', padx=10, pady=(8, 4))
        self.txt_grid_book = tk.Text(bot, height=12, bg='#0E0F12', fg='#E6E6E6', insertbackground='#E6E6E6', relief='flat', wrap='word', font=('Consolas', 10))
        sy2 = ttk.Scrollbar(bot, orient='vertical', command=self.txt_grid_book.yview)
        self.txt_grid_book.configure(yscrollcommand=sy2.set)
        self.txt_grid_book.pack(side='left', fill='both', expand=True, padx=(10,0), pady=(0,10))
        sy2.pack(side='right', fill='y', padx=(0,10), pady=(0,10))
        try:
            self.grid_book_tree.bind('<<TreeviewSelect>>', self._on_grid_book_select)
        except Exception:
            pass
        try:
            self.grid_book_cmp_tree.bind('<<TreeviewSelect>>', self._on_grid_book_cmp_select)
        except Exception:
            pass
        for _canvas in (self.canvas_grid_equity, self.canvas_grid_return):
            try:
                _canvas.bind('<Configure>', lambda _e: self._render_grid_book_overview(getattr(self, '_grid_book_rows', []) or []))
            except Exception:
                pass
        self._grid_book_rows = []
        self._grid_book_cmp_rows = []

    def _fmt_gap_pct_for_book(self, v: Any) -> str:
        try:
            x = _safe_float(v, None)
            if x is None:
                return '-'
            return _fmt_pct100(float(x))
        except Exception:
            return '-'

    def _grid_book_formula_text(self, d: Dict[str, Any]) -> str:
        try:
            gb = d.get('grid_breakdown') if isinstance(d.get('grid_breakdown'), dict) else {}
            parts = []
            base = gb.get('base_pct')
            vol = gb.get('vol_pct')
            trend = gb.get('trend_pct')
            ring = gb.get('ring_pct')
            final = gb.get('final_pct')
            if base is not None:
                parts.append(f'基础 {_fmt_pct100(base)}')
            if vol is not None:
                parts.append(f'+ 波动率修正 {_fmt_pct100(vol)}')
            if trend is not None:
                parts.append(f'+ 趋势修正 {_fmt_pct100(trend)}')
            if ring is not None:
                parts.append(f'+ ring修正 {_fmt_pct100(ring)}')
            if final is not None:
                parts.append(f'= 最终 {_fmt_pct100(final)}')
            if parts:
                return ' | '.join(parts)
            s = str(d.get('grid_calc_desc') or d.get('grid_gap_calc') or d.get('grid_formula') or d.get('grid_gap_formula') or '').strip()
            if s:
                return s
            reason = str(d.get('grid_formula_reason') or '').strip()
            if reason:
                return reason
        except Exception:
            pass
        return '按账本最终间距显示（缺少更细分解）'

    def _grid_book_pick_ts(self, row: Dict[str, Any]) -> float:
        try:
            return float(_pick_row_ts(row) or 0.0)
        except Exception:
            return 0.0

    def _grid_book_equity_total(self) -> float:
        try:
            fp = getattr(self.model, 'fp_equity', None)
            if not fp:
                return 0.0
            obj = _read_json_safe(fp)
            return max(0.0, _safe_float(obj.get('total'), _safe_float(obj.get('total_usd'), _safe_float(obj.get('t'), 0.0))))
        except Exception:
            return 0.0

    def _grid_book_build_curves(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        out = {
            'equity_series': [], 'return_series': [], 'base_equity': 0.0, 'current_equity': 0.0,
            'total_realized': 0.0, 'total_upnl': 0.0, 'day_realized': 0.0, 'day_return_pct': 0.0,
            'cum_return_pct': 0.0, 'running_apr_pct': 0.0, 'day_apr_pct': 0.0, 'last_ts': 0.0,
            'start_ts': 0.0, 'status_text': '暂无曲线数据', 'points': 0,
            'rolling_apr_7d_pct': 0.0, 'rolling_apr_30d_pct': 0.0,
        }
        try:
            total_upnl = 0.0
            total_realized_snapshot = 0.0
            seen_legs = set()
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                leg_key = (
                    str(row.get('platform') or row.get('plat') or '-').upper(),
                    str(row.get('coin') or '-').upper(),
                    str(row.get('side') or row.get('direction') or '-').upper(),
                )
                if leg_key in seen_legs:
                    continue
                seen_legs.add(leg_key)
                total_upnl += _safe_float(row.get('upnl_u') or row.get('floating_pnl_u') or row.get('float_pnl_u'), 0.0)
                total_realized_snapshot += _safe_float(row.get('realized_profit_u') or row.get('realized_u'), 0.0)
            out['total_upnl'] = total_upnl
            out['total_realized'] = total_realized_snapshot

            trades_fp = getattr(self.model, 'fp_trades', None)
            events = []
            cum_realized = 0.0
            last_ts = 0.0
            if trades_fp and Path(trades_fp).exists():
                for row in _iter_jsonl_rows(trades_fp):
                    ts = self._grid_book_pick_ts(row)
                    if ts <= 0:
                        continue
                    realized = _safe_float(row.get('realized_pnl_u'), None)
                    if realized is None:
                        realized = _safe_float(row.get('pnl_u'), None)
                    if realized is None:
                        continue
                    cum_realized += float(realized)
                    last_ts = max(last_ts, ts)
                    events.append((ts, cum_realized))
            current_equity = self._grid_book_equity_total()
            out['current_equity'] = current_equity
            if events:
                base_equity = current_equity - total_upnl - cum_realized if current_equity > 0 else 0.0
                if base_equity <= 0:
                    base_equity = current_equity - cum_realized if current_equity > 0 else 0.0
                if base_equity <= 0:
                    base_equity = max(abs(cum_realized + total_upnl) * 3.0, 1000.0)
                out['base_equity'] = base_equity
                start_ts = float(events[0][0])
                out['start_ts'] = start_ts
                out['last_ts'] = last_ts
                realized_equity_series = []
                total_equity_series = []
                return_series = []
                day_cut = last_ts - 24.0 * 3600.0
                day_realized = 0.0
                prev_cum = 0.0
                for ts, cum in events:
                    realized_eq = base_equity + cum
                    realized_equity_series.append((ts, realized_eq))
                    total_equity_series.append((ts, realized_eq))
                    return_series.append((ts, (realized_eq / base_equity - 1.0) * 100.0 if base_equity > 0 else 0.0))
                    delta = cum - prev_cum
                    prev_cum = cum
                    if ts >= day_cut:
                        day_realized += delta
                if current_equity > 0:
                    tail_ts = max(time.time(), last_ts)
                    tail_return = (current_equity / base_equity - 1.0) * 100.0 if base_equity > 0 else 0.0
                    if total_equity_series:
                        last_series_ts, last_series_eq = total_equity_series[-1]
                        if tail_ts <= last_series_ts:
                            tail_ts = last_series_ts + 1.0
                        if abs(current_equity - last_series_eq) > 1e-9:
                            total_equity_series.append((tail_ts, current_equity))
                            return_series.append((tail_ts, tail_return))
                        else:
                            return_series[-1] = (last_series_ts, tail_return)
                    else:
                        total_equity_series.append((tail_ts, current_equity))
                        return_series.append((tail_ts, tail_return))
                    out['last_ts'] = tail_ts
                else:
                    current_equity = total_equity_series[-1][1] if total_equity_series else 0.0
                    out['current_equity'] = current_equity
                out['equity_series'] = total_equity_series[-240:]
                out['return_series'] = return_series[-240:]
                out['points'] = len(out['equity_series'])
                out['day_realized'] = day_realized
                out['cum_return_pct'] = ((current_equity / base_equity) - 1.0) * 100.0 if (current_equity > 0 and base_equity > 0) else (cum_realized / base_equity * 100.0 if base_equity > 0 else 0.0)
                out['day_return_pct'] = (day_realized / base_equity) * 100.0 if base_equity > 0 else 0.0
                running_days = max((out['last_ts'] - start_ts) / 86400.0, 1.0 / 24.0)
                out['running_apr_pct'] = (cum_realized / base_equity) * (365.0 / running_days) * 100.0 if base_equity > 0 else 0.0
                out['day_apr_pct'] = (day_realized / base_equity) * 365.0 * 100.0 if base_equity > 0 else 0.0

                def _rolling_apr(window_days: float) -> float:
                    if base_equity <= 0 or not events:
                        return 0.0
                    cutoff = out['last_ts'] - window_days * 86400.0
                    before_cum = 0.0
                    after_cum = events[-1][1]
                    found_after = False
                    for ts, cum in events:
                        if ts <= cutoff:
                            before_cum = cum
                        if (not found_after) and ts >= cutoff:
                            after_cum = cum
                            found_after = True
                    period_realized = float(events[-1][1]) - float(before_cum)
                    if out['last_ts'] > last_ts and current_equity > 0:
                        period_realized += total_upnl
                    effective_days = min(window_days, max((out['last_ts'] - start_ts) / 86400.0, 1.0 / 24.0))
                    return (period_realized / base_equity) * (365.0 / effective_days) * 100.0 if effective_days > 0 else 0.0

                out['rolling_apr_7d_pct'] = _rolling_apr(7.0)
                out['rolling_apr_30d_pct'] = _rolling_apr(30.0)
                out['status_text'] = f'历史按 trades.jsonl 已实现盈亏回放，末点叠加当前浮盈亏；共 {len(events)} 个成交落地点'
            else:
                eq = current_equity if current_equity > 0 else max(total_realized_snapshot + total_upnl, 0.0)
                if eq > 0:
                    now_ts = time.time()
                    out['base_equity'] = eq - total_upnl if (eq - total_upnl) > 0 else eq
                    out['current_equity'] = eq
                    out['last_ts'] = now_ts
                    out['start_ts'] = now_ts
                    out['equity_series'] = [(now_ts, eq)]
                    out['return_series'] = [(now_ts, 0.0)]
                    out['points'] = 1
                    out['status_text'] = '未找到 trades.jsonl 历史成交，只显示当前总权益快照'
            return out
        except Exception as e:
            out['status_text'] = f'曲线构建失败：{e}'
            return out

    def _grid_book_set_overview_vars(self, info: Dict[str, Any]) -> None:
        try:
            mapping = getattr(self, 'grid_book_kpi_vars', {}) or {}
            mapping.get('已实现', tk.StringVar()).set(_fmt_u(info.get('total_realized', 0.0)))
            mapping.get('浮盈亏', tk.StringVar()).set(_fmt_u(info.get('total_upnl', 0.0)))
            cur_eq = _safe_float(info.get('current_equity'), 0.0)
            mapping.get('总权益', tk.StringVar()).set(_fmt_u(cur_eq) if cur_eq > 0 else '-')
            mapping.get('累计收益率', tk.StringVar()).set(_fmt_pct100(_safe_float(info.get('cum_return_pct'), 0.0) / 100.0) if info.get('base_equity') else '-')
            mapping.get('近24h收益率', tk.StringVar()).set(_fmt_pct100(_safe_float(info.get('day_return_pct'), 0.0) / 100.0) if info.get('base_equity') else '-')
            mapping.get('近24h APR', tk.StringVar()).set(_fmt_pct100(_safe_float(info.get('day_apr_pct'), 0.0) / 100.0) if info.get('base_equity') else '-')
            mapping.get('运行 APR', tk.StringVar()).set(_fmt_pct100(_safe_float(info.get('running_apr_pct'), 0.0) / 100.0) if info.get('base_equity') else '-')
            mapping.get('7日滚动APR', tk.StringVar()).set(_fmt_pct100(_safe_float(info.get('rolling_apr_7d_pct'), 0.0) / 100.0) if info.get('base_equity') else '-')
            mapping.get('30日滚动APR', tk.StringVar()).set(_fmt_pct100(_safe_float(info.get('rolling_apr_30d_pct'), 0.0) / 100.0) if info.get('base_equity') else '-')
            last_ts = _safe_float(info.get('last_ts'), 0.0)
            mapping.get('更新时间', tk.StringVar()).set(datetime.fromtimestamp(last_ts, REPORT_TZ).strftime('%m-%d %H:%M:%S') if last_ts > 0 else '-')
        except Exception:
            pass

    def _draw_grid_curve(self, canvas: tk.Canvas, series: List[Tuple[float, float]], *, title: str, y_suffix: str, line_color: str, fill_color: str, neutral_zero: bool = False) -> None:
        try:
            canvas.delete('all')
            canvas.update_idletasks()
            w = max(int(canvas.winfo_width()), 320)
            h = max(int(canvas.winfo_height()), 180)
            pad_l, pad_r, pad_t, pad_b = 56, 12, 18, 28
            canvas.create_rectangle(0, 0, w, h, fill='#0E0F12', outline='')
            canvas.create_text(10, 8, anchor='nw', text=title, fill='#E6E6E6', font=('Microsoft YaHei UI', 10, 'bold'))
            if not series:
                canvas.create_text(w / 2, h / 2, text='暂无数据', fill='#7F8C8D', font=('Microsoft YaHei UI', 11))
                return
            xs = [float(x) for x, _ in series]
            ys = [float(y) for _, y in series]
            min_y = min(ys)
            max_y = max(ys)
            if abs(max_y - min_y) < 1e-9:
                bias = max(abs(max_y) * 0.05, 1.0)
                min_y -= bias
                max_y += bias
            if neutral_zero:
                min_y = min(min_y, 0.0)
                max_y = max(max_y, 0.0)
            plot_w = max(w - pad_l - pad_r, 10)
            plot_h = max(h - pad_t - pad_b, 10)
            x0, y0 = pad_l, pad_t
            x1, y1 = w - pad_r, h - pad_b
            canvas.create_rectangle(x0, y0, x1, y1, outline='#23262D')
            for i in range(4):
                yy = y0 + plot_h * i / 3.0
                canvas.create_line(x0, yy, x1, yy, fill='#1B1F26')
            def _map_x(v: float) -> float:
                if xs[-1] == xs[0]:
                    return x0 + plot_w / 2.0
                return x0 + (v - xs[0]) / (xs[-1] - xs[0]) * plot_w
            def _map_y(v: float) -> float:
                return y1 - (v - min_y) / (max_y - min_y) * plot_h
            if neutral_zero and (min_y <= 0.0 <= max_y):
                yz = _map_y(0.0)
                canvas.create_line(x0, yz, x1, yz, fill='#2B313B', dash=(4, 3))
            pts = []
            for xv, yv in series:
                pts.extend((_map_x(float(xv)), _map_y(float(yv))))
            if len(pts) >= 4:
                poly = [x0, y1] + pts + [x1, y1]
                canvas.create_polygon(poly, fill=fill_color, outline='')
                canvas.create_line(*pts, fill=line_color, width=2.0, smooth=True)
                canvas.create_oval(pts[-2] - 3, pts[-1] - 3, pts[-2] + 3, pts[-1] + 3, fill=line_color, outline='')
            canvas.create_text(8, y0, anchor='w', text=f'{max_y:,.2f}{y_suffix}', fill='#97A0AF', font=('Consolas', 9))
            canvas.create_text(8, y1, anchor='w', text=f'{min_y:,.2f}{y_suffix}', fill='#97A0AF', font=('Consolas', 9))
            start_txt = datetime.fromtimestamp(xs[0], REPORT_TZ).strftime('%m-%d') if xs and xs[0] > 0 else '-'
            end_txt = datetime.fromtimestamp(xs[-1], REPORT_TZ).strftime('%m-%d %H:%M') if xs and xs[-1] > 0 else '-'
            canvas.create_text(x0, h - 10, anchor='w', text=start_txt, fill='#97A0AF', font=('Consolas', 9))
            canvas.create_text(x1, h - 10, anchor='e', text=end_txt, fill='#97A0AF', font=('Consolas', 9))
        except Exception:
            pass

    def _render_grid_book_overview(self, rows: List[Dict[str, Any]]) -> None:
        try:
            info = self._grid_book_build_curves(rows or [])
            self._grid_book_set_overview_vars(info)
            hint = str(info.get('status_text') or '暂无曲线数据')
            try:
                self.var_grid_equity_hint.set(hint)
                self.var_grid_return_hint.set(hint)
            except Exception:
                pass
            self._draw_grid_curve(self.canvas_grid_equity, info.get('equity_series') or [], title='总权益曲线（末点含浮盈亏）', y_suffix='U', line_color='#59C3C3', fill_color='#103436', neutral_zero=False)
            self._draw_grid_curve(self.canvas_grid_return, info.get('return_series') or [], title='收益率曲线（末点含浮盈亏）', y_suffix='%', line_color='#F6C85F', fill_color='#3E2C0B', neutral_zero=True)
        except Exception:
            pass

    def _read_grid_book_entries(self) -> Tuple[List[Dict[str, Any]], str, str]:
        try:
            fp = self._grid_book_pick_file('m9_grid_book_observe_last.json')
            if not fp:
                return [], '', '未找到 m9_grid_book_observe_last.json'
            obj, err = _read_json_obj(fp, want='dict', retries=2, sleep_s=0.05)
            if not isinstance(obj, dict):
                return [], str(fp), (err or 'json 解析失败')
            entries = None
            if isinstance(obj, dict):
                entries = obj.get('rows')
                if not isinstance(entries, list):
                    entries = obj.get('entries')
            if not isinstance(entries, list):
                return [], str(fp), 'rows/entries 缺失'
            return [e for e in entries if isinstance(e, dict)], str(fp), ''
        except Exception as e:
            return [], '', str(e)

    def _build_grid_book_detail_text(self, d: Dict[str, Any]) -> str:
        try:
            plat = str(d.get('platform') or d.get('plat') or '-').upper()
            coin = str(d.get('coin') or '-').upper()
            side_raw = str(d.get('side') or d.get('direction') or '-').upper()
            side_cn = '多' if side_raw == 'LONG' else ('空' if side_raw == 'SHORT' else side_raw)
            lines = [f'{coin} {plat} {side_cn}', '']
            lines.append(f"当前层: {d.get('current_layer_label') or d.get('current_layer') or d.get('layer_label') or ('L'+str(int(_safe_float(d.get('current_layers'),0.0))) if int(_safe_float(d.get('current_layers'),0.0))>0 else '-')}")
            opx = d.get('open_px') or d.get('entry_px') or d.get('entry_price')
            if opx not in (None, ''):
                lines.append(f'开仓价: {_fmt_px(opx)}')
            lines.append(f"现价: {_fmt_px(d.get('px') or d.get('mark_px') or d.get('current_px'))}")
            lines.append('')
            lines.append(f"止盈价: {_fmt_px(d.get('target_tp_px') or d.get('tp_px') or d.get('next_tp_px'))}")
            lines.append(f"距离止盈: {self._fmt_gap_pct_for_book(d.get('dist_tp_pct') or d.get('tp_gap_pct') or d.get('distance_to_tp_pct'))}")
            lines.append('')
            lines.append(f"下一加仓价: {_fmt_px(d.get('next_add_px') or d.get('add_px') or d.get('next_buy_px'))}")
            lines.append(f"距离加仓: {self._fmt_gap_pct_for_book(d.get('dist_add_pct') or d.get('add_gap_pct') or d.get('distance_to_add_pct'))}")
            lines.append('')
            lines.append('网格间距计算:')
            formula = self._grid_book_formula_text(d)
            for seg in str(formula).split(' | '):
                lines.append(seg)
            lines.append('')
            lines.append(f"当前动作: {d.get('current_action') or d.get('action') or '-'}")
            lines.append(f"已实现利润: {_fmt_u(d.get('realized_profit_u') or d.get('realized_u') or 0.0)}")
            lines.append(f"浮盈亏: {_fmt_u(d.get('upnl_u') or d.get('floating_pnl_u') or d.get('float_pnl_u') or 0.0)}")
            rs = str(d.get('reason') or d.get('reason_text') or d.get('status_reason') or '').strip()
            if rs:
                lines.append(f'原因: {rs}')
            return '\n'.join(lines)
        except Exception as e:
            return f'账本详情渲染失败：{e}'

    def _on_grid_book_select(self, _event=None) -> None:
        try:
            if not hasattr(self, 'grid_book_tree'):
                return
            sel = self.grid_book_tree.selection()
            if not sel:
                return
            iid = sel[0]
            idx = self.grid_book_tree.index(iid)
            rows = getattr(self, '_grid_book_rows', []) or []
            if idx < 0 or idx >= len(rows):
                return
            txt = self._build_grid_book_detail_text(rows[idx])
            self.txt_grid_book.delete('1.0', 'end')
            self.txt_grid_book.insert('1.0', txt)
        except Exception:
            pass

    def _render_grid_book_tab(self) -> None:
        try:
            if not hasattr(self, 'grid_book_tree'):
                return
            rows, fp, err = self._read_grid_book_entries()
            try:
                for iid in self.grid_book_tree.get_children(''):
                    self.grid_book_tree.delete(iid)
            except Exception:
                pass
            self._grid_book_rows = rows
            cmp_rows = self._grid_book_build_compare_rows(rows)
            self._grid_book_cmp_rows = cmp_rows
            try:
                for iid in self.grid_book_cmp_tree.get_children(''):
                    self.grid_book_cmp_tree.delete(iid)
            except Exception:
                pass
            if err:
                self.var_grid_book_status.set(f'网格账本：{err}')
                self.var_grid_book_meta.set(fp or '')
                try:
                    self.txt_grid_book.delete('1.0', 'end')
                    self.txt_grid_book.insert('1.0', '未读取到网格账本 JSON。\n\n请运行你带网格账本输出的 bot 版本后再看。')
                except Exception:
                    pass
                return
            rows = sorted(rows, key=self._grid_book_sort_key)
            cmp_rows = self._grid_book_build_compare_rows(rows)
            self._grid_book_cmp_rows = cmp_rows
            self.var_grid_book_status.set(f'网格账本：{len(rows)} 条（读取：{fp}）')
            self.var_grid_book_meta.set(self._grid_book_guess_url() or '')
            try:
                self._render_grid_book_overview(rows)
            except Exception:
                pass
            for row in cmp_rows:
                ext = row.get('ext') if isinstance(row.get('ext'), dict) else None
                var = row.get('var') if isinstance(row.get('var'), dict) else None
                tag = self._grid_book_cmp_tag(ext, var)
                vals = row.get('values') or ()
                if tag:
                    self.grid_book_cmp_tree.insert('', 'end', values=vals, tags=(tag,))
                else:
                    self.grid_book_cmp_tree.insert('', 'end', values=vals)
            for d in rows:
                plat = str(d.get('platform') or d.get('plat') or '-').upper()
                coin = str(d.get('coin') or '-').upper()
                side_raw = str(d.get('side') or d.get('direction') or '-').upper()
                side = '多' if side_raw == 'LONG' else ('空' if side_raw == 'SHORT' else side_raw)
                layers = int(_safe_float(d.get('current_layers') or d.get('layers') or 0.0, 0.0) or 0)
                action = str(d.get('current_action') or d.get('action') or '-').strip() or '-'
                px = _fmt_px(d.get('px') or d.get('mark_px') or d.get('current_px'))
                tp_px = _fmt_px(d.get('target_tp_px') or d.get('tp_px') or d.get('next_tp_px'))
                add_px = _fmt_px(d.get('next_add_px') or d.get('add_px') or d.get('next_buy_px'))
                tp_gap = self._fmt_gap_pct_for_book(d.get('dist_tp_pct') or d.get('tp_gap_pct') or d.get('distance_to_tp_pct'))
                add_gap = self._fmt_gap_pct_for_book(d.get('dist_add_pct') or d.get('add_gap_pct') or d.get('distance_to_add_pct'))
                realized = _fmt_u(d.get('realized_profit_u') or d.get('realized_u') or 0.0)
                upnl = _fmt_u(d.get('upnl_u') or d.get('floating_pnl_u') or d.get('float_pnl_u') or 0.0)
                reason = str(d.get('reason') or d.get('reason_text') or d.get('status_reason') or '-').strip() or '-'
                grid_gap = self._fmt_gap_pct_for_book(d.get('grid_gap_pct') or d.get('grid_pct') or d.get('final_grid_pct'))
                formula = self._grid_book_formula_text(d)
                tag = self._grid_book_row_tag(d)
                vals = (plat, coin, side, str(layers), action, px, tp_px, add_px, tp_gap, add_gap, realized, upnl, reason, grid_gap, formula)
                if tag:
                    self.grid_book_tree.insert('', 'end', values=vals, tags=(tag,))
                else:
                    self.grid_book_tree.insert('', 'end', values=vals)
            try:
                if cmp_rows and hasattr(self, 'grid_book_cmp_tree') and self.grid_book_cmp_tree.get_children(''):
                    self.grid_book_cmp_tree.selection_set(self.grid_book_cmp_tree.get_children('')[0])
                    self._on_grid_book_cmp_select()
                elif rows:
                    self.grid_book_tree.selection_set(self.grid_book_tree.get_children('')[0])
                    self._on_grid_book_select()
                else:
                    self.txt_grid_book.delete('1.0', 'end')
                    self.txt_grid_book.insert('1.0', '当前账本为空。')
            except Exception:
                pass
        except Exception as e:
            try:
                self.var_grid_book_status.set(f'网格账本：渲染异常：{e}')
            except Exception:
                pass

    def _read_grid_layers_csv(self) -> dict:
        """读取网格层信息（优先 out/m9_grid_layers_last.csv；否则回退 out/grid_local_ledger.json + out/m9_detail_last.json）。

        返回 {(plat,coin,side): {'layers':.., 'next_tp_px':.., 'next_buy_px':..}}
        说明：
        - 当前展示优先吃 m9_detail_last.json 的实时快照口径
        - grid_local_ledger.json / m9_grid_layers_last.csv 仅作兼容回退
        - next_buy_px / next_tp_px 尽量贴近 m9_detail_last.json 的 next_buy_px / next_sell_tp_px
        """
        out: dict = {}

        od = getattr(self.model, "out_dir", None)
        if not od:
            return out
        od = Path(od)

        def _k(plat, coin, side):
            return (str(plat or "").upper().strip(), str(coin or "").upper().strip(), str(side or "").upper().strip())

        # ---- A) 旧格式：m9_grid_layers_last.csv（存在则解析） ----
        try:
            fp = od / "m9_grid_layers_last.csv"
            if fp.exists() and fp.stat().st_size > 0:
                import csv
                with fp.open("r", encoding="utf-8", errors="ignore", newline="") as f:
                    rd = csv.DictReader(f)
                    rows = list(rd)

                # 只看 OPEN 未 hit 的层作为“待止盈”
                for r in rows:
                    try:
                        plat = str(r.get("platform") or r.get("plat") or "").upper().strip()
                        coin = str(r.get("coin") or r.get("symbol") or r.get("asset") or "").upper().strip()
                        side = str(r.get("side") or "").upper().strip()
                        state = str(r.get("state") or "").upper().strip()
                        hit = str(r.get("hit") or r.get("tp_hit") or "").lower().strip()
                        if state not in ("OPEN", "LIVE", "ACTIVE"):
                            continue
                        if hit in ("1", "true", "yes", "y"):
                            continue
                        tp_px = float(_safe_float(r.get("tp_px") or r.get("tp_price") or r.get("tp") or 0.0, 0.0) or 0.0)

                        kk = _k(plat, coin, side)
                        d = out.get(kk) or {"layers": 0, "tp_candidates": [], "next_tp_px": 0.0, "next_buy_px": 0.0}
                        d["layers"] = int(d.get("layers") or 0) + 1
                        if tp_px > 0:
                            d["tp_candidates"].append(tp_px)
                        out[kk] = d
                    except Exception:
                        continue

                # 归并 next_tp
                for kk, v in out.items():
                    tps = v.get("tp_candidates") or []
                    if tps:
                        side = kk[2]
                        v["next_tp_px"] = (min(tps) if side == "LONG" else max(tps))
                    else:
                        v["next_tp_px"] = float(_safe_float(v.get("next_tp_px"), 0.0) or 0.0)
        except Exception:
            pass

        # ---- B) 主口径：grid_local_ledger.json（OPEN 层） ----
        try:
            fp = od / "grid_local_ledger.json"
            obj = _read_json(fp)
            items = obj.get("items") if isinstance(obj, dict) else None
            if isinstance(items, list):
                # 先清掉由 CSV 得来的 layers（因为 ledger 更可信）
                # 但保留 next_buy_px 等字段（后面再补）
                base_keep = {}
                for kk, v in out.items():
                    base_keep[kk] = {"next_buy_px": float(_safe_float(v.get("next_buy_px"), 0.0) or 0.0)}
                out = {}

                for it in items:
                    if not isinstance(it, dict):
                        continue
                    state = str(it.get("state") or "").upper().strip()
                    if state not in ("OPEN", "LIVE", "ACTIVE"):
                        continue
                    plat = _norm_platform(it.get("platform") or it.get("plat") or "-")
                    coin = str(it.get("coin") or "-").upper().strip()
                    side = str(it.get("side") or "-").upper().strip()
                    tp_px = float(_safe_float(it.get("tp_price") or it.get("tp_px") or 0.0, 0.0) or 0.0)

                    kk = _k(plat, coin, side)
                    d = out.get(kk) or {"layers": 0, "tp_candidates": [], "next_tp_px": 0.0, "next_buy_px": 0.0}
                    d["layers"] = int(d.get("layers") or 0) + 1
                    if tp_px > 0:
                        d["tp_candidates"].append(tp_px)
                    out[kk] = d

                # 合并回之前保留的 next_buy_px
                for kk, v in base_keep.items():
                    if kk in out:
                        out[kk]["next_buy_px"] = float(_safe_float(v.get("next_buy_px"), 0.0) or 0.0)
                    else:
                        # ledger 里没有 OPEN 层，暂不显示 layers，但允许 next_buy_px 留着（后续由 positions 决定是否展示）
                        out[kk] = {"layers": 0, "tp_candidates": [], "next_tp_px": 0.0, "next_buy_px": float(_safe_float(v.get("next_buy_px"), 0.0) or 0.0)}

                # 归并 next_tp
                for kk, v in out.items():
                    tps = v.get("tp_candidates") or []
                    if tps:
                        side = kk[2]
                        v["next_tp_px"] = (min(tps) if side == "LONG" else max(tps))
                    else:
                        v["next_tp_px"] = float(_safe_float(v.get("next_tp_px"), 0.0) or 0.0)
        except Exception:
            pass

        # ---- C) next_buy_px：m9_detail_last.json（更贴近策略计算） ----
        try:
            fp = od / "m9_detail_last.json"
            obj = _read_json(fp)
            ent = obj.get("entries") if isinstance(obj, dict) else None
            if isinstance(ent, list):
                for r in ent:
                    if not isinstance(r, dict):
                        continue
                    plat = _norm_platform(r.get("platform") or "-")
                    coin = str(r.get("coin") or "-").upper().strip()
                    side = str(r.get("side") or "-").upper().strip()
                    nb = float(_safe_float(r.get("next_buy_px") or 0.0, 0.0) or 0.0)
                    if nb <= 0:
                        continue
                    kk = _k(plat, coin, side)
                    d = out.get(kk) or {"layers": 0, "tp_candidates": [], "next_tp_px": 0.0, "next_buy_px": 0.0}
                    d["next_buy_px"] = nb
                    out[kk] = d
        except Exception:
            pass

        # ---- D) 实时快照真相覆盖：m9_detail_last.json 优先 ----
        try:
            fp = od / "m9_detail_last.json"
            obj = _read_json(fp)
            ent = obj.get("entries") if isinstance(obj, dict) else None
            if isinstance(ent, list):
                for r in ent:
                    if not isinstance(r, dict):
                        continue
                    plat = _norm_platform(r.get("platform") or "-")
                    coin = str(r.get("coin") or "-").upper().strip()
                    side = str(r.get("side") or "-").upper().strip()
                    kk = _k(plat, coin, side)
                    d = out.get(kk) or {"layers": 0, "tp_candidates": [], "next_tp_px": 0.0, "next_buy_px": 0.0}
                    layers = int(_safe_float(r.get("layers_open_cnt") or r.get("layers") or d.get("layers") or 0.0, 0.0) or 0.0)
                    next_tp_px = float(_safe_float(r.get("next_sell_tp_px") or r.get("next_tp_px") or d.get("next_tp_px") or 0.0, 0.0) or 0.0)
                    next_buy_px = float(_safe_float(r.get("next_buy_px") or d.get("next_buy_px") or 0.0, 0.0) or 0.0)
                    d["layers"] = layers
                    if next_tp_px > 0:
                        d["next_tp_px"] = next_tp_px
                    if next_buy_px > 0:
                        d["next_buy_px"] = next_buy_px
                    out[kk] = d
        except Exception:
            pass

        # 清理临时字段
        for kk, v in list(out.items()):
            try:
                if isinstance(v, dict) and "tp_candidates" in v:
                    # 不再对外暴露
                    v.pop("tp_candidates", None)
            except Exception:
                pass

        return out


    def _read_grid_ledger_stats(self) -> dict:
        """读取多路网格账本：成功次数 / 已实现利润。

        兼容来源：
        1) out/grid_local_ledger.json
        2) out/state/m9_wangge_zhangben.json
        3) out/trades.jsonl（回补真实减仓次数/利润）
        返回：{(plat, coin, side): {"succ": int, "profit_u": float}}
        """
        out = {}

        def _add(key, succ=0, profit=0.0):
            if not isinstance(key, tuple) or len(key) != 3:
                return
            plat, coin, side = key
            plat = str(plat or '').strip().upper()
            coin = str(coin or '').strip().upper()
            side = str(side or '').strip().upper()
            if not plat or not coin or not side:
                return
            cur = out.get((plat, coin, side), {"succ": 0, "profit_u": 0.0})
            try:
                cur["succ"] = int(cur.get("succ", 0)) + int(_safe_float(succ, 0) or 0)
            except Exception:
                pass
            try:
                cur["profit_u"] = float(cur.get("profit_u", 0.0)) + float(_safe_float(profit, 0.0) or 0.0)
            except Exception:
                pass
            out[(plat, coin, side)] = cur

        def _norm_key(it: dict, fallback_key: str = ''):
            plat = str(it.get("plat") or it.get("platform") or '').strip().upper()
            coin = str(it.get("coin") or '').strip().upper()
            side = str(it.get("side") or it.get("pos_side") or '').strip().upper()
            if (not plat or not coin or not side) and fallback_key:
                parts = str(fallback_key).replace('-', '_').split('_')
                if len(parts) >= 3:
                    if not coin:
                        coin = parts[0].upper()
                    if not plat:
                        plat = parts[1].upper()
                    if not side:
                        side = parts[2].upper()
            return (plat, coin, side)

        try:
            od = getattr(self.model, "out_dir", None)
            if not od:
                return out
            od = Path(od)

            # A) grid_local_ledger.json
            try:
                fp = od / "grid_local_ledger.json"
                obj = _read_json(fp)
                if isinstance(obj, dict):
                    items = None
                    for k in ("items", "rows", "data", "records"):
                        if isinstance(obj.get(k), list):
                            items = obj.get(k)
                            break
                    if items is None and all(isinstance(v, dict) for v in obj.values()):
                        items = list(obj.values())
                    if isinstance(items, list):
                        for idx, it in enumerate(items):
                            if not isinstance(it, dict):
                                continue
                            key = _norm_key(it, str(it.get('grid_id') or it.get('key') or idx))
                            succ = (it.get("success_cnt") or it.get("grid_success_cnt") or it.get("tp_hits") or
                                    it.get("close_cnt") or it.get("closed_cnt") or it.get("run_cnt") or 0)
                            profit = (it.get("profit_u") or it.get("grid_profit_u") or it.get("realized_u") or
                                      it.get("realized_pnl_u") or it.get("realized_pnl_usd") or 0.0)
                            _add(key, succ=succ, profit=profit)
            except Exception:
                pass

            # B) state/m9_wangge_zhangben.json
            try:
                fp = od / "state" / "m9_wangge_zhangben.json"
                obj = _read_json(fp)
                grids = {}
                if isinstance(obj, dict):
                    if isinstance(obj.get("grids"), dict):
                        grids = obj.get("grids") or {}
                    elif isinstance(obj.get("items"), dict):
                        grids = obj.get("items") or {}
                if isinstance(grids, dict):
                    for gid, gv in grids.items():
                        if not isinstance(gv, dict):
                            continue
                        key = _norm_key(gv, str(gid))
                        succ = (gv.get("success_cnt") or gv.get("grid_success_cnt") or gv.get("tp_hits") or
                                gv.get("close_cnt") or gv.get("closed_cnt") or gv.get("realized_cnt") or 0)
                        if not succ:
                            layers = gv.get("layers") if isinstance(gv.get("layers"), list) else []
                            c = 0
                            for ly in layers:
                                if not isinstance(ly, dict):
                                    continue
                                st = str(ly.get("status") or '').upper()
                                if ly.get("close_ts") or ly.get("closed_ts") or ly.get("tp_ts") or st in ("CLOSED", "CLOSE", "TP", "REALIZED", "DONE"):
                                    c += 1
                            succ = c
                        profit = (gv.get("realized_pnl_u") or gv.get("realized_pnl_usd") or gv.get("profit_u") or
                                  gv.get("grid_profit_u") or gv.get("profit_usd") or 0.0)
                        _add(key, succ=succ, profit=profit)
            except Exception:
                pass

            # C) trades.jsonl：回补真实已执行的网格减仓次数/利润
            try:
                fp = od / "trades.jsonl"
                if fp.exists() and fp.stat().st_size > 0:
                    lines = _tail_lines(fp, max_lines=2400)
                    for ln in lines:
                        s = str(ln or '').strip()
                        if not s.startswith('{'):
                            continue
                        try:
                            d = json.loads(s)
                        except Exception:
                            continue
                        if not isinstance(d, dict):
                            continue
                        mod = str(d.get('module') or ((d.get('req') or {}).get('module') if isinstance(d.get('req'), dict) else '') or '').upper()
                        act = str(d.get('action_type') or d.get('action') or ((d.get('req') or {}).get('action_type') if isinstance(d.get('req'), dict) else '') or '').lower()
                        if mod not in ('M9', 'W1'):
                            continue
                        if not any(k in act for k in ('decrease', 'reduce', 'close', 'tp', 'grid_sell')):
                            continue
                        plat = _norm_platform(d.get('platform') or d.get('plat') or ((d.get('req') or {}).get('platform') if isinstance(d.get('req'), dict) else '') or '-')
                        coin = str(d.get('coin') or ((d.get('req') or {}).get('coin') if isinstance(d.get('req'), dict) else '') or '').upper().strip()
                        side = str(d.get('side') or d.get('side_hint') or ((d.get('req') or {}).get('side') if isinstance(d.get('req'), dict) else '') or '').upper().strip()
                        if not side:
                            # 网格减仓：LONG 减仓是卖；SHORT 减仓是买。这里没 side 时就不乱猜。
                            continue
                        profit = _extract_profit_from_row(d)
                        _add((plat, coin, side), succ=1, profit=(profit or 0.0))
            except Exception:
                pass

            return out
        except Exception:
            return out

    def _render_grid_tab(self) -> None:
        """每轮刷新：更新网格插页表格"""
        try:
            # === 网格插页渲染的任何异常，都必须显式写到“网格状态”里，方便定位 ===
            if not hasattr(self, "grid_tree"):
                return
            od = getattr(self.model, "out_dir", None)
            fp_pos = getattr(self.model, "fp_positions", None)

            if not od or not fp_pos or (not Path(fp_pos).exists()):
                # 自动回溯：尝试从 full_log 所在目录推导 out/positions.json，避免“路径探测偏了导致网格为空”
                try:
                    cand = []
                    fp_full = getattr(self.model, "fp_full_log", None) or getattr(self.model, "full_log_fp", None) or ""
                    if fp_full:
                        pf = Path(fp_full)
                        if pf.exists():
                            p_logs = pf.parent
                            cand.append(p_logs.parent / "out" / "positions.json")
                    cand.append(Path(__file__).resolve().parent / "out" / "positions.json")
                    try:
                        cand.append(Path(tempfile.gettempdir()) / "bot_panel_bridge" / "out" / "positions.json")
                    except Exception:
                        pass

                    for c in cand:
                        try:
                            if isinstance(c, Path) and c.exists() and c.stat().st_size > 0:
                                fp_pos = str(c)
                                try:
                                    self.model.fp_positions = fp_pos
                                except Exception:
                                    pass
                                break
                        except Exception:
                            continue
                except Exception:
                    pass

                if (not fp_pos) or (not Path(fp_pos).exists()):
                    self.var_grid_status.set("网格状态：未找到 positions.json（请确认 Bot 正常写出 out/positions.json）")
                    try:
                        for iid in self.grid_tree.get_children(""):
                            self.grid_tree.delete(iid)
                    except Exception:
                        pass
                    return

            pos_obj, pos_err = _read_json_obj(Path(fp_pos), want="dict", retries=3, sleep_s=0.08)
            if not isinstance(pos_obj, dict):
                # 读失败：尽量使用上一次的缓存，避免“偶发读到半截 JSON”导致整页空白
                cached = getattr(self, "_last_pos_obj", None)
                if isinstance(cached, dict):
                    pos_obj = cached
                    self.var_grid_status.set(f"网格状态：positions.json 解析失败（使用上次数据，原因：{pos_err}，读取：{fp_pos}）")
                else:
                    # 尝试自动恢复：很多时候是面板读错了旧 out 目录里的 positions.json
                    try:
                        new_fp, rec_err = self.cache.recover_fp_positions()
                        if new_fp and str(new_fp) != str(fp_pos):
                            fp_pos = new_fp
                            pos_obj, pos_err = _read_json_obj(Path(fp_pos), want="dict", retries=3, sleep_s=0.08)
                    except Exception as _e:
                        rec_err = f"recover exception: {_e}"
                    if isinstance(pos_obj, dict):
                        self._last_pos_obj = pos_obj
                        self.var_grid_status.set(f"网格状态：positions.json 已恢复（读取：{fp_pos}）")
                    else:
                        # 仍失败：显示读取路径 + 文件信息，方便你一眼确认“面板到底在读哪个 positions.json”
                        try:
                            ptmp = Path(fp_pos)
                            finfo = f"size={ptmp.stat().st_size} mtime={datetime.fromtimestamp(ptmp.stat().st_mtime)}"
                        except Exception:
                            finfo = "size/mtime=NA"
                        self.var_grid_status.set(f"网格状态：positions.json 解析失败（原因：{pos_err}，读取：{fp_pos}，{finfo}，recover={rec_err}）")
                        return
            else:
                self._last_pos_obj = pos_obj

            ts_h = str(pos_obj.get("ts_human") or "")
            layers_map = self._read_grid_layers_csv()
            # m9_detail_last.json：用于“投入/网格利润/总盈亏/下一买卖/状态”等更像交易所的字段
            m9_map = {}
            try:
                fp_m9 = Path(od) / "m9_detail_last.json"
                obj_m9 = _read_json(fp_m9)
                ent = obj_m9.get("entries") if isinstance(obj_m9, dict) else None
                if isinstance(ent, list):
                    for e in ent:
                        if not isinstance(e, dict):
                            continue
                        p2 = _norm_platform(e.get("platform") or "-")
                        c2 = str(e.get("coin") or "-").upper().strip()
                        s2 = str(e.get("side") or "-").upper().strip()
                        if p2 and c2 and s2:
                            m9_map[(p2, c2, s2)] = e
            except Exception:
                pass

            ledger_map = self._read_grid_ledger_stats()

            # 从控制文件推导 step/tp（读不到就默认 1%）
            step_pct = 0.01
            tp_pct = 0.01
            try:
                ctl = None
                fp_ctl = getattr(self.model, "fp_control", None)
                if fp_ctl and Path(fp_ctl).exists():
                    ctl = _read_json(Path(fp_ctl))
                if isinstance(ctl, dict):
                    step_pct = float(_safe_float(ctl.get("M9_STEP_PCT") or ctl.get("GRID_STEP_PCT") or 0.01, 0.01))
                    tp_pct = float(_safe_float(ctl.get("M9_TP_PCT") or ctl.get("GRID_TP_PCT") or 0.01, 0.01))
                    if step_pct <= 0:
                        step_pct = 0.01
                    if tp_pct <= 0:
                        tp_pct = 0.01
            except Exception:
                step_pct = 0.01
                tp_pct = 0.01

            # 组装 rows：以 positions 为权威（存在持仓才展示）
            rows = []
            plist = pos_obj.get("positions") if isinstance(pos_obj, dict) else None
            if not isinstance(plist, list):
                plist = []
            for p in plist:
                if not isinstance(p, dict):
                    continue
                if not bool(p.get("has_position", True)):
                    continue
                plat = str(p.get("platform") or "").strip().upper()
                coin = str(p.get("coin") or "").strip().upper()
                side = str(p.get("side") or "").strip().upper()
                notional_u = float(_safe_float(p.get("notional_u") or p.get("notional_usd") or 0.0, 0.0))
                if notional_u <= 0:
                    continue
                px = float(_safe_float(p.get("px") or 0.0, 0.0))
                entry_px = float(_safe_float(p.get("entry_px") or p.get("entry_price") or 0.0, 0.0))
                upnl_u = float(_safe_float(p.get("upnl_u") or p.get("upnl_usd") or 0.0, 0.0))
                pnl_pct100 = float(_safe_float(p.get("pnl_pct100") or (float(_safe_float(p.get("pnl_pct"), 0.0)) * 100.0), 0.0))

                key = (plat, coin, side)
                lm = layers_map.get(key, {})
                # 注意：_safe_float 只接受 (x, default) 两个位置参数。
                
                layers = int(_safe_float(lm.get("layers"), 0.0) or 0.0)
                next_tp = float(_safe_float(lm.get("next_tp_px"), 0.0) or 0.0)

                # next_buy / next_tp：
                # - 优先使用 m9_detail_last.json 提供的 next_buy_px（更贴近策略）
                # - next_tp 优先使用 OPEN 层 tp 候选聚合值；否则用 entry/px + tp_pct 兜底
                next_buy = float(_safe_float(lm.get("next_buy_px"), 0.0) or 0.0)

                if next_buy <= 0:
                    base = entry_px if entry_px > 0 else px
                    k = max(layers, 0) + 1
                    if side == "LONG":
                        next_buy = base * (1.0 - step_pct * k)
                        if next_tp <= 0 and base > 0:
                            next_tp = base * (1.0 + tp_pct)
                    else:
                        next_buy = base * (1.0 + step_pct * k)
                        if next_tp <= 0 and base > 0:
                            next_tp = base * (1.0 - tp_pct)
                else:
                    # 仍保证 next_tp 有值
                    if next_tp <= 0:
                        base = entry_px if entry_px > 0 else px
                        if base > 0:
                            next_tp = (base * (1.0 + tp_pct) if side == "LONG" else base * (1.0 - tp_pct))
                st = ledger_map.get(key, {})
                succ = int(_safe_float(st.get("succ"), 0.0) or 0.0)
                profit_u = float(_safe_float(st.get("profit_u"), 0.0) or 0.0)

                # —— 网格展示字段（参考主流平台口径：投入/网格利润(已实现)/总盈亏(含浮动)/下一买卖/状态） ——
                m9e = m9_map.get(key) or {}
                invest_u = float(_safe_float(m9e.get("grid_open_u_sum") or m9e.get("bought_u_sum") or m9e.get("pos_notional_u") or notional_u, 0.0) or 0.0)
                # 网格利润：优先 m9_detail 的已实现利润；没有就回退 ledger
                grid_profit_u = float(_safe_float(m9e.get("grid_realized_pnl_u"), 0.0) or 0.0)
                if abs(grid_profit_u) <= 0:
                    grid_profit_u = float(_safe_float(profit_u, 0.0) or 0.0)
                # 总盈亏：已实现 + 未实现（优先 m9_detail 计算）
                total_pnl_u = float(_safe_float(m9e.get("grid_total_pnl_u"), 0.0) or 0.0)
                if abs(total_pnl_u) <= 0:
                    total_pnl_u = float(_safe_float(m9e.get("grid_realized_pnl_u"), 0.0) or 0.0) + float(_safe_float(m9e.get("grid_unreal_pnl_u"), 0.0) or 0.0)
                pnl_pct_total = 0.0
                try:
                    pnl_pct_total = (total_pnl_u / invest_u * 100.0) if invest_u > 0 else float(pnl_pct100)
                except Exception:
                    pnl_pct_total = float(pnl_pct100)

                # 下一卖出价：优先 m9_detail 的 next_sell_tp_px；否则回退 next_tp_px(ledger/估算)
                next_sell_tp_px = float(_safe_float(m9e.get("next_sell_tp_px"), 0.0) or 0.0)
                if next_sell_tp_px <= 0:
                    next_sell_tp_px = float(_safe_float(next_tp, 0.0) or 0.0)
                # 买距/卖距：m9_detail 里是“百分数口径”（例如 1.00 表示 1.00%）
                buy_gap_pct = float(_safe_float(m9e.get("next_buy_gap_pct"), 0.0) or 0.0)
                sell_gap_pct = float(_safe_float(m9e.get("next_sell_gap_pct"), 0.0) or 0.0)

                # 状态：冷却/限额/正常（尽量短）
                status = "OK"
                try:
                    g = m9e.get("gates") if isinstance(m9e.get("gates"), dict) else {}
                    lim = g.get("m9_limits") if isinstance(g.get("m9_limits"), dict) else {}
                    if lim.get("ok_now") is False:
                        status = "LIMIT"
                    cd = float(_safe_float(g.get("m9_cd_left_s"), 0.0) or 0.0)
                    add_cd = float(_safe_float(g.get("add_cd_left_s"), 0.0) or 0.0)
                    tp_lock = float(_safe_float(g.get("tp_lock_left_s"), 0.0) or 0.0)
                    if tp_lock > 0:
                        status = f"TP {int(tp_lock)}s"
                    elif add_cd > 0:
                        status = f"ADD {int(add_cd)}s"
                    elif cd > 0:
                        status = f"CD {int(cd)}s"
                except Exception:
                    pass

                note = ""
                try:
                    # 给一个简短提示：触发/未触发、RSI等
                    if bool(m9e.get("next_buy_hit")):
                        note = "买触发"
                    if bool(m9e.get("next_sell_hit")):
                        note = (note + " " if note else "") + "卖触发"
                    rsi = _safe_float(((m9e.get("gates") or {}).get("rsi_gate") or {}).get("rsi_clip"), None)
                    if rsi is not None and rsi == rsi:
                        note = (note + " | " if note else "") + f"RSI {rsi:.1f}"
                except Exception:
                    pass

                rows.append({
                    "plat": plat or "-",
                    "coin": coin or "-",
                    "side": "多" if side == "LONG" else ("空" if side == "SHORT" else side),
                    "status": status,
                    "px": px,
                    "invest_u": invest_u,
                    "layers": int(layers),
                    "run_cnt": int(succ),
                    "grid_profit_u": grid_profit_u,
                    "total_pnl_u": total_pnl_u,
                    "pnl_pct": pnl_pct_total,
                    "next_buy_px": float(_safe_float(next_buy, 0.0) or 0.0),
                    "next_sell_tp_px": next_sell_tp_px,
                    "buy_gap_pct": buy_gap_pct,
                    "sell_gap_pct": sell_gap_pct,
                    "note": note,
                })

            # 排序：平台→币→方向
            def _key(r):
                return (r.get("plat",""), r.get("coin",""), 0 if r.get("side")=="多" else 1)
            rows.sort(key=_key)

            total_runs = int(sum(int(_safe_float(r.get("run_cnt"), 0) or 0) for r in rows))
            total_realized = float(sum(float(_safe_float(r.get("grid_profit_u"), 0.0) or 0.0) for r in rows))
            self.var_grid_status.set(
                f"网格状态：{len(rows)} 行（数据时间 {ts_h or '-'}；运行次数={total_runs}；已实现={total_realized:.2f}U；step={step_pct*100:.2f}%；tp={tp_pct*100:.2f}%）"
            )

            # 清表再插
            try:
                for iid in self.grid_tree.get_children(""):
                    self.grid_tree.delete(iid)
            except Exception:
                pass

            for r in rows:
                vals = (
                    r["plat"], r["coin"], r["side"], r["status"],
                    _fmt_px(r["px"]), _fmt_u(r["invest_u"]), str(r["layers"]), str(r.get("run_cnt", 0)),
                    _fmt_u(r["grid_profit_u"]), _fmt_u(r["total_pnl_u"]), _fmt_pct_plain(r["pnl_pct"]),
                    _fmt_px(r["next_buy_px"]), _fmt_px(r["next_sell_tp_px"]),
                    _fmt_pct_plain(r["buy_gap_pct"]), _fmt_pct_plain(r["sell_gap_pct"]),
                    (r.get("note") or ""),
                )
                tag = None
                # 颜色：总盈亏红/绿；LIMIT/TP/CD 用黄
                try:
                    st = str(r.get("status") or "")
                    if any(k in st for k in ("LIMIT", "TP", "CD", "ADD")):
                        tag = "tag_yellow"
                except Exception:
                    pass
                if tag is None:
                    if float(r.get("total_pnl_u", 0.0)) > 0:
                        tag = "tag_green"
                    elif float(r.get("total_pnl_u", 0.0)) < 0:
                        tag = "tag_red"
                if tag:
                    self.grid_tree.insert("", "end", values=vals, tags=(tag,))
                else:
                    self.grid_tree.insert("", "end", values=vals)


        except Exception as _e:
            try:
                # 尽量给出：异常 + 当前读取路径（如果能取到） + 文件头 80 字节
                fp_pos = getattr(self.model, 'fp_positions', None)
                fp_pos = str(fp_pos) if fp_pos else ''
                head = ''
                try:
                    if fp_pos and Path(fp_pos).exists():
                        with Path(fp_pos).open('rb') as _f:
                            raw = _f.read(80)
                        head = raw.decode('utf-8', errors='replace').replace('\n','\\n').replace('\r','')
                except Exception:
                    head = ''
                if hasattr(self, 'var_grid_status'):
                    msg = f"网格状态：渲染异常：{_e}"
                    if fp_pos:
                        msg += f"（读取：{fp_pos}）"
                    if head:
                        msg += f"（文件头：{head}）"
                    self.var_grid_status.set(msg)
            except Exception:
                pass
            return

    def _set_lights_mode(self, state: str) -> None:
        """状态灯逻辑：
        - stopped：红灯常亮
        - paused：黄灯常亮
        - running：跑马灯（红→黄→蓝 交替闪烁）
        """
        st = (state or "").lower().strip()
        if st not in ("running", "paused", "stopped"):
            st = "stopped"
        if getattr(self, "_light_mode", None) == st:
            # running 状态下若动画 job 丢失，强制重启
            if st == "running" and getattr(self, "_light_anim_job", None) is None:
                self._light_mode = st
                self._tick_lights_anim()
            return
        self._light_mode = st

        # 切换到非 running：停止动画，画静态灯
        if st != "running":
            job = getattr(self, "_light_anim_job", None)
            if job is not None:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
                self._light_anim_job = None
            self._draw_lights(st)
            return

        # running：启动跑马灯
        self._light_anim_idx = 0
        if getattr(self, "_light_anim_job", None) is None:
            self._tick_lights_anim()

    def _tick_lights_anim(self) -> None:
        if getattr(self, "_light_mode", "stopped") != "running":
            self._light_anim_job = None
            return
        idx = int(getattr(self, "_light_anim_idx", 0)) % 3
        self._draw_lights("running", idx=idx)
        self._light_anim_idx = (idx + 1) % 3
        # 速度：300~450ms 都比较像“跑马灯”
        self._light_anim_job = self.root.after(360, self._tick_lights_anim)

    def _draw_lights(self, state: str, idx: Optional[int] = None) -> None:
        self.canvas_lights.delete("all")
        red, yellow, blue, off = "#C94B4B", "#C9A84B", "#4B82C9", "#30343A"
        st = (state or "").lower().strip()

        colors = [red, off, off]  # stopped
        if st == "paused":
            colors = [off, yellow, off]
        elif st == "running":
            # 跑马灯：红→黄→蓝
            k = int(idx or 0) % 3
            colors = [off, off, off]
            colors[k] = [red, yellow, blue][k]

        x0, y0, r, gap = 16, 10, 10, 44
        for i, c in enumerate(colors):
            x = x0 + i * gap
            self.canvas_lights.create_oval(x, y0, x + r * 2, y0 + r * 2, fill=c, outline="#111214", width=1)
        self.canvas_lights.create_text(16, 38, text="红", fill="#9AA5B1", anchor="w", font=("Segoe UI", 9))
        self.canvas_lights.create_text(60, 38, text="黄", fill="#9AA5B1", anchor="w", font=("Segoe UI", 9))
        self.canvas_lights.create_text(104, 38, text="蓝", fill="#9AA5B1", anchor="w", font=("Segoe UI", 9))

    def on_choose_bot(self) -> None:
        fp = filedialog.askopenfilename(title="选择 Bot 脚本", filetypes=[("Python", "*.py"), ("All", "*.*")])
        if not fp:
            return
        self.model.set_bot_script(fp)

    def on_choose_data_dir(self) -> None:
        folder = filedialog.askdirectory(title="选择数据目录（包含 out/logs 的目录）")
        if not folder:
            return
        self.model.choose_data_dir(folder)


    def on_apply_profile(self) -> None:
        prof = str(getattr(self, "var_profile", tk.StringVar(value="normal")).get() or "normal").strip().lower()
        ok = self.model.write_profile(prof, params=getattr(self, "_pending_profile_params", None))
        if not ok:
            messagebox.showwarning("提示", "写入形态失败：请先选择 Bot 目录，并确保 out 目录可写")
        else:
            # 清掉一次性 params：避免下次切换重复覆盖
            self._pending_profile_params = None

    def on_profile_params(self) -> None:
        # 简易弹窗：只覆盖白名单参数（与 Bot 端一致）
        win = tk.Toplevel(self.root)
        win.title("形态参数（可选覆盖）")
        win.geometry("520x340")
        try:
            self._register_topmost(win)
        except Exception:
            pass
        frm = ttk.Frame(win)
        frm.pack(fill="both", expand=True, padx=12, pady=12)

        tips = ("留空=使用形态预设。可调项：\n"
                "- M9_COOLDOWN_MIN_SEC / M9_COOLDOWN_MAX_SEC\n"
                "- B1_COOLDOWN_SEC / B4_COOLDOWN_SEC\n"
                "- HP_ADX_BLOCK（ADX（Average Directional Index，趋势强度指标））\n"
                "- M10_TRIGGER_PCT / M10_PROFIT_MIN_U\n")
        ttk.Label(frm, text=tips, justify="left").pack(anchor="w", pady=(0, 10))

        fields = [
            ("M9_COOLDOWN_MIN_SEC", "M9 最小冷却（秒）"),
            ("M9_COOLDOWN_MAX_SEC", "M9 最大冷却（秒）"),
            ("B1_COOLDOWN_SEC", "B1 冷却（秒）"),
            ("B4_COOLDOWN_SEC", "B4 冷却（秒）"),
            ("HP_ADX_BLOCK", "ADX 阻断阈值"),
            ("M10_TRIGGER_PCT", "M10 触发比例（0.01=1%）"),
            ("M10_PROFIT_MIN_U", "M10 最小盈利（U）"),
        ]
        entries = {}
        grid = ttk.Frame(frm)
        grid.pack(fill="x", pady=(0, 10))
        for i, (k, label) in enumerate(fields):
            ttk.Label(grid, text=label).grid(row=i, column=0, sticky="w", pady=2)
            ent = ttk.Entry(grid, width=18)
            ent.grid(row=i, column=1, sticky="w", pady=2, padx=(8, 0))
            entries[k] = ent

        def _apply():
            g = {}
            c = {}
            def _f(x):
                try:
                    s = str(x).strip()
                    if not s:
                        return None
                    return float(s)
                except Exception:
                    return None
            for k, _ in fields:
                v = _f(entries[k].get())
                if v is None:
                    continue
                if k.startswith("M10_"):
                    c[k] = v
                else:
                    g[k] = v
            params = {"globals": g, "cfg": c}
            self._pending_profile_params = params
            win.destroy()
            # 不自动写入：让用户点“应用形态”生效（避免误操作）

        btns = ttk.Frame(frm)
        btns.pack(fill="x")
        ttk.Button(btns, text="保存（待应用）", command=_apply).pack(side="left")
        ttk.Button(btns, text="取消", command=win.destroy).pack(side="left", padx=(8, 0))
    def on_start(self) -> None:
        ok = self.model.write_control_cmd("start")
        if self.model.cfg.bot_script and self.model.bot_dir:
            self.bot.start(self.model.cfg.bot_script, self.model.cfg.cdp_url, self.model.bot_dir)
        if not ok:
            messagebox.showwarning("提示", "写 control.json 失败（可能无权限或目录不可写）")

    def on_pause(self) -> None:
        ok = self.model.write_control_cmd("pause")
        if not ok:
            messagebox.showwarning("提示", "写 control.json 失败（可能无权限或目录不可写）")

    def on_resume(self) -> None:
        ok = self.model.write_control_cmd("resume")
        if not ok:
            messagebox.showwarning("提示", "写 control.json 失败（可能无权限或目录不可写）")

    def on_close(self) -> None:
        """关闭按钮：优先通知 Bot 退出，再强杀兜底；面板保持运行。"""
        bot_dir = None
        try:
            bd = getattr(getattr(self.model, "cfg", None), "bot_dir", "") or ""
            if bd:
                bot_dir = Path(bd).resolve()
        except Exception:
            bot_dir = None

        wrote_exit = False
        try:
            wrote_exit = bool(self.model.write_control_cmd("exit"))
        except Exception:
            wrote_exit = False

        ok = False
        try:
            ok = self.bot.stop(bot_dir=bot_dir, reason="panel_close_btn")
        except Exception:
            ok = False

        try:
            if wrote_exit and ok:
                self.var_status.set("已执行：发送 exit 并关闭 Bot（面板保持运行）")
            elif wrote_exit:
                self.var_status.set("已发送 exit；停止尚未确认：" + str(self.bot.last_stop_reason))
            elif ok:
                self.var_status.set("未写入 exit，但已强制关闭 Bot（面板保持运行）")
            else:
                self.var_status.set("停止尚未确认：" + str(self.bot.last_stop_reason))
        except Exception:
            pass

    def on_exit(self) -> None:
        """关闭面板窗口：先通知 Bot 退出，再强杀进程，最后退出面板。

        规则：面板关闭必须自动杀 Bot（老板要求）
        """
        # 1) 先写 exit（若 Bot 监听 control.json，可立即退出主循环）
        try:
            _ = self.model.write_control_cmd("exit")
        except Exception:
            pass

        # 2) 再强杀进程（防止后台残留）
        bot_dir = None
        try:
            bd = getattr(getattr(self.model, "cfg", None), "bot_dir", "") or ""
            if bd:
                bot_dir = Path(bd).resolve()
        except Exception:
            bot_dir = None

        try:
            stopped = self.bot.stop(bot_dir=bot_dir, reason="panel_window_exit")
        except Exception:
            stopped = False
        if not stopped:
            self.var_status.set("已请求退出；Bot 停止尚未确认：" + str(self.bot.last_stop_reason))
            messagebox.showwarning("停止尚未确认", "面板保持打开，请核对 Bot 状态。原因：" + str(self.bot.last_stop_reason))
            return

        # 3) 关闭小挂件（如有）
        try:
            if getattr(self, 'mini_win', None) is not None:
                try:
                    self.mini_win.destroy()
                except Exception:
                    pass
        except Exception:
            pass

        # 4) 退出面板
        try:
            self.root.destroy()
        except Exception:
            try:
                self.root.quit()
            except Exception:
                pass

    def on_generate_daily_report(self) -> None:
        """输出近24小时关键日报：自动汇总、去重、按重要性排序，并落盘到 out/日报。"""
        try:
            out_dir = Path(self.model.out_dir) if getattr(self.model, 'out_dir', None) else None
        except Exception:
            out_dir = None
        try:
            logs_dir = Path(self.model.logs_dir) if getattr(self.model, 'logs_dir', None) else None
        except Exception:
            logs_dir = None

        if (not out_dir) and (not logs_dir):
            messagebox.showwarning("提示", "未找到 out/logs 数据目录，暂时无法输出日报。")
            return

        try:
            report = _build_daily_report(out_dir=out_dir, logs_dir=logs_dir, window_hours=DAILY_REPORT_WINDOW_HOURS)
            base_dir = out_dir or logs_dir or Path(__file__).resolve().parent
            docx_fp, json_fp, txt_fp = _write_daily_report_files(base_dir, report)

            win = tk.Toplevel(self.root)
            win.title("近24小时关键日报")
            try:
                win.geometry("980x760")
            except Exception:
                pass
            wrap = ttk.Frame(win)
            wrap.pack(fill="both", expand=True)
            top = ttk.Frame(wrap)
            top.pack(fill="x", padx=12, pady=(10, 6))
            ttk.Label(top, text="近24小时关键日报", style="CardTitle.TLabel").pack(side="left")
            hint = f"已输出：{docx_fp}"
            ttk.Label(top, text=hint, style="Hint.TLabel").pack(side="left", padx=(12, 0))
            body = tk.Text(wrap, wrap="word", bg="#0f1012", fg="#E6E6E6", insertbackground="#E6E6E6", relief="flat", padx=12, pady=12)
            sc = ttk.Scrollbar(wrap, orient="vertical", command=body.yview)
            body.configure(yscrollcommand=sc.set)
            body.pack(side="left", fill="both", expand=True, padx=(12, 0), pady=(0, 12))
            sc.pack(side="right", fill="y", padx=(0, 12), pady=(0, 12))
            body.insert("1.0", str(report.get('markdown') or ''))
            body.configure(state="disabled")

            try:
                self.var_status.set(f"已输出日报：{docx_fp.name}（公文格式，量化专业版）")
            except Exception:
                pass
        except Exception as e:
            try:
                self.var_status.set(f"输出日报失败：{e}")
            except Exception:
                pass
            messagebox.showerror("提示", f"输出日报失败：{e}")

    def on_set_sleep(self) -> None:
        """一键进入睡眠模式：写 control.json，供 Bot 切档。"""
        ok = self.model.write_control_mode("sleep")
        if ok:
            try:
                self.var_status.set("已执行：进入睡眠模式（已写 control.json）")
            except Exception:
                pass
        else:
            messagebox.showwarning("提示", "写 control.json 失败（可能无权限或目录不可写）")

    def on_set_work(self) -> None:
        """一键进入工作模式：写 control.json，供 Bot 切档。"""
        ok = self.model.write_control_mode("work")
        if ok:
            try:
                self.var_status.set("已执行：进入工作模式（已写 control.json）")
            except Exception:
                pass
        else:
            messagebox.showwarning("提示", "写 control.json 失败（可能无权限或目录不可写）")

    def on_toggle_mini(self) -> None:
        if self.mini_win and self.mini_win.winfo_exists():
            # 关闭小挂件：停止边框跑马灯，并恢复主面板
            try:
                self._mini_edge_stop()
            except Exception:
                pass
            # 小挂件强制置顶：关闭时取消定时器
            try:
                if getattr(self, '_mini_topmost_job', None):
                    self.root.after_cancel(self._mini_topmost_job)
            except Exception:
                pass
            self._mini_topmost_job = None
            try:
                self.mini_win.destroy()
            except Exception:
                pass
            self.mini_win = None
            try:
                self.root.deiconify()
            except Exception:
                pass
            return

        self.root.withdraw()
        self.mini_win = tk.Toplevel(self.root)
        self.mini_win.title("北境 小挂件")
        # 小挂件：增加“多空比列表”展示，稍微加高避免拥挤
        self.mini_win.geometry("520x400")
        self.mini_win.configure(bg="#111214")
        self.mini_win.resizable(False, False)

        # 小挂件置顶：跟随全局“置顶”开关
        try:
            self._register_topmost(self.mini_win)
            self._apply_mini_topmost_now()
            if getattr(self, '_topmost_enabled', False):
                self._schedule_mini_topmost_tick()
        except Exception:
            pass

        try:
            self.mini_win.protocol("WM_DELETE_WINDOW", self.on_toggle_mini)
        except Exception:
            pass

        # 小挂件外壳：与主面板一致的边框跑马灯（放大3倍）
        try:
            self._mini_edge_init()
        except Exception:
            pass

        shell = tk.Frame(self.mini_win, bg="#111214", highlightthickness=0, bd=0)
        shell.pack(fill="both", expand=True)

        self._mini_edge_top = tk.Canvas(shell, height=self._mini_edge_thick, bg=self._mini_edge_dim, highlightthickness=0, bd=0)
        self._mini_edge_bottom = tk.Canvas(shell, height=self._mini_edge_thick, bg=self._mini_edge_dim, highlightthickness=0, bd=0)
        self._mini_edge_left = tk.Canvas(shell, width=self._mini_edge_thick, bg=self._mini_edge_dim, highlightthickness=0, bd=0)
        self._mini_edge_right = tk.Canvas(shell, width=self._mini_edge_thick, bg=self._mini_edge_dim, highlightthickness=0, bd=0)
        self._mini_body = tk.Frame(shell, bg="#111214", highlightthickness=0, bd=0)

        shell.grid_rowconfigure(1, weight=1)
        shell.grid_columnconfigure(1, weight=1)

        self._mini_edge_top.grid(row=0, column=0, columnspan=3, sticky="nsew")
        self._mini_edge_left.grid(row=1, column=0, sticky="nsew")
        self._mini_body.grid(row=1, column=1, sticky="nsew")
        self._mini_edge_right.grid(row=1, column=2, sticky="nsew")
        self._mini_edge_bottom.grid(row=2, column=0, columnspan=3, sticky="nsew")

        fr = ttk.Frame(self._mini_body, style="Card.TFrame")
        fr.pack(fill="both", expand=True, padx=10, pady=10)

        self.mini_vars["scan_now"] = tk.StringVar(value="-")
        self.mini_vars["scan_next"] = tk.StringVar(value="-")
        self.mini_vars["trade"] = tk.StringVar(value="-")
        self.mini_vars["upnl3"] = tk.StringVar(value="-")
        self.mini_vars["ls_ratio"] = tk.StringVar(value="-")

        ttk.Label(fr, text="本次扫描时间", style="CardTitle.TLabel").pack(anchor="w", padx=10, pady=(6, 0))
        ttk.Label(fr, textvariable=self.mini_vars["scan_now"], style="Hint.TLabel").pack(anchor="w", padx=10, pady=(2, 0))
        ttk.Label(fr, text="预计下次扫描时间", style="CardTitle.TLabel").pack(anchor="w", padx=10, pady=(6, 0))
        ttk.Label(fr, textvariable=self.mini_vars["scan_next"], style="Hint.TLabel").pack(anchor="w", padx=10, pady=(2, 0))

        ttk.Label(fr, text="交易额 今日/历史", style="CardTitle.TLabel").pack(anchor="w", padx=10, pady=(6, 0))
        ttk.Label(fr, textvariable=self.mini_vars["trade"], style="SubValue.TLabel").pack(anchor="w", padx=10, pady=(2, 0))

        ttk.Label(fr, text="持仓 UPNL 未实现盈亏 前三", style="CardTitle.TLabel").pack(anchor="w", padx=10, pady=(6, 0))
        ttk.Label(fr, textvariable=self.mini_vars["upnl3"], style="Hint.TLabel", wraplength=480, justify="left").pack(anchor="w", padx=10, pady=(2, 8))

        ttk.Label(fr, text="多空比列表", style="CardTitle.TLabel").pack(anchor="w", padx=10, pady=(2, 0))
        ttk.Label(fr, textvariable=self.mini_vars["ls_ratio"], style="Hint.TLabel", wraplength=480, justify="left").pack(anchor="w", padx=10, pady=(2, 6))

        def _on_close():
            try:
                self.mini_win.destroy()
            except Exception:
                pass
            self.mini_win = None
            self.root.deiconify()

        self.mini_win.protocol("WM_DELETE_WINDOW", _on_close)

    def _refresh_loop(self) -> None:
        try:
            data = self.model.refresh()
            self._render(data)
        except Exception as e:
            # 任何异常都写到状态栏（避免“没数据但无原因”）
            msg = f"面板异常：{e}"
            self.var_status.set(msg)
            try:
                h = hashlib.md5(msg.encode('utf-8', errors='ignore')).hexdigest()
                if h != getattr(self, '_last_panel_exc_hash', ''):
                    self._last_panel_exc_hash = h
                    messagebox.showerror("面板异常", msg)
            except Exception:
                pass
        self.root.after(max(200, int(self.model.cfg.refresh_ms or REFRESH_MS)), self._refresh_loop)

    def _maybe_beep(self, data: dict) -> None:
        """只在两种情况下提示音：
        - 下单（已成交/已执行完成）的“新增动作”出现时响一次
        - 报错状态变化（从无→有，或错误文本变化）时响一次

        关键抑制：动作源切换/全量扫描/读到半行 JSON 时，不允许触发下单提示音，避免瞎滴滴。
        """
        now = time.time()
        ev = data.get("events") or {}

        # 1) 报错提示音：只在 error_changed（错误变化）时触发
        if ev.get("error_changed"):
            if now - self._last_error_beep_ts > 1.0:
                _win_beep("error")
                self._last_error_beep_ts = now

        # 2) 下单提示音：只在“新增动作”且非切源/非全量扫描时触发
        added = int(_safe_float(ev.get("added_actions"), 0.0))
        if added <= 0:
            return

        if bool(ev.get("actions_full_scan")) or bool(ev.get("actions_src_switched")):
            # 切源/全量扫描会把历史动作重新“算一遍”，此时绝不响
            return

        rows = data.get("action_rows") or []
        if not rows:
            return
        last = rows[-1] if isinstance(rows, list) else None
        if not isinstance(last, dict):
            return

        # 用“最后一条摘要行”做去重键：避免同一动作因重读/重复落盘而反复响
        key = "|".join([
            str(last.get("ts", "")),
            str(last.get("coin", "")),
            str(last.get("platform", "")),
            str(last.get("module", "")),
            str(last.get("action", "")),
            str(last.get("side", "")),
            str(last.get("notional_u", "")),
        ])

        if key and key == getattr(self, "_last_order_beep_key", ""):
            return

        # 频控：同一秒内最多一次
        if now - self._last_order_beep_ts > 0.6:
            _win_beep("order")
            self._last_order_beep_ts = now
            self._last_order_beep_key = key
    def _queue_signature(self, rows: List[dict]) -> str:
        sig = []
        for r in rows[:2]:
            sig.append(f"{r.get('action','-')}|{r.get('platform','-')}|{r.get('coin','-')}|{int(_safe_float(r.get('notional_u'),0.0))}|{r.get('side','-')}|{r.get('module','-')}")
        return "||".join(sig)

    def _update_queue_banners(self, action_rows: List[dict], queue_rows: Optional[List[dict]] = None) -> None:
        """顶部队列横幅：
        - 优先使用 queue_rows（从全量日志解析的 queued_wait / 队列快照 / 入队）
        - 若 queue_rows 为空，再回退到“最近动作近似”（避免始终空白）
        """
        rows_src: List[dict] = []
        if queue_rows:
            try:
                rows_src = list(queue_rows)[:2]
            except Exception:
                rows_src = []

        # 回退：最近动作近似
        if not rows_src:
            now = time.time()
            recent: List[dict] = []
            for r in reversed(action_rows[-50:]):
                ts = _safe_float(r.get("ts"), 0.0)
                if ts <= 0:
                    continue
                if now - ts <= 180:
                    recent.append(r)
                if len(recent) >= 2:
                    break
            rows_src = list(reversed(recent))[:2]

        def _set_left(line1: str, line2: str) -> None:
            # 本版已移除“队首/下一条”显示；保留函数但不再写入 UI
            return

        def fmt_row(r: dict) -> str:
            action = str(r.get("action") or "-")
            plat = str(r.get("platform") or "-").upper()
            coin = str(r.get("coin") or "-").upper()
            amt = _safe_float(r.get("notional_u"), 0.0)
            side = str(r.get("side") or "-").upper()
            mod = str(r.get("module") or "-").upper()
            # 用“模块/动作”口径展示，避免只看到 EXECUTOR
            modact = f"{mod}/{action}" if (mod != "-" or action != "-") else "-"
            amt_txt = f"{amt:.0f}U" if amt > 0 else "-"
            return f"{modact} | {plat} | {coin} | {amt_txt} | {side}"

        if not rows_src:
            _set_left("队首：无排队动作", "下一条：无排队动作")
            return

        line1 = "队首：" + fmt_row(rows_src[0])
        line2 = "下一条：" + (fmt_row(rows_src[1]) if len(rows_src) > 1 else "无")
        _set_left(line1, line2)


    def _parse_exec_actions_from_log_full(self, log_full: List[str]) -> List[dict]:
        """方式1：从全量文本日志里提取“真实成交/真实执行成功”的动作（宁可漏，不要假）。
        只认强信号：
          - EXEC_DONE / EXEC_OK / ORDER_FILLED / FILLED 且明确 ok/filled>0
          - 不把 ACCEPT/排队/计划/GO_* 当成交
        返回 dict 字段（供摘要渲染使用）：
          ts, time, coin, plat, module, action, order_side(buy/sell), dir(long/short), amount_u, profit_u
        """
        out: List[dict] = []

        # 指标缓存更新（用于摘要逐笔 RSI/ADX）
        try:
            _md = getattr(self, "model", None)
            _ensure_ind_sources_loaded(getattr(_md, "out_dir", None), getattr(_md, "logs_dir", None))
        except Exception:
            pass
        if not isinstance(log_full, list) or not log_full:
            return out

        def _ts_from_line(line: str) -> float:
            s = str(line or "")
            try:
                m = re.search(r"\[(\d{9,}(?:\.\d+)?)\]", s)
                if m:
                    return float(m.group(1))
            except Exception:
                pass
            try:
                m = re.search(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", s)
                if m:
                    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
            except Exception:
                pass
            # 兜底：仅有 HH:MM:SS 的情况（用今天日期拼）
            try:
                m = re.search(r"\b(\d{2}:\d{2}:\d{2})\b", s)
                if m:
                    hh = m.group(1)
                    today = datetime.now()
                    dt = datetime.strptime(today.strftime("%Y-%m-%d") + " " + hh, "%Y-%m-%d %H:%M:%S")
                    return dt.timestamp()
            except Exception:
                pass
            return 0.0

        def _pick_float(pat: str, s: str) -> Optional[float]:
            try:
                m = re.search(pat, s, re.I)
                if m:
                    return float(m.group(1))
            except Exception:
                return None
            return None

        def _pick_str(pat: str, s: str) -> str:
            try:
                m = re.search(pat, s, re.I)
                if m:
                    return (m.group(1) or "").strip()
            except Exception:
                return ""
            return ""

        def _norm_side(s: str) -> str:
            s = (s or "").strip().lower()
            if s in ("buy", "b", "long_buy"):
                return "buy"
            if s in ("sell", "s", "short_sell"):
                return "sell"
            return ""

        def _norm_dir(s: str) -> str:
            s = (s or "").strip().lower()
            if s in ("long", "l", "多"):
                return "long"
            if s in ("short", "s", "空"):
                return "short"
            return ""

        def _infer_dir(order_side: str, act: str) -> str:
            a = (act or "").lower()
            if not order_side:
                return ""
            if "open" in a or "increase" in a or "add" in a:
                return "long" if order_side == "buy" else "short"
            if "close" in a or "reduce" in a or "decrease" in a:
                # 平仓：buy 通常是平空，sell 通常是平多
                return "short" if order_side == "buy" else "long"
            return ""

        # 反向扫：只拿最近 N 条（够摘要了）
        take = 2200
        tail = log_full[-take:] if len(log_full) > take else log_full

        for line in reversed(tail):
            s = str(line or "")
            # 必须包含“执行成功”的强信号之一
            if not (("EXEC_DONE" in s) or ("EXEC_OK" in s) or ("ORDER_FILLED" in s) or ("FILLED" in s) or ("已执行" in s)):
                continue
            # 排除明显非成交
            if ("ACCEPT" in s) or ("GO_ACCEPT" in s) or ("enqueue" in s.lower()) or ("QUEUE" in s):
                continue

            ts = _ts_from_line(s)

            # ok/filled 判定
            ok_cnt = _pick_float(r"\bok_cnt\s*=\s*(\d+)", s)
            filled_cnt = _pick_float(r"\bfilled_cnt\s*=\s*(\d+)", s)
            ok_bool = _pick_str(r"\bok\s*=\s*(true|false)", s).lower()
            status = _pick_str(r"\bstatus\s*=\s*([A-Za-z_]+)", s).lower()

            is_ok = False
            try:
                if ok_cnt is not None and int(ok_cnt) > 0:
                    is_ok = True
            except Exception:
                pass
            try:
                if (not is_ok) and (filled_cnt is not None) and int(filled_cnt) > 0:
                    is_ok = True
            except Exception:
                pass
            if (not is_ok) and ok_bool == "true":
                is_ok = True
            if (not is_ok) and status in ("filled", "executed", "done", "success", "ok"):
                is_ok = True
            if not is_ok:
                continue

            coin = (_pick_str(r"\bcoin\s*=\s*([A-Z0-9_]+)", s) or "").upper()
            plat = (_pick_str(r"\bplat(?:form)?\s*=\s*([A-Z]+)", s) or "").upper()

            # pipe 风格补齐 coin/plat（例如：BNB | EXT | ...）
            if not coin:
                m = re.search(r"\b([A-Z0-9_]{2,12})\b\s*\|", s)
                if m:
                    coin = m.group(1).upper()
            if not plat:
                m = re.search(r"\|\s*(EXT|VAR)\b", s, re.I)
                if m:
                    plat = m.group(1).upper()

            module = _pick_str(r"\bmodule\s*=\s*([A-Za-z0-9_]+)", s)
            action = _pick_str(r"\baction\s*=\s*([A-Za-z0-9_\-]+)", s)
            if not module:
                module = _pick_str(r"\bmod\s*=\s*([A-Za-z0-9_]+)", s)
            if not action:
                action = _pick_str(r"\bact\s*=\s*([A-Za-z0-9_\-]+)", s)

            order_side = _norm_side(_pick_str(r"\bside\s*=\s*(buy|sell)\b", s))
            # 有些日志用 BUY/SELL
            if not order_side:
                if re.search(r"\bBUY\b", s):
                    order_side = "buy"
                elif re.search(r"\bSELL\b", s):
                    order_side = "sell"

            # 金额
            amt = _pick_float(r"(\d+(?:\.\d+)?)\s*U（金额）", s)
            if amt is None:
                amt = _pick_float(r"\busd\s*=\s*(\d+(?:\.\d+)?)", s)
            if amt is None:
                amt = _pick_float(r"\bamount(?:_u)?\s*=\s*(\d+(?:\.\d+)?)", s)
            if amt is None:
                amt = 0.0

            # 利润：没有真实字段时保持 None，不能直接写 0.0，
            # 否则后续合并时会把“未知利润”误当成“真实 0 利润”。
            profit = _pick_float(r"\b(?:profit_u|pnl_u|realized_pnl_u|rpnl|net_u|grid_profit_u|net_profit_u)\s*=\s*(-?\d+(?:\.\d+)?)", s)

            dir0 = _norm_dir(_pick_str(r"\bdir\s*=\s*(long|short)\b", s))
            if not dir0:
                dir0 = _infer_dir(order_side, action)

            # 最后防守：必须有 coin/plat/金额>0 才入摘要
            if (not coin) or (not plat) or float(amt or 0.0) <= 0.0:
                continue

            trace_id = (_pick_str(r"\btrace(?:_id)?\s*=\s*([A-Za-z0-9_\-|:.]+)", s) or "").strip()
            req_id = (_pick_str(r"\b(?:req_id|rid|trade_id)\s*=\s*([A-Za-z0-9_\-]+)", s) or "").strip()
            layer_tag = (_pick_str(r"\blayer(?:_tag)?\s*=\s*([A-Za-z0-9_\-]+)", s) or "").strip()

            out.append(
                {
                    "ts": ts,
                    "time": (datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts > 0 else "--"),
                    "coin": coin,
                    "plat": plat,
                    "module": (module or "-"),
                    "action": (action or "-"),
                    "order_side": order_side,
                    "dir": dir0,
                    "amount_u": float(abs(amt)),
                    "price": _pick_float(r"\b(?:price|px|cur_px|close_price|mark|mark_px)\s*=\s*(-?\d+(?:\.\d+)?)", s),
                    "profit_u": (float(profit) if profit is not None else None),
                    "rsi": _lookup_rsi_adx_at(coin, ts)[0],
                    "adx": _lookup_rsi_adx_at(coin, ts)[1],
                    "trace_id": trace_id,
                    "req_id": req_id,
                    "layer_tag": layer_tag,
                    "source": "log_full",
                }
            )
            # 只拿最新若干条
            if len(out) >= 200:
                break

        return out



    def _collect_structured_exec_source_files(self, trades_only: bool = False) -> List[Path]:
        """收集结构化成交来源文件。

        trades_only=True 时只收 trades.jsonl，供“全历史第几次”统计使用；
        False 时保持原来的多来源解析口径。
        """
        fps: List[Path] = []
        try:
            md = getattr(self, "model", None)
            if md:
                od = getattr(md, "out_dir", None)
                if od:
                    if trades_only:
                        names = ("trades.jsonl",)
                    else:
                        names = (
                            "trades.jsonl",
                            "executor_results.jsonl",
                            "executor_result.jsonl",
                            "exec_results.jsonl",
                            "trace_events.jsonl",
                            "telemetry.jsonl",
                        )
                    for fn in names:
                        p = Path(od) / fn
                        if p.exists() and p.stat().st_size > 0:
                            fps.append(p)

                ld = getattr(md, "logs_dir", None)
                if ld:
                    if trades_only:
                        names = ("trades.jsonl",)
                    else:
                        names = (
                            "executor_results.jsonl",
                            "exec_results.jsonl",
                            "trades.jsonl",
                        )
                    for fn in names:
                        p = Path(ld) / fn
                        if p.exists() and p.stat().st_size > 0:
                            fps.append(p)
                fp_trades = getattr(md, "fp_trades", None)
                if fp_trades:
                    p = Path(fp_trades)
                    if p.exists() and p.stat().st_size > 0:
                        fps.append(p)
        except Exception:
            pass

        try:
            if trades_only:
                names = ("trades.jsonl",)
            else:
                names = ("executor_results.jsonl", "exec_results.jsonl", "trades.jsonl")
            tmp = Path(tempfile.gettempdir()) / "bot_panel_bridge" / "out"
            for fn in names:
                p = tmp / fn
                if p.exists() and p.stat().st_size > 0:
                    fps.append(p)
        except Exception:
            pass

        uniq: List[Path] = []
        seen: set[str] = set()
        for p in fps:
            try:
                pp = str(Path(p).resolve())
            except Exception:
                pp = str(p)
            if pp in seen:
                continue
            seen.add(pp)
            uniq.append(Path(p))
        return uniq


    def _summary_occurrence_meta_from_row(self, row: dict) -> Optional[Dict[str, Any]]:
        try:
            plat0 = str(row.get("plat") or row.get("platform") or row.get("platf") or "").upper().strip()
            coin0 = str(row.get("coin") or "").upper().strip()
            module0 = str(row.get("module") or "-").strip() or "-"
            action0 = str(row.get("action") or "-").strip() or "-"
            oc0 = str(row.get("oc") or "").strip()
            order_side0 = (str(row.get("order_side") or "") or "").lower().strip()
            dir0 = (str(row.get("dir") or row.get("side") or row.get("pos_side") or "") or "").lower().strip()
            if dir0 in ('LONG', 'SHORT', 'Long', 'Short'):
                dir0 = dir0.lower()
            elif dir0 in ('buy', 'sell'):
                dir0 = ''
            amt0 = float(_safe_float(row.get("amount_u"), 0.0) or 0.0)
            ts0 = float(_safe_float(row.get("ts"), 0.0) or 0.0)
            if (not plat0) or (not coin0) or amt0 <= 0:
                return None

            req_key0 = str(row.get("req_id") or row.get("trade_id") or "").strip()
            trace_key0 = str(row.get("trace_id") or row.get("trace") or row.get("req_trace_id") or "").strip()
            layer_tag0 = str(row.get("layer_tag") or row.get("grid_layer") or "").strip()
            step_idx0 = str(row.get("step_idx") or "").strip()
            if req_key0:
                sig0 = f"req|{req_key0}|{plat0}|{coin0}|{module0}|{action0}|{oc0}|{round(amt0, 2):.2f}|{layer_tag0}|{step_idx0}"
            elif trace_key0:
                sig0 = f"trace|{trace_key0}|{plat0}|{coin0}|{module0}|{action0}|{oc0}|{round(amt0, 2):.2f}|{layer_tag0}|{step_idx0}"
            else:
                bucket0 = int(ts0 // 10) if ts0 > 0 else 0
                sig0 = f"{bucket0}|{plat0}|{coin0}|{module0}|{action0}|{oc0}|{round(amt0, 2):.2f}|{layer_tag0}|{step_idx0}"

            if str(row.get('status') or '').upper() == 'ACCEPT':
                act_cn0 = '尝试'
                order_side0 = None
            else:
                act_cn0 = oc0 if oc0 else ("开仓" if order_side0 == "buy" else ("平仓" if order_side0 == "sell" else (action0 if action0 else "-")))
            if not order_side0:
                oc_u0 = str(oc0 or "").strip()
                if oc_u0 in ("开仓", "open"):
                    if dir0 == "long":
                        order_side0 = "buy"
                    elif dir0 == "short":
                        order_side0 = "sell"
                elif oc_u0 in ("平仓", "close"):
                    if dir0 == "long":
                        order_side0 = "sell"
                    elif dir0 == "short":
                        order_side0 = "buy"
            if not act_cn0:
                act_cn0 = '-'

            mod_cn0 = _decorate_summary_module_action(module0, action0, act_cn0, row)
            occ_key0 = f"{coin0}|{plat0}|{mod_cn0}"
            return {'sig': sig0, 'occ_key': occ_key0}
        except Exception:
            return None


    def _build_summary_occurrence_index_from_rows(self, exec_rows: List[dict]) -> Dict[str, int]:
        try:
            rows = sorted(
                list(exec_rows or []),
                key=lambda x: float(_safe_float((x or {}).get("ts"), 0.0) or 0.0),
            )
        except Exception:
            rows = list(exec_rows or [])
        seen_occ_sigs = set()
        occ_counter: Dict[str, int] = defaultdict(int)
        summary_occ_idx_by_sig: Dict[str, int] = {}
        for row in rows:
            meta = self._summary_occurrence_meta_from_row(row)
            if not meta:
                continue
            sig0 = str(meta.get('sig') or '')
            occ_key0 = str(meta.get('occ_key') or '')
            if (not sig0) or (not occ_key0) or (sig0 in seen_occ_sigs):
                continue
            seen_occ_sigs.add(sig0)
            occ_counter[occ_key0] += 1
            summary_occ_idx_by_sig[sig0] = int(occ_counter[occ_key0])
        return summary_occ_idx_by_sig


    def _get_full_history_summary_occ_idx(self) -> Dict[str, int]:
        try:
            # 关键修复：
            # “第N次”必须和摘要展示走同一条真相链，不能再出现
            #   展示链 = executor_results / trades / 其它结构化来源
            #   计数链 = 只看 trades.jsonl
            # 这种上下游不一致的问题。
            # 同时这里要拿“全历史”，因此后面会触发结构化来源的全量读取。
            fps = self._collect_structured_exec_source_files(trades_only=False)
            cache_key_parts = []
            for fp in fps:
                try:
                    st = fp.stat()
                    cache_key_parts.append(f"{fp.resolve()}|{int(st.st_size)}|{int(getattr(st, 'st_mtime_ns', int(st.st_mtime * 1_000_000_000)))}")
                except Exception:
                    cache_key_parts.append(str(fp))
            cache_key = '||'.join(cache_key_parts)
            cache = getattr(self, '_summary_occ_full_history_cache', None)
            if isinstance(cache, dict) and cache.get('key') == cache_key:
                cached_idx = cache.get('idx')
                if isinstance(cached_idx, dict):
                    return dict(cached_idx)

            full_rows = self._parse_exec_actions_from_structured_sources(max_rows=None, trades_only=False)
            full_rows = self._merge_exec_rows([], full_rows)
            full_rows = self._canonicalize_summary_exec_rows(full_rows)
            idx = self._build_summary_occurrence_index_from_rows(full_rows)
            self._summary_occ_full_history_cache = {'key': cache_key, 'idx': dict(idx)}
            return idx
        except Exception:
            return {}


    def _parse_exec_actions_from_structured_sources(self, max_rows: Optional[int] = 260, trades_only: bool = False) -> List[dict]:
        """方式2：从结构化 jsonl 来源解析“真实通过执行器并完成”的成交记录。

        兼容你当前 out/trades.jsonl 的字段（trade_id/req_id/ts/platform/coin/side/amount_u...）。
        原则：宁可漏，不要把“计划/排队/ACCEPT/DRY_RUN”当成交。
        """
        out: List[dict] = []

        uniq = self._collect_structured_exec_source_files(trades_only=trades_only)

        # 指标缓存更新（用于总览/摘要 RSI、ADX 展示）
        try:
            _md = getattr(self, "model", None)
            _ensure_ind_sources_loaded(getattr(_md, "out_dir", None), getattr(_md, "logs_dir", None))
        except Exception:
            pass

        def _is_true_exec_row(d: dict) -> bool:
            """判定是否为“真实执行完成”的记录。"""
            try:
                if not isinstance(d, dict):
                    return False

                # 明确跳过
                if d.get("skipped") is True:
                    return False
                if d.get("dry_run") is True or d.get("dry_plan") is True:
                    return False
                if isinstance(d.get("req"), dict):
                    if d["req"].get("dry_run") is True or d["req"].get("dry_plan") is True:
                        return False

                # 你当前 trades.jsonl：有 trade_id 基本可视为“已执行完成”
                if d.get("trade_id") is not None:
                    # 金额必须>0（避免空记录）
                    au = _extract_amount_u_from_row(d)
                    if au > 0:
                        return True

                # 通用强信号：status/ok/filled/event/type
                st = str(d.get("status") or d.get("state") or "").lower()
                if any(k in st for k in ("filled", "executed", "done", "success", "ok")):
                    return True
                if d.get("ok") is True or d.get("exec_ok") is True or d.get("executed") is True:
                    return True

                # filled 口径：filled_qty/filled_u/filled_notional 等
                for k in ("filled", "filled_qty", "filled_u", "filled_notional", "exec_u", "notional_u"):
                    v = _safe_float(d.get(k), 0.0)
                    if v and abs(v) > 0:
                        # 但必须同时有平台/币/方向，避免把无关流水当成交
                        if d.get("platform") or d.get("plat") or (isinstance(d.get("req"), dict) and d["req"].get("platform")):
                            return True

                et = str(d.get("event") or d.get("type") or "").lower()
                if any(k in et for k in ("exec_done", "exec_ok", "order_filled", "filled", "executor_done")):
                    return True
            except Exception:
                return False
            return False

        def _get_ts(d: dict) -> float:
            for k in ("ts", "t_sec", "t", "time", "timestamp", "ts_ms", "time_ms", "ts_us", "time_us"):
                v = d.get(k)
                ts = _parse_ts_sec_any(v)
                if ts > 0:
                    return ts
            if isinstance(d.get("req"), dict):
                for k in ("ts", "t_sec", "time", "timestamp"):
                    ts = _parse_ts_sec_any(d["req"].get(k))
                    if ts > 0:
                        return ts
            return 0.0

        def _get_field(d: dict, *ks: str) -> Any:
            for k in ks:
                if k in d and d.get(k) is not None:
                    return d.get(k)
            req = d.get("req")
            if isinstance(req, dict):
                for k in ks:
                    if k in req and req.get(k) is not None:
                        return req.get(k)
                params = req.get("params")
                if isinstance(params, dict):
                    for k in ks:
                        if k in params and params.get(k) is not None:
                            return params.get(k)
                    params2 = params.get("params")
                    if isinstance(params2, dict):
                        for k in ks:
                            if k in params2 and params2.get(k) is not None:
                                return params2.get(k)
            return None

        def _is_postcheck_confirmed_row(d: dict) -> bool:
            try:
                if not isinstance(d, dict):
                    return False
                boxes = [d]
                req = d.get('req') if isinstance(d.get('req'), dict) else {}
                if isinstance(req, dict):
                    boxes.append(req)
                    params = req.get('params') if isinstance(req.get('params'), dict) else {}
                    if isinstance(params, dict):
                        boxes.append(params)
                        params2 = params.get('params') if isinstance(params.get('params'), dict) else {}
                        if isinstance(params2, dict):
                            boxes.append(params2)
                    meta = req.get('meta') if isinstance(req.get('meta'), dict) else {}
                    if isinstance(meta, dict):
                        boxes.append(meta)
                deny_terms = (
                    'batch_postcheck_deferred', 'batch_verify_deferred', 'pending_confirm', 'submitted_pending_confirm',
                    'pending_unconfirmed', 'unconfirmed', 'deferred', 'submit_fail', 'reject', 'rejected',
                    'timeout', 'error', 'exception', 'fail', 'failed'
                )
                allow_terms = (
                    'postcheck_position_delta_ok', 'postcheck_trade_fill_ok', 'postcheck_ok', 'position_delta_ok',
                    'confirmed_success', 'confirmed_ok', 'fill_seen_and_confirmed', 'fill_confirmed', 'postcheck_confirmed'
                )
                texts = []
                confirmed_success = False
                for box in boxes:
                    if not isinstance(box, dict):
                        continue
                    if bool(box.get('confirmed_success')):
                        confirmed_success = True
                    for k in ('reason', 'status', 'state', 'display_state', 'confirmation_state', 'status_label', 'event', 'type', 'message', 'msg', 'postcheck_reason'):
                        v = box.get(k)
                        if v is None:
                            continue
                        texts.append(str(v).strip().lower())
                if any(any(term in txt for term in deny_terms) for txt in texts if txt):
                    return False
                if confirmed_success:
                    return True
                if any(txt in ('confirmed', 'postcheck_ok', 'position_delta_ok', 'confirmed_success') for txt in texts if txt):
                    return True
                if any(any(term in txt for term in allow_terms) for txt in texts if txt):
                    return True
                return False
            except Exception:
                return False

        def _infer_dir(action: str, order_side: str) -> str:
            a = str(action or "").lower()
            osd = str(order_side or "").lower()
            if "open" in a or "increase" in a or "add" in a:
                return "long" if osd == "buy" else "short"
            if "close" in a or "reduce" in a or "decrease" in a:
                return "short" if osd == "buy" else "long"
            # 兜底：buy=long, sell=short
            if osd == "buy":
                return "long"
            if osd == "sell":
                return "short"
            return ""

        def _extract_steps_from_row(d: dict) -> List[dict]:
            try:
                req0 = d.get("req") if isinstance(d.get("req"), dict) else {}
                if not isinstance(req0, dict):
                    return []
                p0 = req0.get("params") if isinstance(req0.get("params"), dict) else {}
                cand = []
                if isinstance(p0, dict):
                    cand.append(p0.get("steps"))
                    p0_meta = p0.get("meta") if isinstance(p0.get("meta"), dict) else {}
                    if isinstance(p0_meta, dict):
                        p0_extras = p0_meta.get("extras") if isinstance(p0_meta.get("extras"), dict) else {}
                        if isinstance(p0_extras, dict):
                            cand.append(p0_extras.get("steps"))
                req_meta = req0.get("meta") if isinstance(req0.get("meta"), dict) else {}
                if isinstance(req_meta, dict):
                    req_extras = req_meta.get("extras") if isinstance(req_meta.get("extras"), dict) else {}
                    if isinstance(req_extras, dict):
                        cand.append(req_extras.get("steps"))
                for steps in cand:
                    if isinstance(steps, list) and steps:
                        return [x for x in steps if isinstance(x, dict)]
            except Exception:
                return []
            return []

        def _step_amount_u(stp: dict, parent: dict) -> float:
            try:
                for k in (
                    "target_notional_u", "notional_u", "notional_usd", "usd", "amount_usd", "amount_u",
                    "value_usd", "value_u", "close_notional_u", "close_notional", "exec_u"
                ):
                    v = abs(_safe_float(stp.get(k), 0.0) or 0.0)
                    if v > 0:
                        return float(v)
                meta = stp.get("meta") if isinstance(stp.get("meta"), dict) else {}
                if isinstance(meta, dict):
                    for k in (
                        "win_notional_u", "loss_notional_u", "target_notional_u", "m10_step_std_amt_final",
                        "m1_std_amt_final", "tp_step_std_amt_final", "close_notional_u", "close_notional"
                    ):
                        v = abs(_safe_float(meta.get(k), 0.0) or 0.0)
                        if v > 0:
                            return float(v)
                v = abs(_safe_float(_extract_amount_u_from_row(stp), 0.0) or 0.0)
                if v > 0:
                    return float(v)
                v = abs(_safe_float(_extract_amount_u_from_row(parent), 0.0) or 0.0)
                if v > 0:
                    return float(v)
            except Exception:
                return 0.0
            return 0.0

        def _step_pos_side(stp: dict, parent: dict) -> str:
            try:
                for src in (stp, stp.get("meta") if isinstance(stp.get("meta"), dict) else {}, parent, parent.get("req") if isinstance(parent.get("req"), dict) else {}):
                    if not isinstance(src, dict):
                        continue
                    v = str(src.get("side") or src.get("side_hint") or src.get("position_side") or "").upper().strip()
                    if v in ("LONG", "SHORT"):
                        return v
                rawx = str(stp.get("raw") or parent.get("raw") or "")
                mm_side = re.search(r"\bside\s*=?\s*(LONG|SHORT)\b", rawx, flags=re.I)
                if mm_side:
                    return mm_side.group(1).upper()
                low = rawx.lower()
                if ("方向）=buy" in rawx) or ("side=buy" in low) or (" side = buy" in low):
                    return "LONG"
                if ("方向）=sell" in rawx) or ("side=sell" in low) or (" side = sell" in low):
                    return "SHORT"
            except Exception:
                return ""
            return ""

        # 2) 读取结构化来源，拼成 rows
        # max_rows is None 表示要做“全历史统计”，这里必须真的全量读文件；
        # 不能再只读尾部 1600 行，否则更早的历史次数会被吃掉。
        for fp in uniq:
            try:
                if max_rows is None:
                    try:
                        with Path(fp).open('r', encoding='utf-8', errors='ignore') as _f:
                            lines = list(_f)
                    except Exception:
                        lines = []
                else:
                    lines = _tail_lines(fp, max_lines=1600)
                if not lines:
                    continue
                for ln in reversed(lines):
                    s = (ln or "").strip()
                    if not s:
                        continue
                    if not s.startswith("{"):
                        continue
                    try:
                        d = json.loads(s)
                    except Exception:
                        continue
                    if not isinstance(d, dict):
                        continue

                    if not _is_true_exec_row(d):
                        continue
                    if not _is_postcheck_confirmed_row(d):
                        continue
                    ts = _get_ts(d)
                    # executor_result.jsonl：字段主要在 d['req'] 与 d['req']['params']['params']
                    req = d.get("req") if isinstance(d.get("req"), dict) else None
                    req_params = (req.get("params") if isinstance(req, dict) and isinstance(req.get("params"), dict) else {})
                    inner = (req_params.get("params") if isinstance(req_params.get("params"), dict) else {})

                    coin = str((_get_field(req or {}, "coin") or _get_field(d, "coin", "symbol") or "-")).upper()
                    plat = _norm_platform((_get_field(req or {}, "platform", "plat") or _get_field(d, "platform", "plat") or "-"))
                    module = str((_get_field(req or {}, "module", "mod") or _get_field(d, "module", "mod") or "-"))
                    action = str((_get_field(req or {}, "action", "action_type", "act") or _get_field(d, "action", "action_type", "act") or "-"))

                    # 方向/买卖推断：优先用 side(LONG/SHORT)，再结合模块语义推断 buy/sell
                    pos_side = str((_get_field(inner, "side") or _get_field(req_params, "side") or _get_field(req or {}, "side_hint", "side")
                                    or _get_field(d, "side_hint", "position_side", "side") or "")).upper().strip()

                    def _infer_order_side(module_x: str, action_x: str, order_side_x: str = "") -> str:
                        osd = str(order_side_x or "").lower().strip()
                        al = str(action_x or "").lower()
                        ml = str(module_x or "").upper()
                        open_like = any(k in al for k in ("increase", "open", "add", "grid_add"))
                        close_like = any(k in al for k in ("decrease", "close", "reduce", "tp", "takeprofit", "grid_sell"))
                        if osd in ("buy", "sell"):
                            return osd
                        # 先信 action 语义；只有 action 本身不明确时，才退回模块默认口径。
                        if open_like and not close_like:
                            return "buy"
                        if close_like and not open_like:
                            return "sell"
                        if ml in ("B1", "B13", "M9", "W1", "X1"):
                            return "buy"
                        if ml in ("B2", "M10", "Z1", "S1"):
                            return "sell"
                        return ""

                    order_side = _infer_order_side(module, action, str((_get_field(d, "order_side", "side") or "")).lower().strip())

                    steps = _extract_steps_from_row(d)
                    if str(plat).upper() == "GROUP" and str(module).upper() == "M10" and isinstance(steps, list) and steps:
                        parent_trace = str(d.get("trace_id") or d.get("trace") or d.get("req_trace_id") or req.get("trace_id") or "").strip()
                        parent_profit = _extract_profit_from_row(d)
                        parent_rsi, parent_adx = _extract_rsi_adx_from_row(d, coin, ts)
                        for idx, stp in enumerate(steps, 1):
                            try:
                                s_plat = _norm_platform(stp.get("platform") or "-")
                                if s_plat not in ("EXT", "VAR"):
                                    continue
                                s_action = str(stp.get("action") or action or "-").strip() or "-"
                                s_order_side = _infer_order_side(module, s_action, str(stp.get("order_side") or stp.get("side") or "").lower().strip())
                                if s_order_side not in ("buy", "sell"):
                                    continue
                                s_amt = float(_step_amount_u(stp, d))
                                if s_amt <= 0:
                                    continue
                                s_pos_side = _step_pos_side(stp, d)
                                s_price = _extract_price_from_row(stp)
                                if s_price is None:
                                    s_price = _extract_price_from_row(d)
                                s_profit = _extract_profit_from_row(stp)
                                if s_profit is None and parent_profit is not None and ("close" in s_action.lower() or "decrease" in s_action.lower() or "reduce" in s_action.lower()):
                                    s_profit = parent_profit
                                s_rsi, s_adx = _extract_rsi_adx_from_row(stp, coin, ts)
                                if s_rsi is None:
                                    s_rsi = parent_rsi
                                if s_adx is None:
                                    s_adx = parent_adx
                                out.append(
                                    {
                                        "ts": ts,
                                        "time": (datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts > 0 else "--"),
                                        "coin": coin,
                                        "plat": s_plat,
                                        "module": (module or "-"),
                                        "action": s_action,
                                        "oc": ("平仓" if ("close" in s_action.lower() or "decrease" in s_action.lower() or "reduce" in s_action.lower() or "tp" in s_action.lower()) else "开仓"),
                                        "order_side": s_order_side,
                                        "dir": ("long" if s_pos_side == "LONG" else ("short" if s_pos_side == "SHORT" else _infer_dir(s_action, s_order_side))),
                                        "amount_u": s_amt,
                                        "price": s_price,
                                        "profit_u": (float(s_profit) if (s_profit is not None) else None),
                                        "rsi": s_rsi,
                                        "adx": s_adx,
                                        "trace_id": (f"{parent_trace}|step{idx}|{s_plat}|{s_action}|{s_amt:.2f}" if parent_trace else f"m10step|{int(ts//10) if ts>0 else 0}|{coin}|{s_plat}|{s_action}|{s_amt:.2f}|{idx}"),
                                        "parent_trace_id": parent_trace,
                                        "source": "structured_step",
                                        "req": req,
                                        "step_idx": idx,
                                        "layer_tag": stp.get("layer_tag") or d.get("layer_tag"),
                                        "layer_no": stp.get("layer_no") or d.get("layer_no"),
                                        "layer_id": stp.get("layer_id") or d.get("layer_id"),
                                        "grid_action_label": stp.get("grid_action_label") or d.get("grid_action_label"),
                                    }
                                )
                                if (max_rows is not None) and len(out) >= int(max_rows):
                                    break
                            except Exception:
                                continue
                        if (max_rows is not None) and len(out) >= int(max_rows):
                            break
                        continue

                    # 只显示“可明确归类为买/卖”的执行器动作（避免 GROUP/口径动作混进摘要）
                    if order_side not in ("buy", "sell"):
                        continue

                    amt_u = float(abs(_extract_amount_u_from_row(d)))
                    if amt_u <= 0:
                        continue

                    _profit0 = _extract_profit_from_row(d)
                    # 关键修复：未知利润保留 None，不要强行写 0.0；
                    # 否则平仓/减仓动作会被摘要错误显示成 0。
                    profit_u = (float(_profit0) if (_profit0 is not None) else None)
                    price_v = _extract_price_from_row(d)

                    rsi_v, adx_v = _extract_rsi_adx_from_row(d, coin, ts)
                    _req_meta0 = req.get("meta") if isinstance(req.get("meta"), dict) else {}
                    _step_idx0 = _get_field(d, "step_no", "m10_step_no")
                    if _step_idx0 in (None, "", 0, "0") and isinstance(_req_meta0, dict):
                        _step_idx0 = _req_meta0.get("m10_step_no")
                    _req_id0 = str(_get_field(d, "req_id") or (_req_meta0.get("req_id") if isinstance(_req_meta0, dict) else "") or "").strip()
                    _trace_id0 = str(d.get("trace_id") or d.get("trace") or d.get("req_trace_id") or (_req_meta0.get("trace_id") if isinstance(_req_meta0, dict) else "") or "").strip()
                    out.append(
                        {
                            "ts": ts,
                            "time": (datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts > 0 else "--"),
                            "coin": coin,
                            "plat": plat,
                            "module": (module or "-"),
                            "action": (action or "-"),
                            "oc": ("平仓" if ("close" in str(action).lower() or "decrease" in str(action).lower() or "reduce" in str(action).lower() or "tp" in str(action).lower()) else "开仓"),
                            "order_side": order_side,
                            "dir": ("long" if pos_side == "LONG" else ("short" if pos_side == "SHORT" else _infer_dir(action, order_side))),
                            "amount_u": amt_u,
                            "price": price_v,
                            "profit_u": profit_u,
                            "rsi": rsi_v,
                            "adx": adx_v,
                            "req_id": _req_id0,
                            "trace_id": _trace_id0,
                            "parent_trace_id": str((_req_meta0.get("m10_parent_trace_id") if isinstance(_req_meta0, dict) else "") or "").strip(),
                            "step_idx": _step_idx0,
                            "req": req,
                            "layer_tag": d.get("layer_tag") or _get_field(d, "layer_tag", "grid_layer"),
                            "layer_no": d.get("layer_no") or _get_field(d, "layer_no"),
                            "layer_id": d.get("layer_id") or _get_field(d, "layer_id"),
                            "grid_action_label": d.get("grid_action_label") or _get_field(d, "grid_action_label"),
                            "source": "structured",
                        }
                    )
                    if (max_rows is not None) and len(out) >= int(max_rows):
                        break
            except Exception:
                continue

        out.sort(key=lambda x: float(_safe_float(x.get("ts"), 0.0) or 0.0), reverse=True)
        if max_rows is not None:
            try:
                return out[:max(0, int(max_rows))]
            except Exception:
                return out
        return out


    def _merge_exec_rows(self, rows_a: List[dict], rows_b: List[dict]) -> List[dict]:
        """合并两路来源（方式1+方式2），并做强去重（避免同一成交重复显示）。
        同时优先保留“信息更完整”的那条：真实利润 > 仅有价格 > 空利润占位。
        """
        rows_a = rows_a if isinstance(rows_a, list) else []
        rows_b = rows_b if isinstance(rows_b, list) else []
        all_rows = rows_a + rows_b

        def _is_close_like(r: dict) -> bool:
            a = str(r.get("action") or r.get("action_type") or "").lower()
            m = str(r.get("module") or "").upper()
            return (any(k in a for k in ("decrease", "reduce", "close", "tp", "takeprofit", "grid_sell", "grid_reduce"))
                    or m in ("B2", "M10", "Z1", "S1"))

        def _sig_of(r: dict):
            coin = str(r.get("coin") or "").upper()
            plat = str(r.get("plat") or r.get("platform") or "").upper()
            module = str(r.get("module") or "-")
            action = str(r.get("action") or "-")
            osd = str(r.get("order_side") or "")
            amt = float(_safe_float(r.get("amount_u"), None) or _safe_float(r.get("notional_u"), None) or _safe_float(r.get("notional"), None) or 0.0)
            ts0 = float(_safe_float(r.get("ts"), 0.0) or 0.0)
            req_key = str(r.get("req_id") or r.get("trade_id") or "").strip()
            trace_key = str(r.get("trace_id") or r.get("trace") or r.get("req_trace_id") or "").strip()
            layer_tag = str(r.get("layer_tag") or r.get("grid_layer") or "").strip()
            step_idx = str(r.get("step_idx") or "").strip()
            parent_trace = str(r.get("parent_trace_id") or "").strip()
            is_m10 = str(module or "").upper().startswith("M10")
            if is_m10 and step_idx:
                anchor = req_key or parent_trace or trace_key or f"{int(ts0 // 10) if ts0 > 0 else 0}:{coin}:{plat}:{action}:{round(amt, 2)}"
                return ("m10_step", anchor, plat, coin, module, action, osd, round(amt, 2), layer_tag, step_idx)
            if req_key:
                return ("req", req_key, plat, coin, module, action, osd, round(amt, 2), layer_tag, step_idx)
            if trace_key:
                return ("trace", trace_key, plat, coin, module, action, osd, round(amt, 2), layer_tag, step_idx)
            bucket = int(ts0 // 10) if ts0 > 0 else 0
            return (bucket, plat, coin, module, action, osd, round(amt, 2), layer_tag, step_idx)

        def _score(r: dict):
            profit = _safe_float(r.get("profit_u"), None)
            price = _safe_float(r.get("price"), None)
            rsi = _safe_float(r.get("rsi"), None)
            adx = _safe_float(r.get("adx"), None)
            ts0 = float(_safe_float(r.get("ts"), 0.0) or 0.0)
            return (
                3 if (_is_close_like(r) and profit is not None) else 0,
                1 if price is not None else 0,
                1 if rsi is not None else 0,
                1 if adx is not None else 0,
                1 if str(r.get("source") or "").lower().startswith("structured") else 0,
                ts0,
            )

        merged = {}
        for r in sorted(all_rows, key=lambda x: float(_safe_float(x.get("ts"), 0.0) or 0.0), reverse=True):
            try:
                sig = _sig_of(r)
                old = merged.get(sig)
                if old is None:
                    merged[sig] = dict(r)
                    continue
                cand = dict(old)
                for k in ("price", "profit_u", "rsi", "adx", "dir", "order_side", "module", "action", "coin", "plat", "time"):
                    if cand.get(k) in (None, "", "-") and r.get(k) not in (None, "", "-"):
                        cand[k] = r.get(k)
                if _is_close_like(cand) and _safe_float(cand.get("profit_u"), None) is None and _safe_float(r.get("profit_u"), None) is not None:
                    cand["profit_u"] = r.get("profit_u")
                if _score(cand) >= _score(old):
                    merged[sig] = cand
            except Exception:
                continue

        out = list(merged.values())
        out.sort(key=lambda x: float(_safe_float(x.get("ts"), 0.0) or 0.0), reverse=True)
        return out


    def _canonicalize_summary_exec_rows(self, exec_rows: List[dict]) -> List[dict]:
        """摘要动作轻量规范化：
        目标：摘要里只保留真实执行单，不保留 M10 父动作拆出来的影子单。

        这次修两类问题：
        1) M10 口径A三步里，GROUP 父行拆出的 structured_step 会和真实 executor_result 子单重复显示。
        2) 某些版本 step2 同时会出现 close_partial_* 和 decrease/reduce 两条同金额同方向记录。

        处理原则：
        - 若已存在真实结构化执行单（structured / structured_row / trades / executor_result），
          则同一 10 秒桶内同币/同平台/同动作/同方向/同金额的 M10 影子单一律丢弃。
        - structured_step 只在“没有真实子单”时才保留，充当兜底展示。
        - step2 若已有 close_partial*，则同签名的 decrease/reduce 视为重复。
        """
        if not isinstance(exec_rows, list) or not exec_rows:
            return list(exec_rows or [])

        def _norm_dir(v: Any) -> str:
            s = str(v or '').strip().lower()
            if s in ('long', 'buy', '多'):
                return 'long'
            if s in ('short', 'sell', '空'):
                return 'short'
            return s

        def _norm_act(v: Any) -> str:
            s = str(v or '').strip().lower()
            if 'close_partial' in s or 'partial' in s:
                return 'close_partial'
            if s in ('decrease', 'reduce', 'close_reduce'):
                return 'reduce'
            if 'close_full' in s:
                return 'close_full'
            if 'open_full' in s:
                return 'open_full'
            return s

        def _m10_key(r: dict) -> tuple:
            plat = str(r.get('plat') or r.get('platform') or '').upper().strip()
            coin = str(r.get('coin') or '').upper().strip()
            dir0 = _norm_dir(r.get('dir') or r.get('side') or r.get('pos_side'))
            amt = round(float(_safe_float(r.get('amount_u'), 0.0) or 0.0), 2)
            ts0 = float(_safe_float(r.get('ts'), 0.0) or 0.0)
            bucket = int(ts0 // 10) if ts0 > 0 else 0
            act0 = _norm_act(r.get('action') or r.get('action_type'))
            return (bucket, coin, plat, act0, dir0, amt)

        def _src_kind(r: dict) -> str:
            return str(r.get('source') or '').strip().lower()

        real_m10_keys = set()
        partial_keys = set()
        for r in exec_rows:
            try:
                if not isinstance(r, dict):
                    continue
                mod_u = str(r.get('module') or '').upper()
                if 'M10' not in mod_u:
                    continue
                src = _src_kind(r)
                key = _m10_key(r)
                if key[-1] <= 0:
                    continue
                if ('structured' in src) and (src != 'structured_step'):
                    real_m10_keys.add(key)
                act0 = key[3]
                if act0 == 'close_partial':
                    partial_keys.add((key[0], key[1], key[2], key[4], key[5]))
            except Exception:
                continue

        out: List[dict] = []
        for r in exec_rows:
            try:
                if not isinstance(r, dict):
                    out.append(r)
                    continue
                mod_u = str(r.get('module') or '').upper()
                if 'M10' not in mod_u:
                    out.append(r)
                    continue

                key = _m10_key(r)
                bucket, coin, plat, act0, dir0, amt = key
                src = _src_kind(r)
                action_raw = str(r.get('action') or r.get('action_type') or '').strip().lower()

                # 父口径行（GROUP / koujing_a_3step）不进摘要；摘要只保留真实成功的买/卖执行子单。
                if plat in ('GROUP', 'PAIR') or action_raw in ('koujing_a_3step', 'm10_hard_tp', 'm10_hard_tp_reduce', 'm10_hard_tp_reduce_only'):
                    continue

                # 已有真实子单时，丢掉 GROUP 父行拆出来的影子 step
                if src == 'structured_step' and key in real_m10_keys:
                    continue

                # 若已有真实结构化子单，也丢掉同签名的日志推断单，避免摘要不是“真实执行单”
                if (src in ('', 'log', 'log_full', 'text', 'parsed_log')) and key in real_m10_keys:
                    continue

                # step2 兼容：已有 close_partial，则同币/同平台/同方向/同金额的 reduce/decrease 不再显示
                if act0 == 'reduce' and (bucket, coin, plat, dir0, amt) in partial_keys:
                    continue

                out.append(r)
            except Exception:
                out.append(r)
                continue

        return out

    def _update_run_session_clock(self, run_state: str) -> float:
        """“运行”显示口径：本次连续运行时间，而不是今日累计。"""
        try:
            now = time.time()
            cur = str(run_state or 'stopped').strip().lower()
            prev = str(getattr(self, '_last_run_state', None) or '').strip().lower()

            if cur == 'running':
                if prev != 'running' or (self._run_start_ts is None):
                    self._run_start_ts = now
                    self._run_session_sec = 0.0
                else:
                    self._run_session_sec = max(0.0, now - float(self._run_start_ts or now))
            else:
                if prev == 'running' and (self._run_start_ts is not None):
                    self._run_session_sec = max(0.0, now - float(self._run_start_ts or now))
                if cur == 'stopped':
                    self._run_start_ts = None
                    self._run_session_sec = 0.0

            self._last_run_state = cur
            return float(max(0.0, self._run_session_sec))
        except Exception:
            return float(max(0.0, getattr(self, '_run_session_sec', 0.0) or 0.0))

    def _update_summary_tree_no_jump(self, exec_rows: List[dict]) -> None:
        """摘要 Treeview 防抖渲染（去抖动）：
        - 不清空重画，只增量插入新成交
        - 你滚动查看历史时，插入新行不会把列表“顶回去/跳动”
        - 显示列严格对齐你给的参考图：时间/币种/平台/模块/动作/动作/方向/金额U
        """
        if not hasattr(self, "tree"):
            return
        if not isinstance(exec_rows, list):
            exec_rows = []
        exec_rows = self._canonicalize_summary_exec_rows(exec_rows)

        if not hasattr(self, "_summary_seen"):
            self._summary_seen = set()
        if not hasattr(self, "_summary_order"):
            self._summary_order = deque()
        if not hasattr(self, "_summary_item_ts") or (not isinstance(getattr(self, "_summary_item_ts", None), dict)):
            self._summary_item_ts = {}
        if not hasattr(self, "_summary_item_priority") or (not isinstance(getattr(self, "_summary_item_priority", None), dict)):
            self._summary_item_priority = {}
        if not hasattr(self, "_summary_item_conflict_key") or (not isinstance(getattr(self, "_summary_item_conflict_key", None), dict)):
            self._summary_item_conflict_key = {}

        # 记录当前视口顶部 item（用于“插入新行时不抖动”）
        try:
            y0, _y1 = self.tree.yview()
        except Exception:
            y0 = 0.0

        top_iid = ""
        try:
            if y0 > 0.001:
                top_iid = str(self.tree.identify_row(0) or "")
        except Exception:
            top_iid = ""

        # 记录选中项
        try:
            sel = self.tree.selection()
        except Exception:
            sel = ()

        new_items: List[Tuple[float, str, Tuple[str, ...], Tuple[str, ...], int, str]] = []
        existing_visible_map = self._build_existing_summary_visible_map() if hasattr(self, '_build_existing_summary_visible_map') else {}
        existing_conflict_map = self._build_existing_summary_conflict_map() if hasattr(self, '_build_existing_summary_conflict_map') else {}
        batch_visible_map: Dict[str, Tuple[int, int]] = {}
        batch_conflict_map: Dict[str, Tuple[int, int]] = {}
        # 关键修复：不能再按“同一秒只保留一条”压摘要。
        # M10 三步走（全平/减亏/回补开仓）经常发生在同一秒，
        # 如果按 HH:MM:SS 二次去重，就会把后两步吞掉，面板看起来像只执行了一步。
        # 现在只按更强的业务签名去重（req/trace/平台/币种/模块/动作/金额/层号），
        # 不再因为“时间一样”删行。
        
        # 关键修复：摘要窗口必须始终基于“最新成交”取样，不能依赖上游传入顺序。
        # 否则一旦某条解析链把 exec_rows 变成旧→新，[:SUMMARY_PARSE_WINDOW] 就会只吃到老记录，
        # 后面的新成交虽然已经读到了，也永远进不了摘要。这里统一先按真实 ts 倒序，再截最近窗口。
        try:
            exec_rows_recent = sorted(
                list(exec_rows or []),
                key=lambda x: float(_safe_float((x or {}).get("ts"), 0.0) or 0.0),
                reverse=True,
            )[:SUMMARY_PARSE_WINDOW]
        except Exception:
            exec_rows_recent = list(exec_rows or [])[:SUMMARY_PARSE_WINDOW]

        summary_occ_idx_by_sig: Dict[str, int] = {}
        try:
            summary_occ_idx_by_sig = dict(self._get_full_history_summary_occ_idx() or {})
        except Exception:
            summary_occ_idx_by_sig = {}
        if not summary_occ_idx_by_sig:
            try:
                summary_occ_idx_by_sig = self._build_summary_occurrence_index_from_rows(exec_rows)
            except Exception:
                summary_occ_idx_by_sig = {}

        for r in exec_rows_recent:
            try:
                plat = str(r.get("plat") or r.get("platform") or r.get("platf") or "").upper().strip()
                coin = str(r.get("coin") or "").upper().strip()
                module = str(r.get("module") or "-").strip() or "-"
                action = str(r.get("action") or "-").strip() or "-"
                oc = str(r.get("oc") or "").strip()  # 开仓/平仓（若 bot 有写）
                order_side = (str(r.get("order_side") or "") or "").lower().strip()
                dir0 = (str(r.get("dir") or r.get("side") or r.get("pos_side") or "") or "").lower().strip()
                if dir0 in ('long','short'):
                    pass
                elif dir0 in ('LONG','SHORT','Long','Short'):
                    dir0 = dir0.lower()
                elif dir0 in ('buy','sell'):
                    dir0 = ''

                amt = float(_safe_float(r.get("amount_u"), 0.0) or 0.0)
                ts0 = float(_safe_float(r.get("ts"), 0.0) or 0.0)

                if (not plat) or (not coin) or amt <= 0:
                    continue

                # 去重签名：优先 req_id/trade_id，再 trace_id，最后才用时间桶。
                req_key = str(r.get("req_id") or r.get("trade_id") or "").strip()
                trace_key = str(r.get("trace_id") or r.get("trace") or r.get("req_trace_id") or "").strip()
                layer_tag = str(r.get("layer_tag") or r.get("grid_layer") or "").strip()
                step_idx = str(r.get("step_idx") or "").strip()
                if req_key:
                    sig = f"req|{req_key}|{plat}|{coin}|{module}|{action}|{oc}|{round(amt, 2):.2f}|{layer_tag}|{step_idx}"
                elif trace_key:
                    sig = f"trace|{trace_key}|{plat}|{coin}|{module}|{action}|{oc}|{round(amt, 2):.2f}|{layer_tag}|{step_idx}"
                else:
                    bucket = int(ts0 // 10) if ts0 > 0 else 0
                    sig = f"{bucket}|{plat}|{coin}|{module}|{action}|{oc}|{round(amt, 2):.2f}|{layer_tag}|{step_idx}"
                if sig in self._summary_seen:
                    continue
                self._summary_seen.add(sig)
                try:
                    self._summary_order.appendleft(sig)
                    while len(self._summary_order) > SUMMARY_SIG_MAX:
                        self._summary_seen.discard(self._summary_order.pop())
                except Exception:
                    pass

                # 平台显示
                plat_disp = (plat or "-").strip() or "-"

                # 时间显示（固定下单/成交时刻，不用当前时间冒充）
                try:
                    time_str = datetime.fromtimestamp(ts0).strftime("%H:%M:%S") if ts0 > 0 else (str(r.get("time") or "--")[:8] or "--")
                except Exception:
                    time_str = str(r.get("time") or "--")[:8] or "--"

                # 动作：优先用 oc（开仓/平仓），没有再退回到 action
                # ACCEPT（计划/尝试）行：避免和真实成交行长得一模一样，导致“同一笔订单显示两次”的错觉
                if str(r.get('status') or '').upper() == 'ACCEPT':
                    act_cn = '尝试'
                    order_side = None
                else:
                    act_cn = oc if oc else ("开仓" if order_side == "buy" else ("平仓" if order_side == "sell" else (action if action else "-")))
                # 补齐 order_side（若上游未给）：根据 开/平 + 方向 推断 buy/sell（用于红/绿着色）
                if not order_side:
                    oc_u = str(oc or "").strip()
                    if oc_u in ("开仓", "open"):
                        if dir0 == "long":
                            order_side = "buy"
                        elif dir0 == "short":
                            order_side = "sell"
                    elif oc_u in ("平仓", "close"):
                        if dir0 == "long":
                            order_side = "sell"
                        elif dir0 == "short":
                            order_side = "buy"

                if not act_cn:
                    act_cn = "-"

                # 方向
                dir_cn = "多" if dir0 == "long" else ("空" if dir0 == "short" else "-")

                # 模块/动作：网格模块额外显示层号（例如 M9/increase/L2开、M9/decrease/L2平）
                mod_cn = _decorate_summary_module_action(module, action, act_cn, r)
                try:
                    occ_idx = int(summary_occ_idx_by_sig.get(sig, 0) or 0)
                except Exception:
                    occ_idx = 0
                if occ_idx > 0:
                    mod_cn = f"{mod_cn}（第{occ_idx}次）"

                # 摘要表口径固定：优先下单当时值；没有就回退历史缓存；再不行兜底最新值。
                rsi_v, adx_v = _extract_rsi_adx_from_row(r, coin, ts0)
                if (rsi_v is None) or (adx_v is None):
                    rsi_h, adx_h = _lookup_rsi_adx_at(coin, ts0)
                    if rsi_v is None:
                        rsi_v = rsi_h
                    if adx_v is None:
                        adx_v = adx_h
                if (rsi_v is None) or (adx_v is None):
                    rsi_l, adx_l = _lookup_latest_rsi_adx_live(getattr(getattr(self, "model", None), "out_dir", None), getattr(getattr(self, "model", None), "logs_dir", None), coin)
                    if rsi_v is None:
                        rsi_v = rsi_l
                    if adx_v is None:
                        adx_v = adx_l
                rsi_str = (f"{float(rsi_v):.2f}" if (rsi_v is not None) else "-")
                adx_str = (f"{float(adx_v):.2f}" if (adx_v is not None) else "-")

                price_v = _extract_price_from_row(r)
                if price_v is None or float(_safe_float(price_v, 0.0) or 0.0) <= 0:
                    try:
                        _md = getattr(self, "model", None)
                        price_v = _lookup_latest_price_live(getattr(_md, "out_dir", None), getattr(_md, "logs_dir", None), coin, plat)
                    except Exception:
                        pass
                profit_v = _extract_profit_from_row(r)
                price_str = _fmt_px(price_v)
                grid_gap_str = self._summary_extract_grid_gap_str(r)
                if str(r.get('status') or '').upper() == 'ACCEPT':
                    # 计划/尝试没有已实现利润口径，避免显示 0.00 误导
                    profit_str = '-'
                elif profit_v is None:
                    # 开仓/加仓动作天然没有已实现利润，为了不让摘要看起来像“丢字段”，统一显示 0.00；
                    # 真实减仓/止盈动作则继续显示实际利润。
                    _al = str((r.get('action') or r.get('action_type') or ((r.get('req') or {}).get('action_type') if isinstance(r.get('req'), dict) else '') or '')).lower()
                    _ml = str((r.get('module') or ((r.get('req') or {}).get('module') if isinstance(r.get('req'), dict) else '') or '')).upper()
                    _is_open0 = (any(k in _al for k in ('increase','open','add','grid_add')) or (_ml in ('B1','B13','M9','W1','X1') and not any(k in _al for k in ('decrease','close','reduce','tp','takeprofit','grid_sell'))))
                    profit_str = '0.00' if _is_open0 else '-'
                else:
                    profit_str = f"{float(profit_v):.2f}"

                vals = (
                    time_str,
                    coin,
                    plat_disp,
                    mod_cn,
                    act_cn,
                    dir_cn,
                    f"{amt:.2f}",
                    price_str,
                    grid_gap_str,
                    profit_str,
                    rsi_str,
                    adx_str,
                )

                vis_sig = self._summary_visible_sig(vals) if hasattr(self, '_summary_visible_sig') else '|'.join(map(str, vals[:8]))
                conflict_sig = self._summary_conflict_sig_from_row(r, vals) if hasattr(self, '_summary_conflict_sig_from_row') else ''
                row_priority = self._summary_row_priority(r) if hasattr(self, '_summary_row_priority') else 0

                existed_iid = existing_visible_map.get(vis_sig, '') if isinstance(existing_visible_map, dict) else ''
                if existed_iid:
                    old_priority = int(getattr(self, '_summary_item_priority', {}).get(existed_iid, 0) or 0)
                    if row_priority <= old_priority:
                        continue
                    try:
                        self.tree.delete(existed_iid)
                    except Exception:
                        pass
                    try:
                        self._summary_item_ts.pop(existed_iid, None)
                    except Exception:
                        pass
                    try:
                        self._summary_item_priority.pop(existed_iid, None)
                    except Exception:
                        pass
                    try:
                        old_conflict_sig = str(getattr(self, '_summary_item_conflict_key', {}).pop(existed_iid, '') or '')
                        if old_conflict_sig:
                            existing_conflict_map.pop(old_conflict_sig, None)
                    except Exception:
                        pass
                    existing_visible_map.pop(vis_sig, None)

                if conflict_sig:
                    conflict_iid = existing_conflict_map.get(conflict_sig, '') if isinstance(existing_conflict_map, dict) else ''
                    if conflict_iid:
                        old_priority = int(getattr(self, '_summary_item_priority', {}).get(conflict_iid, 0) or 0)
                        if row_priority <= old_priority:
                            continue
                        try:
                            old_vals = tuple(self.tree.item(conflict_iid, 'values') or ())
                            old_vis_sig = self._summary_visible_sig(old_vals) if hasattr(self, '_summary_visible_sig') else ''
                        except Exception:
                            old_vis_sig = ''
                        try:
                            self.tree.delete(conflict_iid)
                        except Exception:
                            pass
                        try:
                            self._summary_item_ts.pop(conflict_iid, None)
                        except Exception:
                            pass
                        try:
                            self._summary_item_priority.pop(conflict_iid, None)
                        except Exception:
                            pass
                        try:
                            self._summary_item_conflict_key.pop(conflict_iid, None)
                        except Exception:
                            pass
                        if old_vis_sig:
                            existing_visible_map.pop(old_vis_sig, None)
                        existing_conflict_map.pop(conflict_sig, None)

                batch_old = batch_visible_map.get(vis_sig)
                if batch_old is not None:
                    old_priority, old_idx = batch_old
                    if row_priority <= old_priority:
                        continue
                    try:
                        new_items.pop(old_idx)
                    except Exception:
                        continue
                    batch_visible_map = {
                        k: (v[0], (v[1] - 1 if v[1] > old_idx else v[1]))
                        for k, v in batch_visible_map.items() if k != vis_sig
                    }
                    batch_conflict_map = {
                        k: (v[0], (v[1] - 1 if v[1] > old_idx else v[1]))
                        for k, v in batch_conflict_map.items() if v[1] != old_idx
                    }

                if conflict_sig:
                    batch_old_conflict = batch_conflict_map.get(conflict_sig)
                    if batch_old_conflict is not None:
                        old_priority, old_idx = batch_old_conflict
                        if row_priority <= old_priority:
                            continue
                        try:
                            new_items.pop(old_idx)
                        except Exception:
                            continue
                        batch_visible_map = {
                            k: (v[0], (v[1] - 1 if v[1] > old_idx else v[1]))
                            for k, v in batch_visible_map.items() if v[1] != old_idx
                        }
                        batch_conflict_map = {
                            k: (v[0], (v[1] - 1 if v[1] > old_idx else v[1]))
                            for k, v in batch_conflict_map.items() if k != conflict_sig
                        }

                tag = "tag_buy" if order_side == "buy" else ("tag_sell" if order_side == "sell" else "tag_neutral")
                rsi_dir_flag = _rsi_direction_expectation(rsi_v, dir0)
                row_tags = (tag, "tag_blue_mismatch") if rsi_dir_flag == "mismatch" else (tag,)
                new_items.append((ts0, sig, vals, row_tags, row_priority, conflict_sig))
                batch_visible_map[vis_sig] = (row_priority, len(new_items) - 1)
                if conflict_sig:
                    batch_conflict_map[conflict_sig] = (row_priority, len(new_items) - 1)
            except Exception:
                continue

        if not new_items:
            return

        # 时间排序：支持点“时间”列头切换正序/倒序
        desc = bool(getattr(self, '_summary_time_sort_desc', True))
        new_items.sort(key=lambda x: (x[0] or 0.0), reverse=desc)

        try:
            for _ts, _sig, vals, tag, row_priority, conflict_sig in new_items:
                # 不再按同秒删旧行；只依赖前面的强签名去重，
                # 这样同一秒发生的 M10 三步、PAIR 双腿、连续补腿都能完整展示。
                iid = self.tree.insert("", "end", values=vals, tags=(tag,))
                try:
                    self._summary_item_ts[iid] = float(_safe_float(_ts, 0.0) or 0.0)
                except Exception:
                    pass
                try:
                    self._summary_item_priority[iid] = int(row_priority or 0)
                except Exception:
                    pass
                try:
                    self._summary_item_conflict_key[iid] = str(conflict_sig or '')
                except Exception:
                    pass

            # 关键修复：必须先排序，再裁剪。
            # 否则新成交先被 insert 到末尾，摘要已满时会在排序前被当成“尾部旧行”直接删掉。
            self._resort_summary_tree()
            self._dedupe_summary_tree_conflicts()

            # 控制行数，避免无限增长导致卡顿
            kids = self.tree.get_children("")
            max_keep = SUMMARY_MAX_ROWS
            if len(kids) > max_keep:
                for iid in kids[max_keep:]:
                    try:
                        self.tree.delete(iid)
                    except Exception:
                        pass
                    try:
                        self._summary_item_ts.pop(iid, None)
                    except Exception:
                        pass
                    try:
                        self._summary_item_priority.pop(iid, None)
                    except Exception:
                        pass
                    try:
                        self._summary_item_conflict_key.pop(iid, None)
                    except Exception:
                        pass
        except Exception:
            pass

        # 去抖动关键：你在看历史（y0>0）时，用“保持顶部可见 item”而不是 yview 百分比
        try:
            if top_iid:
                self.tree.see(top_iid)
        except Exception:
            pass

        # 恢复选择
        try:
            if sel:
                self.tree.selection_set(sel)
        except Exception:
            pass

    def _render(self, data: dict) -> None:
        self._maybe_beep(data)
        # BOT 报错：有变更就弹窗，并把状态灯拉到 error
        try:
            ev = (data.get('events') or {})
            if ev.get('error_text'):
                txt = str(ev.get('error_text') or '').strip()
                if txt:
                    # 去重：同一错误只弹一次
                    h = hashlib.md5(txt.encode('utf-8', errors='ignore')).hexdigest()
                    if ev.get('error_changed') and h != getattr(self, '_last_err_popup_hash', ''):
                        self._last_err_popup_hash = h
                        messagebox.showerror('BOT 报错', txt[:1800])
                    # 状态栏也给出一行摘要（便于不看弹窗时也能定位）
                    first = (txt.splitlines()[0] if txt.splitlines() else txt)[:160]
                    self.var_status.set(f"BOT 报错：{first}")
        except Exception:
            pass

        # self._hb 心跳计数已取消（避免刷屏/误导）

        eq = data.get("equity", {})
        anchor = data.get("anchor", {})
        hedge = data.get("hedge", {})
        rt = data.get("runtime", {})
        env = data.get("env", {})
        tr = data.get("trades", {})
        action_rows = data.get("action_rows", [])
        log_full = data.get("log_full", [])
        m1_view = data.get("m1_view", "")
        scan = data.get("scan", {})
        top3 = data.get("upnl_top3", [])
        ls_ratio_text = data.get("ls_ratio_text", "-")
        st = data.get("status", {})

        # 卡片区
        e = _safe_float(eq.get("e"), 0.0)
        v = _safe_float(eq.get("v"), 0.0)
        t = _safe_float(eq.get("t"), 0.0)
        self.var_total.set(f"{t:.2f}U" + ("（未核验）" if not eq.get("ok") else ""))
        self.var_total_sub.set(f"e: {e:.2f}U    v: {v:.2f}U")

        anchor_u = _safe_float(anchor.get("anchor_u"), _safe_float(eq.get("anchor_current_u"), 0.0))
        trigger_u = _safe_float(anchor.get("next_trigger_u"), _safe_float(eq.get("anchor_next_trigger_u"), 0.0))
        if anchor_u > 0:
            self.var_anchor.set(f"锚点 {_fmt_u(anchor_u)}")
            if trigger_u > 0:
                self.var_anchor_sub.set(f"触发点 {_fmt_u(trigger_u)}")
            else:
                self.var_anchor_sub.set("触发点 -")
        else:
            diff = _safe_float(anchor.get("diff"), 0.0)
            pct = _safe_float(anchor.get("pct"), 0.0)
            self.var_anchor.set(f"{_fmt_u(diff)}")
            self.var_anchor_sub.set(f"百分比：{_fmt_pct(pct)}")

        e_notional = _safe_float(hedge.get("e_notional"), 0.0)
        v_notional = _safe_float(hedge.get("v_notional"), 0.0)
        hd = _safe_float(hedge.get("diff"), 0.0)
        self.var_hedge.set(f"差额 v-e：{_fmt_u(hd)}" + ("（未核验）" if not hedge.get("ok") else ""))
        self.var_hedge_sub.set(f"v: {v_notional:.2f}U    e: {e_notional:.2f}U")

        # 多空概览：独立卡片，主值看方向，次值看差额和比例，下面看双平台拆分
        try:
            ov = dict(data.get("ls_overview") or {})
            tl = _safe_float(ov.get("total_long_u"), 0.0)
            ts_ = _safe_float(ov.get("total_short_u"), 0.0)
            el = _safe_float(ov.get("ext_long_u"), 0.0)
            es = _safe_float(ov.get("ext_short_u"), 0.0)
            vl = _safe_float(ov.get("var_long_u"), 0.0)
            vs = _safe_float(ov.get("var_short_u"), 0.0)
            tlc = int(ov.get("total_long_cnt") or 0)
            tsc = int(ov.get("total_short_cnt") or 0)
            elc = int(ov.get("ext_long_cnt") or 0)
            esc = int(ov.get("ext_short_cnt") or 0)
            vlc = int(ov.get("var_long_cnt") or 0)
            vsc = int(ov.get("var_short_cnt") or 0)

            total = tl + ts_
            diff_ls = tl - ts_
            if ts_ <= 0 < tl:
                ratio_txt = "INF"
            elif tl <= 0 < ts_:
                ratio_txt = "0.00"
            elif ts_ > 0:
                ratio_txt = f"{(tl / ts_):.2f}"
            else:
                ratio_txt = "-"

            if total > 0:
                long_pct = tl / total * 100.0
                short_pct = ts_ / total * 100.0
                main_txt = f"总多 {tl:.2f}U    总空 {ts_:.2f}U"
                if abs(diff_ls) < 1e-9:
                    bias_txt = f"完全均衡｜差额 0.00U｜多/空 {ratio_txt}｜多 {long_pct:.1f}% / 空 {short_pct:.1f}%"
                    sub_style = "NeutralSubValue.TLabel"
                elif diff_ls > 0:
                    bias_txt = f"偏多 {diff_ls:.2f}U｜多/空 {ratio_txt}｜多 {long_pct:.1f}% / 空 {short_pct:.1f}%"
                    sub_style = "UpSubValue.TLabel"
                else:
                    bias_txt = f"偏空 {abs(diff_ls):.2f}U｜空/多 {(ts_ / tl):.2f}｜多 {long_pct:.1f}% / 空 {short_pct:.1f}%" if tl > 0 else f"偏空 {abs(diff_ls):.2f}U｜空仓占优｜多 {long_pct:.1f}% / 空 {short_pct:.1f}%"
                    sub_style = "DownSubValue.TLabel"
                value_style = "CardTitle.TLabel"
                ext_txt = f"EXT｜多 {el:.2f}U ({elc})    空 {es:.2f}U ({esc})"
                var_txt = f"VAR｜多 {vl:.2f}U ({vlc})    空 {vs:.2f}U ({vsc})"
            else:
                main_txt = "-"
                bias_txt = "暂无可识别的多空持仓"
                ext_txt = "EXT｜-"
                var_txt = "VAR｜-"
                value_style = "CardTitle.TLabel"
                sub_style = "NeutralSubValue.TLabel"

            if hasattr(self, "var_ls_overview"):
                self.var_ls_overview.set(main_txt)
            if hasattr(self, "var_ls_overview_bias"):
                self.var_ls_overview_bias.set(bias_txt)
            if hasattr(self, "var_ls_overview_sub1"):
                self.var_ls_overview_sub1.set(ext_txt)
            if hasattr(self, "var_ls_overview_sub2"):
                self.var_ls_overview_sub2.set(var_txt)
            try:
                if hasattr(self, "lbl_ls_overview") and self.lbl_ls_overview is not None:
                    self.lbl_ls_overview.configure(style=value_style)
                if hasattr(self, "lbl_ls_overview_bias") and self.lbl_ls_overview_bias is not None:
                    self.lbl_ls_overview_bias.configure(style=sub_style)
            except Exception:
                pass
        except Exception:
            pass

        # 顶部右侧：风险状态 + 模式（工作/睡眠）
        try:
            hedge_sum = abs(e_notional) + abs(v_notional)
            thr = float(t) * 0.30 * 50.0  # 50 倍
            risk = "高危" if hedge_sum > thr else "正常"
            if hasattr(self, "var_banner1_right"):
                self.var_banner1_right.set(f"风险状态：{risk}（持仓合计 {hedge_sum:.0f}U / 阈值 {thr:.0f}U）")
            # 颜色：正常=绿色，高危=红色
            try:
                if hasattr(self, "lbl_risk") and self.lbl_risk is not None:
                    self.lbl_risk.configure(foreground=("#3BD16F" if risk == "正常" else "#FF4D4D"))
                try:
                    self._edge_set_risk(risk)
                    try:
                        self._mini_edge_set_risk(risk)
                    except Exception:
                        pass
                except Exception:
                    pass
            except Exception:
                pass
        except Exception:
            if hasattr(self, "var_banner1_right"):
                self.var_banner1_right.set("风险状态：-")
                try:
                    if hasattr(self, "lbl_risk") and self.lbl_risk is not None:
                        self.lbl_risk.configure(foreground="#E6E6E6")
                        try:
                            self._edge_set_risk("")
                            try:
                                self._mini_edge_set_risk("")
                            except Exception:
                                pass
                        except Exception:
                            pass
                except Exception:
                    pass

        try:
            ctl = data.get("ctl") or {}
            mode_raw = str(
                ctl.get("mode")
                or ctl.get("profile")
                or ctl.get("run_mode")
                or ctl.get("work_mode")
                or ctl.get("sleep_mode")
                or ""
            ).lower()
            cmd_raw = str(ctl.get("cmd") or "").lower()
            mode_txt = "工作"
            if ("sleep" in mode_raw) or ("睡" in mode_raw) or (cmd_raw == "pause"):
                mode_txt = "睡眠"
            elif ("work" in mode_raw) or ("工" in mode_raw):
                mode_txt = "工作"
            # 优先使用 Model 提供的模式（来自 status.json / 推断）
            try:
                ov = str(data.get("mode_txt") or "").strip()
                if ov:
                    low = ov.lower()
                    if ("睡" in ov) or ("sleep" in low) or (low.startswith("s")):
                        mode_txt = "睡眠"
                    elif ("工" in ov) or ("work" in low) or (low.startswith("w")):
                        mode_txt = "工作"
                    else:
                        mode_txt = ov
            except Exception:
                pass
            # 不再在横幅上展示模式，但仍用模式驱动边框跑马灯
            try:
                self._edge_set_mode(mode_txt)
                try:
                    self._mini_edge_set_mode(mode_txt)
                except Exception:
                    pass
            except Exception:
                pass
        except Exception:
            # 不显示模式
            pass
            try:
                self._edge_set_mode("")
            except Exception:
                pass

        # 运行状态：
        # - “运行”= 本次连续运行时长（start/resume 后到现在）
        # - “历史”= 历史累计运行时长（total_sec）
        today_sec = _safe_float(rt.get("today_sec"), 0.0)
        total_sec = _safe_float(rt.get("total_sec"), 0.0)

        def _fmt_uptime(sec: float) -> str:
            sec = int(max(0, sec))
            d = sec // 86400
            h = (sec % 86400) // 3600
            m = (sec % 3600) // 60
            s = sec % 60
            if d > 0:
                return f"{d}天{h:02d}:{m:02d}:{s:02d}"
            return f"{h:02d}:{m:02d}:{s:02d}"

        def _fmt_dh(sec: float) -> str:
            sec = int(max(0, sec))
            d = sec // 86400
            h = (sec % 86400) // 3600
            return f"{d}天{h}小时"

        # “运行”不要再用 today_run_sec（日累计），而是用本次连续运行时长。
        run_state = str((data.get("lights") or {}).get("run_state") or "stopped")
        actual_sec = self._update_run_session_clock(run_state)

        scan_cnt = int(((data.get("scan") or {}).get("today_cnt")) or 0)
        self.var_runtime.set(f"运行：{_fmt_uptime(actual_sec)}")
        try:
            self.var_runtime_scan.set(f"扫描次数：{scan_cnt}")
        except Exception:
            pass
        self.var_runtime_sub.set(f"历史：{_fmt_dh(total_sec)}")

        # 盈利（今日/历史）
        profit_today = _safe_float(rt.get('profit_today'), 0.0)
        profit_hist = _safe_float(rt.get('profit_hist'), 0.0)
        if hasattr(self, 'var_profit_today'):
            self.var_profit_today.set(f"今日权益变动：{_fmt_u(profit_today)}" + ("（旧记录）" if not eq.get("ok") else ""))
        if hasattr(self, 'var_profit_hist'):
            self.var_profit_hist.set(f"历史权益变动：{_fmt_u(profit_hist)}")

        self._set_lights_mode(run_state)

        # 横向信息条：环境|交易额|扫描|心跳
        env_ok = bool(env.get("ok"))
        today_u = _safe_float(tr.get("today_u"), 0.0)
        total_u = _safe_float(tr.get("total_u"), 0.0)
        scan_now = str(scan.get("now") or "-")
        scan_next = str(scan.get("next") or "-")
        scan_src = str(scan.get("src") or "")
        self.var_info.set(
            f"环境 {'✓' if env_ok else '✗'} | 交易额 今日 {today_u:,.2f}U / 历史 {total_u:,.2f}U | 扫描 本次 {scan_now} / 下次 {scan_next}"
        )
        self.var_browser_recovery_alert.set(str(data.get('browser_recovery_alert') or ''))
        self.var_funds_imbalance_alert.set(str(data.get('funds_imbalance_alert') or ''))
        # 交易额分类（EXT/VAR）由摘要去重后统一计算，避免重复计数
        # 多空比列表（替代原有模块状态）
        try:
            if hasattr(self, "var_ls_ratio_main"):
                self.var_ls_ratio_main.set(str(data.get("ls_ratio_text") or "-"))
        except Exception:
            pass

                # 摘要表（只显示“通过执行器并完成”的真实下单动作；并且不抖动）
        try:
            rows_1 = []
            rows_2 = self._parse_exec_actions_from_structured_sources()
            exec_rows = self._merge_exec_rows(rows_1, rows_2)
            self._update_summary_tree_no_jump(exec_rows)
            try:
                if hasattr(self, 'var_log_digest'):
                    self.var_log_digest.set(_build_log_digest(log_full, exec_rows=exec_rows, scan_now=scan_now, scan_next=scan_next))
            except Exception:
                pass
        except Exception:
            exec_rows = []
            try:
                if hasattr(self, 'var_log_digest'):
                    self.var_log_digest.set(_build_log_digest(log_full, exec_rows=[], scan_now=scan_now, scan_next=scan_next))
            except Exception:
                pass

# 全量日志（左下）
        try:
            mode = getattr(self, "var_log_mode", tk.StringVar(value="focus")).get()
            kw = getattr(self, "var_log_kw", tk.StringVar(value="")).get()

            # 关键：默认 all 视图要“尽量等于命令行”，不要二次 humanize
            if (mode == "all") and (not kw):
                view_lines = log_full or []
                txt = "\n".join(view_lines)
            else:
                view_lines = _humanize_and_filter_logs(log_full, mode=mode, keyword=kw)
                txt = "\n\n".join(view_lines) if mode == "human" else "\n".join(view_lines)

            cur = self.txt_full.get("1.0", "end-1c")
            if cur != txt:
                import time as _time
                _now_ts = _time.time()
                try:
                    _y0, _y1 = self.txt_full.yview()
                    _x0, _x1 = self.txt_full.xview()
                    _was_at_bottom = float(_y1) >= (0.999 - float(getattr(self, "_log_auto_follow_eps", 0.003) or 0.003))
                except Exception:
                    _y0, _y1, _x0, _x1 = 1.0, 1.0, 0.0, 1.0
                    _was_at_bottom = True
                _manual_hold = ((_now_ts < float(getattr(self, "_log_manual_scroll_until", 0.0) or 0.0)) and (not _was_at_bottom))
                if (_now_ts - float(getattr(self, "_log_last_render_ts", 0.0) or 0.0)) * 1000.0 < float(getattr(self, "_log_scroll_debounce_ms", 180)):
                    return
                self._log_last_render_ts = _now_ts
                self.txt_full.delete("1.0", "end")

                def _pick_tag(line: str) -> Optional[str]:
                    try:
                        s = str(line or "")
                        if not s:
                            return None
                        sl = s.lower()
                        su = s.upper()

                        # 报错/警告优先级最高
                        if any(x in sl for x in ("error", "exception", "traceback")) or ("错误" in s):
                            return "tag_red_bold"
                        if ("warn" in sl) or ("警告" in s):
                            return "tag_yellow_bold"
                        if ("扫描开始" in s) or ("扫描结束" in s) or ("预计下次扫描" in s) or ("持仓进度" in s) or ("扫描进度" in s):
                            return "tag_blue_bold"
                        if ("[STANDBY]" in s) or ("module_disabled" in s):
                            return "tag_gray"

                        mm = re.search(r"\bmodule=([A-Za-z0-9_+\-]+)", s)
                        mod_u = (mm.group(1) if mm else "").upper()
                        ma = re.search(r"\baction=([A-Za-z0-9_+\-]+)", s)
                        act_l = (ma.group(1) if ma else "").lower()
                        related_mod = any(k in su for k in ("M10", "B2", "M9", "B1")) or any(k in mod_u for k in ("M10", "B2", "M9", "B1"))

                        close_hit = (
                            ("平仓" in s) or ("减仓" in s) or ("全平" in s) or ("双平" in s) or ("止盈" in s) or ("减亏" in s)
                            or bool(re.search(r"/L\d+[平减]", s))
                            or act_l.startswith("close") or ("close" in act_l) or (act_l in ("reduce", "decrease", "take_profit", "tp", "close_pair", "close_full", "close_partial"))
                        )
                        open_hit = (
                            ("开仓" in s) or ("加仓" in s) or ("补仓" in s) or ("补腿" in s) or ("回补" in s) or ("首开" in s)
                            or bool(re.search(r"/L\d+[开加]", s))
                            or act_l.startswith("open") or ("open" in act_l) or (act_l in ("increase", "add", "add_layer", "add_leg", "fix_leg", "first_open", "open_pair", "open_full", "reopen"))
                        )

                        if close_hit:
                            return "tag_green_bold"
                        if open_hit:
                            return "tag_red_bold"
                        if related_mod:
                            return "tag_yellow_bold"
                        return None
                    except Exception:
                        return None

                def _ins_one(sline: str, add_nl: bool = True) -> None:
                    try:
                        tag = _pick_tag(sline)
                        if tag:
                            self.txt_full.insert("end", sline, tag)
                        else:
                            self.txt_full.insert("end", sline)
                        if add_nl:
                            self.txt_full.insert("end", "\n")
                    except Exception:
                        try:
                            self.txt_full.insert("end", sline + ("\n" if add_nl else ""))
                        except Exception:
                            pass

                if mode == "human":
                    # human 模式：每个块之间空一行
                    for bi, blk in enumerate(view_lines or []):
                        for ln in str(blk).splitlines():
                            _ins_one(ln, add_nl=True)
                        if bi != len(view_lines) - 1:
                            self.txt_full.insert("end", "\n")
                else:
                    # 其它模式：逐行插入，便于高亮
                    for ln in (txt.splitlines() if isinstance(txt, str) else []):
                        _ins_one(ln, add_nl=True)

                try:
                    if _manual_hold:
                        self.txt_full.yview_moveto(_y0)
                        self.txt_full.xview_moveto(_x0)
                    else:
                        self.txt_full.see("end")
                        try:
                            _log_clear_manual_scroll()
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception:
            pass
        # 多空比表格（仅表格，去掉 EXT/VAR/合计总览）
        try:
            rows = data.get("ls_ratio_coin_rows") or []
            # 清表再插（只有 8 个币，开销忽略）
            for iid in self.tree_ls.get_children(""):
                self.tree_ls.delete(iid)

            for r in rows:
                coin = str(r.get("coin", "-"))
                total = float(r.get("total_u", 0.0))
                long_u = float(r.get("long_u", 0.0))
                short_u = float(r.get("short_u", 0.0))
                lp = float(r.get("long_pct", 0.0))
                sp = float(r.get("short_pct", 0.0))
                try:
                    rsi_raw = r.get("latest_rsi")
                    adx_raw = r.get("latest_adx")
                    if (rsi_raw is None or str(rsi_raw).strip() == "") or (adx_raw is None or str(adx_raw).strip() == ""):
                        lr, la = _lookup_latest_rsi_adx_live(getattr(getattr(self, "model", None), "out_dir", None), getattr(getattr(self, "model", None), "logs_dir", None), coin)
                        if rsi_raw is None or str(rsi_raw).strip() == "":
                            rsi_raw = lr
                        if adx_raw is None or str(adx_raw).strip() == "":
                            adx_raw = la
                    try:
                        rv = float(str(rsi_raw).strip()) if (rsi_raw is not None and str(rsi_raw).strip() != "") else None
                    except Exception:
                        rv = None
                    try:
                        av = float(str(adx_raw).strip()) if (adx_raw is not None and str(adx_raw).strip() != "") else None
                    except Exception:
                        av = None
                    rsi_txt = (f"{rv:.2f}" if rv is not None else "-")
                    adx_txt = (f"{av:.2f}" if av is not None else "-")
                    if rv is not None and rv > 50:
                        row_tag = 'rsi_red'
                    elif rv is not None and rv < 50:
                        row_tag = 'rsi_green'
                    else:
                        row_tag = 'rsi_neutral'
                    values = (
                        coin,
                        f"{total:,.2f}",
                        f"{long_u:,.2f}",
                        f"{short_u:,.2f}",
                        f"{lp:.1f}/{sp:.1f}",
                        str(r.get("ls_ratio") or "-"),
                        str(r.get("cnt") or "-"),
                        rsi_txt,
                        adx_txt,
                    )
                    iid = self.tree_ls.insert('', 'end', values=values)
                    try:
                        self.tree_ls.item(iid, tags=(row_tag,))
                    except Exception:
                        pass
                except Exception:
                    iid = self.tree_ls.insert('', 'end', values=(coin, f"{total:,.2f}", f"{long_u:,.2f}", f"{short_u:,.2f}", f"{lp:.1f}/{sp:.1f}", str(r.get("ls_ratio") or "-"), str(r.get("cnt") or "-"), '-', '-'))
                    try:
                        self.tree_ls.item(iid, tags=('rsi_neutral',))
                    except Exception:
                        pass

        except Exception:
            pass

        # 底部状态：数据状态+原因+路径（关键：你说没数据，这里必须给证据）
        paths = (st.get("paths") or {})
        reason = st.get("reason") or ""
        self.var_status.set(
            f"刷新 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 数据状态 env={st.get('env')} equity={st.get('equity')} positions={st.get('positions')} | {reason}\n"
            f"bot_dir={paths.get('bot_dir','')} | out_dir={paths.get('out_dir','')} | logs_dir={paths.get('logs_dir','')}\n"
            f"equity={paths.get('equity','')} | positions={paths.get('positions','')} | trades={paths.get('trades','')} | log={paths.get('log','')} | scan_src={scan_src}"
        )
        # 网格插页刷新（只读，不影响主面板）
        try:
            self._render_grid_tab()
        except Exception as _e:
            # 重要：不要静默失败，否则你看到的就会一直是“等待数据...”
            try:
                if hasattr(self, 'var_grid_status'):
                    self.var_grid_status.set(f"网格状态：面板渲染异常：{_e}")
            except Exception:
                pass
        try:
            self._render_grid_book_tab()
        except Exception as _e:
            try:
                if hasattr(self, 'var_grid_book_status'):
                    self.var_grid_book_status.set(f"网格账本：面板渲染异常：{_e}")
            except Exception:
                pass
        _panel_status_safe = locals().get('panel_status', {}) if isinstance(locals().get('panel_status', {}), dict) else {}
        try:
            self._render_trend_observe_tab(_panel_status_safe)
        except Exception as _e:
            try:
                if hasattr(self, 'var_trend_observe_status'):
                    self.var_trend_observe_status.set(f"趋势观察：面板渲染异常：{_e}")
            except Exception:
                pass
        try:
            self._render_selection_observe_tab(_panel_status_safe)
        except Exception as _e:
            try:
                if hasattr(self, 'var_selection_observe_status'):
                    self.var_selection_observe_status.set(f"选币排名：面板渲染异常：{_e}")
            except Exception:
                pass

        # 小挂件同步
        if self.mini_win and self.mini_win.winfo_exists():
            try:
                self.mini_vars["scan_now"].set(scan_now)
                self.mini_vars["scan_next"].set(scan_next)
                self.mini_vars["trade"].set(f"今日 {today_u:,.2f}U    历史 {total_u:,.2f}U")
                if not top3:
                    self.mini_vars["upnl3"].set("-")
                else:
                    lines = []
                    for i, (coin, upnl) in enumerate(top3, start=1):
                        lines.append(f"{i}. {coin}  UPNL {upnl:,.2f}U")
                    self.mini_vars["upnl3"].set("\n".join(lines))
                # 多空比列表（EXT/VAR/合计）
                try:
                    if "ls_ratio" in self.mini_vars:
                        self.mini_vars["ls_ratio"].set(ls_ratio_text or "-")
                except Exception:
                    pass
            except Exception:
                pass


def _panel_selftest() -> int:
    """不启动 GUI 的自检：验证 JSON 读取与网格行构建关键路径不报错。
    用法：python 面板.py --selftest --positions <path-to-positions.json>
    """
    import argparse
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--selftest", action="store_true", help="run selftest and exit")
    ap.add_argument("--positions", type=str, default="", help="positions.json path")
    args, _ = ap.parse_known_args()

    if not args.selftest:
        return None

    pos_path = args.positions
    if not pos_path:
        # 尝试默认 out/positions.json
        try:
            pos_path = str((Path(__file__).resolve().parent / "out" / "positions.json"))
        except Exception:
            pos_path = ""

    print("[SELFTEST] positions =", pos_path)
    if not pos_path or not Path(pos_path).exists():
        print("[SELFTEST][FAIL] positions.json not found")
        return 2

    try:
        txt = Path(pos_path).read_text(encoding="utf-8", errors="replace")
        # 去 BOM
        if txt.startswith("\ufeff"):
            txt = txt.lstrip("\ufeff")
        data = json.loads(txt)
    except Exception as e:
        print("[SELFTEST][FAIL] json load error:", repr(e))
        return 3


    # 同目录的 equity/control（如果存在）也做 JSON 可解析性自检
    try:
        base_dir = Path(pos_path).resolve().parent
        for fn in ("equity.json", "control.json", "grid_local_ledger.json", "ind_cache.json"):
            fp = base_dir / fn
            if fp.exists():
                t = fp.read_text(encoding="utf-8", errors="replace")
                if t.startswith("\ufeff"):
                    t = t.lstrip("\ufeff")
                if not t.strip():
                    continue
                s0 = t.lstrip()
                if not (s0.startswith("{") or s0.startswith("[")):
                    # 非 JSON 内容直接跳过（避免空文件/日志占位）
                    continue
                json.loads(t)
                print("[SELFTEST] ok:", fn)
    except Exception as e:
        print("[SELFTEST][FAIL] sibling json load error:", repr(e))
        return 3

    ps = data.get("positions") or []
    print("[SELFTEST] positions count =", len(ps))
    if not isinstance(ps, list):
        print("[SELFTEST][FAIL] positions is not list")
        return 4

    # 关键字段格式化不抛异常
    try:
        for it in ps[:20]:
            px = it.get("px")
            entry = it.get("entry_px") or it.get("avg_px")
            _ = _fmt_px(px)
            _ = _fmt_px(entry)
            _ = _fmt_u(it.get("pos_u") or it.get("hold_u") or 0)
            _ = _fmt_u(it.get("upnl_u") or 0)
            _ = _fmt_pct(it.get("upnl_pct") or 0)
            _ = _safe_float(it.get("layers") or 0, 0.0)
    except Exception as e:
        print("[SELFTEST][FAIL] format/build error:", repr(e))
        return 5

    # 方向/RSI 规则自检：>50 应偏多，<50 应偏空，50 中性
    try:
        assert _rsi_direction_expectation(55, "long") == "ok"
        assert _rsi_direction_expectation(55, "short") == "mismatch"
        assert _rsi_direction_expectation(45, "short") == "ok"
        assert _rsi_direction_expectation(45, "long") == "mismatch"
        assert _rsi_direction_expectation(50, "long") == "neutral"
        assert _rsi_direction_expectation(None, "long") == "neutral"
        assert _decorate_summary_module_action("M9", "increase", "开仓", {"layer_tag": "L2"}) == "M9/L2开"
        assert _decorate_summary_module_action("M9", "decrease", "平仓", {"layer_no": 3}) == "M9/L3平"
        print("[SELFTEST][OK] summary compact/mismatch rules passed")
    except Exception as e:
        print("[SELFTEST][FAIL] summary compact/mismatch rules error:", repr(e))
        return 5

    # 动作摘要自检：确保“每一个动作”都有完整可展示字段（时间/币种/平台/模块/动作/方向/金额U）
    try:
        base_out = Path(pos_path).parent if str(pos_path).endswith("positions.json") else Path(pos_path)
        root_dir = base_out.parent

        cands = [
            base_out / "trades.jsonl",
            root_dir / "logs" / "executor_results.jsonl",
            root_dir / "logs" / "BOT_grid_full.jsonl",
            root_dir / "logs" / "full_all.jsonl",
            root_dir / "logs" / "bot_console.log",
            root_dir / "logs" / "bot_console.txt",
        ]
        fp = None
        for p in cands:
            try:
                if p.exists() and p.stat().st_size > 0:
                    fp = p
                    break
            except Exception:
                continue

        if not fp:
            print("[SELFTEST][WARN] actions source not found, skip actions check")
        else:
            cache = DataCache()
            _ = cache.ingest_trades_incremental(fp)
            rows = list(getattr(cache, "action_rows", []) or [])[-80:]
            if not rows:
                print("[SELFTEST][FAIL] actions parsed 0 rows from:", str(fp))
                return 6


            # 载入 RSI/ADX 缓存（用于摘要逐笔指标展示自检）
            try:
                _ensure_ind_sources_loaded(base_out, root_dir / "logs")
            except Exception:
                pass

            bad = 0
            for r in rows:
                plat = str(r.get("plat") or r.get("platform") or "").upper().strip()
                coin = str(r.get("coin") or "").upper().strip()
                module = str(r.get("module") or "-").strip() or "-"
                action = str(r.get("action") or "-").strip() or "-"
                oc = str(r.get("oc") or "").strip()
                dir0 = str(r.get("dir") or r.get("side") or "").lower().strip()
                if dir0 in ("buy", "多"):
                    dir0 = "long"
                elif dir0 in ("sell", "空"):
                    dir0 = "short"
                amt = float(_safe_float(r.get("amount_u"), 0.0) or _safe_float(r.get("notional_u"), 0.0) or _safe_float(r.get("notional"), 0.0) or 0.0)
                ts0 = float(_safe_float(r.get("ts"), 0.0) or 0.0)
                time_str = (datetime.fromtimestamp(ts0).strftime("%H:%M:%S") if ts0 > 0 else (str(r.get("time") or "--")[:8] or "--"))
                rsi_v, adx_v = _extract_rsi_adx_from_row(r, coin, ts0)

                if (not time_str) or time_str == "--":
                    bad += 1
                if (not coin) or coin == "-":
                    bad += 1
                if (not plat) or plat == "-":
                    bad += 1
                if (not module) or module == "-":
                    # 允许 module 缺失时显示 action，但这里自检更严格：提示但不直接 fail
                    bad += 1
                if (not action) or action == "-":
                    bad += 1
                if dir0 not in ("long", "short"):
                    bad += 1
                if amt <= 0:
                    bad += 1

                if (rsi_v is None) or (adx_v is None):
                    bad += 1

                # 网格模块若能取到层号，顺手校验格式正确；取不到不算失败（兼容旧日志）
                if str(module).upper() in ("M9", "W1"):
                    tag = _extract_grid_layer_tag_from_row(r)
                    if tag and (not re.fullmatch(r"L\d+", tag)):
                        bad += 1

            if bad > 0:
                print("[SELFTEST][FAIL] actions have incomplete fields. bad_count=", bad, "rows=", len(rows), "src=", str(fp))
                # 打印最近 3 条用于定位
                for rr in rows[-3:]:
                    print("[SELFTEST][FAIL][SAMPLE]", rr)
                return 7

            print(f"[SELFTEST][OK] actions fields complete + RSI/ADX: {len(rows)} rows src={str(fp)}")
    except Exception as e:
        print("[SELFTEST][FAIL] actions selftest error:", repr(e))
        return 8

    print("[SELFTEST][OK] basic parse/format passed")
    return 0




# ===== [PATCH][SELECTION_OBSERVE_UI_ENHANCE] 选币排名页：双击详情 + 排名变化 =====
def _panelui_build_selection_observe_tab(self, parent):
    hdr = ttk.Frame(parent, style='Card.TFrame')
    hdr.pack(fill='x', padx=12, pady=(12, 8))
    self.var_selection_observe_status = tk.StringVar(value='选币排名：等待数据…')
    self.var_selection_observe_meta = tk.StringVar(value='更新时间：- | 状态文件：- | 分组条数：-')
    ttk.Label(hdr, text='选币排名', style='CardTitle.TLabel').pack(anchor='w', padx=10, pady=(8, 2))
    ttk.Label(hdr, textvariable=self.var_selection_observe_status, style='SubValue.TLabel').pack(anchor='w', padx=10, pady=(0, 4))
    ttk.Label(hdr, textvariable=self.var_selection_observe_meta, style='Hint.TLabel', wraplength=1200, justify='left').pack(anchor='w', padx=10, pady=(0, 8))

    body = ttk.Frame(parent, style='Card.TFrame')
    body.pack(fill='both', expand=True, padx=12, pady=(0, 12))
    grid = ttk.Frame(body, style='Card.TFrame')
    grid.pack(fill='both', expand=True, padx=10, pady=10)
    for c in range(2):
        grid.grid_columnconfigure(c, weight=1)
    for r in range(2):
        grid.grid_rowconfigure(r, weight=1)
    self._selection_section_vars = {}
    self._selection_section_texts = {}
    self._selection_section_line_meta = {}
    sections = [
        ('binance', '币安原始排名'),
        ('merged', '交集后排名'),
        ('final', '最终候补/入选'),
        ('calc', '计算方式和数据'),
    ]
    for idx0,(key,title) in enumerate(sections):
        row,col = divmod(idx0,2)
        box = ttk.Frame(grid, style='Card.TFrame')
        box.grid(row=row, column=col, sticky='nsew', padx=6, pady=6)
        var = tk.StringVar(value=f'{title}[0]')
        self._selection_section_vars[key]=var
        ttk.Label(box, textvariable=var, style='CardTitle.TLabel').pack(anchor='w', padx=10, pady=(8,4))
        txt = tk.Text(box, height=11, bg='#0E0F12', fg='#E6E6E6', insertbackground='#E6E6E6', relief='flat', wrap='word', font=('Consolas',10))
        sy = ttk.Scrollbar(box, orient='vertical', command=txt.yview)
        txt.configure(yscrollcommand=sy.set)
        txt.pack(side='left', fill='both', expand=True, padx=(10,0), pady=(0,10))
        sy.pack(side='right', fill='y', padx=(0,10), pady=(0,10))
        try: txt.configure(state='disabled')
        except Exception: pass
        try: txt.bind('<Double-Button-1>', lambda e, kk=key: self._on_selection_section_double_click(kk, e))
        except Exception: pass
        self._selection_section_texts[key]=txt
        self._selection_section_line_meta[key]=[]


def _panelui_show_selection_detail_popup(self, title, content):
    try:
        win = tk.Toplevel(self.root if hasattr(self,'root') else None)
        win.title(title or '选币详情')
        try: win.geometry('760x460')
        except Exception: pass
        wrap = ttk.Frame(win, style='Card.TFrame'); wrap.pack(fill='both', expand=True, padx=10, pady=10)
        ttk.Label(wrap, text=title or '选币详情', style='CardTitle.TLabel').pack(anchor='w', padx=8, pady=(8,4))
        txt = tk.Text(wrap, bg='#0E0F12', fg='#E6E6E6', insertbackground='#E6E6E6', relief='flat', wrap='word', font=('Consolas',10))
        sy = ttk.Scrollbar(wrap, orient='vertical', command=txt.yview)
        txt.configure(yscrollcommand=sy.set)
        txt.pack(side='left', fill='both', expand=True, padx=(8,0), pady=(0,8))
        sy.pack(side='right', fill='y', padx=(0,8), pady=(0,8))
        txt.insert('1.0', content or '暂无详情')
        try: txt.configure(state='disabled')
        except Exception: pass
    except Exception:
        try: messagebox.showinfo(title or '选币详情', content or '暂无详情')
        except Exception: pass


def _panelui_on_selection_section_double_click(self, key, event=None):
    try:
        widget = (getattr(self, '_selection_section_texts', {}) or {}).get(key)
        if widget is None:
            return
        idx = widget.index(f'@{getattr(event,"x",0)},{getattr(event,"y",0)}') if event is not None else widget.index('insert')
        line_no = max(1, int(str(idx).split('.')[0]))
        line_txt = str(widget.get(f'{line_no}.0', f'{line_no}.end') or '').strip()
        meta_list = (getattr(self, '_selection_section_line_meta', {}) or {}).get(key) or []
        meta = meta_list[line_no-1] if 0 <= line_no-1 < len(meta_list) else {}
        detail = str(meta.get('detail') or line_txt or '暂无详情') if isinstance(meta, dict) else (line_txt or '暂无详情')
        try:
            self.root.clipboard_clear(); self.root.clipboard_append(line_txt or detail)
        except Exception:
            pass
        title_var = (getattr(self, '_selection_section_vars', {}) or {}).get(key)
        title = f'{title_var.get() if title_var else key} · 详情'
        _panelui_show_selection_detail_popup(self, title, detail)
        try:
            self.var_selection_observe_status.set(f'选币排名：已复制并打开详情 -> {line_txt or key}')
        except Exception:
            pass
    except Exception as e:
        try: self.var_selection_observe_status.set(f'选币排名：详情打开失败：{e}')
        except Exception: pass


def _panelui_render_selection_observe_tab(self, panel_status=None):
    try:
        if getattr(self, 'model', None) and hasattr(self.model, '_load_panel_status_payload'):
            st, fp_status_txt = self.model._load_panel_status_payload(panel_status, required_keys=['selection_observe', 'selection_panel_blocks', 'selection_panel_sections_text'])
        else:
            st = panel_status if isinstance(panel_status, dict) else {}
            fp_status_txt = '-'
        upd_txt = '-'
        try:
            cand_ts = None
            if isinstance(st, dict):
                sel = st.get('selection_observe') or {}
                for k in ('updated_at','ts','timestamp'):
                    v = (sel.get(k) if isinstance(sel, dict) else None) or st.get(k)
                    if v not in (None,'',0,'0'):
                        cand_ts = v; break
            if cand_ts not in (None,'',0,'0'):
                upd_txt = _fmt_report_dt(cand_ts)
            elif fp_status_txt not in ('', '-') and Path(fp_status_txt).exists():
                upd_txt = _fmt_report_dt(Path(fp_status_txt).stat().st_mtime)
        except Exception:
            pass
        blocks = []
        if isinstance(st, dict):
            blocks = list(st.get('selection_panel_blocks') or [])
        if not blocks and isinstance(st, dict):
            sections = st.get('selection_panel_sections_text') or {}
            if isinstance(sections, dict):
                mapping=[('binance','币安原始排名'),('merged','交集后排名'),('final','最终候补/入选'),('calc','计算方式和数据')]
                for key,title in mapping:
                    raw=str(sections.get(key) or '').strip()
                    ls=raw.splitlines() if raw else []
                    head = ls[0] if ls else f'{title}[0]'
                    lines = [x for x in ls[1:] if str(x).strip()]
                    blocks.append({'key':key,'title':head,'lines':lines,'details':lines})
        total_items=0
        mapping=[('binance','币安原始排名'),('merged','交集后排名'),('final','最终候补/入选'),('calc','计算方式和数据')]
        for key,title in mapping:
            block = next((b for b in blocks if isinstance(b,dict) and str(b.get('key') or '')==key), {})
            head = str(block.get('title') or f'{title}[0]')
            lines = [str(x) for x in list(block.get('lines') or []) if str(x).strip()]
            details = list(block.get('details') or [])
            if key in getattr(self,'_selection_section_vars',{}):
                self._selection_section_vars[key].set(head if head.startswith(title) else f'{title}[{len(lines)}]')
            metas=[]
            if lines:
                total_items += len(lines)
                for i,line in enumerate(lines):
                    det = details[i] if i < len(details) else line
                    metas.append({'detail': str(det)})
                self._selection_section_line_meta[key]=metas
                self._set_text_widget(self._selection_section_texts.get(key), '\n'.join(lines))
            else:
                self._selection_section_line_meta[key]=[]
                empty = '暂无该分组数据' if total_items > 0 or blocks else '暂无选币观察数据\n请先运行 bot，并等待状态文件写入 selection_observe / selection_panel_blocks / selection_panel_sections_text。'
                self._set_text_widget(self._selection_section_texts.get(key), empty)
        meta = (st.get('selection_observe') or {}).get('meta') if isinstance(st, dict) else {}
        scope = str((meta or {}).get('selection_scope') or '-')
        qh = str((meta or {}).get('queue_head') or '-')
        rule = str((meta or {}).get('rank_rule') or '-')
        if hasattr(self,'var_selection_observe_status'):
            self.var_selection_observe_status.set(f'选币排名：已加载 {total_items} 条 | scope={scope} | queue_head={qh}' if total_items>0 else '选币排名：未发现选币观察数据')
        if hasattr(self,'var_selection_observe_meta'):
            self.var_selection_observe_meta.set(f'更新时间：{upd_txt} | 状态文件：{fp_status_txt} | 分组条数：{total_items} | scope：{scope} | queue_head：{qh} | 公式：{rule}')
    except Exception as e:
        try:
            self.var_selection_observe_status.set(f'选币排名：面板渲染异常：{e}')
            self.var_selection_observe_meta.set(f'更新时间：- | 状态文件：{str(locals().get("fp_status_txt", "-") or "-")} | 分组条数：-')
        except Exception:
            pass

try:
    PanelUI._build_selection_observe_tab = _panelui_build_selection_observe_tab
    PanelUI._render_selection_observe_tab = _panelui_render_selection_observe_tab
    PanelUI._on_selection_section_double_click = _panelui_on_selection_section_double_click
    PanelUI._show_selection_detail_popup = _panelui_show_selection_detail_popup
except Exception:
    pass


# ===== [PATCH][OBSERVE_UI_BEAUTIFY_20260408] 趋势观察/选币排名：美观与易读性增强 =====
_ORIG_PANELUI_INIT_STYLE = getattr(PanelUI, '_init_style', None)


def _panelui_init_style_beautify(self):
    if callable(_ORIG_PANELUI_INIT_STYLE):
        _ORIG_PANELUI_INIT_STYLE(self)
    style = ttk.Style()
    try:
        style.configure('ObserveHeader.TFrame', background='#14161A')
        style.configure('ObserveBody.TFrame', background='#111214')
        style.configure('ObserveCard.TFrame', background='#171A20')
        style.configure('ObserveCardTitle.TLabel', background='#171A20', foreground='#F3F5F7', font=('Microsoft YaHei UI', 11, 'bold'))
        style.configure('ObserveCardHint.TLabel', background='#171A20', foreground='#8E98A7', font=('Microsoft YaHei UI', 9))
        style.configure('ObserveHero.TLabel', background='#14161A', foreground='#FFFFFF', font=('Microsoft YaHei UI', 12, 'bold'))
        style.configure('ObserveMeta.TLabel', background='#14161A', foreground='#A9B3BF', font=('Microsoft YaHei UI', 9))
        style.configure('ObserveTip.TLabel', background='#14161A', foreground='#7FA8FF', font=('Microsoft YaHei UI', 9, 'bold'))
        style.configure('TNotebook.Tab', padding=(18, 9, 18, 9))
    except Exception:
        pass


def _panelui_observe_compact_path(fp):
    try:
        s = str(fp or '').strip()
        if not s:
            return '-'
        s = s.replace('/', '\\')
        parts = [p for p in s.split('\\') if p]
        if len(parts) <= 4:
            return s
        return '...\\' + '\\'.join(parts[-4:])
    except Exception:
        return str(fp or '-')


def _panelui_observe_make_header(parent, title, status_var, meta_var, tip_text='双击条目可看详情；Shift+滚轮可横向滚动'):
    outer = tk.Frame(parent, bg='#111214', highlightthickness=0, bd=0)
    outer.pack(fill='x', padx=12, pady=(12, 8))
    card = tk.Frame(outer, bg='#14161A', highlightthickness=1, highlightbackground='#232834', bd=0)
    card.pack(fill='x')
    ttk.Label(card, text=title, style='ObserveHero.TLabel').pack(anchor='w', padx=12, pady=(10, 2))
    ttk.Label(card, textvariable=status_var, style='SubValue.TLabel').pack(anchor='w', padx=12, pady=(0, 3))
    ttk.Label(card, textvariable=meta_var, style='ObserveMeta.TLabel', justify='left').pack(anchor='w', padx=12, pady=(0, 4))
    ttk.Label(card, text=tip_text, style='ObserveTip.TLabel').pack(anchor='w', padx=12, pady=(0, 10))
    return card


def _panelui_observe_create_section(parent, title_var, hint_var, wrap='word'):
    card = tk.Frame(parent, bg='#171A20', highlightthickness=1, highlightbackground='#232834', bd=0)
    head = tk.Frame(card, bg='#171A20', bd=0)
    head.pack(fill='x', padx=10, pady=(8, 4))
    ttk.Label(head, textvariable=title_var, style='ObserveCardTitle.TLabel').pack(side='left', anchor='w')
    ttk.Label(head, textvariable=hint_var, style='ObserveCardHint.TLabel').pack(side='right', anchor='e')

    inner = tk.Frame(card, bg='#0D0F13', highlightthickness=1, highlightbackground='#20242E', bd=0)
    inner.pack(fill='both', expand=True, padx=10, pady=(0, 10))

    txt = tk.Text(
        inner,
        bg='#0D0F13', fg='#EAECEF', insertbackground='#EAECEF', relief='flat',
        wrap=wrap, font=('Consolas', 10), padx=10, pady=10, spacing1=1, spacing2=2, spacing3=2,
        selectbackground='#25314A', selectforeground='#FFFFFF', undo=False, bd=0, highlightthickness=0,
    )
    sy = ttk.Scrollbar(inner, orient='vertical', command=txt.yview)
    sx = ttk.Scrollbar(inner, orient='horizontal', command=txt.xview)
    txt.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
    txt.grid(row=0, column=0, sticky='nsew')
    sy.grid(row=0, column=1, sticky='ns')
    sx.grid(row=1, column=0, sticky='ew')
    inner.grid_rowconfigure(0, weight=1)
    inner.grid_columnconfigure(0, weight=1)

    try:
        txt.configure(state='disabled')
    except Exception:
        pass

    def _on_shift_wheel(event):
        try:
            delta = -1 if getattr(event, 'delta', 0) > 0 else 1
            txt.xview_scroll(delta, 'units')
        except Exception:
            pass
        return 'break'

    def _on_ctrl_a(event):
        try:
            txt.tag_add('sel', '1.0', 'end-1c')
            return 'break'
        except Exception:
            return None

    for seq in ('<Shift-MouseWheel>', '<Shift-Button-4>', '<Shift-Button-5>'):
        try:
            txt.bind(seq, _on_shift_wheel)
        except Exception:
            pass
    try:
        txt.bind('<Control-a>', _on_ctrl_a)
    except Exception:
        pass

    return card, txt


def _panelui_observe_config_tags(widget):
    try:
        widget.tag_configure('dim', foreground='#7D8796')
        widget.tag_configure('muted', foreground='#9AA4B2')
        widget.tag_configure('coin', foreground='#F4D35E', font=('Consolas', 10, 'bold'))
        widget.tag_configure('thead', foreground='#8FA8C9', font=('Consolas', 10, 'bold'))
        widget.tag_configure('long', foreground='#FF7B7B', font=('Consolas', 10, 'bold'))
        widget.tag_configure('short', foreground='#48D597', font=('Consolas', 10, 'bold'))
        widget.tag_configure('state', foreground='#7FA8FF', font=('Consolas', 10, 'bold'))
        widget.tag_configure('reason', foreground='#C9D1D9')
        widget.tag_configure('green', foreground='#48D597', font=('Consolas', 10, 'bold'))
        widget.tag_configure('red', foreground='#FF7B7B', font=('Consolas', 10, 'bold'))
        widget.tag_configure('amber', foreground='#F4D35E', font=('Consolas', 10, 'bold'))
        widget.tag_configure('divider', foreground='#2A3140')
    except Exception:
        pass


def _panelui_observe_clear(widget):
    try:
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        _panelui_observe_config_tags(widget)
    except Exception:
        pass


def _panelui_observe_finish(widget):
    try:
        widget.configure(state='disabled')
    except Exception:
        pass


def _panelui_parse_trend_line(line):
    raw = str(line or '').strip()
    if not raw:
        return {}
    parts = [p.strip() for p in raw.split('|') if str(p).strip()]
    if not parts:
        return {'raw': raw}
    out = {'raw': raw, 'coin': parts[0] if parts else ''}
    for idx, p in enumerate(parts[1:], start=1):
        if idx == 1 and p in ('LONG', 'SHORT', '-'):
            out['side'] = p
            continue
        if idx == 2 and '=' not in p and ('EXT' not in p) and ('VAR' not in p) and (not p.startswith('因')):
            out['state'] = p
            continue
        if p.startswith('冻'):
            m = re.search(r'EXT=([^/|]+)', p)
            if m:
                out['freeze_ext'] = m.group(1)
            m = re.search(r'VAR=([^|]+)', p)
            if m:
                out['freeze_var'] = m.group(1)
            continue
        if p.startswith('B2='):
            out['b2'] = p.split('=', 1)[1]
            continue
        if p.startswith('M10='):
            out['m10'] = p.split('=', 1)[1]
            continue
        if p.startswith('因='):
            out['reason'] = p.split('=', 1)[1]
            continue
        out.setdefault('extra', []).append(p)
    return out


def _panelui_trend_state_cn(state):
    mp = {
        'TREND_ALPHA': '主趋势',
        'TREND_CANDIDATE': '候补趋势',
        'EXIT_ONLY': '退出态',
        'NORMAL': '普通',
    }
    return mp.get(str(state or '').strip(), str(state or '-').strip() or '-')


def _panelui_render_trend_block(widget, items, line_meta, empty_text='暂无该分组数据'):
    _panelui_observe_clear(widget)
    try:
        if not items:
            widget.insert('end', empty_text, ('dim',))
            line_meta[:] = [{}]
            return
        new_meta = []
        for idx, item in enumerate(items, start=1):
            line = str(item.get('line') or '').strip()
            detail = str(item.get('detail') or line or '暂无详情')
            parsed = _panelui_parse_trend_line(line)
            coin = str(parsed.get('coin') or '-')
            side = str(parsed.get('side') or '-')
            side_tag = 'long' if side == 'LONG' else ('short' if side == 'SHORT' else 'muted')
            state_cn = _panelui_trend_state_cn(parsed.get('state'))
            freeze_ext = str(parsed.get('freeze_ext') or '0')
            freeze_var = str(parsed.get('freeze_var') or '0')
            b2 = str(parsed.get('b2') or '-')
            m10 = str(parsed.get('m10') or '-')
            reason = str(parsed.get('reason') or '').strip()
            extra = ' | '.join([str(x) for x in (parsed.get('extra') or []) if str(x).strip()])

            widget.insert('end', f'{idx:>2}. ', ('dim',))
            widget.insert('end', f'{coin:<10}', ('coin',))
            widget.insert('end', '  ')
            widget.insert('end', f'{side:<5}', (side_tag,))
            widget.insert('end', '  ')
            widget.insert('end', state_cn, ('state',))
            widget.insert('end', '\n')
            new_meta.append({'detail': detail})

            widget.insert('end', '    冻结 ', ('dim',))
            widget.insert('end', f'EXT={freeze_ext}', ('muted',))
            widget.insert('end', '  ')
            widget.insert('end', f'VAR={freeze_var}', ('muted',))
            widget.insert('end', '    B2 ', ('dim',))
            widget.insert('end', str(b2), ('amber' if str(b2).upper() == 'ON' else 'muted',))
            widget.insert('end', '    M10 ', ('dim',))
            widget.insert('end', str(m10), ('amber' if str(m10).upper() == 'ON' else 'muted',))
            widget.insert('end', '\n')
            new_meta.append({'detail': detail})

            if reason or extra:
                widget.insert('end', '    原因：', ('dim',))
                widget.insert('end', reason or '-', ('reason',))
                if extra:
                    widget.insert('end', f'    备注：{extra}', ('muted',))
                widget.insert('end', '\n')
                new_meta.append({'detail': detail})

            if idx < len(items):
                widget.insert('end', '─' * 34 + '\n', ('divider',))
                new_meta.append({'detail': detail})
        line_meta[:] = new_meta or [{}]
    finally:
        _panelui_observe_finish(widget)


def _panelui_parse_selection_line(line):
    raw = str(line or '').rstrip()
    m = re.match(r'^\s*(\d+)\.([A-Za-z0-9_\-]+)\s*\|\s*分=([\-0-9.]+)\s*\|\s*EXT=([^\s|]+)\s+VAR=([^\s|]+)\s*\|\s*最终=([^\s|]+)\s*\|\s*结果=(.+?)\s*$', raw)
    if not m:
        return {'raw': raw}
    return {
        'raw': raw,
        'rank': int(m.group(1)),
        'coin': m.group(2),
        'score': m.group(3),
        'ext': m.group(4),
        'var': m.group(5),
        'final': m.group(6),
        'result': m.group(7),
    }


def _panelui_render_selection_rank_block(widget, items, line_meta, empty_text='暂无该分组数据'):
    _panelui_observe_clear(widget)
    try:
        if not items:
            widget.insert('end', empty_text, ('dim',))
            line_meta[:] = [{}]
            return
        head = '名次  币种         分数     EXT   VAR  最终位  结果\n'
        widget.insert('end', head, ('thead',))
        widget.insert('end', '─' * 58 + '\n', ('divider',))
        new_meta = [{}, {}]
        for item in items:
            line = str(item.get('line') or '').strip()
            detail = str(item.get('detail') or line or '暂无详情')
            parsed = _panelui_parse_selection_line(line)
            if parsed.get('coin'):
                rank = int(parsed.get('rank') or 0)
                coin = str(parsed.get('coin') or '-')
                score = str(parsed.get('score') or '-')
                ext = str(parsed.get('ext') or '-')
                var = str(parsed.get('var') or '-')
                final = str(parsed.get('final') or '-')
                result = str(parsed.get('result') or '-')
                result_tag = 'green' if ('入选' in result or result.upper() in ('OK', 'PASS')) else ('red' if ('淘汰' in result or '失败' in result) else 'amber')
                widget.insert('end', f'{rank:>4}  ', ('dim',))
                widget.insert('end', f'{coin:<10}', ('coin',))
                widget.insert('end', f'  {score:>8}  ', ('muted',))
                widget.insert('end', f'{ext:<4} ', ('muted',))
                widget.insert('end', f'{var:<4} ', ('muted',))
                widget.insert('end', f'{final:>5}  ', ('amber',))
                widget.insert('end', result, (result_tag,))
                widget.insert('end', '\n')
            else:
                widget.insert('end', line + '\n', ('muted',))
            new_meta.append({'detail': detail})
        line_meta[:] = new_meta or [{}]
    finally:
        _panelui_observe_finish(widget)


def _panelui_render_selection_calc_block(widget, items, line_meta, empty_text='暂无该分组数据'):
    _panelui_observe_clear(widget)
    try:
        if not items:
            widget.insert('end', empty_text, ('dim',))
            line_meta[:] = [{}]
            return
        new_meta = []
        for idx, item in enumerate(items, start=1):
            line = str(item.get('line') or '').strip()
            detail = str(item.get('detail') or line or '暂无详情')
            if not line:
                continue
            if re.match(r'^步骤[:：]', line):
                widget.insert('end', line + '\n', ('thead',))
            elif re.match(r'^第\d+步', line):
                widget.insert('end', line + '\n', ('muted',))
            else:
                prefix = '• ' if not re.match(r'^\d+[.)、]', line) else ''
                widget.insert('end', prefix + line + '\n', ('reason',))
            new_meta.append({'detail': detail})
        line_meta[:] = new_meta or [{}]
    finally:
        _panelui_observe_finish(widget)


def _panelui_format_observe_meta(lines):
    try:
        return '\n'.join([str(x) for x in lines if str(x or '').strip()])
    except Exception:
        return ''


def _panelui_build_trend_observe_tab_beautify(self, parent):
    self.var_trend_observe_status = tk.StringVar(value='趋势观察：等待数据…')
    self.var_trend_observe_meta = tk.StringVar(value='更新时间：-\n状态文件：-')
    _panelui_observe_make_header(parent, '趋势观察', self.var_trend_observe_status, self.var_trend_observe_meta)

    body = tk.Frame(parent, bg='#111214', bd=0, highlightthickness=0)
    body.pack(fill='both', expand=True, padx=12, pady=(0, 12))
    grid = tk.Frame(body, bg='#111214', bd=0, highlightthickness=0)
    grid.pack(fill='both', expand=True)
    for c in range(2):
        grid.grid_columnconfigure(c, weight=1, uniform='trend')
    for r in range(2):
        grid.grid_rowconfigure(r, weight=1, uniform='trend')

    self._trend_section_vars = {}
    self._trend_section_hint_vars = {}
    self._trend_section_texts = {}
    self._trend_section_line_meta = {}
    sections = [
        ('alpha', '主趋势币'),
        ('candidate', '候补趋势币'),
        ('exit_only', '退出态币'),
        ('normal', '普通币'),
    ]
    for idx0, (key, title) in enumerate(sections):
        row, col = divmod(idx0, 2)
        title_var = tk.StringVar(value=f'{title} [0]')
        hint_var = tk.StringVar(value='双击条目看详情')
        card, txt = _panelui_observe_create_section(grid, title_var, hint_var, wrap='word')
        card.grid(row=row, column=col, sticky='nsew', padx=6, pady=6)
        try:
            txt.bind('<Double-Button-1>', lambda e, kk=key: self._on_trend_section_double_click(kk, e))
        except Exception:
            pass
        self._trend_section_vars[key] = title_var
        self._trend_section_hint_vars[key] = hint_var
        self._trend_section_texts[key] = txt
        self._trend_section_line_meta[key] = []


def _panelui_render_trend_observe_tab_beautify(self, panel_status=None):
    try:
        if getattr(self, 'model', None) and hasattr(self.model, '_load_panel_status_payload'):
            st, fp_status_txt = self.model._load_panel_status_payload(panel_status, required_keys=['trend_observe', 'trend_panel_blocks', 'trend_panel_sections_text'])
        else:
            st = panel_status if isinstance(panel_status, dict) else {}
            fp_status_txt = '-'

        trend_update_txt = '-'
        try:
            cand_ts = None
            if isinstance(st, dict):
                for k in ('trend_updated_at', 'trend_observe_updated_at', 'trend_last_update_ts', 'trend_update_ts', 'updated_at', 'status_ts', 'ts'):
                    v = st.get(k)
                    if v not in (None, '', 0, '0'):
                        cand_ts = v
                        break
                if cand_ts in (None, '', 0, '0'):
                    obs0 = st.get('trend_observe') or {}
                    if isinstance(obs0, dict):
                        for k in ('updated_at', 'ts', 'timestamp', 'last_update_ts'):
                            v = obs0.get(k)
                            if v not in (None, '', 0, '0'):
                                cand_ts = v
                                break
            if cand_ts not in (None, '', 0, '0'):
                trend_update_txt = _fmt_report_dt(cand_ts)
            elif fp_status_txt not in ('', '-') and Path(fp_status_txt).exists():
                trend_update_txt = _fmt_report_dt(Path(fp_status_txt).stat().st_mtime)
        except Exception:
            trend_update_txt = '-'

        groups_txt = {}
        groups_meta = {'alpha': [], 'candidate': [], 'exit_only': [], 'normal': []}
        sections = (((st.get('trend_panel_sections_text') if isinstance(st, dict) else {}) or {}) if isinstance(st, dict) else {})
        if isinstance(sections, dict) and sections:
            groups_txt = {k: str(sections.get(k) or '') for k in ('alpha', 'candidate', 'exit_only', 'normal')}
        if not any(groups_txt.values()) and isinstance(st, dict):
            blocks = st.get('trend_panel_blocks') or []
            if isinstance(blocks, list):
                for b in blocks:
                    if not isinstance(b, dict):
                        continue
                    key = str(b.get('key') or '')
                    lines = b.get('lines') or []
                    title = str(b.get('title') or '')
                    if key:
                        groups_txt[key] = '\n'.join([title] + [str(x) for x in lines]) if lines else title
                        groups_meta[key] = [{'detail': str(x), 'line': str(x)} for x in lines]
        if not any(groups_txt.values()) and isinstance(st, dict):
            obs = st.get('trend_observe') or {}
            groups = obs.get('groups') if isinstance(obs, dict) else {}
            counts = obs.get('group_counts') if isinstance(obs, dict) else {}
            if isinstance(groups, dict):
                name_map = {'alpha': '主趋势币', 'candidate': '候补趋势币', 'exit_only': '退出态币', 'normal': '普通币'}
                for key in ('alpha', 'candidate', 'exit_only', 'normal'):
                    items = groups.get(key) or []
                    title = f"{name_map.get(key, key)}[{int((counts or {}).get(key, len(items)) if isinstance(counts, dict) else len(items))}]"
                    lines = []
                    if isinstance(items, list):
                        for it in items:
                            if isinstance(it, dict):
                                line = str(it.get('line') or it.get('text') or it.get('summary') or '')
                                if not line:
                                    coin = str(it.get('coin') or '').upper()
                                    side = str(it.get('trend_side') or it.get('side') or '')
                                    state = str(it.get('trend_state') or '')
                                    reason = str(it.get('trend_reason') or '')
                                    line = '|'.join([x for x in (coin, side, state, reason) if x])
                                if line:
                                    lines.append(line)
                                    detail_parts = [line]
                                    for kk, lab in (('coin', '币种'), ('trend_side', '趋势方向'), ('trend_state', '趋势状态'), ('trend_reason', '趋势原因'), ('opp_side_frozen', '对侧冻结'), ('runner_protect', 'runner保护'), ('b2_guard', 'B2保护'), ('m10_guard', 'M10保护')):
                                        vv = it.get(kk) if isinstance(it, dict) else None
                                        if vv not in (None, '', [], {}):
                                            detail_parts.append(f'{lab}: {vv}')
                                    groups_meta[key].append({'detail': '\n'.join(detail_parts), 'line': line})
                            elif str(it).strip():
                                txt0 = str(it).strip()
                                lines.append(txt0)
                                groups_meta[key].append({'detail': txt0, 'line': txt0})
                    groups_txt[key] = '\n'.join([title] + lines) if lines else title

        name_map = {'alpha': '主趋势币', 'candidate': '候补趋势币', 'exit_only': '退出态币', 'normal': '普通币'}
        total_items = 0
        has_any_group = any(str(groups_txt.get(k) or '').strip() for k in ('alpha', 'candidate', 'exit_only', 'normal'))
        empty_hint = '暂无趋势分组数据\n请先运行 bot，并等待状态文件写入 trend_observe / trend_panel_blocks / trend_panel_sections_text。'
        for key in ('alpha', 'candidate', 'exit_only', 'normal'):
            raw = str(groups_txt.get(key) or '').strip()
            title = name_map.get(key, key)
            lines = []
            metas = list(groups_meta.get(key) or [])
            if raw:
                ls = raw.splitlines()
                if ls:
                    head = ls[0].strip()
                    body = [x for x in ls[1:] if str(x).strip()]
                    if not head.startswith(title):
                        head = title if body else f'{title}[0]'
                    lines = body
                    try:
                        self._trend_section_vars[key].set(head.replace('[', ' [') if '[' in head and ' [' not in head else head)
                        self._trend_section_hint_vars[key].set(f'共 {len(lines)} 条')
                    except Exception:
                        pass
                total_items += len(lines)
                if not metas:
                    metas = [{'detail': str(x), 'line': str(x)} for x in lines]
                items = [{'line': str(x), 'detail': str(metas[i].get('detail') if i < len(metas) and isinstance(metas[i], dict) else x)} for i, x in enumerate(lines)]
                _panelui_render_trend_block(self._trend_section_texts.get(key), items, self._trend_section_line_meta[key], empty_text='暂无该分组数据')
            else:
                try:
                    self._trend_section_vars[key].set(f'{title} [0]')
                    self._trend_section_hint_vars[key].set('暂无数据')
                except Exception:
                    pass
                _panelui_render_trend_block(self._trend_section_texts.get(key), [], self._trend_section_line_meta[key], empty_text='暂无该分组数据' if has_any_group else empty_hint)

        if hasattr(self, 'var_trend_observe_status'):
            self.var_trend_observe_status.set(f'趋势观察：已加载分组，共 {total_items} 条' if total_items > 0 else '趋势观察：未发现趋势分组数据')
        if hasattr(self, 'var_trend_observe_meta'):
            self.var_trend_observe_meta.set(_panelui_format_observe_meta([
                f'更新时间：{trend_update_txt}    分组条数：{total_items}',
                f'状态文件：{_panelui_observe_compact_path(fp_status_txt)}',
            ]))
    except Exception as e:
        try:
            if hasattr(self, 'var_trend_observe_status'):
                self.var_trend_observe_status.set(f'趋势观察：面板渲染异常：{e}')
            if hasattr(self, 'var_trend_observe_meta'):
                self.var_trend_observe_meta.set(_panelui_format_observe_meta([
                    '更新时间：-',
                    f'状态文件：{_panelui_observe_compact_path(str(locals().get("fp_status_txt", "-") or "-"))}',
                ]))
        except Exception:
            pass


def _panelui_build_selection_observe_tab_beautify(self, parent):
    self.var_selection_observe_status = tk.StringVar(value='选币排名：等待数据…')
    self.var_selection_observe_meta = tk.StringVar(value='更新时间：-\n状态文件：-')
    _panelui_observe_make_header(parent, '选币排名', self.var_selection_observe_status, self.var_selection_observe_meta, tip_text='左边看排名，右下看公式；双击条目可看详情')

    body = tk.Frame(parent, bg='#111214', bd=0, highlightthickness=0)
    body.pack(fill='both', expand=True, padx=12, pady=(0, 12))
    grid = tk.Frame(body, bg='#111214', bd=0, highlightthickness=0)
    grid.pack(fill='both', expand=True)
    for c in range(2):
        grid.grid_columnconfigure(c, weight=1, uniform='selection')
    for r in range(2):
        grid.grid_rowconfigure(r, weight=1, uniform='selection')

    self._selection_section_vars = {}
    self._selection_section_hint_vars = {}
    self._selection_section_texts = {}
    self._selection_section_line_meta = {}
    sections = [
        ('binance', '币安原始排名', '横向滚动可看完整列', 'none'),
        ('merged', '交集后排名', '横向滚动可看完整列', 'none'),
        ('final', '最终候补/入选', '横向滚动可看完整列', 'none'),
        ('calc', '计算方式和数据', '规则说明与明细', 'word'),
    ]
    for idx0, (key, title, hint, wrap_mode) in enumerate(sections):
        row, col = divmod(idx0, 2)
        title_var = tk.StringVar(value=f'{title} [0]')
        hint_var = tk.StringVar(value=hint)
        card, txt = _panelui_observe_create_section(grid, title_var, hint_var, wrap=wrap_mode)
        card.grid(row=row, column=col, sticky='nsew', padx=6, pady=6)
        try:
            txt.bind('<Double-Button-1>', lambda e, kk=key: self._on_selection_section_double_click(kk, e))
        except Exception:
            pass
        self._selection_section_vars[key] = title_var
        self._selection_section_hint_vars[key] = hint_var
        self._selection_section_texts[key] = txt
        self._selection_section_line_meta[key] = []


def _panelui_render_selection_observe_tab_beautify(self, panel_status=None):
    try:
        if getattr(self, 'model', None) and hasattr(self.model, '_load_panel_status_payload'):
            st, fp_status_txt = self.model._load_panel_status_payload(panel_status, required_keys=['selection_observe', 'selection_panel_blocks', 'selection_panel_sections_text'])
        else:
            st = panel_status if isinstance(panel_status, dict) else {}
            fp_status_txt = '-'
        upd_txt = '-'
        try:
            cand_ts = None
            if isinstance(st, dict):
                sel = st.get('selection_observe') or {}
                for k in ('updated_at', 'ts', 'timestamp'):
                    v = (sel.get(k) if isinstance(sel, dict) else None) or st.get(k)
                    if v not in (None, '', 0, '0'):
                        cand_ts = v
                        break
            if cand_ts not in (None, '', 0, '0'):
                upd_txt = _fmt_report_dt(cand_ts)
            elif fp_status_txt not in ('', '-') and Path(fp_status_txt).exists():
                upd_txt = _fmt_report_dt(Path(fp_status_txt).stat().st_mtime)
        except Exception:
            pass

        section_map = {'binance': [], 'merged': [], 'final': [], 'calc': []}
        blocks = list(st.get('selection_panel_blocks') or []) if isinstance(st, dict) else []
        if blocks:
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                key = str(b.get('key') or '')
                lines = [str(x) for x in list(b.get('lines') or []) if str(x).strip()]
                details = list(b.get('details') or [])
                if key in section_map:
                    section_map[key] = [{'line': line, 'detail': str(details[i]) if i < len(details) else line} for i, line in enumerate(lines)]
        if not any(section_map.values()) and isinstance(st, dict):
            sections = st.get('selection_panel_sections_text') or {}
            if isinstance(sections, dict):
                for key, title in [('binance', '币安原始排名'), ('merged', '交集后排名'), ('final', '最终候补/入选'), ('calc', '计算方式和数据')]:
                    raw = str(sections.get(key) or '').strip()
                    ls = raw.splitlines() if raw else []
                    lines = [x for x in ls[1:] if str(x).strip()] if ls else []
                    section_map[key] = [{'line': line, 'detail': line} for line in lines]

        total_items = 0
        mapping = [('binance', '币安原始排名'), ('merged', '交集后排名'), ('final', '最终候补/入选'), ('calc', '计算方式和数据')]
        for key, title in mapping:
            items = list(section_map.get(key) or [])
            try:
                self._selection_section_vars[key].set(f'{title} [{len(items)}]')
            except Exception:
                pass
            if key == 'calc':
                try:
                    self._selection_section_hint_vars[key].set(f'共 {len(items)} 行规则')
                except Exception:
                    pass
                _panelui_render_selection_calc_block(self._selection_section_texts.get(key), items, self._selection_section_line_meta[key], empty_text='暂无该分组数据')
                total_items += len(items)
            else:
                try:
                    self._selection_section_hint_vars[key].set('横向滚动可看完整列' if items else '暂无数据')
                except Exception:
                    pass
                _panelui_render_selection_rank_block(self._selection_section_texts.get(key), items, self._selection_section_line_meta[key], empty_text='暂无该分组数据')
                total_items += len(items)

        meta = (st.get('selection_observe') or {}).get('meta') if isinstance(st, dict) else {}
        scope = str((meta or {}).get('selection_scope') or '-')
        qh = str((meta or {}).get('queue_head') or '-')
        rule = str((meta or {}).get('rank_rule') or '-')
        if hasattr(self, 'var_selection_observe_status'):
            self.var_selection_observe_status.set(f'选币排名：已加载 {total_items} 条 | scope={scope} | queue_head={qh}' if total_items > 0 else '选币排名：未发现选币观察数据')
        if hasattr(self, 'var_selection_observe_meta'):
            self.var_selection_observe_meta.set(_panelui_format_observe_meta([
                f'更新时间：{upd_txt}    分组条数：{total_items}',
                f'状态文件：{_panelui_observe_compact_path(fp_status_txt)}',
                f'scope：{scope}',
                f'公式：{rule}',
            ]))
    except Exception as e:
        try:
            self.var_selection_observe_status.set(f'选币排名：面板渲染异常：{e}')
            self.var_selection_observe_meta.set(_panelui_format_observe_meta([
                '更新时间：-',
                f'状态文件：{_panelui_observe_compact_path(str(locals().get("fp_status_txt", "-") or "-"))}',
            ]))
        except Exception:
            pass


try:
    PanelUI._init_style = _panelui_init_style_beautify
    PanelUI._build_trend_observe_tab = _panelui_build_trend_observe_tab_beautify
    PanelUI._render_trend_observe_tab = _panelui_render_trend_observe_tab_beautify
    PanelUI._build_selection_observe_tab = _panelui_build_selection_observe_tab_beautify
    PanelUI._render_selection_observe_tab = _panelui_render_selection_observe_tab_beautify
except Exception:
    pass


def _panel_enable_hidpi() -> None:
    """高分屏：声明 DPI 感知，避免 Windows 把低分辨率位图拉伸导致发糊。必须在创建 Tk 之前调用。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # System DPI aware
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _panel_apply_hidpi_scale(root: "tk.Tk") -> float:
    """按屏幕 DPI 放大面板里写死的像素尺寸（窗口/最小尺寸/表格列宽/行高/边距/换行宽度/画布）。
    字体是磅值，Tk 已按真实 DPI 自动缩放，这里不动字体。返回缩放倍数。"""
    try:
        f = float(root.winfo_fpixels("1i")) / 96.0
    except Exception:
        return 1.0
    if f < 1.1:
        return 1.0

    def _px(v: Any) -> Any:
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            return int(round(v * f))
        if isinstance(v, float):
            return v * f
        if isinstance(v, (tuple, list)):
            return type(v)(_px(x) for x in v)
        if isinstance(v, str) and re.fullmatch(r"\d+( \d+)*", v.strip() or "x"):
            return " ".join(str(int(round(int(x) * f))) for x in v.split())
        return v

    def _scale_keys(d: Any, keys: tuple) -> Any:
        if isinstance(d, dict):
            d = dict(d)
            for k in keys:
                if k in d and d[k] is not None:
                    d[k] = _px(d[k])
        return d

    _geo_re = re.compile(r"^(\d+)x(\d+)(.*)$")
    _orig_geometry = tk.Wm.wm_geometry

    def _wm_geometry(self, newGeometry=None):
        if isinstance(newGeometry, str):
            m = _geo_re.match(newGeometry.strip())
            if m:
                newGeometry = f"{_px(int(m.group(1)))}x{_px(int(m.group(2)))}{m.group(3)}"
        return _orig_geometry(self, newGeometry)
    tk.Wm.wm_geometry = tk.Wm.geometry = _wm_geometry

    _orig_minsize = tk.Wm.wm_minsize

    def _wm_minsize(self, width=None, height=None):
        return _orig_minsize(self, _px(width) if width is not None else None, _px(height) if height is not None else None)
    tk.Wm.wm_minsize = tk.Wm.minsize = _wm_minsize

    _pad_keys = ("padx", "pady", "ipadx", "ipady")
    _orig_pack = tk.Pack.pack_configure

    def _pack_configure(self, cnf={}, **kw):
        return _orig_pack(self, _scale_keys(cnf, _pad_keys), **_scale_keys(kw, _pad_keys))
    tk.Pack.pack_configure = tk.Pack.pack = _pack_configure

    _orig_grid = tk.Grid.grid_configure

    def _grid_configure(self, cnf={}, **kw):
        return _orig_grid(self, _scale_keys(cnf, _pad_keys), **_scale_keys(kw, _pad_keys))
    tk.Grid.grid_configure = tk.Grid.grid = _grid_configure

    # 只在构造时放大（后续 configure 里的 wraplength 等常由 winfo 实测像素算出，不能再乘）
    _orig_bw_init = tk.BaseWidget.__init__

    def _bw_init(self, master, widgetName, cnf={}, kw={}, extra=()):
        keys = ("wraplength", "padding", "padx", "pady")
        if str(widgetName).lower().endswith("canvas"):
            keys = keys + ("width", "height")
        return _orig_bw_init(self, master, widgetName, _scale_keys(cnf, keys), _scale_keys(kw, keys), extra)
    tk.BaseWidget.__init__ = _bw_init

    _orig_tv_column = ttk.Treeview.column

    def _tv_column(self, column, option=None, **kw):
        return _orig_tv_column(self, column, option, **_scale_keys(kw, ("width", "minwidth")))
    ttk.Treeview.column = _tv_column

    _orig_style_cfg = ttk.Style.configure

    def _style_configure(self, style, query_opt=None, **kw):
        return _orig_style_cfg(self, style, query_opt, **_scale_keys(kw, ("rowheight", "padding")))
    ttk.Style.configure = _style_configure
    return f


def main() -> int:
    _hide_console_windows()
    cfg = PanelConfig.load()
    _panel_enable_hidpi()
    root = tk.Tk()
    _panel_apply_hidpi_scale(root)
    model = PanelModel(cfg)
    PanelUI(root, model)
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        rc = _panel_selftest()
        if rc is not None:
            raise SystemExit(rc)
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        import traceback as _tb
        import time as _time
        _ts = _time.strftime("%Y%m%d_%H%M%S")
        _here = os.path.abspath(os.path.dirname(__file__))
        _log_candidates = [
            os.path.join(_here, f"panel_crash_{_ts}.log"),
            os.path.join(_here, "out", f"panel_crash_{_ts}.log"),
            os.path.join(_here, "logs", f"panel_crash_{_ts}.log"),
        ]
        _log_path = None
        _crash_text = _tb.format_exc()
        for _lp in _log_candidates:
            try:
                os.makedirs(os.path.dirname(_lp), exist_ok=True)
                with open(_lp, "w", encoding="utf-8", errors="ignore") as f:
                    f.write(_crash_text)
                _log_path = _lp
                break
            except Exception:
                continue

        # show GUI error message if possible (double-click friendly)
        try:
            import tkinter as _tk
            from tkinter import messagebox as _mb
            _r = _tk.Tk()
            _r.withdraw()
            _msg = (
                "面板启动时发生异常，已写入崩溃日志：\\n"
                + str(_log_path or "(写日志失败)")
                + "\\n\\n请把这个日志文件发给我，我会按根因修复。\\n\\n异常摘要：\\n"
                + repr(e)
            )
            _mb.showerror("面板启动失败", _msg)
            try:
                _r.destroy()
            except Exception:
                pass
        except Exception:
            pass

        # also print (for cmd run)
        try:
            print("[PANEL][FATAL] crash_log =", _log_path)
            print(_crash_text)
        except Exception:
            pass
        raise
