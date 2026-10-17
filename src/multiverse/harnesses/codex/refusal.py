"""Native startup refusal evidence; no terminal, transport or runtime ownership."""
import re


OPEN_ELSEWHERE = 'This conversation is open in another app'
CLOSE_ELSEWHERE = 'Close it there and press R to continue here.'


def plain_refusal(evidence):
    """Recognize the active 0.163.0 read-only widget, never scrollback prose.

    Core supplies only the current owned pane and its cursor visibility. The
    native widget hides the cursor and ends the visible content with its exact
    action row. Matching a headline alone would misclassify quoted history.
    Unknown versions/layouts intentionally provide no refusal evidence.
    """
    version = evidence.get('cli_version')
    if version not in ('0.163.0', 'codex-cli 0.163.0', (0, 163, 0), [0, 163, 0]):
        return None
    text = evidence.get('text')
    if (evidence.get('cursor_visible') is not False or not isinstance(text, str)
            or len(text) > 32768 or '\x1b' in text):
        return None
    lines = text.rstrip().splitlines()
    # The refusal is the native composer, at the end of the visible pane.
    # Require its unquoted lock marker and all native controls, including the
    # final transcript control; an ordinary live composer below it disqualifies it.
    candidates = [i for i, line in enumerate(lines) if line.lstrip().startswith('🔒')]
    if not candidates:
        return None
    block = ' '.join(' '.join(lines[candidates[-1]:]).split())
    pattern = (r'🔒\s+' + re.escape(OPEN_ELSEWHERE) + r'(?:\s+r to retry)?\s+'
               + re.escape(CLOSE_ELSEWHERE)
               + r'\s+r retry\s+f fork\s+esc/⌃c/q exit\s+⌃t transcript')
    if re.fullmatch(pattern, block) is None:
        return None
    return {'source': 'native-screen', 'method': 'thread/resume', 'code': None,
            'message': OPEN_ELSEWHERE + '\n' + CLOSE_ELSEWHERE,
            'native_id': evidence.get('native_id')}
