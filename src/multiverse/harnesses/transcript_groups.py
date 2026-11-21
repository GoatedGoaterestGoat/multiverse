"""Group a provider's public transcript labels into turns."""

def turn_anchors(history):
    """Yield (row, label, turn anchor), with None for rows before the first prompt."""
    prompt = command = latest = None
    for index, label in history:
        if label == 'prompt':
            prompt = latest = index
            anchor = prompt
        elif label == 'command':
            command = latest = index
            anchor = command
        elif label == 'command_output' and command is not None:
            anchor = command
        elif label == 'compaction' and latest is not None and latest == command:
            anchor = command
        else:
            anchor = prompt
        yield index, label, anchor
