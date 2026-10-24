"""Read-only legacy reconciliation replay. No runtime activation or live writes."""
from pathlib import Path
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bot_core.legacy_reconciliation import replay


REASONS = {
    'already_terminal':'已结算，保留原状态',
    'separate_m9_m10_confirmation_policy':'M9/M10 专用确认，保留数量增量校验，不套用 50% 规则',
    'not_legacy_open_scope':'非旧开仓记录，保持原确认规则',
    'snapshot_not_fresh_or_complete':'快照过期、早于提交或不完整，需要新的完整扫描',
    'position_unavailable_or_not_covered':'此快照未可靠覆盖该币，缺失不能视为空仓',
    'position_side_mismatch':'当前方向与原意图相反，继续待确认',
    'position_quantity_unavailable':'没有可靠的持仓数量，继续待确认',
    'intent_or_notional_unavailable':'意图金额或当前名义金额不可用',
    'position_below_half_intent':'同向仓位低于原意图金额的 50%',
    'legacy_same_side_at_least_half_intent':'同向仓位达到原意图金额的 50%，仅结算旧占位',
    'known_baseline_quantity_delta':'有下单前数量，按数量增量确认',
    'later_confirmed_platform_close_and_fresh_empty':'后来已有平台全平成功证据，且此快照明确空仓',
    'working_order_not_resolved':'仍有活动订单证据，不解除',
    'duplicate_empty_snapshot':'同一空仓快照不能重复累计，需要下一次独立扫描',
    'empty_scans_too_close':'空仓扫描间隔不足 30 秒',
    'needs_three_fresh_empty_scans':'独立新鲜空仓证据不足三次或订单年龄不足两分钟',
    'observed_position_now_flat':'曾观测到持仓，连续三次新鲜扫描确认现已空仓',
    'no_fill_after_three_fresh_empty_scans':'无已成交/活动订单证据，连续三次新鲜空仓推定未成交',
}


def run(journal_path, positions_path, output_dir=None, *, as_of=None):
    journal_path, positions_path = Path(journal_path).resolve(), Path(positions_path).resolve()
    journal_raw, snapshot_raw = journal_path.read_bytes(), positions_path.read_bytes()
    journal = json.loads(journal_raw.decode('utf-8-sig'))
    snapshot = json.loads(snapshot_raw.decode('utf-8-sig'))
    if not isinstance(journal.get('records'), dict):
        raise ValueError('journal_records_missing')
    now = float(snapshot.get('ts') or 0) if as_of is None else float(as_of)
    if not math.isfinite(now) or now <= 0:
        raise ValueError('valid_replay_time_required')
    folder = Path(output_dir or tempfile.mkdtemp(prefix='bot_legacy_replay_')).resolve()
    if folder.is_relative_to(ROOT):
        parts = folder.relative_to(ROOT).parts
        if not parts or not parts[0].startswith('audit_'):
            raise ValueError('replay_output_must_be_temporary_or_audit_directory')
    if folder in (journal_path.parent, positions_path.parent):
        raise ValueError('do_not_write_replay_outputs_beside_live_inputs')
    folder.mkdir(parents=True,exist_ok=True)
    outputs = [folder/'replay.json',folder/'replay.txt',folder/'proposed_journal.json']
    if any(p.exists() for p in outputs):
        raise FileExistsError('replay_output_already_exists')
    updated, decisions = replay(journal['records'],snapshot,now=now,
        scan_id='replay:' + hashlib.sha256(snapshot_raw).hexdigest())
    counts = dict(Counter(r['proposed_state'] for r in decisions))
    changes = [r for r in decisions if r['proposed_state'] != r['original_state']]
    result = {'mode':'preview_only','runtime_auto_reconciliation_enabled':False,
        'replayed_at':datetime.fromtimestamp(now, timezone.utc).astimezone().isoformat(),
        'uses_snapshot_time_as_replay_clock':as_of is None,'total_records':len(decisions),
        'proposed_state_counts':counts,'changed_records':len(changes),
        'source_journal_sha256':hashlib.sha256(journal_raw).hexdigest(),
        'source_snapshot_sha256':hashlib.sha256(snapshot_raw).hexdigest(),
        'decisions':decisions}
    outputs[0].write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    outputs[2].write_text(json.dumps(dict(journal,records=updated),ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['旧待确认记录对账回放（仅预览，未写入机器人）',
        '回放时刻：'+result['replayed_at'],
        '采用输入快照时刻模拟判断；不代表当前实时持仓。上线时必须重新读取新鲜完整扫描。',
        '只结算记录，不新增成交、手续费、利润或网格层。',
        f"记录总数：{len(decisions)}；建议改变状态：{len(changes)}；建议状态统计：{counts}",'',
        '序号 | 记录键 | 原状态 -> 建议状态 | 快照中的方向/名义金额 | 依据']
    for i,row in enumerate(decisions,1):
        value=row['current_notional_u'];value='-' if value is None else f'{value:.4f} U'
        reason=REASONS.get(row['reason'],row['reason'])
        if row.get('minimum_required_u') is not None:
            reason+=f"；门槛={row['minimum_required_u']:.4f} U"
        if row.get('empty_scan_count') is not None:
            reason+=f"；已累计={row['empty_scan_count']}/3"
        lines.append(f"{i:02d} | {row['record_key']} | {row['original_state']} -> {row['proposed_state']} | {row['current_side'] or '-'} / {value} | {reason}")
    outputs[1].write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return result, folder


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal',required=True)
    parser.add_argument('--positions',required=True)
    parser.add_argument('--output-dir')
    parser.add_argument('--as-of',type=float)
    args=parser.parse_args()
    result,folder=run(args.journal,args.positions,args.output_dir,as_of=args.as_of)
    print(json.dumps({'mode':result['mode'],'records':result['total_records'],
        'changes':result['changed_records'],'states':result['proposed_state_counts'],
        'report':str(folder/'replay.txt')},ensure_ascii=False))


if __name__=='__main__':main()
