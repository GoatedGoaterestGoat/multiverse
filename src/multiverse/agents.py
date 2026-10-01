from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sys
import time

from . import universe, harnesses
from .harnesses.types import LaunchContext, SessionHandle, normalize_session_id
from .store import lock, now, read, uid, write
from .tmux import Tmux, observe, owned


def directory(root, reference):
    matches = []
    for path in (root / "state" / "agents").glob("*/properties.json"):
        data = read(path)
        if reference in (data["id"], data["name"]) or data["id"].startswith(reference):
            matches.append(path.parent)
    if len(matches) != 1:
        detail = (". Use a full UUID from these matches: " + ", ".join(sorted(path.name for path in matches))
                  if matches else ". Use `mv2 list --json` to find the agent UUID")
        raise ValueError(f"Expected one agent matching {reference!r}; found {len(matches)}{detail}")
    return matches[0]


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
                rows.append({**properties, "usage": measured, "tokens": usage.display(measured),
                             "usage_detail": usage.details(measured), "universe": str(scope), **observation,
                             "status": facts.get("status", "unknown") if observation["live"] == "alive" else "-",
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


def start(root, name=None, task=None, harness="codex", model=None, resume=None, socket_path=None):
    if resume:
        agent_dir = directory(root, resume)
        properties = read(agent_dir / "properties.json")
        harness, model = properties["harness"], model or properties.get("model")
    else:
        universe.slug(name)
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
        context = LaunchContext(mode, values, model, {**os.environ, **(properties.get("native_environment", {}) if resume else {})})
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
        (launch / "task.md").write_text(properties["task"])
        cli = Path(__file__).resolve().parents[2] / "bin" / "mv2"
        invoked = Path(sys.argv[0]).resolve()
        cli_path = str(cli) if cli.exists() else str(invoked) if invoked.name == "mv2" and invoked.is_file() else shutil.which("mv2")
        if not cli_path:
            raise ValueError("Cannot locate mv2 CLI")
        env = {"MV2_UNIVERSE": str(root), "MV2_AGENT_ID": properties["id"], "MV2_AGENT_DIR": str(agent_dir),
               "MV2_LAUNCH_ID": launch_id, "MV2_CONTROL_SOCKET": values["control_socket"], "MV2_CLI": cli_path, "MV2_PYTHON": sys.executable}
        # Persist adapter-selected storage so resume cannot accidentally switch
        # accounts/history when launched by a different monitor environment.
        if command.shared_service:
            env.update(command.shared_service.environment)
            properties['native_environment'] = dict(command.shared_service.environment)
        properties.update(model=model, updated_at=now())
        write(agent_dir / "properties.json", properties)
        write(agent_dir / "runtime.json", {"launch_id": launch_id, "state": "starting", "host": socket.gethostname(), "started_at": now()})
        backend = Tmux(socket_path or os.environ.get("MV2_TMUX_SOCKET"))
        terminal = None
        service_directory_created = False
        try:
            shared_endpoint = None
            if command.shared_service:
                from .native import start_shared_service
                shared_endpoint = start_shared_service(command.shared_service, workdir,
                                                       launch / 'native-service.log', command.unset_env)
            if command.control_endpoint:
                service_dir.mkdir(mode=0o700)
                service_directory_created = True
            runner = Path(__file__).with_name("runner.py")
            terminal = backend.create(f'mv2-{properties["id"][:8]}-{launch_id[:8]}', workdir,
                                      [sys.executable, str(runner), str(launch)])
            backend = Tmux(terminal["socket"])
            backend.tag(terminal["pane"], properties, launch_id)
            update_runtime(agent_dir, launch_id, tmux=terminal)
            # Use the server that actually owns this pane, including when tmux
            # resolved a default socket. The runner scrubs inherited MV2 identity.
            env["MV2_TMUX_SOCKET"] = terminal["socket"]
            env['PATH'] = str(Path(cli_path).parent) + os.pathsep + os.environ.get('PATH', '')
            executor = None
            if command.shared_service:
                from .native import NativeExecutor
                executor = NativeExecutor(workdir, properties, runtime(agent_dir))
            context = LaunchContext(mode, values, model, env, shared_endpoint, executor)
            if command.shared_service:
                command = adapter.resume(session, context) if resume else adapter.spawn(context)
            argv = list(command.argv)
            endpoint = asdict(shared_endpoint or command.control_endpoint) if shared_endpoint or command.control_endpoint else None
            update_runtime(agent_dir, launch_id, control_endpoint=endpoint)
            # Adapter variables never override this launch's identity.
            env = {**command.environment, **env}
            write(launch / "request.json", {"argv": argv, "env": env, "unset_env": list(command.unset_env),
                                           "control_endpoint": endpoint,
                                           "shared_service": bool(command.shared_service),
                                           "rpc_proxy": asdict(command.rpc_proxy) if command.rpc_proxy else None})
            (launch / "ready").touch()
        except Exception as error:
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
    return {**properties, "universe": str(root), "runtime": runtime(agent_dir),
            "session": asdict(SessionHandle.from_records(properties, runtime(agent_dir), agent_dir))}


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
    backend, pane = owned(read(agent_dir / "properties.json"), facts)
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
            update_runtime(agent_dir, launch_id, state=facts.get('state', 'running'),
                           ending_at=facts.get('ending_at'),
                           error=f'Native cancellation failed; remaining terminal retained: {error}')
            raise

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
            update_runtime(agent_dir, launch_id, state='dormant', status='unknown', dormant_at=now())
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
