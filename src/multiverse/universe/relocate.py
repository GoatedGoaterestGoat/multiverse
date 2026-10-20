"""Move and remove agents and subverses by renaming their directories.

The dashboard renders whatever is on disk, so these are ordinary file moves
plus the few recorded facts that depend on location. Anything running must be
stopped first: a live agent has its directory baked into its environment,
system prompt, runner and tmux tags. Removal is a move into the containing
universe's trash directory, so it can be undone by moving the entry back.
"""
from contextlib import ExitStack
import os

from . import exact, parent, trash, tree, workspace
from .agents import directory, runtime, update_runtime, name_matches, record_paths, orphaned
from . import records, sessions
from ..store import lock, now, read, write
from ..terminal.tmux import observe, owned



def _stopped(agent_dir):
    """Require native exit and remove any retained, owned terminal wrapper."""
    from ..runtime import missing_runtime
    from .agents import interrupted_preparation
    if orphaned(agent_dir):
        missing_runtime.require_stopped(agent_dir, _orphan_identity(agent_dir), operation='move or remove', orphan=True)
        if not orphaned(agent_dir):
            raise records.RecordError(agent_dir / 'properties.json',
                                      'core records changed during orphan discovery; retry with the current record')
        return  # No authority was invented, and no terminal was controlled.
    properties = records.properties(agent_dir / "properties.json")
    previous, malformed = missing_runtime.recovery_snapshot(agent_dir)
    # _hold owns the launch lock. Broken terminal/generation identity cannot
    # turn an unmatched pane into permission to rename its live records.
    if malformed is not None or missing_runtime.damage(agent_dir, previous) or not (agent_dir / 'runtime.json').exists():
        missing_runtime.require_stopped(agent_dir, properties, facts=previous, operation='move or remove')
        missing_runtime._unchanged(agent_dir, previous, malformed)
        return  # Absence was independently proved; never rewrite damaged facts.
    facts = interrupted_preparation(previous, agent_dir)
    if facts is not previous and facts.get('state') != 'failed':
        raise records.RecordError(agent_dir / 'runtime.json',
            'preparation record has native startup evidence; repair the runtime record before acting on this agent')
    if not facts.get("launch_id"):
        return  # A genuinely never-launched record has nothing to stop.
    state = observe(properties, facts)
    if error := missing_runtime.mismatch(agent_dir, facts, state):
        raise error
    if state["live"] not in ('stopped', 'dormant'):
        raise ValueError(f'Agent {properties["name"]} is {state["live"]}; stop it first'
                         + (f' ({state["reason"]})' if state["reason"] else ""))
    from ..runtime import attachments
    if closing := attachments.problem(agent_dir, facts):
        raise ValueError(closing + '; retry after attachment cleanup finishes')
    if state['live'] == 'stopped' and facts.get('tmux'):
        # A moved/trashed record must not leave an unreachable terminal wrapper.
        backend, pane = owned(properties, facts, states=('stopped',))
        backend.run('kill-pane', '-t', pane)
        update_runtime(agent_dir, facts['launch_id'], state='dormant', status='unknown', dormant_at=now())


def _agents(scope):
    return sorted(p.parent for root in tree(scope)
                  for p in record_paths(root))


def _orphan_identity(agent_dir):
    return {'id': agent_dir.name, 'name': agent_dir.name,
            'universe_id': read(agent_dir.parents[2] / 'universe.json')['id']}


def _hold(stack, agent_dirs):
    # Resume and stop take the launch lock, so none can start mid-move.
    for agent_dir in agent_dirs:
        stack.enter_context(lock(agent_dir / ".launch.lock", create_parent=False))
        _stopped(agent_dir)


def _session_namespaces(stack, *roots):
    for root in sorted({sessions.top(root) for root in roots}):
        stack.enter_context(sessions.namespace(root))


def _session_claims(root, directory):
    data, rows = sessions.effective(directory, read(directory / 'properties.json'))
    data['native_session_provider'] = sessions.provider(directory.parents[2], data)
    claims = {(data.get('native_session_id'), data.get('native_session_home')),
              *((row[key], row['home']) for row in rows for key in ('from', 'to'))}
    for native_id, home in claims:
        if native_id:
            sessions.claim(root, directory, data, native_id, home)
    return data


def _pin_workspaces(scope):
    """Record absolute workspaces, valid wherever the directory ends up."""
    for root in tree(scope):
        meta = read(root / "universe.json")
        meta["workspace"] = str(workspace(root))
        write(root / "universe.json", meta)


def _relative_workspaces(scope):
    for root in tree(scope):
        meta = read(root / "universe.json")
        meta["workspace"] = os.path.relpath(workspace(root), root)
        write(root / "universe.json", meta)


def _subverse(path):
    path = exact(path)
    if parent(path) is None:
        raise ValueError("A top-level universe cannot be moved or removed; only its subverses")
    return path


def move_agent(root, reference, destination):
    root, destination = exact(root), exact(destination)
    agent_dir = directory(root, reference, allow_damaged=True)
    properties = records.properties(agent_dir / "properties.json")
    if destination == root:
        raise ValueError(f'{properties["name"]} is already in that universe')
    if not (destination / "harnesses" / properties["harness"] / "harness.json").is_file():
        raise ValueError(f'The destination has no {properties["harness"]} harness configured')
    for path in record_paths(destination):
        if name_matches(path, properties["name"]):
            raise ValueError(f'The destination already has an agent named {properties["name"]}')
    target = destination / "state" / "agents" / agent_dir.name
    with ExitStack() as stack:
        _hold(stack, [agent_dir])
        _session_namespaces(stack, root, destination)
        moved = _session_claims(destination, agent_dir)
        # Spawn publishes names under the same namespace before agents.lock.
        stack.enter_context(lock(destination / 'state' / '.agents.lock'))
        for path in record_paths(destination):
            if name_matches(path, properties['name']):
                raise ValueError(f'The destination already has an agent named {properties["name"]}')
        target.parent.mkdir(parents=True, exist_ok=True)
        agent_dir.rename(target)
        with lock(target / ".properties.lock"):
            data = read(target / "properties.json")
            data.update(universe_id=read(destination / "universe.json")["id"], updated_at=now(),
                        native_session_provider=moved['native_session_provider'])
            write(target / "properties.json", data)
    # Both journals: subscribers where it left and where it arrived.
    for scope in (root, destination):
        _event(scope, "agent.moved", data, {"from": str(root), "to": str(destination)})
    return {**data, "universe": str(destination)}


def _event(root, kind, agent, data):
    """Record an event for plugins that subscribe to it; best effort, never raises."""
    try:
        from ..plugins import events
        events.emit(root, kind, agent, None, data)
    except Exception:
        pass


def move_subverse(path, destination):
    path = _subverse(path)
    destination = exact(destination)
    if destination == path or destination.is_relative_to(path):
        raise ValueError("A subverse cannot be moved into itself")
    if destination == parent(path):
        raise ValueError(f"{path.name} is already in that universe")
    target = destination / "subverses" / path.name
    if target.exists():
        raise ValueError(f"The destination already has a subverse named {path.name}")
    with ExitStack() as stack:
        _hold(stack, _agents(path))
        _session_namespaces(stack, path, destination)
        for directory in _agents(path):
            _session_claims(destination, directory)
        _pin_workspaces(path)
        target.parent.mkdir(exist_ok=True)
        path.rename(target)
        _relative_workspaces(target)
    return {"universe": str(target), **read(target / "universe.json")}


def remove_agent(root, reference):
    root = exact(root)
    agent_dir = directory(root, reference, allow_damaged=True)
    orphan = orphaned(agent_dir)
    properties = _orphan_identity(agent_dir) if orphan else records.properties(agent_dir / "properties.json")
    with ExitStack() as stack:
        _hold(stack, [agent_dir])
        _session_namespaces(stack, root)
        if orphan and not orphaned(agent_dir):
            raise records.RecordError(agent_dir / 'properties.json',
                                      'core records changed before orphan removal; retry with the current record')
        entry = trash.entry(root, f'{properties["name"]}-{agent_dir.name[:8]}')
        agent_dir.rename(entry)
    record = {"kind": "agent", "name": properties["name"], "id": agent_dir.name,
              "origin": str(agent_dir), "removed_at": now()}
    write(entry / trash.RECORD, record)
    _event(root, "agent.removed", properties, {"trash": str(entry)})
    return {**record, "trash": str(entry)}


def remove_subverse(path):
    path = _subverse(path)
    meta = read(path / "universe.json")
    with ExitStack() as stack:
        agent_dirs = _agents(path)
        _hold(stack, agent_dirs)
        _session_namespaces(stack, path)
        # Absolute workspaces keep the entry valid if it is moved back.
        _pin_workspaces(path)
        entry = trash.entry(parent(path), path.name)
        path.rename(entry)
    record = {"kind": "subverse", "name": meta["name"], "id": meta["id"], "origin": str(path),
              "agents": len(agent_dirs), "removed_at": now()}
    write(entry / trash.RECORD, record)
    return {**record, "trash": str(entry)}
