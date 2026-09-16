"""One terminal child per launch; no central supervisor or model loop."""
import os
from pathlib import Path
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
        update_runtime(agent_dir, launch.name, state="running", activity="unknown")
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


if __name__ == "__main__":
    sys.exit(main())
