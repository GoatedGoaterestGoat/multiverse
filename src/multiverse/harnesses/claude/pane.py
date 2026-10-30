"""The live pane without Claude Code's input box (the pane display protocol), as Claude Code 2.1.314 draws it.

The box is the bottom of the screen: a rule across the pane (the session's name may sit at its right end), the
prompt rows, the same rule without the name, then a few status rows (the mod's line, the status line, the
permission mode). The row above the box is Claude's too: blank, a hint at the right (the effort, "ctrl+g to
edit"), or a notice ("✔ Update installed · /restart to apply"). A tip may stand at the first column near the box.
The whole box goes, typed text included, with the status rows, the row above it, tips and the blank rows around
them. Any other screen (a dialog in place of the box, another layout) comes back unchanged. Conversation text never
matches: Claude indents it, and the rules start at the first column.
"""
import unicodedata

RULE = '─'
TIP = 'Tip: '
PROMPTS = ('❯', '!')  # the prompt's first character: a message, or a shell command
STATUS_ROWS = 8       # the most rows the box may have under it; more is not the box


def display(text):
    lines = text.split('\n')
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    bottom = next((row for row in range(end - 1, max(end - 2 - STATUS_ROWS, -1), -1) if plain_rule(lines[row])), None)
    if bottom is None or not all(indented(row) or row.startswith(TIP) for row in lines[bottom + 1:end]):
        return text
    top = next((row for row in range(bottom - 1, -1, -1) if lines[row].startswith(RULE)), None)
    if top is None or not named_rule(lines[top], len(lines[bottom])):
        return text
    prompt = lines[top + 1:bottom]
    if (not prompt or prompt[0][:1] not in PROMPTS or prompt[0][1:2] not in ('', ' ', '\xa0')
            or not all(indented(row) for row in prompt[1:])):
        return text
    top = max(top - 1, 0)  # Claude's own row: a hint at the right, a notice such as "Update installed", or blank
    while top and (not lines[top - 1].strip() or lines[top - 1].startswith(TIP)):
        top -= 1
    return '\n'.join(lines[:top])


def indented(row):
    return row.startswith('  ') or not row.strip()


def plain_rule(line):
    return set(line) == {RULE}


def named_rule(line, width):
    """The upper rule: as wide as the lower one, maybe with the session's name before its last character."""
    cells = sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in line)
    head, _, name = line.partition(' ')
    return cells == width and set(head) == {RULE} and (not name or name.endswith(' ' + RULE))
