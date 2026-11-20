"""Positive user-item observations, never queue acceptance or message text."""

VERSIONS = frozenset(((0, 166, 0), (0, 167, 0), (0, 167, 1), (0, 167, 2), (0, 169, 0), (0, 170, 0)))
IDENTIFIER_BYTES = 4096


def identifier(value):
    if not isinstance(value, str) or not value or '\0' in value:
        return False
    try:
        return len(value.encode('utf-8')) <= IDENTIFIER_BYTES
    except UnicodeError:
        return False


def observation(value, native_id):
    if (not isinstance(value, dict) or 'id' in value
            or value.get('method') not in ('item/started', 'item/completed')):
        return None
    params = value.get('params')
    if not isinstance(params, dict) or params.get('threadId') != native_id:
        return None
    item = params.get('item')
    if not isinstance(item, dict) or item.get('type') != 'userMessage':
        return None
    result = {'native_session_id': native_id, 'turn_id': params.get('turnId'),
              'item_id': item.get('id'), 'client_message_id': item.get('clientId')}
    return result if all(identifier(value) for value in result.values()) else None
