"""Fast-forward the running mv2 checkout and sync its Python environment."""
import os
from pathlib import Path
import shutil
import subprocess


NOTES = [
    "Running agents and the dashboard keep their old code until relaunched.",
    "Run mv2 refresh --apply to bring a universe's templates up to date.",
]


def configure(parser):
    parser.add_argument("--check", action="store_true", help="Fetch and report changes without updating code or environment")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON")


def _git(checkout, *args):
    env = os.environ.copy()
    # An inherited Git environment must not redirect the operation to another repository.
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE"):
        env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(["git", "-c", "merge.autostash=false", "-c", "submodule.recurse=false",
                           "-C", str(checkout), *args], env=env,
                          capture_output=True, text=True, timeout=120)


def _detail(process):
    return (process.stderr.strip() or process.stdout.strip() or f"exit code {process.returncode}")[-1200:]


def _read(checkout, *args):
    process = _git(checkout, *args)
    if process.returncode:
        raise ValueError(_detail(process))
    return process.stdout.strip()


def source_checkout():
    """Resolve the package's source tree, never the caller's directory or universe."""
    source = Path(__file__).resolve()
    if source.parents[2].name != "src":
        raise ValueError("This mv2 installation is not running from a Git source checkout.")
    checkout = source.parents[3]
    process = _git(checkout, "rev-parse", "--show-toplevel")
    if process.returncode or Path(process.stdout.strip()).resolve() != checkout:
        raise ValueError("This mv2 installation is not running from a Git source checkout.")
    return checkout


def _clean(checkout, *, allow_dirty=False):
    if _read(checkout, "status", "--porcelain", "--untracked-files=no", "--ignore-submodules=untracked"):
        reason = "The mv2 checkout has uncommitted changes to tracked files; commit them before updating."
        if allow_dirty:
            return reason
        raise ValueError(reason)


def run(root=None, *, check=False, json=False):
    # root is deliberately unused: -u selects a universe, not mv2's source repository.
    result = {"ok": False, "check": check, "json": json, "checkout": None, "branch": None,
              "upstream": None, "before": None, "after": None, "commits": 0,
              "environment": {"state": "not_run"}, "notes": []}
    refusals = []
    try:
        if not shutil.which("git"):
            raise ValueError("Git is not available; install it before updating mv2.")
        checkout = source_checkout()
        result["checkout"] = str(checkout)
        branch_result = _git(checkout, "symbolic-ref", "--quiet", "--short", "HEAD")
        if branch_result.returncode:
            raise ValueError("The mv2 checkout has no current branch (detached HEAD); select a branch manually before updating.")
        branch = branch_result.stdout.strip()
        result["branch"] = branch
        dirty = _clean(checkout, allow_dirty=check)
        if dirty:
            refusals.append(dirty)
        upstream = _git(checkout, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        if upstream.returncode:
            raise ValueError(f"The mv2 branch {branch} has no upstream; configure one before updating.")
        result["upstream"] = upstream.stdout.strip()
        before = _read(checkout, "rev-parse", "HEAD")
        result["before"] = result["after"] = before
        remote = _read(checkout, "config", "--get", f"branch.{branch}.remote")
        fetched = _git(checkout, "fetch", "--no-recurse-submodules", "--", remote)
        if fetched.returncode:
            raise ValueError(f"Could not fetch mv2's upstream: {_detail(fetched)}")
        dirty = _clean(checkout, allow_dirty=check)
        if dirty and dirty not in refusals:
            refusals.append(dirty)
        if (_read(checkout, "symbolic-ref", "--quiet", "--short", "HEAD") != branch
                or _read(checkout, "rev-parse", "HEAD") != before):
            raise ValueError("The mv2 branch changed during the check; try again after other Git operations finish.")
        ahead, behind = map(int, _read(checkout, "rev-list", "--left-right", "--count", "HEAD...@{u}").split())
        result["commits"] = behind
        result["available"] = _read(checkout, "rev-parse", "@{u}")
        if ahead and behind:
            raise ValueError(f"The mv2 branch {branch} has diverged from {result['upstream']}; resolve it manually before updating.")
        if ahead:
            raise ValueError(f"The mv2 branch {branch} is ahead of {result['upstream']}; publish or resolve its local commits before updating.")
        uv = shutil.which("uv")
        has_lock = (not _git(checkout, "cat-file", "-e", "@{u}:uv.lock").returncode
                    if check else (checkout / "uv.lock").is_file())
        if not has_lock:
            result["environment"] = {"state": "skipped", "reason": "uv.lock is missing"}
        elif not uv:
            result["environment"] = {"state": "skipped", "reason": "uv is not available"}
        else:
            result["environment"] = {"state": "pending"}
        if check:
            if refusals:
                result["reason"] = " ".join(refusals)
            else:
                result["ok"] = True
            return result
        if behind:
            merged = _git(checkout, "merge", "--ff-only", "@{u}")
            result["after"] = _read(checkout, "rev-parse", "HEAD")
            if result["after"] != before:
                result["notes"] = list(NOTES)
            if merged.returncode:
                raise ValueError(f"Could not fast-forward mv2: {_detail(merged)}")
        # Recheck after merging: the new revision may add or remove uv.lock.
        if not (checkout / "uv.lock").is_file():
            result["environment"] = {"state": "skipped", "reason": "uv.lock is missing"}
        elif not uv:
            result["environment"] = {"state": "skipped", "reason": "uv is not available"}
        else:
            result["environment"] = {"state": "failed"}
            env = os.environ.copy()
            for key in ("UV_PROJECT", "UV_WORKING_DIRECTORY", "UV_ACTIVE"):
                env.pop(key, None)
            env["UV_PROJECT_ENVIRONMENT"] = str(checkout / ".venv")
            synced = subprocess.run([uv, "sync", "--locked"], cwd=checkout,
                                    env=env, capture_output=True, text=True, timeout=600)
            if synced.returncode:
                raise ValueError(f"The code is current, but the environment sync failed: {_detail(synced)}. Run uv sync --locked in the mv2 checkout to retry.")
            result["environment"] = {"state": "synced"}
        result["ok"] = True
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result["reason"] = " ".join(dict.fromkeys([*refusals, str(error)]))
    return result


def format_output(result):
    if result["json"]:
        return {key: value for key, value in result.items() if key != "json"}
    if not result["ok"]:
        if result["check"]:
            reason = result["reason"]
            if reason.startswith("The "):
                reason = "the " + reason[4:]
            lines = [f"An update would be refused: {reason}"]
            if "available" in result:
                count = result["commits"]
                lines.append(f"{count} commit{'s are' if count != 1 else ' is'} waiting on {result['upstream']}.")
        else:
            lines = [f"mv2 was not fully updated: {result['reason']}"]
    elif result["commits"]:
        count = result["commits"]
        if result["check"]:
            lines = [f"mv2 would update from {result['before'][:7]} to {result['available'][:7]} ({count} commit{'s' if count != 1 else ''})."]
        else:
            lines = [f"Updated mv2 from {result['before'][:7]} to {result['after'][:7]} ({count} commit{'s' if count != 1 else ''})."]
    else:
        lines = [f"mv2 is already up to date ({result['after'][:7]})."]
    environment = result["environment"]
    if result["ok"]:
        if environment["state"] == "synced":
            lines[0] = lines[0].rstrip(".") + "; environment synced."
        elif environment["state"] == "skipped":
            lines.append(f"Environment sync skipped: {environment['reason']}.")
        elif result["check"]:
            lines.append("The environment would be synced with uv sync --locked.")
    if result["check"]:
        lines.append("Check only: code and environment unchanged.")
    lines.extend(result["notes"])
    return "\n".join(lines)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
