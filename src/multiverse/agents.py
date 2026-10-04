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

from . import universe, harnesses
from .harnesses.types import LaunchContext, SessionHandle, NativeResult, normalize_session_id, BridgeUnavailable
from .store import lock, now, read, uid, write, message_record, update_message
from .tmux import Tmux, observe, owned


def directory(root, reference):
    matches = []
    for path in (root / "state" / "agents").glob("*/properties.json"):
        data = read(path)
        if reference in (data["id"], data["name"]) or data["id"].startswith(reference):
            matches.append(path.parent)
    if not matches:
        raise ValueError(f"No agent named {reference} here. Run mv2 list to see this universe's agents.")
    if len(matches) != 1:
        detail = ". Use a full UUID from these matches: " + ", ".join(sorted(path.name for path in matches))
        raise ValueError(f"Expected one agent matching {reference!r}; found {len(matches)}{detail}")
    return matches[0]


def _check_available_name(root, name):
    for path in (root / "state" / "agents").glob("*/properties.json"):
        if read(path).get("name") == name:
            raise ValueError(f"This universe already has an agent named {name}; "
                             f"pick another name or resume it with mv2 resume {name}")


def runtime(agent_dir):
    path = agent_dir / "runtime.json"
    return read(path) if path.exists() else {}


def update_runtime(agent_dir, launch_id, **changes):
    with lock(agent_dir / ".runtime.lock"):
        data = runtime(agent_dir)
        if data.get("launch_id") != launch_id:
            raise ValueError("Outdated launch; refusing to update current runtime")
        data.update(changes, observed_at=now())
        write(agent_dir / "runtime.json", data)
        return data


def listing(root, recursive=False):
    rows, snapshots = [], {}
    for scope in universe.tree(root) if recursive else [root]:
        for path in sorted((scope / "state" / "agents").glob("*/properties.json")):
            try:
                properties = read(path)
                facts = runtime(path.parent)
                observation = observe(properties, facts, snapshots)
                from . import native, usage
                measured = native.usage_snapshot(scope, properties, facts)
                relay = facts.get('native_relay') or {}
                launch_error = facts.get('reason') or (facts.get('error') if facts.get('state') == 'failed' else None)
                rows.append({**properties, "usage": measured, "tokens": usage.display(measured),
                             "usage_detail": usage.details(measured), "universe": str(scope), **observation,
                             "reason": launch_error or observation.get('reason', ''), "launch_error": launch_error,
                             "status": ('disconnected' if relay.get('state') == 'lost' else facts.get("status", "unknown"))
                                       if observation["live"] == "alive" else "-",
                             "connection_error": relay.get('reason') if relay.get('state') == 'lost' else None,
                             "runtime": facts})
            except (OSError, ValueError, KeyError) as error:
                rows.append({"id": path.parent.name, "name": path.parent.name, "universe": str(scope),
                             "live": "unknown", "status": "unknown", "phase": "unknown", "summary": str(error)})
    return rows


def bind_session(root, reference, session_id, launch_id=None):
    """Record a harness-reported session without guessing from global history."""
    agent_dir = directory(root, reference)
    with lock(agent_dir / ".launch.lock"), lock(agent_dir / ".properties.lock"):
        if launch_id and runtime(agent_dir).get("launch_id") != launch_id:
            raise ValueError("Outdated launch")
        data = read(agent_dir / "properties.json")
        session_id = normalize_session_id(session_id)
        if data.get("native_session_id") not in (None, session_id):
            raise ValueError("This agent is already bound to a different native session")
        data.update(native_session_id=session_id, updated_at=now())
        write(agent_dir / "properties.json", data)
    return data


def plain_fallback(command, error):
    if command.fallback is None:
        raise error
    return replace(command.fallback,
                   native_mode={**(command.fallback.native_mode or {}), 'fallback_reason': str(error)})


def start(root, name=None, task=None, harness="codex", model=None, resume=None, socket_path=None,
          wait=False, wait_timeout=60, resume_prompt=None, resume_message_id=None):
    if not math.isfinite(wait_timeout) or wait_timeout <= 0:
        raise ValueError('wait timeout must be a finite positive number of seconds')
    if (resume_prompt is not None or resume_message_id is not None) and not resume:
        raise ValueError('A resume prompt requires an exact saved agent')
    if resume:
        agent_dir = directory(root, resume)
        properties = read(agent_dir / "properties.json")
        harness, model = properties["harness"], model or properties.get("model")
    else:
        universe.slug(name)
        _check_available_name(root, name)
        task = task or ""
        properties = {"id": uid(), "universe_id": read(root / "universe.json")["id"],
                      "name": name, "task": task, "summary": "", "phase": "starting",
                      "harness": harness, "model": model, "created_at": now(), "updated_at": now()}
        agent_dir = root / "state" / "agents" / properties["id"]
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
    agent_dir.mkdir(parents=True, exist_ok=True)
    with lock(agent_dir / ".launch.lock"):
        previous = runtime(agent_dir)
        if previous and observe(properties, previous)["live"] not in ('stopped', 'dormant'):
            raise ValueError("Agent is running or its liveness is unknown; refusing a duplicate launch")
        if resume_message_id is not None:
            from .native import framed_text
            message, tracking = message_record(agent_dir, resume_message_id)
            target = tracking.get('target_session') or message.get('target_session')
            if target and target != properties.get('native_session_id'):
                raise ValueError('Wake message targets a different native session')
            if message.get('delivery') not in ('steer', 'follow-up') or tracking.get('submission_state', 'pending') not in ('pending', 'failed'):
                raise ValueError('Wake message is ineligible or may already have been submitted; refusing replay')
            resume_prompt = framed_text(root, message)
        session = SessionHandle.from_records(properties, previous or {}, agent_dir) if resume else None
        launch_id = uid()
        launch = agent_dir / "launches" / launch_id
        bootstrap = universe.bootstrap(root, properties)
        if adapter.bootstrap:
            bootstrap += "\n" + adapter.bootstrap.strip() + "\n"
        values = {"name": properties["name"], "agent_id": properties["id"],
                  "session_id": properties.get("native_session_id", ""),
                  "bootstrap": bootstrap, "bootstrap_json": json.dumps(bootstrap, ensure_ascii=False),
                  "task": properties["task"], "agent_dir": str(agent_dir), "harness_dir": str(harness_dir),
                  "session_file": str(agent_dir / "session.jsonl"), "bootstrap_file": str(launch / "bootstrap.md"),
                  "task_file": str(launch / "task.md"), "workspace": str(workdir), "model": model or ""}
        values["adapter_dir"] = str(harnesses.PACKAGES / harness)
        # A short private path avoids Unix socket length limits in deep worktrees.
        # Created only when the adapter requests a native service below.
        service_dir = Path('/tmp') / ('mv2-native-' + launch_id)
        values['control_socket'] = str(service_dir / 'rpc.sock')
        context = LaunchContext(mode, values, model, {**os.environ, **(properties.get("native_environment", {}) if resume else {})},
                                resume_prompt=resume_prompt)
        command = adapter.resume(session, context) if resume else adapter.spawn(context)
        argv = list(command.argv)
        if not shutil.which(argv[0]):
            raise ValueError(f'Harness executable not found: {argv[0]}')
        if previous and observe(properties, previous)['live'] == 'stopped':
            # Resume creates a fresh pane; remove only the old owned wrapper.
            old_backend, old_pane = owned(properties, previous, states=('stopped',))
            old_backend.run('kill-pane', '-t', old_pane)
        launch.mkdir(parents=True)
        (launch / "bootstrap.md").write_text(bootstrap)
        (launch / "task.md").write_text(resume_prompt if resume_prompt is not None else properties["task"])
        cli = Path(__file__).resolve().parents[2] / "bin" / "mv2"
        invoked = Path(sys.argv[0]).resolve()
        cli_path = str(cli) if cli.exists() else str(invoked) if invoked.name == "mv2" and invoked.is_file() else shutil.which("mv2")
        if not cli_path:
            raise ValueError("Cannot locate mv2 CLI")
        env = {"MV2_UNIVERSE": str(root), "MV2_AGENT_ID": properties["id"], "MV2_AGENT_DIR": str(agent_dir),
               "MV2_LAUNCH_ID": launch_id, "MV2_CONTROL_SOCKET": values["control_socket"], "MV2_CLI": cli_path, "MV2_PYTHON": sys.executable}
        if resume:
            env.update(properties.get('native_environment', {}))
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
                _check_available_name(root, name)
                write(agent_dir / "properties.json", properties)
        write(agent_dir / "runtime.json", {"launch_id": launch_id, "state": "starting", "host": socket.gethostname(),
                                           "started_at": now(), "native_mode": command.native_mode})
        backend = Tmux(socket_path or os.environ.get("MV2_TMUX_SOCKET"))
        terminal = None
        service_directory_created = False
        try:
            shared_endpoint = None
            if command.shared_service:
                from .native import start_shared_service
                try:
                    shared_endpoint = start_shared_service(command.shared_service, workdir,
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
            terminal = backend.create(f'mv2-{properties["id"][:8]}-{launch_id[:8]}', workdir,
                                      [sys.executable, str(runner), str(launch)])
            backend = Tmux(terminal["socket"])
            backend.tag(terminal["pane"], properties, launch_id)
            family = next((name for name, cls in harnesses.BUILTINS.items() if isinstance(adapter, cls)), '')
            backend.configure_status(terminal, root, properties, launch_id, family)
            if isinstance(adapter, harnesses.CodexAdapter):
                # Codex detects mouse support during TUI startup. Set only its
                # wrapper session, before releasing the runner's ready gate.
                backend.run('set-option', '-t', terminal['session'], 'mouse', 'on')
            update_runtime(agent_dir, launch_id, tmux=terminal)
            # Use the server that actually owns this pane, including when tmux
            # resolved a default socket. The runner scrubs inherited MV2 identity.
            env["MV2_TMUX_SOCKET"] = terminal["socket"]
            env['PATH'] = str(Path(cli_path).parent) + os.pathsep + os.environ.get('PATH', '')
            executor = None
            if command.shared_service:
                from .native import NativeExecutor
                executor = NativeExecutor(workdir, properties, runtime(agent_dir))
            context = LaunchContext(mode, values, model, env, shared_endpoint, executor, resume_prompt=resume_prompt)
            if command.shared_service:
                try:
                    command = adapter.resume(session, context) if resume else adapter.spawn(context)
                except BridgeUnavailable as error:
                    command = plain_fallback(command, error)
                if not command.shared_service:
                    shared_endpoint = None
                    if service_directory_created:
                        shutil.rmtree(service_dir)
                        service_directory_created = False
            argv = list(command.argv)
            endpoint = asdict(shared_endpoint or command.control_endpoint) if shared_endpoint or command.control_endpoint else None
            update_runtime(agent_dir, launch_id, control_endpoint=endpoint,
                           native_mode=command.native_mode,
                           startup_delivery='waiting' if endpoint else None)
            # Adapter variables never override this launch's identity.
            env = {**command.environment, **env}
            write(launch / "request.json", {"argv": argv, "env": env, "unset_env": list(command.unset_env),
                                           "control_endpoint": endpoint,
                                           "shared_service": bool(command.shared_service),
                                           "native_mode": command.native_mode,
                                           "fallback": asdict(command.fallback) if command.fallback else None,
                                           "resume_message_id": resume_message_id,
                                           "rpc_proxy": asdict(command.rpc_proxy) if command.rpc_proxy else None})
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
                from .native import finish_wake
                finish_wake(agent_dir, launch_id, resume_message_id, False,
                            f'Resume failed before native process startup: {error}')
            if service_directory_created:
                shutil.rmtree(service_dir)
            if terminal:
                try:
                    backend.run("kill-session", "-t", terminal["session"])
                except Exception:
                    pass
            update_runtime(agent_dir, launch_id, state="failed", error=str(error))
            recovery = (f"Resume this agent by its full UUID: {properties['id']}"
                        if resume
                        else "The native session has not started; retry spawn with a unique name")
            raise RuntimeError(f"Agent {properties['name']!r} ({properties['id']}) launch failed: {error}. "
                               f"Failed record preserved. {recovery}.") from error
    if wait:
        return wait_ready(root, agent_dir, launch_id, adapter, wait_timeout)
    return launch_result(root, agent_dir)


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
        native = bool(facts.get('control_endpoint'))
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
        ready = running and (not native or (session.native_id and (request.get('rpc_proxy') or
            adapter.capabilities(session)['send_steer'].state == 'supported')))
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


def report(root, reference, phase=None, summary=None):
    agent_dir = directory(root, reference)
    with lock(agent_dir / ".properties.lock"):
        data = read(agent_dir / "properties.json")
        if phase is not None:
            data["phase"] = phase
        if summary is not None:
            data["summary"] = summary
        data["updated_at"] = now()
        write(agent_dir / "properties.json", data)
    return data


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
        # Older launches may have inherited remain-on-exit=off.
        if backend is not None:
            backend.run('set-option', '-p', '-t', pane, 'remain-on-exit', 'on')
        update_runtime(agent_dir, launch_id, state='ending', status='unknown', ending_at=now())
        try:
            from .native import stop
            cancellation = stop(root, reference)
        except Exception as error:
            if live not in ('stopped', 'dormant'):
                update_runtime(agent_dir, launch_id, state=facts.get('state', 'running'),
                               ending_at=facts.get('ending_at'),
                               error=f'Native cancellation failed; remaining terminal retained: {error}')
                raise
            # A dead child has no terminal connection to preserve. Removing its
            # owned wrapper must not depend on an unreachable shared service.
            from .native import end_startup_messages
            cancelled = end_startup_messages(agent_dir, launch_id, 'Cancelled by explicit agent stop', cancelled=True)
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
