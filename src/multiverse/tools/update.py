"""Fast-forward the running mv2 checkout and sync its Python environment.

Plugins that are Git submodules at plugins/<name> move with the checkout when
they are initialized: each must be exactly at the commit mv2 pins and have no
tracked changes, before the fetch, again before the merge and again before it
moves. One that is not initialized is never fetched or initialized; a new one
is only reported (mv2 plugins install NAME). A plugin that cannot move leaves
the rest of the update in place, and says how to repair it.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess


NOTES = [
    "Running agents and the dashboard keep their old code until relaunched.",
    "Run mv2 refresh --apply to bring a universe's templates up to date.",
]


def configure(parser):
    parser.add_argument("--check", action="store_true", help="Fetch and report changes without updating code or environment")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON")


def _git(checkout, *args, timeout=120):
    env = os.environ.copy()
    # An inherited Git environment must not redirect the operation to another repository.
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE"):
        env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(["git", "-c", "merge.autostash=false", "-c", "submodule.recurse=false",
                           "-C", str(checkout), *args], env=env,
                          capture_output=True, text=True, timeout=timeout)


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


def _clean(checkout, *, allow_dirty=False, plugins=()):
    status = _git(checkout, "status", "--porcelain", "-z", "--untracked-files=no", "--ignore-submodules=untracked")
    if status.returncode:
        raise ValueError(_detail(status))
    changed = []
    records = iter(status.stdout.split("\0"))
    for record in records:
        if not record:
            continue
        if record[0] in "RC":
            next(records, None)  # a rename's or copy's source path follows
        # A plugin's own worktree (its HEAD or its files) is checked by _plugin_state, by name; anything in
        # the index (a staged pin, a conflict, a deletion, a type change) is the checkout's own change.
        if record[3:] in plugins and record[0] == " " and record[1] == "M":
            continue
        changed.append(record)
    if changed:
        reason = "The mv2 checkout has uncommitted changes to tracked files; commit them before updating."
        if allow_dirty:
            return reason
        raise ValueError(reason)


PLUGIN_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
PLUGIN_TIMEOUT = 600


def _must(directory, *args):
    """A Git read that must succeed: an error is never taken for a clean or empty answer."""
    process = _git(directory, *args)
    if process.returncode:
        raise ValueError(f"could not read {' '.join(args[:2])} in {directory}: {_detail(process)}")
    return process.stdout


def _plugin_links(checkout, revision):
    """{"plugins/NAME": pinned commit} of the direct plugin gitlinks in a revision's tree; never .gitmodules alone."""
    found = {}
    for record in _must(checkout, "ls-tree", "-z", revision, "--", "plugins/").split("\0"):
        meta, tab, path = record.partition("\t")
        fields = meta.split()
        name = path[len("plugins/"):] if path.startswith("plugins/") else ""
        if (tab and len(fields) == 3 and fields[0] == "160000" and fields[1] == "commit"
                and len(name) <= 40 and PLUGIN_NAME.fullmatch(name)):
            found[path] = fields[2]
    return found


def _plugin_urls(checkout, revision):
    """{"plugins/NAME": url} from a revision's .gitmodules (NUL-separated, never the working copy)."""
    if _git(checkout, "cat-file", "-e", f"{revision}:.gitmodules").returncode:
        return {}
    process = _git(checkout, "config", "--blob", f"{revision}:.gitmodules", "-z", "--get-regexp",
                   r"^submodule\..*\.(path|url)$")
    if process.returncode not in (0, 1):  # 1: no submodule entries at all
        raise ValueError(f"could not read {revision}:.gitmodules: {_detail(process)}")
    paths, urls = {}, {}
    for record in process.stdout.split("\0"):
        key, _, value = record.partition("\n")
        name, _, field = key.rpartition(".")
        (paths if field == "path" else urls)[name] = value
    return {path: urls.get(name) for name, path in paths.items()}


def _plugin_repository(checkout, path):
    """(directory, its resolved git directory) of the initialized submodule at exactly checkout/path, or None
    when it is not initialized. Raises ValueError when it cannot be trusted: a symbolic link anywhere on the
    way, a directory outside the checkout, or a repository Git cannot read."""
    directory = checkout
    for part in Path(path).parts:
        directory = directory / part
        if directory.is_symlink():
            raise ValueError(f"{path} is reached through a symbolic link; mv2 will not update it")
    if not directory.is_dir() or not (directory / ".git").exists():
        return None
    if not directory.resolve().is_relative_to(checkout.resolve()):
        raise ValueError(f"{path} is outside the mv2 checkout")
    top = Path(_must(directory, "rev-parse", "--show-toplevel").strip()).resolve()
    if top != directory.resolve():
        raise ValueError(f"{path} is not a repository of its own")
    return directory, str(Path(_must(directory, "rev-parse", "--absolute-git-dir").strip()).resolve())


def _plugin_facts(checkout, path):
    """(HEAD, tracked changes, conflict, git directory) of an initialized plugin, or None; raises when unreadable."""
    found = _plugin_repository(checkout, path)
    if found is None:
        return None
    directory, identity = found
    head = _must(directory, "rev-parse", "HEAD").strip()
    changes = _must(directory, "status", "--porcelain", "-z", "--untracked-files=no", "--ignore-submodules=untracked")
    conflict = _must(checkout, "ls-files", "-u", "-z", "--", path)
    return head, bool(changes), bool(conflict), identity


def _plugin_state(checkout, links):
    """({path: facts} of initialized plugins, [refusals]). Anything unreadable is a refusal, never clean."""
    state, refusals = {}, []
    for path, pin in sorted(links.items()):
        try:
            facts = _plugin_facts(checkout, path)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            refusals.append(f"{path} could not be checked: {error}")
            state[path] = ("unknown",)
            continue
        if facts is None:
            continue
        state[path] = facts
        head, changes, conflict, _ = facts
        if conflict:
            refusals.append(f"{path} has a merge conflict in the mv2 checkout; resolve it before updating.")
        elif changes:
            refusals.append(f"{path} has local changes; commit or discard them before updating.")
        elif head != pin:
            refusals.append(f"{path} is at {head[:7]}, not the {pin[:7]} mv2 pins; check it out at the pin "
                            f"(git -C {checkout} submodule update --checkout -- {path}) before updating.")
    return state, refusals


def _plugin_plan(checkout, before, after, state, urls_before, urls_after):
    plan = []
    for path in sorted(set(before) | set(after)):
        name = path.split("/", 1)[1]
        entry = {"name": name, "path": path, "before": before.get(path), "after": after.get(path)}
        if path not in before:
            entry["state"] = "available"
        elif path not in after:
            entry["state"] = "removed"
        elif path not in state:
            entry["state"] = "not_installed"
        elif before[path] == after[path]:
            entry["state"] = "unchanged"
        else:
            entry["state"] = "would_update"
        if path in urls_before and path in urls_after and urls_before[path] != urls_after[path]:
            entry["url_changed"] = {"before": urls_before[path], "after": urls_after[path]}
        plan.append(entry)
    return plan


def _index_pin(checkout, path):
    """The commit the checkout's index pins for path (what git submodule update checks out), or None."""
    for record in _must(checkout, "ls-files", "-s", "-z", "--", path).split("\0"):
        meta, tab, found = record.partition("\t")
        fields = meta.split()
        if tab and found == path and len(fields) == 3 and fields[0] == "160000" and fields[2] == "0":
            return fields[1]
    return None


def _move_plugins(checkout, plan, state):
    """Move each initialized plugin whose pin changed, one at a time; returns the failures.

    Everything for one plugin, its checks included, is inside its own failure
    boundary: one that cannot be read or moved is reported, and the others and
    the environment sync go on."""
    failures = []
    for entry in plan:
        if entry["state"] != "would_update":
            continue
        path, old, new = entry["path"], entry["before"], entry["after"]
        try:
            facts = _plugin_facts(checkout, path)
            if facts is None or facts != (old, False, False, state[path][3]) or state[path][:3] != (old, False, False):
                raise ValueError("it changed during the update and was left as it is")
            if _index_pin(checkout, path) != new:
                raise ValueError("the commit the mv2 checkout pins for it changed during the update; it was left as it is")
            # --checkout overrides any submodule.<name>.update (merge, rebase, a command);
            # nested submodules are neither fetched nor updated.
            moved = _git(checkout, "-c", "fetch.recurseSubmodules=false", "submodule", "update", "--checkout",
                         "--no-recommend-shallow", "--", path, timeout=PLUGIN_TIMEOUT)
            if moved.returncode:
                raise ValueError(_detail(moved))
            now = _plugin_facts(checkout, path)
            if now is None or now[0] != new:
                raise ValueError(f"it is not at {new[:7]} after the update")
            entry["state"] = "updated"
        except subprocess.TimeoutExpired as error:
            entry["state"], entry["reason"] = "failed", f"git did not finish within {error.timeout:g} s"
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            entry["state"], entry["reason"] = "failed", str(error)
        if entry["state"] == "failed":
            failures.append(f"mv2 updated, but {path} could not be updated: {entry['reason']}. Repair it with: "
                            f"git -C {checkout} submodule update --checkout -- {path}")
    return failures


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
        links = _plugin_links(checkout, "HEAD")
        state, plugin_refusals = _plugin_state(checkout, links)
        if plugin_refusals and not check:
            raise ValueError(" ".join(plugin_refusals))
        refusals.extend(plugin_refusals)
        dirty = _clean(checkout, allow_dirty=check, plugins=links)
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
        dirty = _clean(checkout, allow_dirty=check, plugins=links)
        if dirty and dirty not in refusals:
            refusals.append(dirty)
        now_links, (now_state, _) = _plugin_links(checkout, "HEAD"), _plugin_state(checkout, links)
        moved = sorted(path for path in set(links) | set(now_links) | set(state) | set(now_state)
                       if links.get(path) != now_links.get(path) or state.get(path) != now_state.get(path))
        if moved:
            changed = (f"{', '.join(moved)} changed while mv2 fetched its upstream; try again after other "
                       "Git operations finish.")
            if not check:
                raise ValueError(changed)
            refusals.append(changed)
        upstream_links = _plugin_links(checkout, "@{u}")
        plan = _plugin_plan(checkout, links, upstream_links, state, _plugin_urls(checkout, "HEAD"),
                            _plugin_urls(checkout, "@{u}"))
        if plan:
            result["plugins"] = plan
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
        plugin_failures = _move_plugins(checkout, plan, state) if result["after"] != before else []
        moved = [entry["path"] for entry in plan if entry["state"] == "updated"]
        if moved:
            result["notes"] = [*result["notes"], f"Running agents reload the Claude code of {', '.join(moved)} at once; "
                               "other plugin code is picked up when an agent or the dashboard is relaunched."]
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
            try:
                synced = subprocess.run([uv, "sync", "--locked"], cwd=checkout,
                                        env=env, capture_output=True, text=True, timeout=600)
                problem = _detail(synced) if synced.returncode else None
            except subprocess.TimeoutExpired as error:
                problem = f"uv did not finish within {error.timeout:g} s"
            except (OSError, subprocess.SubprocessError) as error:
                problem = f"uv could not run: {error}"
            if problem:
                plugin_failures.append(f"The code is current, but the environment sync failed: {problem}. "
                                       f"Run uv sync --locked in {checkout} to retry.")
            else:
                result["environment"] = {"state": "synced"}
        if plugin_failures:
            raise ValueError(" ".join(plugin_failures))
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
    if not result["ok"] and not result["check"] and result["environment"]["state"] != "not_run":
        environment = result["environment"]
        lines.append({"synced": "Environment synced.", "failed": "Environment sync failed (see above).",
                      "skipped": f"Environment sync skipped: {environment.get('reason')}."}.get(
                          environment["state"], f"Environment: {environment['state']}."))
    for entry in result.get("plugins", []):
        lines.extend(_plugin_lines(result, entry))
    if result["check"]:
        lines.append("Check only: code and environment unchanged.")
    lines.extend(result["notes"])
    return "\n".join(lines)


def _plugin_lines(result, entry):
    path, before, after = entry["path"], (entry["before"] or "")[:7], (entry["after"] or "")[:7]
    found = {"would_update": [f"{path} would move from {before} to {after}; running agents would reload its "
                              "Claude code at once."],
             "updated": [f"Updated {path} from {before} to {after}."],
             "available": [f"New optional plugin {entry['name']}: install it with mv2 plugins install {entry['name']}."],
             "removed": [f"{path} would no longer be part of mv2; it would be left on disk." if result["check"]
                         else f"{path} is no longer part of mv2; it was left on disk."],
             "failed": [f"! {path} was not updated: {entry.get('reason')}"]}.get(entry["state"], [])
    if entry.get("url_changed"):
        found.append(f"{path} now comes from {entry['url_changed']['after']}; to fetch from there, run "
                     f"git -C {result['checkout']} submodule sync -- {path}")
    return found


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
