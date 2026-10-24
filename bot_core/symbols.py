"""Exact instrument identity helpers. No network or process side effects."""
import re
from urllib.parse import unquote, urlsplit


def canonical_symbol(value):
    """Parse a market label or instrument URL; never search arbitrary substrings."""
    text = unquote(str(value or '')).strip().upper()
    if not text:
        return ''
    if '://' in text or text.startswith('/'):
        path = urlsplit(text).path.rstrip('/')
        match = re.search(r'/(?:PERPETUAL|PERP|TRADE)/([^/]+)$', path)
        if not match:
            return ''
        text = match.group(1)
    # The first market token is allowed, never a token buried in another cell.
    text = re.split(r'\s+', text, maxsplit=1)[0]
    text = re.sub(r'[/_:-](?:USD|USDT|USDC|PERP)$', '', text)
    return text if re.fullmatch(r'[A-Z0-9][A-Z0-9._-]*', text) else ''


def same_symbol(target, market, *, aliases=()):
    wanted = {canonical_symbol(target)}
    wanted.update(canonical_symbol(alias) for alias in aliases)
    wanted.discard('')
    actual = canonical_symbol(market)
    return bool(actual and actual in wanted)
