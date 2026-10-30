"""The live pane without Codex's composer and what is under it (the pane display protocol), as Codex 0.164.0
draws them.

Codex ends its screen with the composer (`› ` and the draft, or a fixed placeholder when it is empty), a blank row
and the footer. The composer, any draft in it and everything under it go when the footer proves the composer: the
rows after the last blank row are one or two footer rows Codex draws there. One is the status line, known by its
folder second, its model and effort first or a context reading among its items; the other has shortcut, queue and
context hints and the warning count. Anything else comes back unchanged: an approval, selection or warnings dialog,
whose chosen option (`› 1. Yes, proceed`) looks like a draft but whose last row is its own hint; a composer over a
search, a message or no footer yet; and every screen whose last `› ` row has unindented rows under it, such as a sent
message in the history.
"""

FOOTER_ROWS = 2    # the most footer rows under the composer
HINTS = (' for shortcuts', ' to queue message', ' to queue')
EFFORTS = ('minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')  # the effort after the model's name


def display(text):
    lines = text.split('\n')
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    composer = end - 1
    while composer >= 0 and (lines[composer].startswith('  ') or not lines[composer].strip()):
        composer -= 1
    if composer < 0 or not lines[composer].startswith('› '):
        return text
    start = end  # the footer: the rows after the last blank row under the composer
    while start > composer + 1 and lines[start - 1].strip():
        start -= 1
    if start == composer + 1 or end - start > FOOTER_ROWS or not all(footer(row.strip()) for row in lines[start:end]):
        return text
    return '\n'.join(lines[:composer])


def footer(row):
    """The status line, or a row of shortcut, queue and context hints; either may end with the warning count."""
    parts = [part.strip() for part in row.split('  ') if part.strip()]
    if parts and all(hint(part) or context(part) or warning(part) for part in parts):
        return True
    items = parts[0].split(' · ') if parts else []
    return (len(items) >= 2 and all(map(warning, parts[1:]))
            and (folder(items[1]) or model(items[0]) or any(map(reading, items))))


def model(item):
    """The status line's model with its effort, as "GPT-6.1-Sol xhigh"."""
    words = item.split()
    return len(words) >= 2 and words[-1] in EFFORTS


def reading(item):
    """The status line's context, as "Context 76% left" or "Context 20% used"."""
    word, _, rest = item.partition(' ')
    number, _, state = rest.partition('% ')
    return word == 'Context' and number.isdigit() and state in ('left', 'used')


def folder(item):
    """A folder as the status line names it: from the root or home, or cut at the left; not a key such as "/ search"."""
    return item[:1] in ('/', '~', '…') and item[1:2] != ' '


def hint(part):
    return any(part.endswith(end) and ' ' not in part.removesuffix(end) for end in HINTS)


def warning(part):
    """Codex's warning count at the right of the hint row: "⚠ 1 warning · f2 to view", "⚠ 2 · f2" or "⚠ 3"."""
    count, _, key = part.removeprefix('⚠ ').partition(' · ')
    count = count.removesuffix(' warnings').removesuffix(' warning')
    return part.startswith('⚠ ') and count.isdigit() and ' ' not in key.removesuffix(' to view')


def context(part):
    number, _, rest = part.partition('% ')
    return rest == 'context left' and number.isdigit() and int(number) <= 100
