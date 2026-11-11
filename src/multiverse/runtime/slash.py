"""Harness command submission and correlated outcomes; a wait never submits twice."""
from dataclasses import asdict, replace
import math
import shlex
import time
from uuid import UUID, uuid4

from .. import harnesses, universe
from ..harnesses.types import (AttachmentSpec, CommandResult, NativeEndpoint, NativeResult,
                               NativeServiceHandle, SessionHandle, SlashCommand, SlashRequest)
from ..plugins import agent_files
from ..store import exists_replaced, lock, now, write
from ..universe import agents, focus, records
from . import agent_identity, delivery_evidence, effects, native

OUTPUT_BYTES = 64 * 1024
ARGS_BYTES = 32 * 1024
POLL = .5
MAX_PENDING = 128
FINAL = frozenset(('ran', 'refused', 'failed', 'lost'))


def _name(value):
    if isinstance(value, str):
        value = value.removeprefix('/')
    if not isinstance(value, str) or not value or value.startswith('/') or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError('The command name must be one word; put the rest in the command arguments')
    return value


def _managed(command, args='', *, family='', agent):
    name = command.name
    if name in ('exit', 'quit'):
        return f'mv2 manages the terminal; use mv2 kill {agent}'
    if name == 'resume':
        return f'mv2 manages the conversation; use mv2 resume {agent}'
    if name in ('login', 'logout'):
        return f'Change authentication outside the running agent, then use mv2 kill {agent} and mv2 resume {agent}'
    if family == 'claude':
        if name == 'restart':
            return f'Use mv2 kill {agent}, then mv2 resume {agent} so its connection is restored'
        if name == 'multiverse' or name == 'multiverse:multiverse':
            return 'Use the corresponding mv2 command directly'
        if name == 'plugin' and args:
            try:
                words = shlex.split(args)
            except ValueError:
                return 'Plugin arguments must have balanced quotes'
            if (words and words[0] not in ('list', 'help')
                    and any(word.split('@', 1)[0] == 'multiverse' or word == '--all' for word in words)):
                return 'mv2 manages its own plugin; use mv2 refresh instead'
    return command.refused


def _catalog(adapter, session, executor):
    if not all(callable(native.adapter_hook(adapter, method)) for method in ('slash_commands', 'run_slash', 'slash_result')):
        return {'state': 'unavailable', 'reason': 'This harness does not run its commands for mv2'}
    executor.check_session()
    reply = effects.call(adapter, 'slash_commands', session, executor)
    if not isinstance(reply, NativeResult) or reply.state != 'ok':
        return {'state': 'unavailable', 'reason': getattr(reply, 'reason', '') or 'The harness command list is unavailable'}
    values = reply.value
    if not isinstance(values, (list, tuple)) or len(values) > 4096:
        raise ValueError('The harness returned an invalid command list')
    found, names = [], set()
    for value in values:
        item = value if isinstance(value, SlashCommand) else SlashCommand(**value)
        try:
            if _name(item.name) != item.name:
                continue
        except ValueError:
            continue  # Display-only names cannot disable the other exact, addressable commands.
        if (item.name in names or not isinstance(item.description, str) or not isinstance(item.source, str)
                or type(item.needs_args) is not bool or type(item.changes_settings) is not bool
                or item.plugin is not None and not isinstance(item.plugin, str)
                or item.refused is not None and not isinstance(item.refused, str)):
            raise ValueError('The harness returned an invalid command description')
        names.add(item.name)
        found.append(asdict(replace(item, refused=_managed(item, family=getattr(adapter, 'family', adapter.name),
                                                         agent=executor.properties['name']))))
    return {'state': 'ok', 'commands': found}


def commands(root, agent):
    effects.require('slash_commands', agent)
    root = universe.resolve(root)
    directory = agents.directory(root, agent)
    with lock(directory / '.launch.lock', create_parent=False):
        _, adapter, session, executor = native.context(root, directory.name, reconcile=True)
        if native.observe(executor.properties, executor.facts)['live'] != 'alive':
            return {'state': 'unavailable', 'reason': 'The agent must be running with its command bridge'}
        return _catalog(adapter, session, executor)


def _outcome(record):
    return CommandResult(**{**record['result'], 'changed': tuple(record['result'].get('changed', ()))})



def _pending(directory):
    path = directory / 'slash' / 'pending.json'
    record = records._object(path, missing_ok=True)
    if record is None:
        return []
    pending = record.get('ids')
    try:
        valid = (isinstance(pending, list) and len(pending) <= MAX_PENDING
                 and all(isinstance(id, str) and str(UUID(id)) == id for id in pending)
                 and len(set(pending)) == len(pending))
    except ValueError:
        valid = False
    if not valid:
        raise records.RecordError(path, 'invalid pending command IDs; repair this index before continuing')
    return pending


def _set_pending(directory, pending):
    path = directory / 'slash' / 'pending.json'
    if pending:
        agent_files.write_json(path, {'ids': pending})
    else:
        path.unlink(missing_ok=True)


def _save(directory, record, value):
    pending = _pending(directory)
    if value.state in FINAL:
        pending = [id for id in pending if id != record['id']]
    elif record['id'] not in pending:
        if len(pending) >= MAX_PENDING:
            raise ValueError('Too many pending commands; read their results before submitting another')
        pending.append(record['id'])
    record['result'] = asdict(value)
    agent_files.write_json(directory / 'slash' / (record['id'] + '.json'), record)
    _set_pending(directory, pending)
    agents._event(directory, 'agent.command', record['session']['launch_id'],
                  {'command': record['command'], 'state': value.state,
                   **({'caller': record['caller']} if 'caller' in record else {})})
    return value


def _session(record):
    value = dict(record['session'])
    if value.get('endpoint'):
        value['endpoint'] = NativeEndpoint(**value['endpoint'])
    if value.get('attachment'):
        value['attachment'] = AttachmentSpec(**value['attachment'])
    if value.get('service'):
        value['service'] = NativeServiceHandle.from_record(value['service'])
    return SessionHandle(**value)


def _settings(directory, record, value):
    """Publish only this launch's correlated readback, never an older result over a newer command."""
    changed = []
    with lock(directory / '.properties.lock', create_parent=False):
        properties = records.properties(directory / 'properties.json')
        if agents.runtime(directory).get('launch_id') != record['session']['launch_id']:
            return changed
        reported_session = value.get('session_id')
        if reported_session is not None and reported_session != properties.get('native_session_id'):
            raise ValueError('The command outcome is waiting for its session lineage report')
        if record['command'] == 'clear':
            if not reported_session or reported_session == record['session']['native_id']:
                raise ValueError('The clear command has no confirmed new session')
            changed.append('session')
        if record['command'] in ('model', 'effort'):
            model, effort = value.get('model'), value.get('effort')
            if (not isinstance(model, str) or not model or record['command'] == 'effort' and 'effort' not in value
                    or effort is not None and not isinstance(effort, str)):
                raise ValueError('The command did not report its current model and effort')
            previous = properties.get('slash_settings')
            previous = previous if isinstance(previous, dict) else {}
            if previous.get('requested_at', '') <= record['requested_at']:
                settings = {'model': model, **({'effort': effort} if 'effort' in value else {})}
                for key, setting in settings.items():
                    if properties.get(key) != setting:
                        changed.append(key)
                    properties[key] = setting
                properties['slash_settings'] = {'id': record['id'], 'requested_at': record['requested_at']}
                properties['updated_at'] = now()
                write(directory / 'properties.json', properties)
    return changed


def _complete(directory, record, reply):
    current = _outcome(record)
    if not isinstance(reply, NativeResult):
        return replace(current, reason='The command outcome could not be read; do not resubmit it')
    value = reply.value if isinstance(reply.value, dict) else {}
    state = value.get('state') if reply.state == 'ok' else None
    if state not in ('ran', 'failed', 'refused'):
        return replace(current, reason=reply.reason or current.reason)
    output = value.get('output')
    if output is not None and not isinstance(output, str):
        return replace(current, reason='The harness returned invalid command output')
    output = output.encode()[:OUTPUT_BYTES].decode('utf-8', errors='ignore') if output is not None else None
    if value.get('prompted') is True:
        output = None
    try:
        changed = _settings(directory, record, value) if state == 'ran' else []
    except ValueError as error:
        return replace(current, reason=str(error))
    return _save(directory, record, replace(current, state=state, output=output,
                 prompted=value.get('prompted') is True, changed=tuple(changed),
                 saved_default=state == 'ran' and record.get('changes_settings') is True,
                 reason=value.get('reason') or reply.reason or None))


def result(root, agent, id):
    effects.require('slash_result', agent)
    if not isinstance(id, str) or str(UUID(id)) != id:
        raise ValueError('Give the complete command run ID')
    root = universe.resolve(root)
    directory = agents.directory(root, agent)
    with lock(directory / '.launch.lock', create_parent=False), lock(directory / '.slash.lock', create_parent=False):
        return _result(root, directory, id)


def _result(root, directory, id, adapter=None):
    """Caller holds launch and slash locks; never submits anything."""
    record = records._object(directory / 'slash' / (id + '.json'))
    if record.get('schema') != 1 or record.get('id') != id or record.get('agent') != directory.name:
        raise ValueError('The command run record does not match this agent')
    current = _outcome(record)
    if current.state in FINAL:
        _set_pending(directory, [pending for pending in _pending(directory) if pending != id])
        return current
    session = _session(record)
    # End proof precedes the negative receipt read: a live writer can still publish a result.
    try:
        ended = delivery_evidence.launch_ended(directory, session.launch_id)
    except (OSError, ValueError, TypeError, KeyError):
        ended = False
    adapter = (adapter if adapter is not None and adapter.name == session.harness
               else harnesses.load(root, session.harness))
    request = SlashRequest(id=id, command=record['command'], args=record['args'])
    executor = native.NativeExecutor(session.workspace)
    try:
        reply = effects.call(adapter, 'slash_result', session, request, executor)
    except Exception:
        return replace(current, reason='The command outcome cannot be read; do not resubmit it')
    completed = _complete(directory, record, reply)
    if completed.state in FINAL:
        return completed
    if ended and isinstance(reply, NativeResult) and reply.state == 'unknown' and reply.value == {'absent': True}:
        return _save(directory, record, replace(current, state='lost',
                     reason='The agent stopped before the command produced a result; it was not resent'))
    return completed



def reconcile(root, directory, properties, facts, *, adapter=None):
    """Settle bounded receipts under the caller's launch lock; no native probe or history scan."""
    if not exists_replaced(directory / 'slash' / 'pending.json'):
        return properties
    with lock(directory / '.slash.lock', create_parent=False):
        if agents.runtime(directory).get('launch_id') != facts.get('launch_id'):
            raise ValueError('Launch changed while settling command outcomes; retry')
        for id in _pending(directory):
            _result(root, directory, id, adapter)
        return agents.session_properties(root, directory, launch_locked=True)


def require_settled(directory, properties, facts, *, model=None, effort=None):
    """A new launch cannot silently undo a setting whose command outcome is still unknown."""
    if not exists_replaced(directory / 'slash' / 'pending.json'):
        return
    with lock(directory / '.slash.lock', create_parent=False):
        for id in _pending(directory):
            record = records._object(directory / 'slash' / (id + '.json'))
            setting = record.get('command')
            if (setting not in ('model', 'effort') or _outcome(record).state in FINAL
                    or record['session']['launch_id'] != facts.get('launch_id')
                    or {'model': model, 'effort': effort}[setting] is not None):
                continue
            name = properties.get('name') or directory.name
            raise ValueError(f'Command run {id} has no confirmed {setting} outcome. '
                             f'Read mv2 slash {name} --result {id}, or use mv2 resume {name} '
                             f'--{setting} VALUE to choose it explicitly')


def run(root, agent, command, args='', *, timeout=600, wait=True, caller=None):
    effects.require('run_slash', agent)
    command = _name(command)
    if not isinstance(args, str) or '\0' in args or len(args.encode()) > ARGS_BYTES:
        raise ValueError('Command arguments must be text without NUL, at most 32 KiB')
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError('The command timeout must be a finite nonnegative number of seconds')
    if type(wait) is not bool:
        raise ValueError('wait must be a boolean')
    deadline = time.monotonic() + timeout
    if caller is not None and (not isinstance(caller, dict) or caller.get('kind') != 'plugin'
                               or not isinstance(caller.get('name'), str) or not caller['name']):
        raise ValueError('An explicit command caller must name its plugin')
    caller = caller or agent_identity.sender_actor(acting='run a harness command')
    root = universe.resolve(root)
    directory = agents.directory(root, agent)
    refused = CommandResult(id='', command=command, args=args, state='refused')
    with lock(directory / '.send.lock', create_parent=False), lock(directory / '.launch.lock', create_parent=False):
        if caller is not None and focus.current(directory):
            return replace(refused, reason='The agent is focused; wait until it is unfocused, or ask the person to run the command')
        _, adapter, session, executor = native.context(root, directory.name, reconcile=True)
        if native.observe(executor.properties, executor.facts)['live'] != 'alive':
            return replace(refused, state='unavailable', reason='The agent must be running with its command bridge')
        catalog = _catalog(adapter, session, executor)
        if catalog['state'] != 'ok':
            return replace(refused, state='unavailable', reason=catalog['reason'])
        entry = next((SlashCommand(**item) for item in catalog['commands'] if item['name'] == command), None)
        if entry is None:
            return replace(refused, reason='The live harness command list has no such command; use --list')
        problem = _managed(entry, args, family=getattr(adapter, 'family', adapter.name), agent=executor.properties['name'])
        if problem or entry.needs_args and not args.strip():
            return replace(refused, reason=problem or 'This command opens a picker without arguments; give its arguments')
        if entry.changes_settings and caller is not None:
            return replace(refused, reason="This command also changes the person's harness defaults; only a person may run it. "
                           f"Use mv2 resume {executor.properties['name']} --model X --effort Y to change only that agent")
        executor.check_session()
        request = SlashRequest(id=str(uuid4()), command=entry.name, args=args, wait_until=deadline)
        queued = CommandResult(id=request.id, command=entry.name, args=args, state='queued',
                               reason='Still queued; it may still run. Use --result with this ID; do not resubmit it')
        record = {'schema': 1, 'id': request.id, 'agent': directory.name, 'command': entry.name, 'args': args,
                  'requested_at': now(), 'changes_settings': entry.changes_settings,
                  'caller': ({'kind': 'person'} if caller is None else
                             {'kind': 'plugin', 'name': caller['name']} if isinstance(caller, dict) else
                             {'kind': 'agent', **caller.sender()}),
                  'session': asdict(session), 'result': asdict(queued)}
        with lock(directory / '.slash.lock', create_parent=False):
            _save(directory, record, queued)
            try:
                reply = effects.call(adapter, 'run_slash', session, request, executor)
            except Exception:
                reply = NativeResult('unknown', 'Command submission could not be confirmed; it may still run')
            if isinstance(reply, NativeResult) and reply.state in ('unavailable', 'error'):
                queued = _save(directory, record, replace(queued, state='failed', reason=reply.reason))
            else:
                queued = _complete(directory, record, reply)
    if queued.state in FINAL or not wait:
        return queued
    while time.monotonic() < deadline:
        queued = result(root, directory.name, request.id)
        if queued.state in FINAL:
            return queued
        time.sleep(min(POLL, max(0, deadline - time.monotonic())))
    return queued
