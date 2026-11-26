"""The live pane without Antigravity CLI's input box and footer (the pane display protocol), as Antigravity 1.5.14–1.5.16
draws them.

The composer sits at the bottom of the screen: a full-width rule of `─`, the prompt rows starting with `>` (or `!`
for shell mode) and indented continuations, a matching full-width `─` rule, and up to a few footer rows (`? for
shortcuts`, `esc to cancel`, the `mode · Model · effort` bar, or mv2's `mv2:...` statusLine). Everything from the
top rule down (plus blank rows right above it) goes; conversation text, spinners above the box, dialogs (such as the
workspace trust prompt), and any screen without a recognized composer + footer come back unchanged.
"""

RULE = '─'
MIN_RULE_WIDTH = 20
PROMPTS = ('>', '!')
FOOTER_ROWS = 4
EFFORTS = frozenset(('low', 'medium', 'high', 'xhigh', 'max'))
MODES = frozenset(('auto', 'plan', 'accept-edits', 'bypass', 'default'))


def display(text: str) -> str:
    lines = text.split('\n')
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    bottom = next(
        (row for row in range(end - 1, max(end - 2 - FOOTER_ROWS, -1), -1) if plain_rule(lines[row])),
        None,
    )
    if bottom is None:
        return text
    footer = [row for row in lines[bottom + 1:end] if row.strip()]
    if not footer or len(footer) > FOOTER_ROWS or not all(footer_row(row) for row in footer):
        return text
    width = len(lines[bottom])
    top = next((row for row in range(bottom - 1, -1, -1) if plain_rule(lines[row])), None)
    if top is None or len(lines[top]) != width:
        return text
    prompt = lines[top + 1:bottom]
    if (not prompt or prompt[0][:1] not in PROMPTS or prompt[0][1:2] not in ('', ' ', '\xa0')
            or not all(indented(row) for row in prompt[1:])):
        return text
    while top and not lines[top - 1].strip():
        top -= 1
    return '\n'.join(lines[:top])


def plain_rule(line: str) -> bool:
    return len(line) >= MIN_RULE_WIDTH and set(line) == {RULE}


def indented(row: str) -> bool:
    return row.startswith('  ') or not row.strip()


def footer_row(row: str) -> bool:
    """Antigravity's built-in shortcut/model footer or mv2's `mv2:` statusLine; never a dialog bar."""
    stripped = row.strip()
    if not stripped:
        return True
    if stripped.startswith('mv2:'):
        return True
    if stripped.startswith(('? for shortcuts', 'esc to cancel', '! for shell')):
        return True
    if ' · ' in stripped:
        parts = [part.strip() for part in stripped.split(' · ')]
        if parts[-1].lower() in EFFORTS:
            return True
        if parts[0].lower() in MODES and len(parts) >= 2:
            return True
    return False
