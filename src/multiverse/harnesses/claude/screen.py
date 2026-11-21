"""What Claude's screen shows as a launch starts: its input box, or a prompt the person answers before the session.

Claude asks some questions before it starts a session at all: whether to trust the folder, whether to add a
directory `--add-dir` named, whether to accept bypass-permissions mode. Its plugin can't report them, because Claude
runs a plugin's calls only once the session has started, so they are read from the pane. Each is a selection list
(one row marked `❯`) with an "Enter to confirm" footer and no input box; its first line under the top rule names it.
A dialog inside a session (a tool's approval, `/model`) has another footer, and the plugin reports those itself.
"""
import re

RULE = re.compile(r'^\s*[─━]{8,}')
CHOICE = re.compile(r'^\s*❯\s+\S')
INPUT = re.compile(r'^[❯!](\s|$)')  # the box's first row: a prompt, or `!` in shell mode
BOX_ROWS = 20  # how far a draft can run before the rule under the box
CONFIRM = 'Enter to confirm'
TITLE_CHARS = 120


def _lines(text):
    return [line.rstrip() for line in (text or '').splitlines()]


def _rule(line):
    return bool(RULE.match(line))


def ready(text):
    """Whether the screen shows Claude's input box: a rule, then a row starting `❯` (or `!` in shell mode), then
    another rule within a draft's few rows."""
    lines = _lines(text)
    return any(INPUT.match(line) and n > 0 and _rule(lines[n - 1])
               and any(_rule(below) for below in lines[n + 1:n + 1 + BOX_ROWS]) for n, line in enumerate(lines))


def startup_prompt(text):
    """The prompt Claude's screen asks before its session starts, as {'title', 'detail'}, or None.

    `title` is the dialog's first line under its top rule, without a trailing colon; `detail` the line after it when
    that is a path (the folder it asks about), else None. A screen with the input box is never one.
    """
    lines = _lines(text)
    if ready(text):
        return None
    # The dialog is the block that ends in the confirm footer: the rule nearest above that footer, and the selected
    # row inside it. Another section of the screen, above that rule, never names it.
    footer = next((n for n in range(len(lines) - 1, -1, -1) if CONFIRM in lines[n]), None)
    if footer is None:
        return None
    top = next((n for n in range(footer - 1, -1, -1) if _rule(lines[n])), None)
    if top is None:
        return None
    chosen = next((n for n in range(top + 1, footer) if CHOICE.match(lines[n])), None)
    if chosen is None:
        return None
    words = [line.strip() for line in lines[top + 1:chosen] if line.strip()]
    if not words:
        return None
    title = words[0].rstrip(':').strip()[:TITLE_CHARS]
    detail = words[1] if len(words) > 1 and words[1].startswith(('/', '~')) else None
    return {'title': title, 'detail': detail}
