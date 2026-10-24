"""Read-only margin evidence. Missing/ambiguous UI numbers never resize an order."""
import math
import re


def money(text):
    value = str(text or '').strip().replace('\u2212', '-')
    match = re.fullmatch(r'\$?\s*([-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(?:USD|USDC|USDT)?', value, re.I)
    if not match:
        return None
    number = float(match[1].replace(',', ''))
    return number if math.isfinite(number) and number >= 0 else None


def labeled_values(text, labels):
    number = r'[-+]?\d[\d,]*(?:\.\d+)?'
    pattern = r'(?:' + '|'.join(re.escape(x) for x in labels) + r')\s*:?\s*(\$\s*' + number + r'(?:[ \t]*USD[CT]?)?|' + number + r'[ \t]*USD[CT]?)(?![A-Za-z0-9.])'
    return [v for match in re.finditer(pattern, str(text or ''), re.I)
            if (v := money(match[1])) is not None]


AVAILABLE_LABELS = ('Available To Trade', 'Available Margin', 'Free Margin', 'Available Collateral')
COST_LABELS = ('Cost', 'Required Margin', 'Margin Required', 'Initial Margin', 'Order Margin')


def decide(evidence):
    e = evidence if isinstance(evidence, dict) else {}
    available = set(labeled_values(e.get('available_text', ''), AVAILABLE_LABELS))
    cost = set(labeled_values(e.get('cost_text', ''), COST_LABELS))
    av = next(iter(available)) if len(available) == 1 else None
    required = next(iter(cost)) if len(cost) == 1 else None
    detail = {'available_u': av, 'required_u': required, 'source': 'visible_order_form',
              'side_cost_bound': bool(e.get('side_cost_bound')), 'amount_changed': False}
    if av is None or required is None or required <= 0 or not e.get('side_cost_bound'):
        detail['reason'] = 'available_margin_or_cost_unreadable'
        return True, detail
    enough = av + 1e-9 >= required
    detail.update(reason='available_margin_ok' if enough else 'insufficient_available_margin',
                  shortfall_u=max(0.0, required - av))
    return enough, detail


READ_QUOTE_JS = r"""button => {
  const visible = e => { const r=e.getBoundingClientRect(), s=getComputedStyle(e);
    return r.width>0 && r.height>0 && s.visibility!=='hidden' && s.display!=='none'; };
  const br=button.getBoundingClientRect();
  const av=/^(Available To Trade|Available Margin|Free Margin|Available Collateral)\s*:?\s*\$?\s*\d/i;
  const cost=/^(Cost|Required Margin|Margin Required|Initial Margin|Order Margin)\s*:?\s*\$?\s*\d/i;
  const available=[], costs=[];
  // Read text only. Never click, focus, navigate, alter fields or use balance/equity
  // as available margin. Labels from unrelated history tables are excluded.
  for (const el of document.querySelectorAll('div,span,p,label')) {
    if (!visible(el) || el.childElementCount>4) continue;
    const text=(el.innerText||'').trim();
    if (!text || text.length>140) continue;
    if ([...el.children].some(c => (c.innerText||'').trim()===text)) continue;
    const r=el.getBoundingClientRect();
    const aligned = Math.abs((r.left+r.right)/2-(br.left+br.right)/2) <= Math.max(br.width*0.8,80);
    const above = r.bottom <= br.bottom+4 && br.top-r.bottom < 260;
    if (av.test(text)) available.push(text);
    const costLabels = text.match(/\b(Cost|Required Margin|Margin Required|Initial Margin|Order Margin)\b/ig)||[];
    if (cost.test(text) && costLabels.length===1 && aligned && above) costs.push(text);
  }
  return {available_text:[...new Set(available)].join('\n'),
          cost_text:[...new Set(costs)].join('\n'), side_cost_bound:costs.length>0};
}"""
