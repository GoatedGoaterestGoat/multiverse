from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import socket
import sys

from . import universe, harnesses
from .harnesses.types import LaunchContext, SessionHandle, normalize_session_id
from .store import lock, now, read, uid, write
from .tmux import Tmux, observe, owned, stopped_reason


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
                             "activity": facts.get("activity", "unknown") if observation["live"] == "alive" else "—",
                             "runtime": facts, "start_empty_supported": getattr(harnesses.load(scope, properties["harness"]),
                                                                               "claude_lifecycle", False)})
            except (OSError, ValueError, KeyError) as error:
                rows.append({"id": path.parent.name, "name": path.parent.name, "universe": str(scope),
                             "live": "unknown", "activity": "unknown", "phase": "unknown", "summary": str(error)})
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


def start(root, name=None, task=None, harness="codex", model=None, resume=None, socket_path=None, start_empty=False):
    if start_empty and not resume:
        raise ValueError("Start empty conversation requires an existing agent")
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
    if start_empty and not getattr(adapter, "claude_lifecycle", False):
        raise ValueError("Start empty conversation is only supported for Claude")
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
    launch_identity = {key: properties.get(key) for key in
                       ('native_session_id', 'native_environment', 'workspace', 'harness')}
    properties["workspace"] = str(workdir)
    agent_dir.mkdir(parents=True, exist_ok=True)
    with lock(agent_dir / ".launch.lock"):
        if resume:
            current = read(agent_dir / 'properties.json')
            if any(current.get(key) != value for key, value in launch_identity.items()):
                raise ValueError('Agent conversation changed while preparing launch; retry using its current identity')
        previous = runtime(agent_dir)
        if previous and observe(properties, previous)["live"] != "stopped":
            raise ValueError("Agent is running or its liveness is unknown; refusing a duplicate launch")
        session = SessionHandle.from_records(properties, previous or {}, agent_dir) if resume else None
        if start_empty and previous.get('claude_session', {}).get('closed') is False:
            # A killed runner may never close its evidence. Explicit recovery is
            # safe only once both recorded processes are known to be gone.
            for key in ('runner_pid', 'pid'):
                pid = previous.get(key)
                if not isinstance(pid, int) or pid <= 0:
                    raise ValueError('Previous Claude launch has not finished stopping; process identity is unknown')
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                except PermissionError:
                    pass
                raise ValueError('Previous Claude launch has not finished stopping; retry after it exits')
        if start_empty:
            old_id = properties.get('native_session_id')
            properties = {**properties, 'native_session_id': uid(),
                          'native_session_history': [*properties.get('native_session_history', []),
                                                     {'session_id': old_id, 'launch_id': previous.get('launch_id'),
                                                      'environment': properties.get('native_environment', {}),
                                                      'workspace': properties['workspace'], 'at': now()}]}
        launch_id = uid()
        launch = agent_dir / "launches" / launch_id
        bootstrap = universe.bootstrap(root, properties)
        if adapter.bootstrap:
            bootstrap += "\n" + adapter.bootstrap.strip() + "\n"
        values = {"name": properties["name"], "agent_id": properties["id"],
                  "session_id": properties.get("native_session_id", ""),
                  "bootstrap": bootstrap, "bootstrap_json": json.dumps(bootstrap, ensure_ascii=False),
                  "task": "" if start_empty else properties["task"], "agent_dir": str(agent_dir), "harness_dir": str(harness_dir),
                  "session_file": str(agent_dir / "session.jsonl"), "bootstrap_file": str(launch / "bootstrap.md"),
                  "task_file": str(launch / "task.md"), "workspace": str(workdir), "model": model or ""}
        values["adapter_dir"] = str(harnesses.PACKAGES / harness)
        # A short private path avoids Unix socket length limits in deep worktrees.
        # Created only when the adapter requests a native service below.
        service_dir = Path('/tmp') / ('mv2-native-' + launch_id)
        values['control_socket'] = str(service_dir / 'rpc.sock')
        context = LaunchContext(mode, values, model, {**os.environ, **(properties.get("native_environment", {}) if resume else {})})
        command = adapter.resume(session, context) if resume and not start_empty else adapter.spawn(context)
        argv = list(command.argv)
        if not shutil.which(argv[0]):
            raise ValueError(f'Harness executable not found: {argv[0]}')
        launch.mkdir(parents=True)
        (launch / "bootstrap.md").write_text(bootstrap)
        (launch / "task.md").write_text(values["task"])
        cli = Path(__file__).resolve().parents[2] / "bin" / "mv2"
        invoked = Path(sys.argv[0]).resolve()
        cli_path = str(cli) if cli.exists() else str(invoked) if invoked.name == "mv2" and invoked.is_file() else shutil.which("mv2")
        if not cli_path:
            raise ValueError("Cannot locate mv2 CLI")
        env = {"MV2_UNIVERSE": str(root), "MV2_AGENT_ID": properties["id"], "MV2_AGENT_DIR": str(agent_dir),
               "MV2_LAUNCH_ID": launch_id, "MV2_CONTROL_SOCKET": values["control_socket"], "MV2_CLI": cli_path, "MV2_PYTHON": sys.executable}
        if command.environment:
            env.update(command.environment)
            properties['native_environment'] = dict(command.environment)
        claude_session = dict(command.claude_session or {})
        if claude_session:
            claude_session['launch_id'] = launch_id
        # Persist adapter-selected storage so resume cannot accidentally switch
        # accounts/history when launched by a different monitor environment.
        if command.shared_service:
            env.update(command.shared_service.environment)
            properties['native_environment'] = dict(command.shared_service.environment)
        properties.update(model=model, updated_at=now())
        with lock(agent_dir / '.properties.lock'):
            if resume:
                current = read(agent_dir / 'properties.json')
                current.update({key: properties[key] for key in ('native_session_id', 'native_environment',
                               'native_session_history', 'workspace', 'model', 'updated_at') if key in properties})
                properties = current
            write(agent_dir / "properties.json", properties)
        write(agent_dir / "runtime.json", {"launch_id": launch_id, "state": "starting", "host": socket.gethostname(), "started_at": now(),
                                            **({"claude_session": claude_session} if claude_session else {})})
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
                command = adapter.resume(session, context) if resume and not start_empty else adapter.spawn(context)
            argv = list(command.argv)
            endpoint = asdict(shared_endpoint or command.control_endpoint) if shared_endpoint or command.control_endpoint else None
            update_runtime(agent_dir, launch_id, control_endpoint=endpoint)
            write(launch / "request.json", {"argv": argv, "env": env, "unset_env": list(command.unset_env),
                                           "control_endpoint": endpoint,
                                           "shared_service": bool(command.shared_service),
                                           "claude_session": claude_session})
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


def status(root, reference, phase=None, summary=None):
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
    agent_dir = directory(root, reference)
    facts = runtime(agent_dir)
    if action == 'inspect' and observe(read(agent_dir / 'properties.json'), facts)['live'] == 'stopped':
        return stopped_reason(facts, 'No live pane; no retained launch diagnostics.')
    backend, pane = owned(read(agent_dir / "properties.json"), facts)
    if action == "inspect":
        return backend.run("capture-pane", "-p", "-S", "-100", "-t", pane)
    if action == "stop":
        with lock(agent_dir / '.launch.lock'):
            # Recheck ownership under the same lock used by send/resume.
            facts = runtime(agent_dir)
            backend, pane = owned(read(agent_dir / 'properties.json'), facts)
            from .native import stop
            stop(root, reference)
            backend.run("kill-pane", "-t", pane)
            update_runtime(agent_dir, facts["launch_id"], state="stopped", activity="unknown", stopped_at=now())
            return {"id": agent_dir.name, "state": "stopped"}
    return backend, pane
