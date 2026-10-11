"""The harness side of plugins: what a launch adds, capabilities, usage and status-line items.

Core builds a launch's LaunchPlan here, before the adapter makes its command;
the adapter applies the parts its harness understands (Claude plugin
directories and settings, Pi extensions, Codex entries) and returns the
plan's record, which lifecycle stores as runtime.json `plugins`. Nothing a
plugin returns is an argv fragment: each family has fixed fields, merged by
fixed rules. A plugin that fails, conflicts or does not fit this harness's
version is left out of that launch with the reason, and the launch goes on.
"""
from dataclasses import dataclass, field
import json
from pathlib import Path
import re

from . import loader

# Environment a plugin may never set: core's identity of the launch.
CORE_ENVIRONMENT = re.compile(r"MV2_[A-Z0-9_]*")
ITEMS = "status-items"
ITEM_LENGTH = 200


@dataclass(frozen=True)
class LaunchInfo:
    """What a launch contribution is told about the launch."""
    universe: Path
    agent_id: str
    agent_name: str
    mode: str          # spawn or resume (a wake is a resume with a prompt)
    family: str        # claude, codex or pi
    harness: str       # the harness's name in this universe
    version: tuple | None = None   # the harness executable's (major, minor, patch), when it could be read
    bridge: bool = False           # whether the launch uses mv2's bridge (the Claude mod, Pi's extension, Codex's relay)


@dataclass(frozen=True)
class Contribution:
    """What a plugin adds to one launch. Every field is optional."""
    environment: dict = field(default_factory=dict)
    claude_settings: dict = field(default_factory=dict)
    problem: str | None = None  # the plugin cannot do its job in this launch: shown as its fallback reason
    mode: str | None = None     # e.g. "without compaction": appended to the launch's native mode


@dataclass
class LaunchPlan:
    """Everything the plugins on in a universe add to one launch, merged, plus what to record."""
    environment: dict = field(default_factory=dict)
    claude_plugin_dirs: list = field(default_factory=list)
    claude_settings: dict = field(default_factory=dict)
    pi_extensions: list = field(default_factory=list)
    codex: list = field(default_factory=list)  # (plugin name, plugin directory, HarnessEntry)
    problems: list = field(default_factory=list)
    modes: list = field(default_factory=list)
    status_items: list = field(default_factory=list)  # {"name", "plugin", "claude", "tmux"}
    plugins: dict = field(default_factory=dict)  # name -> {"version", "state", "reason"?, "added"?}

    def record(self):
        """runtime.json's `plugins`: what each plugin added to this launch, or why it added nothing."""
        return {"plugins": self.plugins, "status_items": self.status_items}

    def skip(self, name, reason):
        entry = self.plugins.setdefault(name, {})
        entry.update(state="skipped", reason=reason)


def _merge_settings(base, extra, path=()):
    """Deep-merge Claude settings: objects merge, lists (hook arrays) concatenate; a scalar both set is a conflict."""
    merged = dict(base)
    for key, value in extra.items():
        if key not in merged:
            merged[key] = value
        elif isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _merge_settings(merged[key], value, (*path, key))
        elif isinstance(merged[key], list) and isinstance(value, list):
            merged[key] = merged[key] + value
        elif merged[key] != value:
            raise ValueError(f"settings {'.'.join((*path, key))} is set differently by another plugin")
    return merged


def launch_plan(info, base_settings=None, base_environment=None):
    """The LaunchPlan of the plugins on in `info.universe` for this launch. Never raises.

    `base_settings` and `base_environment` are what the adapter sets itself:
    a plugin that would change them is left out, so the adapter's own launch
    never changes. `claude_settings` and `environment` in the plan are the
    merged result, base included. Claude settings reach only a bridged
    launch (a plain one passes no --settings); a plugin that sets them in a
    plain launch is recorded as skipped.
    """
    plan = LaunchPlan(environment=dict(base_environment or {}), claude_settings=dict(base_settings or {}))
    if loader.safe_mode():
        return plan
    owners = {key: "the harness" for key in plan.environment}  # environment variable -> who set it
    for loaded in loader.active(info.universe):
        manifest = loaded.manifest
        entries = manifest.harnesses.get(info.family, ())
        contributors = loaded.registrations.launch.get(info.family, [])
        items = [{"name": item.name, "plugin": loaded.name, "claude": item.claude, "tmux": item.tmux}
                 for item in manifest.statusline]
        plan.status_items.extend(items)
        if not entries and not contributors:
            continue
        record = plan.plugins.setdefault(loaded.name, {"version": manifest.version, "state": "loaded"})
        added = []
        try:
            for entry in entries:
                reason = entry.fits(info.version)
                if reason is None and entry.needs_bridge and not info.bridge and info.family != "codex":
                    reason = "needs mv2's bridge, which this launch does not use"
                if reason is not None:
                    raise _Skip(reason)
                if info.family == "claude":
                    added.append(("claude_plugin_dirs", str(Path(manifest.directory, entry.path).resolve())))
                elif info.family == "pi":
                    added.append(("pi_extensions", str(Path(manifest.directory, entry.path).resolve())))
                else:
                    added.append(("codex", (loaded.name, Path(manifest.directory), entry)))
            contributions = []
            for function in contributors:
                result = loader.guard(loaded, f"launch {info.family}", function, info, root=info.universe,
                                      default=_FAILED, timeout=loader.SYNCHRONOUS_BUDGET)
                if result is _FAILED:
                    raise _Skip(f"its launch contribution failed: {loaded.errors[-1] if loaded.errors else 'unknown'}")
                if result is not None and not isinstance(result, Contribution):
                    raise _Skip("its launch contribution must return api.Contribution or None")
                if result is not None:
                    contributions.append(result)
            environment, settings = {}, dict(plan.claude_settings)
            for contribution in contributions:
                for key, value in contribution.environment.items():
                    if not isinstance(key, str) or CORE_ENVIRONMENT.fullmatch(key) or not isinstance(value, str):
                        raise _Skip(f"it may not set the environment variable {key!r}")
                    if key in owners and owners[key] != loaded.name:
                        raise _Skip(f"{owners[key]} already sets {key}")
                    environment[key] = value
                if contribution.claude_settings:
                    if info.family != "claude" or not info.bridge:
                        raise _Skip("it sets Claude settings, which only a bridged Claude launch takes")
                    try:
                        settings = _merge_settings(settings, contribution.claude_settings)
                    except ValueError as error:
                        raise _Skip(str(error)) from None
        except _Skip as skip:
            plan.skip(loaded.name, str(skip))
            continue
        for kind, value in added:
            getattr(plan, kind).append(value)
        for key, value in environment.items():
            owners[key] = loaded.name
            plan.environment[key] = value
        plan.claude_settings = settings
        for contribution in contributions:
            if contribution.problem:
                plan.problems.append(contribution.problem)
            if contribution.mode:
                plan.modes.append(contribution.mode)
        record["added"] = sorted({kind for kind, _ in added} | ({"environment"} if environment else set())
                                 | ({"claude_settings"} if any(c.claude_settings for c in contributions) else set()))
        if any(contribution.problem for contribution in contributions):
            record["problem"] = "; ".join(c.problem for c in contributions if c.problem)
    return plan


class _Skip(Exception):
    pass


_FAILED = object()


@dataclass(frozen=True)
class SessionInfo:
    """The session a capability or usage contribution is asked about."""
    universe: Path
    agent_id: str
    harness: str
    family: str
    native_id: str | None = None
    launch_id: str | None = None
    state: str = "unknown"
    native_mode: dict | None = None
    plugins: dict | None = None  # this launch's runtime.json `plugins` record

    @property
    def directory(self):
        return self.universe / "state" / "agents" / self.agent_id


def session_info(session, family, runtime=None):
    """SessionInfo for a core SessionHandle (its session_file lies in the agent's directory)."""
    directory = Path(session.session_file).parent
    return SessionInfo(directory.parent.parent.parent, session.agent_id, session.harness, family or "",
                       session.native_id, session.launch_id, session.state,
                       dict(session.native_mode) if session.native_mode else None,
                       (runtime or {}).get("plugins"))


def capabilities(info, taken):
    """{name: (state, reason, mechanism)} plugins on here add; names in `taken` (core's) are refused."""
    result = {}
    for loaded in loader.active(info.universe):
        for name, function in loaded.registrations.capabilities.items():
            if name in taken or name in result:
                loader.record(loaded, f"capability {name}", ValueError("another owner already answers it"), info.universe)
                continue
            answer = loader.guard(loaded, f"capability {name}", function, info, root=info.universe,
                                  default=None, timeout=loader.SYNCHRONOUS_BUDGET)
            if answer is None:
                answer = ("error", f"plugin {loaded.name} could not answer", None)
            result[name] = answer
    return result


def status_items(root):
    """The runtime.json `plugins` record of a launch whose adapter applies no plugins: its status-line items only."""
    items = [] if loader.safe_mode() else [
        {"name": item.name, "plugin": loaded.name, "claude": item.claude, "tmux": item.tmux}
        for loaded in loader.active(root) for item in loaded.manifest.statusline]
    return {"plugins": {}, "status_items": items}


def adjust_usage(info, value):
    """`value` (a usage record) after every plugin's adjustment here, in plugin-name order."""
    for loaded in loader.active(info.universe):
        for function in loaded.registrations.usage:
            adjusted = loader.guard(loaded, "usage", function, info, dict(value), root=info.universe,
                                    default=None, timeout=loader.SYNCHRONOUS_BUDGET)
            if isinstance(adjusted, dict):
                value = adjusted
    return value


def item_path(agent_dir, name):
    return Path(agent_dir) / ITEMS / f"{name}.txt"


def write_item(agent_dir, name, text):
    """Write one status-line item for an agent; empty text removes it. One line, at most 200 characters."""
    path = item_path(agent_dir, name)
    text = " ".join(str(text or "").split())[:ITEM_LENGTH]
    if not text:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text + "\n")
    temporary.replace(path)


def read_items(agent_dir, runtime):
    """{name: text} of the plugin items this launch declared (runtime.json `plugins.status_items`)."""
    record = (runtime or {}).get("plugins") or {}
    found = {}
    for item in record.get("status_items") or []:
        name = item.get("name") if isinstance(item, dict) else None
        if not isinstance(name, str) or "/" in name or name.startswith("."):
            continue
        try:
            text = item_path(agent_dir, name).read_text()[:ITEM_LENGTH + 1]
        except OSError:
            continue
        found[name] = " ".join(text.split())
    return found


def as_json(plan):
    return json.dumps(plan.record())
