"""Native startup refusal evidence; no terminal, transport or runtime ownership."""
import re
from collections.abc import Mapping


OPEN_ELSEWHERE = 'This conversation is open in another app'
CLOSE_ELSEWHERE = 'Close it there and press R to continue here.'


def supports_plain_refusal(version):
    """Whether a current-pane refusal layout is known for this CLI version."""
    if isinstance(version, str):
        return version in ('0.163.0', 'codex-cli 0.163.0')
    return (isinstance(version, (tuple, list)) and len(version) == 3
            and all(type(part) is int for part in version) and tuple(version) == (0, 163, 0))


def plain_started(evidence):
    """End startup observation at the tested active, configured empty composer.

    A placeholder also appears while loading/resuming, so require the cursor on
    its exact row plus the configured status footer with no pending indicator.
    This is UI evidence only, never a binding or native permission check.
    Unknown layouts retain the bounded observer.
    """
    if not isinstance(evidence, Mapping) or not supports_plain_refusal(evidence.get('cli_version')):
        return False
    text = evidence.get('text')
    x, y = evidence.get('cursor_x'), evidence.get('cursor_y')
    if (evidence.get('cursor_visible') is not True or not isinstance(text, str)
            or len(text) > 32768 or '\x1b' in text or '\0' in text
            or type(x) is not int or type(y) is not int or x != 2 or y < 0):
        return False
    lines = text.rstrip().splitlines()
    if (y >= len(lines) or lines[y] != '› Ask Codex to do anything'
            or any(line.strip() in ('loading', 'Resuming session…', 'Forking session…') for line in lines[:y])):
        return False
    footer = [line for line in lines[y + 1:] if line.strip()]
    return (0 < len(footer) <= 2 and all(line.startswith('  ') for line in footer)
            and any(' · ' in line for line in footer))


def plain_refusal(evidence):
    """Recognize the active 0.163.0 read-only widget, never scrollback prose.

    Core supplies only the current owned pane and its cursor visibility. The
    native widget hides the cursor and ends the visible content with its exact
    action row. Matching a headline alone would misclassify quoted history.
    Unknown versions/layouts intentionally provide no refusal evidence.
    """
    if not supports_plain_refusal(evidence.get('cli_version')):
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
