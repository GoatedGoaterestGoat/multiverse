"""An agent's process lifecycle: launch (spawn, resume, wake), readiness, listing and kill."""
from dataclasses import asdict, replace
import json
import math
import os
from pathlib import Path
import shutil
import signal
import socket
import sys
import time

from .. import display, harnesses, universe
from ..harnesses.types import LaunchContext, SessionHandle, NativeResult, BridgeUnavailable
from ..store import existing_lock, lock, now, read, uid, write
from ..terminal.tmux import Tmux, observe, owned
from ..universe import agents, catalog, mailbox
from ..universe.agents import check_available_name, directory, runtime, update_runtime
from ..universe.mailbox import message_record, update_message
from . import native

def listing(root, recursive=False):
    rows, snapshots = [], {}
    for scope in universe.tree(root) if recursive else [root]:
        for path in sorted((scope / "state" / "agents").glob("*/properties.json")):
            try:
                properties = read(path)
                facts = runtime(path.parent)
                observation = observe(properties, facts, snapshots)
                measured = native.usage_snapshot(scope, properties, facts)
                relay = facts.get('native_relay') or {}
                recovery = facts.get('auto_recovery') or {}
                launch_error = facts.get('reason') or (facts.get('error') if facts.get('state') == 'failed' else None)
                rows.append({**properties, "usage": measured, "tokens": display.usage_total(measured),
                             "usage_detail": display.usage_details(measured), "universe": str(scope), **observation,
                             "reason": launch_error or observation.get('reason', ''), "launch_error": launch_error,
                             "status": ('disconnected' if relay.get('state') == 'lost' else facts.get("status", "unknown"))
                                       if observation["live"] == "alive" else "-",
                             "connection_error": relay.get('reason') if relay.get('state') == 'lost' else None,
                             "recovery_note": recovery.get('reason') if recovery.get('state') == 'reconnected' else None,
                             "runtime": facts})
            except (OSError, ValueError, KeyError) as error:
                rows.append({"id": path.parent.name, "name": path.parent.name, "universe": str(scope),
                             "live": "unknown", "status": "unknown", "phase": "unknown", "summary": str(error)})
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
          wait=False, wait_timeout=60, resume_prompt=None, resume_message_id=None, _recovery=None):
    if not math.isfinite(wait_timeout) or wait_timeout <= 0:
        raise ValueError('wait timeout must be a finite positive number of seconds')
    if (resume_prompt is not None or resume_message_id is not None) and not resume:
        raise ValueError('A resume prompt requires an exact saved agent')
    if _recovery and (not resume or resume_prompt is not None or resume_message_id is not None):
        raise ValueError('Automatic recovery requires an exact resume without a prompt')
    agent_dir, properties, harness, model = _record(root, name, task, harness, model, resume)
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
    # A universe can contain agents assigned to different worktrees. Resume in
    # the agent's recorded workspace even if the universe default has changed.
    workdir = Path(properties["workspace"]).resolve() if resume and properties.get("workspace") else universe.workspace(root)
    if not workdir.is_dir():
        raise ValueError(f"Workspace does not exist: {workdir}")
    properties["workspace"] = str(workdir)
    if _recovery:
        launch_lock = existing_lock
    else:
        agent_dir.mkdir(parents=True, exist_ok=True)
        launch_lock = lock
    with launch_lock(agent_dir / ".launch.lock"):
        previous = runtime(agent_dir)
        if _recovery:
            _check_recovery(previous, properties, _recovery)
        if previous and observe(properties, previous)["live"] not in ('stopped', 'dormant'):
            raise ValueError("Agent is running or its liveness is unknown; refusing a duplicate launch")
        if resume_message_id is not None:
            resume_prompt, tracking = _wake_prompt(root, agent_dir, properties, resume_message_id)
        session = SessionHandle.from_records(properties, previous or {}, agent_dir) if resume else None
        launch_id = uid()
        launch = agent_dir / "launches" / launch_id
        bootstrap = catalog.bootstrap(root, properties)
        if adapter.bootstrap:
            bootstrap += "\n" + adapter.bootstrap.strip() + "\n"
        # A short private path avoids Unix socket length limits in deep worktrees.
        # Created only when the adapter requests a native service below.
        service_dir = Path('/tmp') / ('mv2-native-' + launch_id)
        values = _values(properties, bootstrap, agent_dir, harness, harness_dir, launch, workdir, model, service_dir)
        cli_path = _cli_path()
        # This launch's own identity, known before the adapter is asked, so a command prepared only once (plain)
        # can name it, e.g. in a plugin's MCP server. It always wins over ambient or saved values, and the
        # parent's MV2 identity never reaches the child. The tmux socket is added once the pane is owned.
        target = {"MV2_UNIVERSE": str(root), "MV2_AGENT_ID": properties["id"], "MV2_AGENT_DIR": str(agent_dir),
                  "MV2_LAUNCH_ID": launch_id, "MV2_CONTROL_SOCKET": values["control_socket"], "MV2_CLI": cli_path,
                  "MV2_PYTHON": sys.executable, "PATH": str(Path(cli_path).parent) + os.pathsep + os.environ.get('PATH', '')}
        saved = properties.get("native_environment", {}) if resume else {}
        ambient = {key: value for key, value in os.environ.items() if not key.startswith("MV2_")}
        context = LaunchContext(mode, values, model, {**ambient, **saved, **target}, resume_prompt=resume_prompt)
        command = adapter.resume(session, context) if resume else adapter.spawn(context)
        if command.shared_service and command.shared_service.identity:
            identity = asdict(command.shared_service.identity)
            if resume and properties.get('native_service_identity') not in (None, identity):
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
        (launch / "task.md").write_text(resume_prompt if resume_prompt is not None else properties["task"])
        env = {key: value for key, value in target.items() if key != "PATH"}
        if resume:
            env.update(saved)
        # Persist adapter-selected storage so resume cannot accidentally switch
        # accounts/history when launched by a different monitor environment.
        if command.shared_service:
            env.update(command.shared_service.environment)
            properties['native_environment'] = dict(command.shared_service.environment)
        properties.update(model=model, updated_at=now())
        if resume:
            write(agent_dir / "properties.json", properties)
        else:
            # Publish a name atomically across spawns, before starting any
            # native service or terminal. Resume keeps its existing record.
            with lock(root / "state" / ".agents.lock"):
                check_available_name(root, name)
                write(agent_dir / "properties.json", properties)
        write(agent_dir / "runtime.json",
              _initial_runtime(launch_id, command, adapter, session, previous, resume, _recovery))
        backend = Tmux(socket_path or os.environ.get("MV2_TMUX_SOCKET"))
        terminal = None
        service_directory_created = False
        try:
            shared_endpoint = None
            if command.shared_service:
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
                    service_dir.mkdir(mode=0o700)
                    service_directory_created = True
                except OSError as error:
                    command = plain_fallback(command, BridgeUnavailable(f'Native relay directory unavailable: {error}'))
                    shared_endpoint = None
            runner = Path(__file__).with_name("runner.py")
            if _recovery:
                backend = Tmux(_recovery['terminal']['socket'])
                terminal = backend.respawn(properties, previous, workdir, [sys.executable, str(runner), str(launch)])
            else:
                terminal = backend.create(f'mv2-{properties["id"][:8]}-{launch_id[:8]}', workdir,
                                          [sys.executable, str(runner), str(launch)])
            backend = Tmux(terminal["socket"])
            backend.tag(terminal["pane"], properties, launch_id)
            family = getattr(adapter, 'family', '')  # independent adapters may not declare one
            backend.configure_status(terminal, root, properties, launch_id, family)
            update_runtime(agent_dir, launch_id, tmux=terminal)
            # Use the server that actually owns this pane, including when tmux
            # resolved a default socket. The runner scrubs inherited MV2 identity.
            env["MV2_TMUX_SOCKET"] = terminal["socket"]
            env['PATH'] = str(Path(cli_path).parent) + os.pathsep + os.environ.get('PATH', '')
            executor = None
            if command.shared_service:
                executor = native.NativeExecutor(workdir, properties, runtime(agent_dir))
            context = LaunchContext(mode, values, model, env, shared_endpoint, executor, resume_prompt=resume_prompt)
            if command.shared_service:
                try:
                    command = adapter.resume(session, context) if resume else adapter.spawn(context)
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
            if command.fallback is not None:
                # The runner may still switch to the fallback; it records this, never the discarded plan.
                command = replace(command, fallback=replace(command.fallback, plugins=plugins_record(root, command.fallback)))
            if command.attachment and command.attachment.readiness == 'binding':
                if not command.rpc_proxy or not command.rpc_proxy.binds_session:
                    raise ValueError('Attachment requires an adapter protocol that validates binding')
            mouse = bool(command.attachment and command.attachment.mouse)
            if mouse:
                # Apply the final resolved policy before releasing the runner.
                backend.run('set-option', '-t', terminal['session'], 'mouse', 'on')
            argv = list(command.argv)
            endpoint = asdict(shared_endpoint or command.control_endpoint) if shared_endpoint or command.control_endpoint else None
            update_runtime(agent_dir, launch_id, control_endpoint=endpoint,
                           native_mode=command.native_mode, plugins=plugins_record(root, command),
                           native_service=asdict(service) if service else None,
                           attachment=asdict(command.attachment) if command.attachment else None,
                           startup_delivery='waiting' if endpoint else None)
            # Adapter variables never override this launch's identity.
            env = {**command.environment, **env}
            write(launch / "request.json", _request(argv, env, command, endpoint, resume_message_id))
            if resume_message_id is not None:
                # The runner cannot start until ready exists. Persist uncertainty
                # first so a crash can never silently replay this command-line input.
                update_message(agent_dir, resume_message_id, submission_state='submitting',
                               delivery_method='resume_prompt', wake_launch_id=launch_id,
                               target_session=properties.get('native_session_id'),
                               attempts=[*tracking.get('attempts', []), {'state': 'unknown',
                                   'reason': 'Resume registered; awaiting native process startup',
                                   'launch_id': launch_id, 'at': now()}])
            (launch / "ready").touch()
        except Exception as error:
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
    if wait:
        return wait_ready(root, agent_dir, launch_id, adapter, wait_timeout)
    return launch_result(root, agent_dir)



# Steps of start() that need nothing from its launch lock.

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
    target = tracking.get('target_session') or message.get('target_session')
    if target and target != properties.get('native_session_id'):
        raise ValueError('Wake message targets a different native session')
    if message.get('delivery') not in ('steer', 'follow-up') or tracking.get('submission_state', 'pending') not in ('pending', 'failed'):
        raise ValueError('Wake message is ineligible or may already have been submitted; refusing replay')
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


def _request(argv, env, command, endpoint, resume_message_id):
    """What the pane runner reads before it starts the native process."""
    return {"argv": argv, "env": env, "unset_env": list(command.unset_env),
            "control_endpoint": endpoint,
            "shared_service": bool(command.shared_service),
            "attachment": asdict(command.attachment) if command.attachment else None,
            "native_mode": command.native_mode,
            "fallback": asdict(command.fallback) if command.fallback else None,
            "resume_message_id": resume_message_id,
            "rpc_proxy": asdict(command.rpc_proxy) if command.rpc_proxy else None}


def launch_result(root, agent_dir):
    properties, facts = read(agent_dir / 'properties.json'), runtime(agent_dir)
    return {**properties, 'universe': str(root), 'runtime': facts,
            'session': asdict(SessionHandle.from_records(properties, facts, agent_dir))}


def wait_ready(root, agent_dir, launch_id, adapter, timeout, *, include_delivery=True):
    """Observe one launch without holding the lock needed by native binding."""
    request = read(agent_dir / 'launches' / launch_id / 'request.json')
    deadline = time.monotonic() + timeout
    exit_deadline = None
    while True:
        result = launch_result(root, agent_dir)
        facts = result['runtime']
        if facts.get('launch_id') != launch_id:
            raise ValueError('Launch changed while waiting; refusing to follow a replacement')
        if facts.get('reason'):
            return {**result, 'ok': False, 'ready': False, 'error': facts['reason']}
        if (facts.get('native_relay') or {}).get('state') == 'lost':
            return {**result, 'ok': False, 'ready': False, 'error': facts['native_relay']['reason']}
        live = observe(result, facts)['live']
        running = (facts.get('state') == 'running' and bool(facts.get('pid')) and live == 'alive'
                   and facts.get('startup_checked', True))
        session = SessionHandle.from_records(result, facts, agent_dir)
        if ('attachment' not in facts and 'attachment' not in request
                and request.get('rpc_proxy') and session.endpoint):
            session = replace(session, attachment=native.legacy_attachment(request.get('shared_service')))
        ready = running and native.attachment_ready(adapter, session)
        ready = ready and (not include_delivery or facts.get('startup_delivery') not in ('waiting', 'failed'))
        if ready:
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


def kill(root, reference):
    """End native work, await its process exit, then remove the owned wrapper."""
    agent_dir = directory(root, reference)
    with lock(agent_dir / '.launch.lock'):
        facts = runtime(agent_dir)
        properties = read(agent_dir / 'properties.json')
        live = observe(properties, facts)['live']
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
            cancellation = native.stop(root, reference)
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
        with lock(agent_dir / '.launch.lock'):
            latest = current()
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
