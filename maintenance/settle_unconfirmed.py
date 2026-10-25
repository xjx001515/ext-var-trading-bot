"""Inspect or enqueue an audited settlement; never edits the live journal directly."""
from pathlib import Path
import argparse
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from bot_core.legacy_reconciliation import fingerprint, TERMINAL
from bot_core.settlement_commands import SettlementInbox, proposal


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir',type=Path,default=ROOT/'out')
    sub=parser.add_subparsers(dest='action',required=True)
    show=sub.add_parser('list');show.add_argument('--all',action='store_true')
    settle=sub.add_parser('settle')
    settle.add_argument('--key',required=True)
    settle.add_argument('--state',required=True,choices=sorted(TERMINAL))
    settle.add_argument('--reason',required=True)
    settle.add_argument('--expected',required=True,help='list 输出的完整记录 fingerprint')
    settle.add_argument('--special-review',action='store_true',help='仅允许 M9/M10 明确结算为 CONFIRMED_NOT_FILLED')
    settle.add_argument('--apply',action='store_true',help='入队；机器人下次完整扫描时核对并结算')
    check=sub.add_parser('status');check.add_argument('id')
    args=parser.parse_args(argv)
    inbox=SettlementInbox(args.out_dir)
    if args.action=='status':
        result=inbox.status(args.id)
    else:
        path=args.out_dir/'state/execution_unconfirmed.json'
        records=json.loads(path.read_text(encoding='utf-8-sig'))['records']
        if args.action=='list':
            result={'records':[{'key':k,'state':r.get('state'),'module':r.get('module'),
                'side':r.get('side'),'intent_usd':r.get('intent_usd'),'submitted_ts':r.get('ts'),
                'fingerprint':fingerprint(r),'reconciliation':r.get('reconciliation_last')}
                for k,r in records.items() if args.all or r.get('state') not in TERMINAL]}
        else:
            command=proposal(args.key,records.get(args.key),state=args.state,reason=args.reason,
                expected_fingerprint=args.expected,now=time.time(),allow_special=bool(args.special_review))
            result={'mode':'QUEUED' if args.apply else 'PREVIEW','command':command,
                'message':'仅结算状态，不下单、不生成成交、利润或网格层。实际结果以 status 回执为准。'}
            if args.apply:inbox.enqueue(command)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return result


if __name__=='__main__':
    try:main()
    except (ValueError,KeyError,OSError) as exc:
        raise SystemExit('未执行：'+str(exc))
