"""An agent's process lifecycle: launch (spawn, resume, wake), readiness, listing and kill."""
from dataclasses import asdict, replace
from contextlib import contextmanager
import inspect
import json
import math
import os
from pathlib import Path
import shutil
import signal
import socket
import sys
import time
import tempfile

from .. import display, harnesses, source, universe
from ..harnesses.types import LaunchContext, SessionHandle, NativeResult, BridgeUnavailable
from ..store import existing_lock, lock, now, read, uid, write
from ..terminal.tmux import Tmux, observe, observation_key, owned, status_setup
from ..universe import agents, catalog, focus, mailbox, records, storage as storage_records
from ..universe.agents import check_available_name, directory, runtime, update_runtime
from ..universe.mailbox import message_record, update_message
from . import effects, native, paths, attachments, agent_identity, registration, missing_runtime, storage_adoption

TEARDOWN_TIMEOUT = 10

def damaged_row(scope, error, properties=None):
    """The row of an agent whose record cannot be used: always well formed, never raising.

    `error` is the RecordError; `properties` is the agent's properties when only its runtime.json is damaged,
    so its name, harness and reported phase still show. `damaged` says which file and why.
    """
    known = properties if properties is not None else (error.data if isinstance(error.data, dict) else {})
    text = lambda key: known[key] if isinstance(known.get(key), str) else ""
    agent_dir = error.file.parent if error.file.suffix in ('.json', '.jsonl') else error.file
    try:
        shown = str(error.file.relative_to(scope))
    except ValueError:
        shown = str(error.file)
    label = error.file.name if error.file.suffix in ('.json', '.jsonl') else shown
    reason, summary = error.reason, f"damaged: {label}: {error.reason}"
    recover = {'recover': 'kill'} if getattr(error, 'recover', None) == 'kill' else {}
    try:
        if error.file.name == 'properties.json' and agents.properties_missing(agent_dir):
            recover = {'recover': 'remove'}
            reason = (f'{error.reason}; mv2 remove {agent_dir.name} checks the surviving process evidence '
                      'before moving this leftover folder to the trash')
            summary = f'damaged: {label}: {reason}'
            if agents.orphaned(agent_dir):
                shown = str(Path(shown).parent)
                reason = summary = (f'leftover folder with no agent records; '
                                    f'mv2 remove {agent_dir.name} moves it to the trash')
    except OSError:
        pass  # Unreadable authority is not an orphan-removal hint.
    return {"id": agent_dir.name, "name": text("name") or agent_dir.name, "harness": text("harness"), "universe": str(scope),
            "live": "unknown", "status": "unknown", "phase": text("phase") or "unknown",
            "summary": summary, "damaged": {"file": shown, "reason": reason, **recover}}


def text_defaults(row):
    """A reported field that is present but null shows as empty text, so every reader can format it."""
    for key in records.TEXT_FIELDS:
        if key in row and row[key] is None:
            row[key] = ""
    return row


def _listing_observation(properties, facts, snapshots):
    terminal = facts.get('tmux') or {}
    cached = terminal.get('socket') in snapshots
    observation = observe(properties, facts, snapshots)
    if cached and observation['live'] == 'dormant' and facts.get('state') in ('starting', 'running'):
        # Another row may have captured the server before this pane was tagged.
        snapshots.pop(terminal.get('socket'), None)
        observation = observe(properties, facts, snapshots)
    if facts.get('preparing') is True and facts.get('state') == 'starting':
        if observation['live'] == 'dormant':
            observation = {'live': 'unknown', 'reason': 'Launch is being prepared'}
        elif observation['live'] == 'unknown' and terminal:
            facts = {**facts, 'preparing': False}  # An unreadable server remains a diagnostic.
    return facts, observation


def _listing_runtime(directory, facts, *, prepare=False):
    """A newer launch sidecar cannot condemn an older runtime snapshot.

    Re-read once on damage, only for the same launch, then validate again.
    Control paths keep their original authority checks; this never repairs disk.
    """
    for retry in (True, False):
        try:
            checked = agents.preparation_snapshot(directory, facts) if prepare else facts
            if error := missing_runtime.damage(directory, checked):
                raise error
            return checked
        except records.RecordError:
            if not retry:
                raise
            current = records.runtime(directory / 'runtime.json')
            if (not facts.get('launch_id') or current.get('launch_id') != facts['launch_id']
                    or current == facts):
                raise
            facts = current


# Built-in fields accepted by JSON projections. Full rows also preserve any
# additional legacy properties; projecting never changes the stored record.
ROW_FIELDS = frozenset((
    'id', 'universe_id', 'name', 'harness', 'model', 'task', 'phase', 'summary',
    'workspace', 'created_at', 'updated_at', 'native_session_id', 'native_session_home',
    'native_session_homes', 'native_session_provider', 'native_session_transition',
    'native_storage_environment', 'native_environment', 'native_service_identity',
    'agent_identity', 'agent_environment', 'native_fresh_pending',
    'universe', 'live', 'status', 'reason', 'launch_error', 'connection_error', 'identity_error',
    'recovery_note', 'delivery_problem', 'focus', 'runtime', 'damaged',
    'usage', 'tokens', 'usage_detail',
))


def row_usage(row):
    """Measure an already reconciled row, without rebinding or writing core records.

    The caller must discard a result if the displayed row's universe, agent,
    launch, native ID or storage changed while the reader ran. A damaged or
    identity-error row never becomes a valid measurement through this helper.
    """
    if row.get('damaged'):
        return {}
    measured = (NativeResult('error', row['identity_error']).to_dict() if 'identity_error' in row else
                native.usage_snapshot(Path(row['universe']), row, row['runtime'], reconciled=True))
    return {'usage': measured, 'tokens': display.usage_total(measured), 'usage_detail': display.usage_details(measured)}


def listing(root, recursive=False, only=None, *, usage=True):
    """Every agent's row; with `only` (an agent's ID, already resolved), just that agent's, or none.

    One agent's damaged records never stop the others: that agent's row says which file is unusable and why
    (see damaged_row). With usage=False, omit usage fields without reading native transcripts."""
    if only is not None and (not isinstance(only, str) or only in ("", ".", "..") or "/" in only
                             or "\\" in only or os.sep in only or (os.altsep and os.altsep in only)):
        raise ValueError(f"Not an agent ID: {only!r}")
    rows, snapshots = [], {}
    for scope in universe.tree(root) if recursive else [root]:
        agents_dir = scope / "state" / "agents"
        paths = [agents_dir / only / "properties.json"] if only is not None else agents.record_paths(scope)
        for path in paths:
            if only is not None and not path.parent.is_dir():
                continue
            try:
                properties = records.properties(path)
            except records.RecordError as error:
                rows.append(damaged_row(scope, error))
                continue
            try:
                facts = records.runtime(path.parent / "runtime.json")
                facts = _listing_runtime(path.parent, facts, prepare=True)
            except records.RecordError as error:
                rows.append(damaged_row(scope, error, properties))
                continue
            try:
                facts, observation = _listing_observation(properties, facts, snapshots)
                if error := missing_runtime.mismatch(path.parent, facts, observation):
                    raise error
                observed_key = observation_key(properties, facts)
                preparing_launch = facts.get('launch_id') if facts.get('preparing') is True else None
                identity_problem = None
                try:
                    properties, facts = native.session_snapshot(scope, path.parent, properties, facts)
                except Exception as error:
                    identity_problem = str(error)
                    facts = records.runtime(path.parent / "runtime.json")
                facts = _listing_runtime(path.parent, facts)
                if preparing_launch and facts.get('launch_id') == preparing_launch and facts.get('state') == 'starting':
                    facts = {**facts, 'preparing': True}  # Reconciliation may reread the same launch without this display hint.
                if observation_key(properties, facts) != observed_key:
                    facts, observation = _listing_observation(properties, facts, snapshots)
                if error := missing_runtime.mismatch(path.parent, facts, observation):
                    raise error
                identity_fields = {'identity_error': identity_problem} if identity_problem is not None else {}
                usage_fields = row_usage({**properties, **identity_fields, 'universe': str(scope), 'runtime': facts}) if usage else {}
                relay = facts.get('native_relay') or {}
                relay_problem = relay.get('reason') if relay.get('state') == 'lost' else None
                if relay_problem and observation['live'] == 'alive':
                    relay_problem = native.missing_socket_problem(scope, properties, facts) or relay_problem
                recovery = facts.get('auto_recovery') or {}
                launch_error = (None if facts.get('exit_code') in (-15, 143) else
                                facts.get('reason') or (facts.get('error') if facts.get('state') == 'failed' else None))
                try:
                    delivery_problem = mailbox.delivery_problem(path.parent, properties['name'],
                        is_final=native.launch_only_filter(scope, path.parent, properties, facts, observation['live']))
                except (OSError, ValueError, TypeError, KeyError):
                    delivery_problem = None  # Doctor reports ledger damage separately.
                rows.append(text_defaults({**properties, **identity_fields, **usage_fields, "universe": str(scope), **observation,
                             "reason": launch_error or observation.get('reason', ''), "launch_error": launch_error,
                             "status": ('disconnected' if relay.get('state') == 'lost' else facts.get("status", "unknown"))
                                       if observation["live"] == "alive" else "-",
                             "connection_error": (facts.get('native_refusal') or {}).get('message')
                                 or relay_problem
                                 or facts.get('binding_conflict') or identity_problem
                                 or (native.connection_problem(scope, properties, facts) if observation['live'] == 'alive' else None),
                             "recovery_note": recovery.get('reason') if recovery.get('state') == 'reconnected' else None,
                             "delivery_problem": delivery_problem,
                             "focus": focus.current(path.parent), "runtime": facts}))
            except records.RecordError as error:
                rows.append(damaged_row(scope, error, properties))
            except Exception as error:  # content no reader expected: this agent's row says so, the others still list
                rows.append(damaged_row(scope, records.RecordError(path.parent, f"unexpected content: {type(error).__name__}: {error}"),
                                        properties))
    return rows


def plugins_record(root, command):
    """runtime.json `plugins` for a command: what the adapter's plugin plan recorded, or, for an adapter
    that applies no plugins, the status-line items of the plugins on here. Always a JSON object of that
    shape; anything else (or any failure) is the empty record. Never fails a launch."""
    try:
        if command.plugins is not None:
            record = command.plugins
        else:
            from ..plugins import harness as plugin_harness
            record = plugin_harness.status_items(root)
        record = json.loads(json.dumps(record))
        if (not isinstance(record, dict) or not isinstance(record.get("plugins"), dict)
                or not isinstance(record.get("status_items"), list)):
            raise ValueError("not a plugins record")
        return record
    except Exception:
        return {"plugins": {}, "status_items": []}


def plain_fallback(command, error):
    if command.fallback is None:
        raise error
    return replace(command.fallback,
                   native_mode={**(command.fallback.native_mode or {}), 'fallback_reason': str(error)})


def start(root, name=None, task=None, harness="codex", model=None, resume=None, socket_path=None,
          wait=False, wait_timeout=60, resume_prompt=None, resume_message_id=None, _recovery=None,
          _queue_wake=False, _restart_queue=False, start_fresh=False):
    effects.require('resume' if resume else 'spawn', resume or name or harness)
    if start_fresh and (not resume or _recovery or resume_message_id is not None or resume_prompt is not None):
        raise ValueError('--start-fresh requires an explicit resume without a wake prompt')
    if not math.isfinite(wait_timeout) or wait_timeout <= 0:
        raise ValueError('wait timeout must be a finite positive number of seconds')
    if (resume_prompt is not None or resume_message_id is not None) and not resume:
        raise ValueError('A resume prompt requires an exact saved agent')
    if _recovery and (not resume or resume_prompt is not None or resume_message_id is not None):
        raise ValueError('Automatic recovery requires an exact resume without a prompt')
    calling_socket, run_root = (None, None) if _recovery else agent_identity.calling_paths()
    socket_path = (_recovery['terminal']['socket'] if _recovery else
                   socket_path if socket_path is not None else calling_socket)
    storage_socket = socket_path  # Only an explicit caller selection contributes storage evidence.
    socket_path = socket_path or str(run_root / 'tmux.sock')
    agent_dir, properties, harness, model = _record(root, name, task, harness, model, resume)
    if _recovery:
        saved_root = runtime(agent_dir).get('run_directory')
        run_root = paths.recorded_directory(saved_root) if saved_root is not None else paths.run_directory()
    paths.prepare(run_root)
    universe.slug(harness)
    harness_dir = root / "harnesses" / harness
    adapter = harnesses.load(root, harness)
    model = model or adapter.default_model
    mode = "resume" if resume else "spawn"
    session_mode = adapter.session_mode
    if not resume and session_mode == "assigned":
        properties["native_session_id"] = properties["id"]
    if not shutil.which("tmux"):
        raise ValueError("tmux is required to spawn agents")
    Tmux(socket_path).ensure(root)
    # A universe can contain agents assigned to different worktrees. Resume in
    # the agent's recorded workspace even if the universe default has changed.
    workdir = Path(properties["workspace"]).resolve() if resume and properties.get("workspace") else universe.workspace(root)
    if not workdir.is_dir():
        raise ValueError(f"Workspace does not exist: {workdir}")
    properties["workspace"] = str(workdir)
    if resume:
        previous = runtime(agent_dir)
        if previous and missing_runtime.damage(agent_dir, previous) is None:
            observation = observe(properties, previous)
            if error := missing_runtime.mismatch(agent_dir, previous, observation):
                raise error
            if observation['live'] in ('stopped', 'dormant'):
                attachments.wait(agent_dir, previous.get('launch_id'), TEARDOWN_TIMEOUT)
    proposed_launch = uid() if not resume else None
    if not resume:
        launch_lock = lambda path: _publish_record(root, agent_dir, properties, proposed_launch)
    elif _recovery:
        launch_lock = existing_lock
    else:
        launch_lock = lambda path: lock(path, create_parent=False)
    with launch_lock(agent_dir / ".launch.lock"):
        missing_notices = []
        previous_observation = None
        if resume:
            # _record's snapshot predates this lock; a /clear report may have
            # rebound the conversation while we waited for it.
            properties = agents.session_properties(root, agent_dir, launch_locked=True)
            properties.setdefault('workspace', str(workdir))
            previous = runtime(agent_dir)
            if error := missing_runtime.damage(agent_dir, previous):
                if _recovery:
                    raise error  # Automatic recovery cannot replace lost authority.
                missing_notices = missing_runtime.require_stopped(agent_dir, properties, socket_path, facts=previous)
                if runtime(agent_dir) != previous:
                    raise records.RecordError(agent_dir / 'runtime.json',
                                              'changed during terminal discovery; retry with the current record')
                previous = {}  # Discard damaged authority only after independent absence proof.
            else:
                previous_observation = observe(properties, previous)
                if error := missing_runtime.mismatch(agent_dir, previous, previous_observation):
                    raise error
                observed_key = observation_key(properties, previous)
                properties, previous = native.session_snapshot(root, agent_dir, properties, previous,
                                                                adapter=adapter, launch_locked=True)
                if error := missing_runtime.damage(agent_dir, previous):
                    raise error
                if observation_key(properties, previous) != observed_key:
                    previous_observation = observe(properties, previous)
                if error := missing_runtime.mismatch(agent_dir, previous, previous_observation):
                    raise error
            workdir = Path(properties['workspace']).resolve()
            if not workdir.is_dir():
                raise ValueError(f'Workspace does not exist: {workdir}')
        else:
            previous = runtime(agent_dir)
        if not resume and agents.preparing(previous) and previous['launch_id'] == proposed_launch:
            previous = {}  # This call published the minimal record while holding this lock.
        else:
            prepared = agents.interrupted_preparation(previous, agent_dir)
            if prepared is not previous:
                previous_observation = None
            previous = prepared
        if _recovery:
            _check_recovery(previous, properties, _recovery)
        if previous and (previous_observation or observe(properties, previous))["live"] not in ('stopped', 'dormant'):
            raise ValueError("Agent is running or its liveness is unknown; refusing a duplicate launch")
        if previous and (closing := attachments.problem(agent_dir, previous)):
            raise RuntimeError(closing + '; retry after terminal cleanup finishes')
        if resume_message_id is not None:
            resume_prompt, tracking = _wake_prompt(root, agent_dir, properties, resume_message_id)
            if _queue_wake:
                resume_prompt = None
        session = SessionHandle.from_records(properties, previous or {}, agent_dir) if resume else None
        launch_id = proposed_launch or uid()
        launch = agent_dir / "launches" / launch_id
        bootstrap = catalog.bootstrap(root, properties)
        if adapter.bootstrap:
            bootstrap += "\n" + adapter.bootstrap.strip() + "\n"
        # Keep launch sockets outside temporary storage. Validate the complete
        # address before native preparation or a child can start.
        service_dir = paths.native_socket(launch_id, directory=run_root).parent
        values = _values(properties, bootstrap, agent_dir, harness, harness_dir, launch, workdir, model, service_dir)
        if resume:
            values['task'] = resume_prompt or ''
        cli_path = _cli_path()
        # This launch's own identity, known before the adapter is asked, so a command prepared only once (plain)
        # can name it, e.g. in a plugin's MCP server. It always wins over ambient or saved values, and the
        # parent's MV2 identity never reaches the child. The tmux socket is added once the pane is owned.
        target = {"MV2_UNIVERSE": str(root), "MV2_AGENT_ID": properties["id"], "MV2_AGENT_DIR": str(agent_dir),
                  "MV2_LAUNCH_ID": launch_id, "MV2_CONTROL_SOCKET": values["control_socket"], "MV2_RUN_DIR": str(run_root), "MV2_CLI": cli_path,
                  "MV2_PYTHON": sys.executable, "MV2_SOURCE": source(),
                  "PATH": str(Path(cli_path).parent) + os.pathsep + os.environ.get('PATH', '')}
        policy_hook = native.adapter_hook(adapter, 'agent_identity_policy')
        policy_spec = policy_hook() if policy_hook else None
        agent_environment = agent_identity.environment(target, policy_spec, properties.get('agent_environment'))
        saved = properties.get("native_environment", {}) if resume else {}
        ambient = {key: value for key, value in os.environ.items() if not key.startswith("MV2_")}
        environment = {**ambient, **saved, **target}
        storage_notices = []
        retired_id = properties.get('native_session_id')
        fresh = start_fresh or agents.sessions.pending_fresh(agent_dir, properties)
        if start_fresh:
            storage_notices.append(storage_adoption.prove_missing(adapter, properties, previous, agent_dir,
                                                                  ambient, _storage_terminals(previous, agent_dir, storage_socket, adapter.storage_env)))
            # Preparation is still read-only. The old binding is retired only
            # once the replacement command is valid and ready to be published.
            for key in agents.sessions.FRESH_FIELDS:
                properties.pop(key, None)
            properties['native_session_id'] = uid() if session_mode == 'assigned' else None
            saved = {}
            environment = {**ambient, **target}
        if fresh:
            mode, session = 'spawn', None
            values['session_id'] = properties.get('native_session_id') or ''
        storage = _storage_environment(adapter, properties, environment, ambient, resume and not start_fresh, agent_dir, previous,
                                       storage_notices, socket_path=storage_socket)
        environment = {key: value for key, value in environment.items() if key not in storage}
        environment.update({key: value for key, value in storage.items() if value is not None})
        context = LaunchContext(mode, values, model, environment, resume_prompt=resume_prompt,
                                agent_environment=agent_environment, storage_keys=tuple(key for key in storage if key != 'HOME'))
        home_hook = native.adapter_hook(adapter, 'session_home')
        home = home_hook(context.environment) if home_hook else None
        if home is not None:
            home = agents.sessions.canonical_home(home)
            if properties.get('native_session_home') not in (None, home):
                raise _storage_difference(properties['native_session_home'], home)
            properties['native_session_home'] = home
            if session is not None:
                session = replace(session, home=home)
        family = getattr(adapter, 'family', '')
        provider = family if isinstance(family, str) and family else harness
        if properties.get('native_session_provider') not in (None, provider):
            raise ValueError('Native provider differs from the saved conversation')
        properties['native_session_provider'] = provider
        # A harness setting for its storage applies at spawn, and so to a fresh start; a resume keeps the
        # recorded value, and says so.
        from_settings = getattr(adapter, 'environment', None) if getattr(adapter, 'settings_storage', False) else None
        from_settings = from_settings if isinstance(from_settings, dict) else {}
        ignored_settings = [f'Kept its recorded {key} {"unset" if storage.get(key) is None else json.dumps(storage[key])}; '
                            f'ignored its harness setting {key}={json.dumps(value)}.'
                            for key, value in from_settings.items()
                            if resume and not start_fresh and key in storage and not _same_directory(storage[key], value)]
        notices = display.notice_lines(missing_notices, ignored_settings, [item.notice for item in storage_notices],
                    _resume_notices(adapter, session, ambient, context.environment, storage,
                                    fresh_start=not any(item.fresh_start for item in storage_notices)) if resume and session else [])
        command = effects.call(adapter, 'resume', session, context) if resume and not fresh else effects.call(adapter, 'spawn', context)
        # An adapter may resolve a saved path through a changed symlink. Compare
        # its effective selection with recorded strings, never re-resolve history.
        selected_storage = dict(storage)
        for selected in (command.environment, command.shared_service.environment if command.shared_service else {}):
            selected_storage.update({key: selected[key] for key in storage
                                     if key in selected and not (resume and not start_fresh and key in from_settings)})
        if resume and not start_fresh:
            recorded_storage = {**storage, **saved, **(properties.get('native_service_identity') or {}).get('scope', {}),
                                **(properties.get('native_storage_environment') or {})}
            for key, value in selected_storage.items():
                if key not in getattr(adapter, 'storage_env', ()):
                    continue  # Other harnesses' settings are inherited by future children, not this conversation.
                recorded = recorded_storage.get(key)
                if recorded not in (None, '') and recorded != value:
                    raise _storage_difference(recorded, value, variable=key)
        storage = selected_storage
        command = _storage_command(command, storage)
        properties['native_storage_environment'] = storage
        if _queue_wake:
            command = replace(command, fallback=None)
        if command.shared_service and command.shared_service.identity:
            identity = asdict(command.shared_service.identity)
            if resume and not start_fresh and properties.get('native_service_identity') not in (None, identity):
                raise ValueError('Native service storage/account scope differs from the saved session; '
                                 'resume with the original native storage settings or spawn a new agent')
            properties['native_service_identity'] = identity
        if _recovery:
            native.validate_recovery_command(command, session)
            command = replace(command, fallback=None)
        argv = list(command.argv)
        if not shutil.which(argv[0]):
            raise ValueError(f'Harness executable not found: {argv[0]}')
        if previous and not _recovery and observe(properties, previous)['live'] == 'stopped':
            # Resume creates a fresh pane; remove only the old owned wrapper.
            old_backend, old_pane = owned(properties, previous, states=('stopped',))
            old_backend.run('kill-pane', '-t', old_pane)
        launch.mkdir(parents=True)
        (launch / "bootstrap.md").write_text(bootstrap)
        (launch / "task.md").write_text(values['task'])
        env = {key: value for key, value in target.items() if key != "PATH"}
        if resume:
            env.update(saved)
        env = {key: value for key, value in env.items() if key not in storage}
        env.update({key: value for key, value in storage.items() if value is not None})
        env["HOME"] = context.environment.get("HOME") or str(Path.home())
        # Persist adapter-selected storage so resume cannot accidentally switch
        # accounts/history when launched by a different monitor environment.
        if command.shared_service:
            env.update(command.shared_service.environment)
            properties['native_environment'] = dict(command.shared_service.environment)
        properties.update(model=model, updated_at=now())
        if start_fresh:
            properties, retirement = agents.sessions.retire(root, agent_dir, retired_id, properties, launch_id,
                                                            previous_launch=previous.get('launch_id'))
            agents._session_event(agent_dir, retirement)
        elif resume:
            with agents.sessions.namespace(root), lock(agent_dir / '.properties.lock'):
                current = read(agent_dir / 'properties.json')
                current.update({key: properties[key] for key in ('model', 'workspace', 'native_environment',
                               'native_storage_environment', 'native_service_identity', 'native_session_home', 'native_session_provider', 'updated_at')
                                if key in properties})
                properties = current
                write(agent_dir / "properties.json", properties)
        else:
            # Publish a name atomically across spawns, before starting any
            # native service or terminal. Resume keeps its existing record.
            with agents.sessions.namespace(root), lock(root / "state" / ".agents.lock"):
                if read(root / 'universe.json')['id'] != properties['universe_id']:
                    raise ValueError('Universe changed before the agent could be published')
                check_available_name(root, name, locked=True, exclude=agent_dir)
                if properties.get('native_session_id'):
                    agents.sessions.claim(root, agent_dir, properties, properties['native_session_id'])
                write(agent_dir / "properties.json", properties)
            _event(agent_dir, "agent.created", None, {"harness": harness, "model": model}, properties)
        write(agent_dir / "runtime.json",
              {**_initial_runtime(launch_id, command, adapter, session, previous, resume and not fresh, _recovery),
               'run_directory': str(run_root)})
        if fresh:
            # The replacement launch has now been published. If retirement
            # crashed earlier, its next resume used spawn preparation above.
            with lock(agent_dir / '.properties.lock'):
                properties = read(agent_dir / 'properties.json')
                properties['native_fresh_pending'] = False
                write(agent_dir / 'properties.json', properties)
        backend = Tmux(socket_path)
        terminal = None
        progress = None
        service_directory_created = False
        try:
            progress = registration.Progress(launch)
            progress.mark('preparing terminal status')
            prepared_status = status_setup(root, properties, launch_id, family)
            env['PATH'] = target['PATH']
            shared_endpoint = None
            if command.shared_service:
                progress.mark('preparing native service')
                if _recovery:
                    shared_endpoint = session.service.endpoint
                else:
                    try:
                        shared_endpoint = native.start_shared_service(command.shared_service, workdir,
                            launch / 'native-service.log', command.unset_env,
                            observe_versions=lambda versions: update_runtime(agent_dir, launch_id, native_versions=versions))
                    except BridgeUnavailable as error:
                        command = plain_fallback(command, error)
            if command.control_endpoint:
                try:
                    paths.prepare(service_dir.parent)
                    service_dir.mkdir(mode=0o700)
                    service_directory_created = True
                except OSError as error:
                    command = plain_fallback(command, BridgeUnavailable(f'Native relay directory unavailable: {error}'))
                    shared_endpoint = None
            runner = Path(__file__).with_name("runner.py")
            progress.mark('creating terminal')
            if _recovery:
                backend = Tmux(_recovery['terminal']['socket'])
                terminal = backend.respawn(properties, previous, workdir, [sys.executable, str(runner), str(launch)])
            else:
                terminal = backend.create(f'mv2-{properties["id"][:8]}-{launch_id[:8]}', workdir,
                                          [sys.executable, str(runner), str(launch)])
            backend = Tmux(terminal["socket"])
            progress.mark('tagging terminal')
            backend.tag(terminal["pane"], properties, launch_id)
            progress.mark('configuring terminal status')
            backend.configure_status(terminal, root, properties, launch_id, family, prepared=prepared_status)
            update_runtime(agent_dir, launch_id, tmux=terminal)
            # Use the server that actually owns this pane, including when tmux
            # resolved a default socket. The runner scrubs inherited MV2 identity.
            env["MV2_TMUX_SOCKET"] = terminal["socket"]
            executor = None
            if command.shared_service:
                progress.mark('preparing native attachment')
                executor = native.NativeExecutor(workdir, properties, runtime(agent_dir))
            context = LaunchContext(mode, values, model, env, shared_endpoint, executor, resume_prompt=resume_prompt,
                                    agent_environment=agent_environment, storage_keys=tuple(key for key in storage if key != 'HOME'))
            if command.shared_service:
                try:
                    command = _storage_command(effects.call(adapter, 'resume', session, context) if resume and not fresh else effects.call(adapter, 'spawn', context), storage)
                    if _recovery:
                        native.validate_recovery_command(command, session)
                        command = replace(command, fallback=None)
                except BridgeUnavailable as error:
                    command = plain_fallback(command, error)
                if not command.shared_service:
                    shared_endpoint = None
                    if service_directory_created:
                        shutil.rmtree(service_dir)
                        service_directory_created = False
            service = native.service_handle(command.shared_service, shared_endpoint) if command.shared_service else None
            if service and asdict(service.identity) != properties.get('native_service_identity'):
                raise ValueError('Native service scope changed during launch preparation')
            if command.rpc_proxy and shared_endpoint and command.rpc_proxy.upstream_path != shared_endpoint.address:
                raise ValueError('Native relay targets a different service endpoint')
            command = replace(command, attachment=native.launch_attachment(command))
            previous_agent_identity = properties.get('agent_identity')
            previous_agent_environment = properties.get('agent_environment')
            if command.agent_identity is not None:
                if command.agent_identity != policy_spec or agent_environment.get('MV2_IDENTITY_MODE') != policy_spec.mode:
                    raise ValueError('Adapter changed its agent identity policy during launch preparation')
                policy = agent_identity.record(command.agent_identity, properties, service)
                with lock(agent_dir / '.properties.lock'):
                    properties = read(agent_dir / 'properties.json')
                    properties['agent_identity'] = policy
                    properties['agent_environment'] = dict(agent_environment)
                    write(agent_dir / 'properties.json', properties)
            if command.fallback is not None:
                # The runner may still switch to the fallback; it records this, never the discarded plan.
                command = replace(command, fallback=replace(command.fallback, plugins=plugins_record(root, command.fallback)))
            if command.attachment and command.attachment.readiness == 'binding':
                if not command.rpc_proxy or not command.rpc_proxy.binds_session:
                    raise ValueError('Attachment requires an adapter protocol that validates binding')
            mouse = bool(command.attachment and command.attachment.mouse)
            progress.mark('recording final launch')
            if mouse:
                # Apply the final resolved policy before releasing the runner.
                backend.run('set-option', '-t', terminal['session'], 'mouse', 'on')
            argv = list(command.argv)
            selected_notices = display.notice_lines(notices, command.notices)
            endpoint = asdict(shared_endpoint or command.control_endpoint) if shared_endpoint or command.control_endpoint else None
            if _queue_wake:
                bound_queue = command.attachment and command.attachment.readiness in ('binding', 'legacy-binding')
                scoped_queue = (getattr(adapter, 'delivery_attempt_ids', False) is True
                                and (command.native_mode or {}).get('mode') == 'bridge')
                if not endpoint or not (bound_queue or scoped_queue):
                    raise ValueError('Queued wake delivery requires its native bridge')
            update_runtime(agent_dir, launch_id, control_endpoint=endpoint,
                           native_mode=command.native_mode, plugins=plugins_record(root, command),
                           native_service=asdict(service) if service else None,
                           attachment=asdict(command.attachment) if command.attachment else None,
                           launch_notices=selected_notices,
                           startup_delivery='waiting' if endpoint else None)
            # Adapter variables never override this launch's identity.
            env = {**command.environment, **env}
            write(launch / "request.json", {**_request(argv, env, command, endpoint, None if _queue_wake else resume_message_id),
                                           'notices': selected_notices, 'resume_notices': notices,
                                           'previous_agent_identity': previous_agent_identity,
                                           'previous_agent_environment': previous_agent_environment,
                                           'agent_environment': dict(agent_environment) if command.agent_identity else None,
                                           'observe_refusal': native.adapter_hook(adapter, 'launch_refusal') is not None})
            if resume_message_id is not None and _queue_wake:
                update_message(agent_dir, resume_message_id, submission_state='pending',
                               delivery_method='native', deferred_launch_id=launch_id,
                               restart_on_delivery=bool(_restart_queue),
                               target_session=properties.get('native_session_id'))
            elif resume_message_id is not None:
                # The runner cannot start until ready exists. Persist uncertainty
                # first so a crash can never silently replay this command-line input.
                update_message(agent_dir, resume_message_id, submission_state='submitting',
                               delivery_method='resume_prompt', wake_launch_id=launch_id,
                               wake_prompt_context=None,
                               target_session=properties.get('native_session_id'),
                               attempts=[*tracking.get('attempts', []), {'state': 'unknown',
                                   'reason': 'Resume registered; awaiting native process startup',
                                   'launch_id': launch_id, 'at': now()}])
            # Before the runner is released, so ready and exited never come before launched.
            _launched(root, agent_dir, launch_id, command, "wake" if resume_message_id is not None else mode, properties)
            progress.release()
        except Exception as error:
            if progress is not None:
                progress.fail()
            if resume_message_id is not None and not (launch / 'ready').exists():
                mailbox.finish_wake(agent_dir, launch_id, resume_message_id, False,
                            f'Resume failed before native process startup: {error}')
            if service_directory_created:
                shutil.rmtree(service_dir)
            if terminal:
                try:
                    if not _recovery:
                        backend.run("kill-session", "-t", terminal["session"])
                except Exception:
                    pass
            update_runtime(agent_dir, launch_id, state="failed", error=str(error))
            if _recovery:
                agents.record_recovery(agent_dir, launch_id, 'failed', 'Automatic reconnect failed: ' + str(error))
            recovery = (f"Resume this agent by its full UUID: {properties['id']}"
                        if resume
                        else "The native session has not started; retry spawn with a unique name")
            raise RuntimeError(f"Agent {properties['name']!r} ({properties['id']}) launch failed: {error}. "
                               f"Failed record preserved. {recovery}.") from error
    result = (wait_ready(root, agent_dir, launch_id, adapter, wait_timeout) if wait
              else launch_result(root, agent_dir))
    notices = display.notice_lines(result.get('notices'), result.get('runtime', {}).get('launch_notices'))
    return {**result, 'notices': notices} if notices else result


def _resume_notices(adapter, session, caller_environment, effective_environment, storage=None, *, fresh_start=True):
    """Optional presentation only; never changes the storage or launch decision."""
    hook = native.adapter_hook(adapter, 'resume_notice')
    notices = []
    try:
        options = ({'fresh_start': False}
                   if not fresh_start and hook and 'fresh_start' in inspect.signature(hook).parameters else {})
        notice = hook(session, dict(caller_environment), dict(effective_environment), **options) if hook else None
    except Exception:
        notice = None
    notice = display.clean(notice).strip() if isinstance(notice, str) else ''
    if notice:
        notices.append(notice)
    for key, value in (storage or {}).items():
        requested = caller_environment.get(key)
        if key in getattr(adapter, 'storage_env', ()) or requested is None or requested == value:
            continue
        kept = 'unset' if value is None else json.dumps(value)
        notices.append(f'Kept its recorded {key} {kept}; ignored {key}={json.dumps(requested)}.')
    return notices



# Steps of start() that need nothing from its launch lock.

@contextmanager
def _publish_record(root, agent_dir, properties, launch_id):
    """Publish a complete identity folder atomically, retaining its launch lock.

    Preparation can take seconds. No peer may mistake its not-yet-written
    properties for a missing record, or reserve the same name concurrently.
    """
    with tempfile.TemporaryDirectory(prefix='.agent-create-', dir=root / 'state') as temporary:
        staging = Path(temporary)
        published = dict(properties)
        with lock(staging / '.launch.lock'):
            with agents.sessions.namespace(root), lock(root / 'state' / '.agents.lock'):
                check_available_name(root, properties['name'], locked=True)
                if agent_dir.exists():
                    raise ValueError(f'Agent directory already exists: {agent_dir}')
                write(staging / 'properties.json', properties)
                write(staging / 'runtime.json', {'launch_id': launch_id, 'state': 'starting',
                      'status': 'unknown', 'started_at': now(), 'preparing': True})
                agent_dir.parent.mkdir(parents=True, exist_ok=True)
                staging.rename(agent_dir)
            try:
                yield
            except BaseException as error:
                try:
                    if agent_dir.is_dir():
                        facts = runtime(agent_dir)
                        minimal = agents.preparing(facts) and facts.get('launch_id') == launch_id
                        unstarted = minimal and all(
                            path.name == launch_id and all(item.name in ('bootstrap.md', 'task.md')
                                                          for item in path.iterdir())
                            for path in missing_runtime.launches(agent_dir))
                        if unstarted:
                            # This is our new reservation, before a runner/service
                            # could start. Rename under the publication namespace;
                            # the staging context disposes of only this attempt.
                            with agents.sessions.namespace(root), lock(root / 'state' / '.agents.lock'), \
                                    lock(agent_dir / '.properties.lock', create_parent=False):
                                if (runtime(agent_dir) == facts
                                        and read(agent_dir / 'properties.json') == published):
                                    agent_dir.rename(staging)
                        if agent_dir.is_dir() and (not (agent_dir / 'runtime.json').exists() or minimal):
                            write(agent_dir / 'runtime.json', {'launch_id': launch_id, 'state': 'failed',
                                  'status': 'unknown', 'error': str(error) or 'Launch preparation interrupted',
                                  'reason': str(error) or 'Launch preparation interrupted', 'stopped_at': now()})
                except (OSError, ValueError):
                    pass  # Keep the original failure; doctor can name damaged state.
                raise

def _record(root, name, task, harness, model, resume):
    """The saved agent to resume, or a new record not yet written: (directory, properties, harness, model)."""
    if resume:
        agent_dir = directory(root, resume)
        properties = read(agent_dir / "properties.json")
        return agent_dir, properties, properties["harness"], model or properties.get("model")
    universe.slug(name)
    check_available_name(root, name)
    properties = {"id": uid(), "universe_id": read(root / "universe.json")["id"],
                  "name": name, "task": task or "", "summary": "", "phase": "starting",
                  "harness": harness, "model": model, "created_at": now(), "updated_at": now()}
    return root / "state" / "agents" / properties["id"], properties, harness, model


def _check_recovery(previous, properties, recovery):
    if (previous.get('launch_id') != recovery['launch_id']
            or (previous.get('auto_recovery') or {}).get('state') != 'restarting'
            or previous.get('tmux') != recovery['terminal']
            or properties.get('native_session_id') != recovery['native_id']
            or previous.get('native_service') != recovery['service']):
        raise ValueError('Automatic recovery cancelled or superseded')


def _wake_prompt(root, agent_dir, properties, message_id):
    """The waking message as the resumed session's starting prompt, and its delivery tracking."""
    message, tracking = message_record(agent_dir, message_id)
    if message.get('delivery') not in ('steer', 'follow-up') or tracking.get('submission_state', 'pending') not in ('pending', 'failed'):
        raise ValueError('Wake message is ineligible or may already have been submitted; refusing replay')
    tracking = native.retarget_message(agent_dir, properties, message, tracking)
    return native.framed_text(root, message), tracking


def _values(properties, bootstrap, agent_dir, harness, harness_dir, launch, workdir, model, service_dir):
    """The placeholders a harness definition's argv can use."""
    return {"name": properties["name"], "agent_id": properties["id"],
            "session_id": properties.get("native_session_id", ""),
            "bootstrap": bootstrap, "bootstrap_json": json.dumps(bootstrap, ensure_ascii=False),
            "task": properties["task"], "agent_dir": str(agent_dir), "harness_dir": str(harness_dir),
            "session_file": str(agent_dir / "session.jsonl"), "bootstrap_file": str(launch / "bootstrap.md"),
            "task_file": str(launch / "task.md"), "workspace": str(workdir), "model": model or "",
            "adapter_dir": str(harnesses.PACKAGES / harness), "control_socket": str(service_dir / 'rpc.sock')}


def _cli_path():
    """The mv2 executable agents call: this checkout's, the one running, or the one on PATH."""
    cli = Path(__file__).resolve().parents[3] / "bin" / "mv2"
    invoked = Path(sys.argv[0]).resolve()
    cli_path = str(cli) if cli.exists() else str(invoked) if invoked.name == "mv2" and invoked.is_file() else shutil.which("mv2")
    if not cli_path:
        raise ValueError("Cannot locate mv2 CLI")
    return cli_path


def _initial_runtime(launch_id, command, adapter, session, previous, resume, recovery):
    runtime_record = {"launch_id": launch_id, "state": "starting", "host": socket.gethostname(),
                      "started_at": now(), "native_mode": command.native_mode}
    if resume and previous.get('automatic_restarts'):
        runtime_record['automatic_restarts'] = previous['automatic_restarts'][-5:]
    queue = previous.get('queue_state')
    if resume and isinstance(queue, dict) and session and queue.get('native_session_id') == session.native_id:
        runtime_record['queue_state'] = dict(queue)
    if recovery:
        event = {'at': now(), 'from_launch_id': previous['launch_id'], 'launch_id': launch_id,
                 'reason': 'Native relay disconnected while the exact thread was idle'}
        runtime_record.update(automatic_restarts=[*previous.get('automatic_restarts', [])[-4:], event],
                              auto_recovery={**(previous.get('auto_recovery') or {}),
                                             'state': 'resuming', 'previous_launch_id': previous['launch_id'],
                                             'reason': 'Automatically resuming the idle native thread'})
    if resume and getattr(adapter, 'retained_usage', None):
        measured = adapter.retained_usage(session, previous)
        if measured is not None:
            # Keep the exact thread's sample and original timestamp until
            # this launch reports again, without treating it as fresh usage.
            runtime_record['last_usage'] = measured
    return runtime_record


def _storage_difference(recorded, selected, *, variable=None):
    label = f' ({variable})' if variable else ''
    return ValueError(f'Native session storage differs from the saved conversation{label}: '
                      f'saved {recorded!r}; requested {selected!r}. '
                      'Restore the original storage at its saved path (undo a rename or symlink change), '
                      'or spawn a new agent for the requested storage')


def _storage_terminals(previous, directory, socket_path=None, keys=()):
    """Storage discovery may inspect a recorded or explicitly selected server, never guess the default.

    Retained requests identify inherited storage; an explicit request value or
    absence takes precedence. A caller-selected server has caller origin.
    """
    recorded = storage_adoption.inherited_terminals(directory, previous, keys)
    backends = [('recorded terminal server', Tmux(saved), overrides) for saved, overrides in recorded]
    if socket_path and not any(saved == socket_path for saved, _ in recorded):
        backends.append(('caller-selected terminal server', Tmux(socket_path), {}))
    return backends


def _storage_environment(adapter, properties, environment, ambient, resume, directory, previous, notices, *, socket_path=None):
    """Replay storage values and explicit absence before the adapter computes home."""
    own_keys = getattr(adapter, 'storage_env', ())
    keys = tuple(dict.fromkeys([*own_keys, *(key for cls in harnesses.BUILTINS.values()
                                           for key in getattr(cls, 'storage_env', ()))]))
    saved = properties.get('native_storage_environment') if resume else None
    if saved is not None and (not isinstance(saved, dict) or any(
            value is not None and (not isinstance(value, str) or '\0' in value) for value in saved.values())):
        raise ValueError('Invalid native_storage_environment in properties.json; restore the launch storage values')
    request = storage_records.launch_request(directory, previous) if resume else {}
    historical = storage_records.request_environment(request, keys)
    scope = (properties.get('native_service_identity') or {}).get('scope', {}) if resume else {}
    for key in own_keys:
        if scope and key not in scope and ambient.get(key):
            raise ValueError(f'Cannot add {key}={ambient[key]!r} to this saved native storage scope; '
                             f'unset {key} to resume, or spawn a new agent for that storage')
    if not resume:
        return {key: environment.get(key) for key in keys}
    family = getattr(adapter, 'family', None)
    proof = {**historical, **properties.get('native_environment', {}), **scope}
    unproven_alias = (family == 'codex' and saved is not None and not properties.get('native_session_home')
        and any(isinstance(saved.get(key), str) and saved[key] and os.path.isabs(saved[key])
                and proof.get(key) in (None, saved[key])
                and os.path.realpath(saved[key]) != saved[key] for key in own_keys))
    if saved is not None and not unproven_alias:
        # A hand-restored alternate spelling may be normalized only against an
        # independent saved canonical path, never by resolving history anew.
        normalized = dict(saved)
        if own_keys and properties.get('native_session_home'):
            proof[own_keys[0]] = properties['native_session_home']
        # Claude's literal variable selects login/settings as well as history;
        # its separately recorded canonical session home guards later alias changes.
        for key in own_keys if family != 'claude' else ():
            value, canonical = saved.get(key), proof.get(key)
            if isinstance(value, str) and value and isinstance(canonical, str) and canonical and value != canonical:
                if os.path.realpath(value) == canonical:
                    normalized[key] = canonical
        properties['native_storage_environment'] = normalized
        return storage_records.snapshot_environment(properties, request, keys)
    candidate_recorded = saved
    if family == 'codex' and saved is None and properties.get('native_session_id'):
        adopted = storage_records.legacy_codex_environment(properties, request)
        if adopted is not None and all(not value or os.path.realpath(value) == value for value in adopted.values()):
            return {**historical, **adopted}
        candidate_recorded = adopted
    if family in ('claude', 'codex') and properties.get('native_session_id'):
        adoption = storage_adoption.discover(adapter, properties, previous, directory, ambient,
                                            _storage_terminals(previous, directory, socket_path, own_keys), recorded=candidate_recorded)
        adopted = dict(adoption.environment)
        if own_keys and adopted.get(own_keys[0]):
            # An explicit storage root does not depend on the candidate user's
            # HOME. Only adoption of its default storage must pin that HOME.
            adopted.pop('HOME', None)
        if family == 'codex':
            # The finder proved this first home's conversation. Save its canonical
            # scope now; later resumes compare these strings without re-resolving history.
            adopted = {key: os.path.realpath(value) if key in own_keys and value else value
                       for key, value in adopted.items()}
            properties['native_storage_environment'] = {**historical, **(saved or {}), **adopted}
            if isinstance(properties.get('native_environment'), dict):
                properties['native_environment'] = {**properties['native_environment'],
                    **{key: adopted[key] for key in own_keys if key in properties['native_environment'] and key in adopted}}
            if isinstance(properties.get('native_service_identity'), dict):
                identity = properties['native_service_identity']
                properties['native_service_identity'] = {**identity, 'scope': {
                    key: adopted.get(key, value) for key, value in identity.get('scope', {}).items()}}
        if adopted.get('HOME'):
            environment['HOME'] = adopted['HOME']
        notices.append(adoption)
        return {**{key: value for key, value in {**historical, **(saved or {})}.items() if key not in own_keys},
                **{key: adopted[key] for key in own_keys if key in adopted},
                **({'HOME': adopted['HOME']} if adopted.get('HOME') else {})}
    return {**historical, **{key: environment.get(key) for key in own_keys}}


def _same_directory(recorded, value):
    """Whether a recorded storage path and a setting reach one directory, so keeping one ignores nothing."""
    if recorded == value:
        return True
    if not (isinstance(recorded, str) and isinstance(value, str) and os.path.isabs(recorded) and os.path.isabs(value)):
        return False
    try:
        return os.path.realpath(recorded) == os.path.realpath(value)
    except (OSError, ValueError):
        return False


def _storage_command(command, storage):
    """The runner must not inherit a different home from its tmux server."""
    environment = {key: value for key, value in command.environment.items() if key not in storage}
    environment.update({key: value for key, value in storage.items() if value is not None})
    unset = tuple(key for key in command.unset_env if key not in storage)
    unset += tuple(key for key, value in storage.items() if value is None)
    return replace(command, environment=environment, unset_env=unset,
                   fallback=_storage_command(command.fallback, storage) if command.fallback else None)


def _request(argv, env, command, endpoint, resume_message_id):
    """What the pane runner reads before it starts the native process."""
    return {"argv": argv, "env": env, "unset_env": list(command.unset_env),
            "control_endpoint": endpoint,
            "shared_service": bool(command.shared_service),
            "attachment": asdict(command.attachment) if command.attachment else None,
            "native_mode": command.native_mode,
            "fallback": asdict(command.fallback) if command.fallback else None,
            "startup_retry": asdict(command.startup_retry) if command.startup_retry else None,
            "resume_message_id": resume_message_id,
            "rpc_proxy": asdict(command.rpc_proxy) if command.rpc_proxy else None}


def launch_result(root, agent_dir):
    properties, facts = read(agent_dir / 'properties.json'), runtime(agent_dir)
    notices = display.notice_lines(facts.get('launch_notices'))
    return {**properties, 'universe': str(root), 'runtime': facts,
            'session': asdict(SessionHandle.from_records(properties, facts, agent_dir)),
            **({'notices': notices} if notices else {})}


def _event(agent_dir, kind, launch=None, data=None, properties=None):
    """Record an event for plugins that subscribe to it; best effort, never raises."""
    try:
        from ..plugins import events
        events.for_agent(agent_dir, kind, launch, data, properties)
    except Exception:
        pass


def _launched(root, agent_dir, launch_id, command, mode, properties):
    """agent.launched: how it launched and which plugins it loaded. Never fails the launch."""
    try:
        loaded = plugins_record(root, command)["plugins"]
        _event(agent_dir, "agent.launched", launch_id, {
            "mode": mode, "native_mode": (command.native_mode or {}).get("mode"),
            "plugins": sorted(name for name, entry in loaded.items()
                              if isinstance(entry, dict) and entry.get("state") == "loaded")}, properties)
    except Exception:
        pass


def launch_ready(adapter, properties, facts, agent_dir, request, live, *, include_delivery=True):
    """Whether a launch is ready: no launch error and no lost relay, running with its process in a live pane,
    past its startup check, attached as its adapter requires (the bridge registered, the binding validated)
    and, with `include_delivery`, its startup messages delivered. wait_ready and the agent.ready event use
    this one predicate; wait_ready checks the first two itself first, to say why."""
    if ((facts.get('native_startup_observation') or {}).get('state') == 'pending'
            or facts.get('reason') or (facts.get('native_relay') or {}).get('state') == 'lost'):
        return False
    running = (facts.get('state') == 'running' and bool(facts.get('pid')) and live == 'alive'
               and facts.get('startup_checked', True))
    if not running:
        return False
    session = SessionHandle.from_records(properties, facts, agent_dir)
    if ('attachment' not in facts and 'attachment' not in request
            and request.get('rpc_proxy') and session.endpoint):
        session = replace(session, attachment=native.legacy_attachment(request.get('shared_service')))
    ready = native.attachment_ready(adapter, session)
    return ready and (not include_delivery or facts.get('startup_delivery') not in ('waiting', 'failed'))


def note_ready(root, agent_dir, launch_id):
    """Record agent.ready once, the first time this launch passes launch_ready. Never raises.

    Called by the processes that change readiness (the runner after its startup
    check, startup delivery once done), outside their locks. The pane's liveness is
    observed as wait_ready observes it. A marker in the launch's directory, created
    exclusively, makes it once per launch.
    """
    try:
        from ..plugins import events
        launch = agent_dir / 'launches' / launch_id
        marker = launch / 'ready-event'
        if marker.exists() or not events.subscribers(root, 'agent.ready'):
            return
        properties, facts = read(agent_dir / 'properties.json'), runtime(agent_dir)
        if facts.get('launch_id') != launch_id:
            return
        adapter = harnesses.load(root, properties['harness'])
        live = observe(properties, facts)['live']
        if not launch_ready(adapter, properties, facts, agent_dir, read(launch / 'request.json'), live):
            return
        _record_ready(agent_dir, launch_id, properties)
    except Exception:
        pass


def _record_ready(agent_dir, launch_id, properties):
    """The caller already checked readiness and subscriber interest. No native query."""
    try:
        marker = agent_dir / 'launches' / launch_id / 'ready-event'
        os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        _event(agent_dir, 'agent.ready', launch_id, {}, properties)
    except (OSError, ValueError):
        pass


def wait_ready(root, agent_dir, launch_id, adapter, timeout, *, include_delivery=True, settle_on_timeout=True):
    """Observe one launch without holding the lock needed by native binding."""
    request = read(agent_dir / 'launches' / launch_id / 'request.json')
    deadline = time.monotonic() + timeout
    exit_deadline = None
    screen_deadline = None
    screen_expired = False
    caller_unverified = False
    while True:
        result = launch_result(root, agent_dir)
        facts = result['runtime']
        if facts.get('launch_id') != launch_id:
            raise ValueError('Launch changed while waiting; refusing to follow a replacement')
        if facts.get('reason'):
            return {**result, 'ok': False, 'ready': False, 'error': facts['reason']}
        if (facts.get('native_relay') or {}).get('state') == 'lost':
            return {**result, 'ok': False, 'ready': False, 'error': facts['native_relay']['reason']}
        if (facts.get('native_startup_observation') or {}).get('state') == 'pending':
            from . import refusal
            if screen_deadline is None:
                screen_deadline = refusal.startup_deadline(facts)
            if not screen_expired and time.monotonic() >= screen_deadline:
                screen_expired = True
                refusal.settle_startup(agent_dir, launch_id, 'unverified')
                continue  # Re-read refusal/generation/state after atomic settlement.
        live = observe(result, facts)['live']
        # A caller deadline changes only this answer. The runner keeps its own
        # observation/retry window, and a re-read catches a refusal or retry claim.
        unverified = (caller_unverified and (facts.get('native_startup_observation') or {}).get('state') == 'pending'
                      and (facts.get('attachment') or {}).get('readiness') == 'process')
        ready_facts = {**facts, 'native_startup_observation': None} if unverified else facts
        if launch_ready(adapter, result, ready_facts, agent_dir, request, live, include_delivery=include_delivery):
            if screen_expired and launch_ready(adapter, result, facts, agent_dir, request, live):
                try:
                    from ..plugins import events
                    if events.subscribers(root, 'agent.ready'):
                        _record_ready(agent_dir, launch_id, result)
                except Exception:
                    pass  # Optional events never hold up readiness.
            if unverified or (facts.get('native_startup_observation') or {}).get('state') == 'unverified':
                result = {**result, 'notices': [*result.get('notices', []),
                    'Startup could not be confirmed from the native screen; continuing with process readiness.']}
            return {**result, 'ok': True, 'ready': True}
        error = None
        if facts.get('state') == 'failed' or live in ('ending', 'stopped', 'dormant'):
            error = facts.get('error') or f'Launch ended before becoming ready ({live})'
        elif facts.get('state') == 'starting' and facts.get('error'):
            error = facts['error']
        elif include_delivery and facts.get('startup_delivery') == 'failed':
            error = facts.get('delivery_error') or 'Startup delivery failed; inspect the launch and saved messages'
        if error:
            if facts.get('state') in ('starting', 'running') and live in ('stopped', 'dormant'):
                # observe() can finish after the runner writes its exit record,
                # leaving this iteration with stale runtime facts. Allow its
                # diagnostic to settle, but never wait indefinitely for a dead
                # runner or exceed the caller's readiness deadline.
                if exit_deadline is None:
                    exit_deadline = min(deadline, time.monotonic() + .5)
                remaining = exit_deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(.05, remaining))
                    continue
            return {**result, 'ok': False, 'ready': False, 'error': error}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if (settle_on_timeout and not caller_unverified and not screen_expired
                    and (facts.get('native_startup_observation') or {}).get('state') == 'pending'
                    and (facts.get('attachment') or {}).get('readiness') == 'process'
                    and launch_ready(adapter, result, {**facts, 'native_startup_observation': None},
                                     agent_dir, request, live, include_delivery=include_delivery)):
                caller_unverified = True
                continue  # Re-read without settling the runner's observation.
            return {**result, 'ok': False, 'ready': False, 'timed_out': True,
                    'error': f'Timed out after {timeout:g}s waiting for launch readiness; '
                             'the launch was not stopped. Inspect its runtime or terminal before retrying.'}
        time.sleep(min(.05, remaining))


def terminal_action(root, reference, action):
    if action in ('stop', 'kill'):
        return kill(root, reference)
    agent_dir = directory(root, reference)
    facts = runtime(agent_dir)
    if action == 'inspect' and facts.get('reason'):
        return facts['reason']
    backend, pane = owned(read(agent_dir / "properties.json"), facts,
                          states=('alive', 'stopped') if action == 'inspect' else ('alive',))
    if action == "inspect":
        return backend.run("capture-pane", "-p", "-S", "-100", "-t", pane)
    return backend, pane


def kill(root, reference, *, attachment_only=False):
    """End native work, await its process exit, then remove the owned wrapper."""
    effects.require('stop', reference)
    agent_dir = directory(root, reference, allow_damaged=True)
    if agents.properties_missing(agent_dir):
        return missing_runtime.kill(root, agent_dir, attachment_only=attachment_only)
    records.properties(agent_dir / 'properties.json')
    initial, malformed = missing_runtime.recovery_snapshot(agent_dir)
    initial_observation = None
    damaged = malformed is not None or missing_runtime.damage(agent_dir, initial)
    if not damaged and (agent_dir / 'runtime.json').exists():
        initial_observation = observe(records.properties(agent_dir / 'properties.json'), initial)
        damaged = missing_runtime.mismatch(agent_dir, initial, initial_observation)
    if not (agent_dir / 'runtime.json').exists() or damaged:
        recovered = missing_runtime.kill(root, agent_dir, attachment_only=attachment_only)
        if recovered is not None:
            return recovered
        initial_observation = None  # Recovery found it healthy; the earlier pane snapshot is stale.
    with lock(agent_dir / '.launch.lock'):
        previous = runtime(agent_dir)
        facts = agents.interrupted_preparation(previous, agent_dir)
        if facts is not previous and facts.get('state') != 'failed':
            raise records.RecordError(agent_dir / 'runtime.json',
                f'preparation record has native startup evidence; repair or remove this damaged runtime record, '
                f'then run mv2 kill {reference} to discover its tagged terminal')
        if facts is not previous:
            update_runtime(agent_dir, facts['launch_id'], state='failed', preparing=False,
                           error=facts['error'], reason=facts['reason'])
            facts = runtime(agent_dir)
        if not facts:
            raise records.RecordError(agent_dir / 'runtime.json', 'disappeared during stop; retry to discover the tagged terminal')
        properties = read(agent_dir / 'properties.json')
        observation = initial_observation if facts == initial else None
        if error := missing_runtime.damage(agent_dir, facts):
            raise error  # Changed after the outer recovery check; retry with these facts.
        observation = observation or observe(properties, facts)
        if error := missing_runtime.mismatch(agent_dir, facts, observation):
            raise error  # Recovery must recheck positive identity under its own lock.
        live = observation['live']
        if live == 'dormant':
            backend = pane = None
        else:
            backend, pane = owned(properties, facts, states=('alive', 'ending', 'stopped'))
        launch_id = facts['launch_id']
        if (facts.get('auto_recovery') or {}).get('state') in ('checking', 'restarting', 'resuming'):
            update_runtime(agent_dir, launch_id, auto_recovery={**facts['auto_recovery'],
                           'state': 'cancelled', 'reason': 'Automatic reconnect cancelled by explicit stop'})
        # Older launches may have inherited remain-on-exit=off.
        if backend is not None:
            backend.run('set-option', '-p', '-t', pane, 'remain-on-exit', 'on')
        update_runtime(agent_dir, launch_id, state='ending', status='unknown', ending_at=now())
        try:
            if attachment_only:
                attachment = facts.get('attachment')
                service_owned = isinstance(attachment, dict) and attachment.get('work_owner') == 'service'
                reason = ('Only this terminal attachment was closed; conversation work was not cancelled'
                          if service_owned else
                          'Terminal process closed; this also ends work owned by that process. '
                          'No separate native cancellation was requested')
                cancellation = NativeResult('ok', reason)
            else:
                cancellation = native.stop(root, reference)
            if attachment_only:
                mailbox.end_startup_messages(agent_dir, launch_id,
                    'Terminal attachment closed before delivery; saved for explicit retry')
        except Exception as error:
            if live not in ('stopped', 'dormant'):
                update_runtime(agent_dir, launch_id, state=facts.get('state', 'running'),
                               ending_at=facts.get('ending_at'),
                               error=f'Native cancellation failed; remaining terminal retained: {error}')
                raise
            # A dead child has no terminal connection to preserve. Removing its
            # owned wrapper must not depend on an unreachable shared service.
            cancelled = mailbox.end_startup_messages(agent_dir, launch_id, 'Cancelled by explicit agent stop', cancelled=True)
            cancellation = NativeResult('unknown',
                f'Native cancellation unconfirmed: {error}. Retry stop when the native service is reachable.',
                {'cancelled_startup_messages': cancelled})
        update_runtime(agent_dir, launch_id, native_cancellation=cancellation.to_dict() if cancellation else None)

    # The runner's relay can be binding under .launch.lock during startup or
    # shutdown. Never hold that lock while waiting for the child/runner to exit.
    def current():
        latest = runtime(agent_dir)
        if latest.get('launch_id') != launch_id:
            raise ValueError('Outdated launch; refusing to kill a replacement')
        return latest

    def wait_for_exit(timeout):
        deadline = time.monotonic() + timeout
        while True:
            latest = current()
            live = observe(properties, latest)['live']
            if live in ('stopped', 'dormant'):
                return
            if live == 'unknown':
                raise RuntimeError('Cannot verify native exit; terminal retained')
            if time.monotonic() >= deadline:
                raise TimeoutError('Native process has not exited; terminal retained')
            time.sleep(.05)

    try:
        if observe(properties, current())['live'] in ('alive', 'ending'):
            backend.signal_child(properties, current(), signal.SIGTERM)
            try:
                wait_for_exit(2)
            except TimeoutError:
                backend.signal_child(properties, current(), signal.SIGKILL)
                wait_for_exit(5)
        attachments.wait(agent_dir, launch_id, TEARDOWN_TIMEOUT)
        with lock(agent_dir / '.launch.lock'):
            latest = current()
            if closing := attachments.problem(agent_dir, latest):
                raise RuntimeError(closing)
            live = observe(properties, latest)['live']
            if live == 'stopped':
                backend, pane = owned(properties, latest, states=('stopped',))
                update_runtime(agent_dir, launch_id, state='stopped', status='unknown', stopped_at=now())
                backend.run('kill-pane', '-t', pane)
            elif live != 'dormant':
                raise RuntimeError(f'Native exit not confirmed ({live}); terminal retained')
            update_runtime(agent_dir, launch_id, state='dormant', status='unknown', dormant_at=now(),
                           error=cancellation.reason if cancellation and cancellation.state != 'ok' else None,
                           native_cancellation=cancellation.to_dict() if cancellation else None)
        _event(agent_dir, "agent.killed", launch_id,
               {"native_cancellation": cancellation.state if cancellation else "ok"}, properties)
        # Receipt IDs remain internal; callers need counts and the native result.
        details = (cancellation.value or {}) if cancellation else {}
        return {'id': agent_dir.name, 'state': 'dormant', 'native_cancellation': {
            'state': cancellation.state if cancellation else 'ok',
            'reason': cancellation.reason if cancellation else '',
            **{key: value for key, value in details.items() if key != 'cancelled_message_ids'}}}
    except Exception as error:
        if runtime(agent_dir).get('launch_id') == launch_id:
            update_runtime(agent_dir, launch_id, error=f'Kill incomplete: {error}')
        raise
