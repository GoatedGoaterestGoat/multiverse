"""Move and remove agents and subverses by renaming their directories.

The dashboard renders whatever is on disk, so these are ordinary file moves
plus the few recorded facts that depend on location. Anything running must be
stopped first: a live agent has its directory baked into its environment,
system prompt, runner and tmux tags. Removal is a move into the containing
universe's trash directory, so it can be undone by moving the entry back.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil

from . import universe
from .agents import directory, runtime, update_runtime
from .store import lock, now, read, write
from .tmux import observe, owned

TRASH = "trash"
RECORD = ".removed.json"


def exact(value, root=None):
    """A universe directory named exactly, never an ancestor found by searching.

    Accepts a universe directory, a workspace containing `.multiverse`, or,
    when `root` is given, a subverse path such as `cad-lab/verification`
    relative to it. Operations below take exact paths; callers resolve names.
    """
    path = Path(value).expanduser()
    for candidate in (path, path / ".multiverse"):
        if (candidate / "universe.json").is_file():
            return candidate.resolve()
    if root is not None and not path.is_absolute():
        nested = Path(root)
        for part in path.parts:
            nested = nested / "subverses" / universe.slug(part)
        if (nested / "universe.json").is_file():
            return nested.resolve()
    raise ValueError(f"Not a universe or subverse: {value}")


def _stopped(agent_dir):
    """Require native exit and remove any retained, owned terminal wrapper."""
    properties, facts = read(agent_dir / "properties.json"), runtime(agent_dir)
    if not facts.get("launch_id"):
        return
    state = observe(properties, facts)
    if state["live"] not in ('stopped', 'dormant'):
        raise ValueError(f'Agent {properties["name"]} is {state["live"]}; stop it first'
                         + (f' ({state["reason"]})' if state["reason"] else ""))
    if state['live'] == 'stopped' and facts.get('tmux'):
        # A moved/trashed record must not leave an unreachable terminal wrapper.
        backend, pane = owned(properties, facts, states=('stopped',))
        backend.run('kill-pane', '-t', pane)
        update_runtime(agent_dir, facts['launch_id'], state='dormant', status='unknown', dormant_at=now())


def _agents(scope):
    return sorted(p.parent for root in universe.tree(scope)
                  for p in (root / "state" / "agents").glob("*/properties.json"))


def _hold(stack, agent_dirs):
    # Resume and stop take the launch lock, so none can start mid-move.
    for agent_dir in agent_dirs:
        stack.enter_context(lock(agent_dir / ".launch.lock"))
        _stopped(agent_dir)


def _pin_workspaces(scope):
    """Record absolute workspaces, valid wherever the directory ends up."""
    for root in universe.tree(scope):
        meta = read(root / "universe.json")
        meta["workspace"] = str(universe.workspace(root))
        write(root / "universe.json", meta)


def _relative_workspaces(scope):
    for root in universe.tree(scope):
        meta = read(root / "universe.json")
        meta["workspace"] = os.path.relpath(universe.workspace(root), root)
        write(root / "universe.json", meta)


def _subverse(path):
    path = exact(path)
    if universe.parent(path) is None:
        raise ValueError("A top-level universe cannot be moved or removed; only its subverses")
    return path


def _entry(container, name):
    base = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S-") + name
    entry, number = container / TRASH / base, 2
    while entry.exists():
        entry, number = container / TRASH / f"{base}-{number}", number + 1
    entry.parent.mkdir(exist_ok=True)
    return entry


def move_agent(root, reference, destination):
    root, destination = exact(root), exact(destination)
    agent_dir = directory(root, reference)
    properties = read(agent_dir / "properties.json")
    if destination == root:
        raise ValueError(f'{properties["name"]} is already in that universe')
    if not (destination / "harnesses" / properties["harness"] / "harness.json").is_file():
        raise ValueError(f'The destination has no {properties["harness"]} harness configured')
    for path in (destination / "state" / "agents").glob("*/properties.json"):
        if read(path)["name"] == properties["name"]:
            raise ValueError(f'The destination already has an agent named {properties["name"]}')
    target = destination / "state" / "agents" / agent_dir.name
    with ExitStack() as stack:
        _hold(stack, [agent_dir])
        target.parent.mkdir(parents=True, exist_ok=True)
        agent_dir.rename(target)
        with lock(target / ".properties.lock"):
            data = read(target / "properties.json")
            data.update(universe_id=read(destination / "universe.json")["id"], updated_at=now())
            write(target / "properties.json", data)
    return {**data, "universe": str(destination)}


def move_subverse(path, destination):
    path = _subverse(path)
    destination = exact(destination)
    if destination == path or destination.is_relative_to(path):
        raise ValueError("A subverse cannot be moved into itself")
    if destination == universe.parent(path):
        raise ValueError(f"{path.name} is already in that universe")
    target = destination / "subverses" / path.name
    if target.exists():
        raise ValueError(f"The destination already has a subverse named {path.name}")
    with ExitStack() as stack:
        _hold(stack, _agents(path))
        _pin_workspaces(path)
        target.parent.mkdir(exist_ok=True)
        path.rename(target)
        _relative_workspaces(target)
    return {"universe": str(target), **read(target / "universe.json")}


def remove_agent(root, reference):
    root = exact(root)
    agent_dir = directory(root, reference)
    properties = read(agent_dir / "properties.json")
    with ExitStack() as stack:
        _hold(stack, [agent_dir])
        entry = _entry(root, f'{properties["name"]}-{agent_dir.name[:8]}')
        agent_dir.rename(entry)
    record = {"kind": "agent", "name": properties["name"], "id": agent_dir.name,
              "origin": str(agent_dir), "removed_at": now()}
    write(entry / RECORD, record)
    return {**record, "trash": str(entry)}


def remove_subverse(path):
    path = _subverse(path)
    meta = read(path / "universe.json")
    with ExitStack() as stack:
        agent_dirs = _agents(path)
        _hold(stack, agent_dirs)
        # Absolute workspaces keep the entry valid if it is moved back.
        _pin_workspaces(path)
        entry = _entry(universe.parent(path), path.name)
        path.rename(entry)
    record = {"kind": "subverse", "name": meta["name"], "id": meta["id"], "origin": str(path),
              "agents": len(agent_dirs), "removed_at": now()}
    write(entry / RECORD, record)
    return {**record, "trash": str(entry)}


def trash(root, empty=False):
    """List, or permanently delete, removed entries across this universe tree."""
    entries = []
    for scope in universe.tree(exact(root)):
        for entry in sorted((scope / TRASH).glob("*/" + RECORD)):
            entries.append({**read(entry), "trash": str(entry.parent)})
    if empty:
        for entry in entries:
            shutil.rmtree(entry["trash"])
    return {"entries": entries, "emptied": empty}
