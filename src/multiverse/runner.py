"""One terminal child per launch; no central supervisor or model loop."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from multiverse.agents import update_runtime
from multiverse.store import now, read


def main():
    launch = Path(sys.argv[1])
    agent_dir = launch.parent.parent
    process = None
    service_dir = None

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
        if request.get('control_endpoint') and not request.get('shared_service'):
            endpoint = Path(request['control_endpoint']['address'])
            if endpoint != Path('/tmp') / ('mv2-native-' + launch.name) / 'rpc.sock':
                raise ValueError('Native control socket does not belong to this launch')
            service_dir = endpoint.parent
        update_runtime(agent_dir, launch.name, state="running", activity="unknown")
        if request.get('claude_session'):
            from multiverse.claude_terminal import run
            code = run(request['argv'], env, launch, request['claude_session'])
        else:
            process = subprocess.Popen(request["argv"], env=env)
            update_runtime(agent_dir, launch.name, pid=process.pid)
            code = process.wait()
        update_runtime(agent_dir, launch.name, state="stopped" if code == 0 else "failed", exit_code=code,
                       activity="unknown", stopped_at=now())
        return code
    except Exception as error:
        print(f"Multiverse launch failed: {error}", file=sys.stderr)
        update_runtime(agent_dir, launch.name, state="failed", error=str(error))
        return 1
    finally:
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
