"""Agent records: identity and names, the runtime record of the current launch, and reported progress."""
from ..harnesses.types import normalize_session_id
from ..store import existing_lock, lock, now, read, write


def directory(root, reference):
    matches = []
    for path in (root / "state" / "agents").glob("*/properties.json"):
        data = read(path)
        if reference in (data["id"], data["name"]) or data["id"].startswith(reference):
            matches.append(path.parent)
    if not matches:
        raise ValueError(f"No agent named {reference} here. Run mv2 list to see this universe's agents.")
    if len(matches) != 1:
        detail = ". Use a full UUID from these matches: " + ", ".join(sorted(path.name for path in matches))
        raise ValueError(f"Expected one agent matching {reference!r}; found {len(matches)}{detail}")
    return matches[0]


def check_available_name(root, name):
    for path in (root / "state" / "agents").glob("*/properties.json"):
        if read(path).get("name") == name:
            raise ValueError(f"This universe already has an agent named {name}; "
                             f"pick another name or resume it with mv2 resume {name}")


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


def record_recovery(directory, launch_id, state, reason, **extra):
    with existing_lock(directory / '.runtime.lock'):
        facts = runtime(directory)
        if facts.get('launch_id') != launch_id:
            return
        previous = facts.get('auto_recovery') or {}
        if previous.get('state') == 'cancelled':
            return
        facts.update(auto_recovery={**previous, **extra, 'state': state, 'reason': reason, 'updated_at': now()},
                     observed_at=now())
        relay = facts.get('native_relay') or {}
        if relay.get('state') == 'lost':
            facts['native_relay'] = {**relay, 'reason': 'Native relay lost. ' + reason +
                                     ' Kill then resume manually if needed; no native messages were replayed.'}
        write(directory / 'runtime.json', facts)
