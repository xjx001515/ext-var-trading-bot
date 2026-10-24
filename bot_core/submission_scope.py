"""Operation-scoped submission claims; no IO, time expiry or legacy settlement."""
from .execution_confirmation import pending_blocks


def family(kind):
    value = str(kind or '').strip().lower()
    if value.startswith('open') or value in ('increase', 'add', 'add_leg', 'reopen_after_tp'):
        return 'open'
    if value.startswith('close') or value in ('decrease', 'reduce', 'trim', 'reduce_pair',
            'reduce_loss_leg', 'force_close_all', 'tp_full'):
        return 'close'
    return 'unknown'


def side(value):
    value = str(value or '').strip().upper()
    value = {'BUY': 'LONG', 'SELL': 'SHORT'}.get(value, value)
    return value if value in ('LONG', 'SHORT') else ''


def order_scope(order):
    get = order.get if isinstance(order, dict) else lambda k, d=None: getattr(order, k, d)
    held = side(get('side'))
    if get('reduce_only', False):
        # An order side is the trade direction; close claims use the HELD side.
        return 'close', {'LONG': 'SHORT', 'SHORT': 'LONG'}.get(held, '')
    return 'open', held


def key(platform, coin, kind, held_side=''):
    base = str(platform).upper() + '|' + str(coin).upper()
    if family(kind) == 'close':
        return base + '|CLOSE|' + ('ALL' if str(coin).upper() == 'ALL' else side(held_side) or 'ANY')
    # Preserve the opening key used by the existing M10 confirmation protocol.
    return base


def index_records(records):
    """Read v1 and v2; split legacy close keys in memory without losing evidence."""
    result = {}
    for old_key, record in records.items():
        if not isinstance(record, dict):
            raise ValueError('invalid unconfirmed-order journal record')
        p, c = record.get('platform'), record.get('coin')
        if p not in ('EXT', 'VAR') or not isinstance(c, str) or not c or c != c.upper() or '|' in c:
            raise ValueError('invalid unconfirmed-order identity')
        new_key = key(p, c, record.get('kind'), record.get('side'))
        if old_key not in (p + '|' + c, new_key) or new_key in result:
            raise ValueError('conflicting unconfirmed-order journal keys')
        result[new_key] = record
    return result


def blocks(record, platform, coin, kind='open', held_side=''):
    if not pending_blocks(record) or str(record.get('platform') or '').upper() != str(platform).upper():
        return False
    c, other = str(coin).upper(), str(record.get('coin') or '').upper()
    if c != 'ALL' and other not in (c, 'ALL'):
        return False
    if family(kind) != 'close':
        return True  # Existing restrictions on new risk remain conservative.
    if family(record.get('kind')) == 'open':
        return False
    # Unknown operation/direction cannot establish absence of a duplicate close.
    a, b = side(held_side), side(record.get('side'))
    return c == 'ALL' or other == 'ALL' or not a or not b or a == b


def memory_key(platform, coin, held_side, kind='open'):
    base = (str(platform).upper(), str(coin).upper(), side(held_side))
    return base + ('close',) if family(kind) == 'close' else base


def memory_kind(k, info):
    return 'close' if len(k) == 4 and k[3] == 'close' else family((info or {}).get('kind') or 'open')
