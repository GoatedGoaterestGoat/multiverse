"""One terminal child per launch; no central supervisor or model loop."""
import os
from pathlib import Path
import shutil
import signal
import stat
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
    process = service = service_log = None
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
        env["PATH"] = str(Path(env["MV2_CLI"]).parent) + os.pathsep + env.get("PATH", "")
        if request.get('control_endpoint'):
            endpoint = Path(request['control_endpoint']['address'])
            if endpoint != Path('/tmp') / ('mv2-native-' + launch.name) / 'rpc.sock':
                raise ValueError('Native control socket does not belong to this launch')
            service_dir = endpoint.parent
        if request.get('service'):
            specification = request['service']
            ready = Path(specification['ready_socket'])
            if ready != Path('/tmp') / ('mv2-native-' + launch.name) / 'rpc.sock':
                raise ValueError('Native service socket does not belong to this launch')
            service_dir = ready.parent
            service_log = (launch / 'native-service.log').open('ab')
            service = subprocess.Popen(specification['argv'], env=env, stdin=subprocess.DEVNULL,
                                       stdout=service_log, stderr=service_log, start_new_session=True)
            update_runtime(agent_dir, launch.name, service_pid=service.pid)
            deadline = time.monotonic() + 15
            while not ready.exists():
                if service.poll() is not None:
                    raise RuntimeError(f'Native service exited {service.returncode}; see {launch / "native-service.log"}')
                if time.monotonic() >= deadline:
                    raise RuntimeError(f'Native service did not create its socket; see {launch / "native-service.log"}')
                time.sleep(.05)
            if not stat.S_ISSOCK(ready.stat().st_mode):
                raise RuntimeError('Native service readiness path is not a socket')
        update_runtime(agent_dir, launch.name, state="running", activity="unknown")
        process = subprocess.Popen(request["argv"], env=env)
        update_runtime(agent_dir, launch.name, pid=process.pid)
        while service is not None and process.poll() is None:
            if service.poll() is not None:
                raise RuntimeError(f'Native service exited {service.returncode}; see {launch / "native-service.log"}')
            time.sleep(.1)
        code = process.wait()
        update_runtime(agent_dir, launch.name, state="stopped" if code == 0 else "failed", exit_code=code,
                       activity="unknown", stopped_at=now())
        return code
    except Exception as error:
        print(f"Multiverse launch failed: {error}", file=sys.stderr)
        update_runtime(agent_dir, launch.name, state="failed", error=str(error))
        return 1
    finally:
        for child in (process, service):
            if child is not None and child.poll() is None:
                if child is service:
                    os.killpg(child.pid, signal.SIGTERM)
                else:
                    child.terminate()
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    if child is service:
                        os.killpg(child.pid, signal.SIGKILL)
                    else:
                        child.kill()
                    child.wait()
        if service_log:
            service_log.close()
        if service_dir:
            shutil.rmtree(service_dir, ignore_errors=True)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    sys.exit(main())
