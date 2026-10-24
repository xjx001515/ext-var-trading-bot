"""Classify visible access messages without logging in or bypassing restrictions."""


def inspect_platform_access(platform, visible_text):
    platform = str(platform or '').upper()
    prefix = platform.lower() or 'platform'
    text = ' '.join(str(visible_text or '').split()).lower()
    if not text:
        return {'blocked': True, 'reason': prefix + '_page_text_unavailable', 'user_action_required': False}
    if 'service access is restricted in your region' in text:
        return {'blocked': True, 'reason': prefix + '_access_restricted', 'user_action_required': True}
    if ('log in to view the tables' in text or 'connect your wallet to continue' in text
            or 'connect your wallet to see your positions' in text
            or 'sign in to view your positions' in text
            or (platform == 'EXT' and 'connect wallet' in text and 'start trading' in text)):
        return {'blocked': True, 'reason': prefix + '_auth_required', 'user_action_required': True}
    # Absence of a warning does not prove account login or valid positions.
    return {'blocked': False, 'reason': 'no_access_warning', 'user_action_required': False}


def snapshot_access_block(snapshot):
    if not isinstance(snapshot, dict):
        return ''
    raw = snapshot.get('raw') if isinstance(snapshot.get('raw'), dict) else {}
    states = raw.get('platform_access') if isinstance(raw.get('platform_access'), dict) else {}
    return '|'.join(str(value.get('reason') or platform.lower() + '_access_unavailable')
                    for platform, value in states.items()
                    if isinstance(value, dict) and value.get('blocked') is True)
