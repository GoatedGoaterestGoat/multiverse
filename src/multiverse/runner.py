"""One terminal child per launch; no central supervisor or model loop."""
import asyncio
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import threading

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from multiverse.agents import update_runtime, bind_session, runtime
from multiverse.store import now, read


class UsagePublisher:
    """Publish adapter observations only into their exact running launch."""
    def __init__(self, agent_dir, launch_id):
        self.agent_dir, self.launch_id = agent_dir, launch_id

    def __call__(self, usage):
        try:
            facts = read(self.agent_dir / 'runtime.json')
            properties = read(self.agent_dir / 'properties.json')
            if (facts.get('launch_id') != self.launch_id or facts.get('state') != 'running'
                    or not usage.get('native_session_id')
                    or properties.get('native_session_id') != usage['native_session_id']):
                return
            previous = facts.get('usage')
            previous = previous if isinstance(previous, dict) else {}
            if usage != {key: value for key, value in previous.items() if key != 'reported_at'}:
                update_runtime(self.agent_dir, self.launch_id, usage={**usage, 'reported_at': now()})
        except (OSError, ValueError, TypeError):
            pass  # A stale launch or unavailable runtime cannot affect native work.


class StatusWatcher:
    """Core-owned observer, bounded by one terminal launch and exact binding."""
    def __init__(self, agent_dir, launch_id, root):
        self.agent_dir, self.launch_id, self.root = agent_dir, launch_id, root
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name='native-status', daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stopped.set()
        self.thread.join()

    def _run(self):
        try:
            asyncio.run(self._observe())
        except Exception:
            # Optional observations must not take down the interactive terminal.
            self._publish('unknown')

    def _publish(self, status):
        if status not in ('working', 'waiting', 'idle', 'unknown'):
            status = 'unknown'
        try:
            facts = read(self.agent_dir / 'runtime.json')
            if (facts.get('launch_id') == self.launch_id and facts.get('state') == 'running'
                    and facts.get('status') != status):
                update_runtime(self.agent_dir, self.launch_id, status=status)
        except (OSError, ValueError):
            pass  # A new launch or a removed agent can never inherit this stream.

    async def _observe(self):
        from multiverse import harnesses, rpc
        from multiverse.harnesses.types import SessionHandle
        properties = read(self.agent_dir / 'properties.json')
        adapter = harnesses.load(self.root, properties['harness'])
        describe = getattr(adapter, 'status_watch', None)
        if describe is None:
            return
        task = None
        native_id = None
        try:
            while not self.stopped.is_set():
                facts = read(self.agent_dir / 'runtime.json')
                properties = read(self.agent_dir / 'properties.json')
                if facts.get('launch_id') != self.launch_id or facts.get('state') not in ('starting', 'running'):
                    return
                if facts.get('state') == 'starting':
                    await asyncio.sleep(.1)
                    continue
                if task is not None:
                    if properties.get('native_session_id') != native_id or task.done():
                        return
                elif properties.get('native_session_id'):
                    session = SessionHandle.from_records(properties, facts, self.agent_dir)
                    specification = describe(session)
                    if specification is None:
                        return
                    native_id = session.native_id
                    task = asyncio.create_task(rpc.watch(*specification, self._publish))
                await asyncio.sleep(1)
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self._publish('unknown')


class StartupDelivery:
    """Deliver this launch's durable startup queue once its bridge is ready."""
    def __init__(self, agent_dir, launch_id, root, prompt_protocol=None):
        self.agent_dir, self.launch_id, self.root = agent_dir, launch_id, root
        self.prompt_protocol = prompt_protocol
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name='startup-delivery', daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stopped.set()
        self.thread.join()

    def _run(self):
        from multiverse import agents, harnesses, native
        error = 'Launch ended before startup delivery; use deliver AGENT --retry'
        try:
            adapter = harnesses.load(self.root, read(self.agent_dir / 'properties.json')['harness'])
            while not self.stopped.is_set():
                result = agents.wait_ready(self.root, self.agent_dir, self.launch_id, adapter, .5, include_delivery=False)
                if result['ready']:
                    if self.prompt_protocol is not None:
                        if self.prompt_protocol.prompt_error:
                            error = 'Interactive starting prompt failed: ' + self.prompt_protocol.prompt_error
                            break
                        if not self.prompt_protocol.prompt_submitted:
                            self.stopped.wait(.05)
                            continue
                    error = None
                    break
                if not result.get('timed_out'):
                    error = result['error'] + '; startup messages remain saved for deliver AGENT --retry'
                    break
        except Exception as failure:
            error = f'Startup delivery failed: {failure}; messages remain saved for deliver AGENT --check'
        try:
            native.flush_startup(self.root, self.agent_dir, self.launch_id, error)
        except Exception as failure:
            try:
                update_runtime(self.agent_dir, self.launch_id, startup_delivery='failed', delivery_error=str(failure))
            except (OSError, ValueError):
                pass


def main():
    launch = Path(sys.argv[1])
    agent_dir = launch.parent.parent
    process = None
    service_dir = None
    watcher = None
    proxy = None
    startup = None
    request = {}

    def stop(_signum, _frame):
        raise SystemExit(128 + _signum)

    previous_handlers = {signum: signal.signal(signum, stop) for signum in (signal.SIGHUP, signal.SIGTERM)}
    try:
        deadline = time.monotonic() + 15
        while not (launch / "ready").exists():
            if time.monotonic() > deadline:
                raise RuntimeError("Spawn did not finish registering this pane")
            time.sleep(0.05)
        request = read(launch / "request.json")
        env = {key: value for key, value in os.environ.items() if not key.startswith("MV2_")}
        for key in request.get("unset_env", []):
            env.pop(key, None)
        env.update(request["env"])
        cli_prefix = str(Path(env["MV2_CLI"]).parent) + os.pathsep
        if not env.get("PATH", "").startswith(cli_prefix):
            env["PATH"] = cli_prefix + env.get("PATH", "")
        if request.get('rpc_proxy') or (request.get('control_endpoint') and not request.get('shared_service')):
            endpoint = Path(request['rpc_proxy']['socket_path'] if request.get('rpc_proxy') else request['control_endpoint']['address'])
            if endpoint != Path('/tmp') / ('mv2-native-' + launch.name) / 'rpc.sock':
                raise ValueError('Native control socket does not belong to this launch')
            service_dir = endpoint.parent
        # A bridge resume already has a saved ID, but it must validate the new
        # native reply (including requested grants) before accepting more input.
        update_runtime(agent_dir, launch.name, state="starting" if request.get('rpc_proxy') else "running", status="unknown",
                       runner_pid=os.getpid())
        if request.get('rpc_proxy'):
            from multiverse import harnesses, rpc
            properties = read(agent_dir / 'properties.json')
            adapter = harnesses.load(Path(env['MV2_UNIVERSE']), properties['harness'])
            def bound(native_id):
                bind_session(Path(env['MV2_UNIVERSE']), agent_dir.name, native_id, launch.name)
                update_runtime(agent_dir, launch.name, state='running')
            proxy = rpc.LaunchProxy(request['rpc_proxy'], adapter.launch_proxy(request['rpc_proxy']['settings']),
                bound,
                lambda error: update_runtime(agent_dir, launch.name, error='Native launch relay: ' + error),
                UsagePublisher(agent_dir, launch.name))
            proxy.start()
        process = subprocess.Popen(request["argv"], env=env)
        update_runtime(agent_dir, launch.name, pid=process.pid)
        from multiverse.native import finish_wake
        finish_wake(agent_dir, launch.name, request.get('resume_message_id'), True)
        if request.get('control_endpoint'):
            startup = StartupDelivery(agent_dir, launch.name, Path(env['MV2_UNIVERSE']),
                                      proxy.protocol if proxy is not None and request.get('resume_message_id') else None)
            startup.start()
        if request.get('shared_service'):
            watcher = StatusWatcher(agent_dir, launch.name, Path(env['MV2_UNIVERSE']))
            watcher.start()
        code = process.wait()
        update_runtime(agent_dir, launch.name,
                       state="stopped" if code == 0 or runtime(agent_dir).get('ending_at') else "failed",
                       exit_code=code, status="unknown", stopped_at=now())
        if watcher is not None:
            watcher.close()
            watcher = None
        return code
    except Exception as error:
        print(f"Multiverse launch failed: {error}", file=sys.stderr)
        update_runtime(agent_dir, launch.name, state="failed", status="unknown", error=str(error))
        return 1
    finally:
        if request.get('resume_message_id') and process is None:
            from multiverse.native import finish_wake
            finish_wake(agent_dir, launch.name, request['resume_message_id'], False,
                        'Runner exited before starting the native process; use deliver AGENT --retry')
        if startup is not None:
            startup.close()
        elif request.get('control_endpoint'):
            from multiverse.native import flush_startup
            flush_startup(Path(request['env']['MV2_UNIVERSE']), agent_dir, launch.name,
                          'Runner ended before bridge readiness; use deliver AGENT --retry')
        if proxy is not None:
            proxy.close()
        if watcher is not None:
            watcher.close()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        # Shared services deliberately outlive terminal clients and monitors.
        if service_dir:
            shutil.rmtree(service_dir, ignore_errors=True)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    sys.exit(main())
