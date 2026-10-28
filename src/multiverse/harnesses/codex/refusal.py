"""Native startup refusal evidence; no terminal, transport or runtime ownership."""
import re
from collections.abc import Mapping


OPEN_ELSEWHERE = 'This conversation is open in another app'
CLOSE_ELSEWHERE = 'Close it there and press R to continue here.'
# Releases whose native screens and unmanaged side-thread protocol were checked. 0.164.0 kept 0.163.0's
# layouts, so dropping 0.163.0 would turn its refusal check and /btw off for nothing.
SCREEN_VERSIONS = ((0, 163, 0), (0, 164, 0))


def supports_plain_refusal(version):
    """Whether a current-pane refusal layout is known for this CLI version."""
    if isinstance(version, str):
        return version.removeprefix('codex-cli ') in {'.'.join(map(str, known)) for known in SCREEN_VERSIONS}
    return (isinstance(version, (tuple, list)) and len(version) == 3
            and all(type(part) is int for part in version) and tuple(version) in SCREEN_VERSIONS)


def plain_started(evidence):
    """End startup observation at the tested active, initialized empty composer.

    A placeholder also appears while loading/resuming, so require the cursor on
    its exact row plus initialized footer content with no pending indicator.
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
    placeholder = '› Ask Codex to do anything'
    width = evidence.get('width')
    if width is not None:
        if type(width) is not int or not 24 <= width <= 4096:
            return False
        placeholder = placeholder[:width - 1]  # Native textarea reserves the rightmost column.
    if (y >= len(lines) or lines[y] != placeholder
            or any(line.strip() in ('loading', 'Resuming session…', 'Forking session…') for line in lines[:y])):
        return False
    footer = [line for line in lines[y + 1:] if line.strip()]
    if not 0 < len(footer) <= 2 or not all(line.startswith('  ') for line in footer):
        return False
    # The provisional StartupDraft has no status value and suppresses context
    # percentage. The ready footer may contain one configured item (no separator)
    # or the legacy percentage when the status line is disabled. Hints alone and
    # the native deferred-submission override never establish startup success.
    if any(line.strip().startswith(('Waiting for startup', 'esc cancel')) for line in footer):
        return False
    for line in footer:
        content = line.strip()
        if 'context left' in content:
            return re.search(r'(?:^|\s)(?:100|[1-9]?\d)% context left$', content) is not None
        # Shortcut labels are configurable even in StartupDraft: F1 or a chord
        # can replace '?'. An agents hint may share that row with a separator.
        hints = (' for shortcuts', ' for agents', ' again to quit', ' to queue',
                 ' to queue message', ' to edit previous message', ' to edit last message')
        if any(part.strip() and not part.strip().endswith(hints) for part in content.split(' · ')):
            return True
    return False


def plain_refusal(evidence):
    """Recognize the active 0.164.0 read-only widget, never scrollback prose.

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
