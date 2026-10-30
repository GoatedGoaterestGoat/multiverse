"""The live pane without Codex's composer and what is under it (the pane display protocol), as Codex 0.164.0
draws them.

Codex ends its screen with the composer (`› ` and the draft, or a fixed placeholder when it is empty), a blank row
and the footer. The composer, any draft in it and everything under it go when the footer proves the composer: the
rows after the last blank row are one or two footer rows Codex draws there (the status line naming the folder
second, or shortcut, queue and context hints). Anything else comes back unchanged: an approval or selection dialog,
whose chosen option (`› 1. Yes, proceed`) looks like a draft but whose last row is its own hint; a composer over a
search, a message or no footer yet; and every screen whose last `› ` row has unindented rows under it, such as a
sent message in the history.
"""

FOOTER_ROWS = 2    # the most footer rows under the composer
HINTS = (' for shortcuts', ' to queue message', ' to queue')


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
    """The status line naming the folder second, or a row of shortcut, queue and context hints."""
    parts = [part.strip() for part in row.split('  ') if part.strip()]
    if parts and all(hint(part) or context(part) for part in parts):
        return True
    items = row.split(' · ')
    return len(parts) == 1 and len(items) >= 2 and folder(items[1])


def folder(item):
    """A folder as the status line names it: from the root or home, or cut at the left; not a key such as "/ search"."""
    return item[:1] in ('/', '~', '…') and item[1:2] != ' '


def hint(part):
    return any(part.endswith(end) and ' ' not in part.removesuffix(end) for end in HINTS)


def context(part):
    number, _, rest = part.partition('% ')
    return rest == 'context left' and number.isdigit() and int(number) <= 100
