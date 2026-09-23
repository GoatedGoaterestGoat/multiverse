"""Claude terminal lifecycle with conservative, exact-conversation recovery."""
from dataclasses import replace
from pathlib import Path

from ..base import ConfiguredAdapter
from ..types import NativeResult, normalize_session_id
from . import history

RECOVERY = ('Use `mv2 resume AGENT --start-empty` (Start empty conversation) to assign a new '
            'Claude UUID without deleting the old history. The agent identity stays the same.')


class ClaudeAdapter(ConfiguredAdapter):
    package = Path(__file__).parent
    claude_lifecycle = True

    def _checked(self, command, context, session=None):
        env = history.environment(context.environment)
        session_id = normalize_session_id(session.native_id if session else context.values['session_id'])
        workspace = session.workspace if session else context.values['workspace']
        previous = session.resume_evidence if session else {}
        if session and not session.native_environment:
            raise ValueError('Claude storage identity was not recorded for this older launch; resumability is unknown. ' + RECOVERY)
        if session:
            if previous and previous.get('closed') is False:
                raise ValueError('Previous Claude launch has not finished stopping; retry after it exits. ' + RECOVERY)
            env = history.environment(session.native_environment)
        else:
            # Only a new identity may create storage. Discovery itself is read-only.
            (Path(env['CLAUDE_CONFIG_DIR']) / 'projects').mkdir(parents=True, exist_ok=True)
        found = history.discover(env, workspace, session_id)
        storage_fingerprint = history.fingerprint(env)
        same = (storage_fingerprint is not None and previous.get('session_id') == session_id and previous.get('workspace') == workspace
                and previous.get('environment') == env and previous.get('launch_id') == session.launch_id
                and previous.get('storage_fingerprint') == storage_fingerprint) if session else False
        fresh = (same and previous.get('never_initialized') is True and found == 'absent')
        if session and found != 'saved' and not fresh:
            raise ValueError(f'Claude conversation is {"missing" if found == "absent" else "unknown or inaccessible"}; '
                             'an assigned UUID does not prove saved history, and automatic reset is unsafe. ' + RECOVERY)
        if not session and found != 'absent':
            raise ValueError('New Claude identity could not be verified absent in accessible storage. ' + RECOVERY)
        argv = list(command.argv)
        options = argv[:argv.index('--')] if '--' in argv else argv
        flag = '--resume' if session else '--session-id'
        if options.count(flag) != 1 or options[options.index(flag) + 1:options.index(flag) + 2] != [session_id]:
            raise ValueError('Claude launch recipe must target exactly the recorded session UUID')
        if any(arg.split('=')[0] in ('--continue', '-c', '-r', '--fork-session', '--worktree', '-w', '--no-session-persistence')
               or arg.startswith(('--resume=', '--session-id=')) for arg in options) or ('--resume' in options and '--session-id' in options):
            raise ValueError('Claude launch options conflict with exact persistent session identity')
        if fresh:
            # Keep all local resume flags, bootstrap and model; never replay the original task.
            argv[argv.index('--resume')] = '--session-id'
        evidence = {'session_id': session_id, 'workspace': workspace, 'environment': env,
                    'storage_fingerprint': storage_fingerprint,
                    'never_initialized': found == 'absent' and (fresh or not context.values.get('task')) and no_prompt(argv),
                    'discovery': found, 'closed': True}
        return replace(command, argv=tuple(argv), environment=env, claude_session=evidence)

    def spawn(self, context):
        # Legacy Claude definitions also omit an empty positional prompt.
        command = super().spawn(context)
        if not context.values.get('task') and command.argv[-2:] == ('--', ''):
            command = replace(command, argv=command.argv[:-2])
        return self._checked(command, context)

    def resume(self, session, context):
        return self._checked(super().resume(session, context), context, session)

    def send(self, session, message, executor):
        return NativeResult('unavailable', 'No verified Mods/channel bridge attached to this Claude terminal')


class LegacyClaudeAdapter(ClaudeAdapter):
    """Keep schema-1 recipes and files; apply only Claude lifecycle checks."""
    package = None


def no_prompt(argv):
    """Custom positional prompts/options cannot establish never-initialized evidence."""
    values = {'--session-id', '--resume', '--append-system-prompt', '--add-dir', '--model', '--permission-mode'}
    flags = {'--dangerously-skip-permissions', '--allow-dangerously-skip-permissions'}
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg in values and index + 1 < len(argv):
            index += 2
        elif arg in flags:
            index += 1
        elif arg == '--' and index == len(argv) - 1:
            return True
        else:
            return False
    return True
