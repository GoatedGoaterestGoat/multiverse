"""Run a harness command when the agent is idle, or read its command list and results."""
from dataclasses import asdict

from ..runtime import slash


def configure(parser):
    parser.add_argument('agent')
    parser.add_argument('command', nargs='?', help='The live command name without a slash')
    parser.add_argument('args', nargs='*', help='Arguments as you would type them after the command')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--list', action='store_true', dest='list_commands', help='List commands and their refusals')
    mode.add_argument('--result', metavar='ID', help='Read a previously submitted command result; never resend')
    parser.add_argument('--timeout', type=float, default=600, help='Seconds to wait (default: 600); a timeout leaves the command queued')
    parser.add_argument('--no-wait', action='store_true', help='Print the command run ID without waiting')
    parser.add_argument('--json', action='store_true', help='Print the complete result as JSON')


def run(root=None, *, agent, command=None, args=(), list_commands=False, result=None, timeout=600, no_wait=False, json=False):
    if list_commands or result is not None:
        if command is not None or args or no_wait or timeout != 600:
            raise ValueError('--list and --result cannot be combined with a command or wait options')
        return slash.commands(root, agent) if list_commands else asdict(slash.result(root, agent, result))
    if command is None:
        raise ValueError('Give a command name, --list, or --result ID')
    arguments = args if isinstance(args, str) else ' '.join(args)
    return asdict(slash.run(root, agent, command, arguments, timeout=timeout, wait=not no_wait))


def cli_run(root=None, **arguments):
    from ..display import clean
    found = run(root, **arguments)
    state = found['state']
    if arguments.get('json'):
        return {**found, 'ok': state in ('ok', 'ran', 'queued')}
    if state == 'ok':
        return '\n'.join(f"{item['name']}: {clean(item['description'])}"
                         + (f" (refused: {clean(item['refused'])})" if item.get('refused') else
                            ' (arguments required)' if item.get('needs_args') else '')
                         + (' (person only; also saves harness defaults)' if item.get('changes_settings') else '')
                         for item in found['commands']) or 'No harness commands are available.'
    if state == 'ran':
        output = '\n'.join(clean(line) for line in found['output'].splitlines()) if found.get('output') is not None else (
            'Command expanded into a prompt; its answer is the agent\'s turn.' if found.get('prompted') else 'Command ran with no output.')
        return output + ("\nAlso saved as the person's default for this harness." if found.get('saved_default') else '')
    if state == 'queued':
        return f"Command {found['id']} is still queued; it may still run. Read it with mv2 slash {arguments['agent']} --result {found['id']}."
    raise ValueError(clean(found.get('reason') or f'Command {state}'))


if __name__ == '__main__':
    from ._common import main
    main(configure, run)
