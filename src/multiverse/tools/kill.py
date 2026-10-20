"""End native work and its process, then remove the agent's tmux wrapper."""
from ..runtime import lifecycle
from .. import universe


def configure(parser):
    parser.add_argument('agent')
    parser.add_argument('--attachment-only', action='store_true', help='Close mv2\'s terminal without requesting native cancellation; work owned by that process ends')
    parser.add_argument('--json', action='store_true', help='Print the full record as JSON')


def run(root=None, *, agent, attachment_only=False, json=False):  # `json`: presentation only
    return lifecycle.kill(universe.resolve(root), agent, attachment_only=attachment_only)


def stopped(root, agent, record, attachment_only):
    """One sentence for a finished kill: what stopped, and anything left unconfirmed or undelivered."""
    from ..display import agent_name, sentence
    name = agent_name(universe.resolve(root), record.get('id') or agent)
    cancellation = record.get('native_cancellation') or {}
    reason = str(cancellation.get('reason') or '')
    if cancellation.get('state', 'ok') != 'ok':
        line = f"Stopped {name}, but couldn't confirm its work was cancelled: {sentence(reason)}"
    elif attachment_only:
        line = f"Closed {name}'s terminal. {sentence(reason)}"
    else:
        line = f'Stopped {name}.'
    cancelled = cancellation.get('cancelled_startup_messages') or 0
    if isinstance(cancelled, int) and cancelled > 0:
        line += f" {cancelled} message{'s' if cancelled != 1 else ''} waiting for it to start {'were' if cancelled != 1 else 'was'} not delivered."
    return line


def cli_run(root=None, *, agent, attachment_only=False, json=False):
    record = run(root, agent=agent, attachment_only=attachment_only)
    return record if json else stopped(root, agent, record, attachment_only)


if __name__ == '__main__':
    from ._common import main
    main(configure, run)
