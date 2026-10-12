"""The harness side of plugins: what a launch adds, capabilities, usage and status-line items.

Core builds a launch's LaunchPlan here, before the adapter makes its command;
the adapter applies the parts its harness understands (Claude plugin
directories and settings, Pi extensions, Codex entries) and returns the
plan's record, which lifecycle stores as runtime.json `plugins`. Nothing a
plugin returns is an argv fragment: each family has fixed fields, merged by
fixed rules. A plugin that fails, conflicts or does not fit this harness's
version is left out of that launch with the reason, and the launch goes on.
"""
from dataclasses import dataclass, field, replace
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from . import loader

# Core's own variables: the launch's identity and mv2's switches. A plugin may set MV2_<ITS NAME> and
# MV2_<ITS NAME>_* (its name upper-cased, hyphens as underscores) and nothing else under MV2_.
CORE_ENVIRONMENT = frozenset({"MV2_UNIVERSE", "MV2_AGENT_ID", "MV2_AGENT_DIR", "MV2_LAUNCH_ID", "MV2_CONTROL_SOCKET",
                              "MV2_CLI", "MV2_PYTHON", "MV2_TMUX_SOCKET", "MV2_PLUGINS", "MV2_PLUGIN_PATH",
                              # Where a session's account, history and executables come from: core and the
                              # user decide these, never a plugin.
                              "HOME", "PATH", "CODEX_HOME", "CODEX_SQLITE_HOME", "CLAUDE_CONFIG_DIR",
                              "PI_CODING_AGENT_DIR",
                              # Markers of the session that started mv2, which adapters unset on purpose.
                              "CODEX_THREAD_ID", "CLAUDECODE"})


def may_set(plugin, key):
    """Whether a plugin may set environment variable `key` in a launch."""
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key in CORE_ENVIRONMENT:
        return False
    if not key.startswith("MV2_"):
        return True
    own = "MV2_" + plugin.upper().replace("-", "_")
    return key == own or key.startswith(own + "_")
ITEMS = "status-items"
ITEM_LENGTH = 200


@dataclass(frozen=True)
class LaunchInfo:
    """What a launch contribution is told about the launch."""
    universe: Path | None  # None for an agent outside a universe: only bundled defaults apply
    agent_id: str
    agent_name: str
    mode: str          # spawn or resume (a wake is a resume with a prompt)
    family: str        # claude, codex or pi
    harness: str       # the harness's name in this universe
    # The harness executable's (major, minor, patch), when it could be read. An adapter may pass a function
    # instead: launch_plan calls it only when a plugin here has an entry or contribution for this harness.
    version: object = None
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
    error: str | None = None  # why no plugin could be considered at all

    def record(self):
        """runtime.json's `plugins`: what each plugin added to this launch, or why it added nothing."""
        return {"plugins": self.plugins, "status_items": self.status_items, **({"error": self.error} if self.error else {})}

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
    a plugin that would change them is left out, so the adapter's own values
    and entries never change; a plugin's hook entries come after the
    adapter's own in each list. `claude_settings` and `environment` in the plan are the
    merged result, base included. Claude settings reach only a bridged
    launch (a plain one passes no --settings); a plugin that sets them in a
    plain launch is recorded as skipped.

    Each manifest entry fits the launch (harness version, bridge) on its own,
    and one that does not is recorded as `unfit`. A plugin none of whose
    entries for this harness fit is left out entirely, contributions included.
    """
    plan = LaunchPlan(environment=dict(base_environment or {}), claude_settings=dict(base_settings or {}))
    if loader.safe_mode():
        return plan
    try:
        return _launch_plan(info, plan)
    except Exception as error:  # discovery itself failed: the launch goes on as the adapter made it
        empty = LaunchPlan(environment=dict(base_environment or {}), claude_settings=dict(base_settings or {}))
        empty.error = f"plugins were left out of this launch: {type(error).__name__}: {error}"
        return empty


def _launch_plan(info, plan):
    owners = {key: "the harness" for key in plan.environment}  # environment variable -> who set it
    active = loader.active(info.universe)
    if callable(info.version):
        wanted = any(loaded.manifest.harnesses.get(info.family) or loaded.registrations.launch.get(info.family)
                     for loaded in active)
        try:
            version = info.version() if wanted else None
        except Exception:
            version = None
        info = replace(info, version=version)
    plan.status_items = declared_items(info.universe, active)
    for loaded in active:
        manifest = loaded.manifest
        entries = manifest.harnesses.get(info.family, ())
        contributors = loaded.registrations.launch.get(info.family, [])
        if not entries and not contributors:
            continue
        record = plan.plugins.setdefault(loaded.name, {"version": manifest.version, "state": "loaded"})
        added, unfit = [], []
        try:
            for entry in entries:
                # Each entry fits this launch or not on its own: a plugin may ship one per version or mode.
                reason = entry.fits(info.version)
                if reason is None and entry.needs_bridge and not info.bridge and info.family != "codex":
                    reason = "needs mv2's bridge, which this launch does not use"
                if reason is not None:
                    unfit.append(reason)
                    continue
                if info.family == "claude":
                    added.append(("claude_plugin_dirs", str(Path(manifest.directory, entry.path).resolve())))
                elif info.family == "pi":
                    added.append(("pi_extensions", str(Path(manifest.directory, entry.path).resolve())))
                else:
                    added.append(("codex", (loaded.name, Path(manifest.directory), entry)))
            if unfit and not added:
                # Nothing of it fits this launch: its contributions stay out too, so a guard never
                # comes without the hook it guards.
                raise _Skip("; ".join(dict.fromkeys(unfit)), failure=False)
            contributions = []
            for function in contributors:
                result = loader.guard(loaded, f"launch {info.family}", function, info, root=info.universe,
                                      default=_FAILED, timeout=loader.SYNCHRONOUS_BUDGET)
                if result is _FAILED:
                    raise _Skip(f"its launch contribution failed: {loaded.errors[-1] if loaded.errors else 'unknown'}")
                if result is not None and not isinstance(result, Contribution):
                    raise _Skip("its launch contribution must return api.Contribution or None")
                if result is not None:
                    _check_contribution(result)
                    contributions.append(result)
            environment, settings = {}, dict(plan.claude_settings)
            for contribution in contributions:
                for key, value in contribution.environment.items():
                    if not may_set(loaded.name, key) or not isinstance(value, str) or "\0" in value:
                        raise _Skip(f"it may not set the environment variable {key!r}")
                    if key in owners and owners[key] != loaded.name:
                        raise _Skip(f"{owners[key]} already sets {key}")
                    environment[key] = value
                if contribution.claude_settings:
                    if info.family != "claude" or not info.bridge:
                        raise _Skip("it sets Claude settings, which only a bridged Claude launch takes",
                                    failure=info.family != "claude")
                    try:
                        settings = _merge_settings(settings, contribution.claude_settings)
                    except ValueError as error:
                        raise _Skip(str(error)) from None
        except _Skip as skip:
            plan.skip(loaded.name, str(skip))
            if skip.failure:
                # Shown where the launch's own problems are (native_mode.fallback_reason for Claude),
                # not only in runtime.json: a session that runs without a plugin it relies on says so.
                plan.problems.append(f"plugin {loaded.name} was left out: {skip}")
            continue
        if unfit:
            record["unfit"] = list(dict.fromkeys(unfit))
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


_versions = {}


def version_of(executable):
    """(major, minor, patch) from `EXECUTABLE --version`; None when it cannot be told.

    Asked again only when the binary changes (its resolved path, time or size),
    so an update in place is seen by a long-running dashboard. A probe that
    fails is not kept: the next launch asks again."""
    path = shutil.which(executable)
    if path is None:
        return None
    try:
        real = os.path.realpath(path)
        status = os.stat(real)
    except OSError:
        return None
    key = (real, status.st_mtime_ns, status.st_size)
    if key not in _versions:
        try:
            result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        found = re.search(r"(\d+)\.(\d+)\.(\d+)", result.stdout) if result.returncode == 0 else None
        if found is None:
            return None
        _versions[key] = tuple(int(part) for part in found.groups())
    return _versions[key]


def universe_of(agent_dir):
    """The universe an agent directory belongs to, or None outside one."""
    if not agent_dir:
        return None
    directory = Path(agent_dir)
    root = directory.parent.parent.parent
    if directory.parent.name != "agents" or not (root / "universe.json").is_file():
        return None
    return root


def plan_for(context, family, harness, version=None, bridge=False, base_settings=None, base_environment=None):
    """launch_plan for an adapter's LaunchContext. Never raises.

    An agent outside a universe gets the bundled plugins that are on by default."""
    try:
        root = universe_of(context.values.get("agent_dir"))
        info = LaunchInfo(root, context.values.get("agent_id", ""), context.values.get("name", ""), context.mode,
                          family, harness, version, bridge)
    except Exception as error:
        plan = LaunchPlan(environment=dict(base_environment or {}), claude_settings=dict(base_settings or {}))
        plan.error = f"plugins were left out of this launch: {type(error).__name__}: {error}"
        return plan
    return launch_plan(info, base_settings, base_environment)


class _Skip(Exception):
    """A plugin left out of one launch. `failure`: it broke (raised, timed out, returned something unusable,
    conflicted), rather than not fitting this launch (version, bridge, a plain launch)."""
    def __init__(self, reason, failure=True):
        super().__init__(reason)
        self.failure = failure


def _check_contribution(contribution):
    """Raise _Skip naming the first field of a Contribution that cannot be used."""
    if not isinstance(contribution.environment, dict):
        raise _Skip("its contribution's environment must be a dict of strings")
    if not isinstance(contribution.claude_settings, dict):
        raise _Skip("its contribution's claude_settings must be a dict")
    try:
        json.dumps(contribution.claude_settings)
    except (TypeError, ValueError):
        raise _Skip("its contribution's claude_settings must be JSON") from None
    for name in ("problem", "mode"):
        value = getattr(contribution, name)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise _Skip(f"its contribution's {name} must be text or None")


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


def declared_items(root, active):
    """The status-line items of the plugins on here: [{"name", "plugin", "claude", "tmux"}].

    An item named after a plugin belongs to that plugin alone (compaction's
    `compaction`), and a name two plugins declare is shown for neither; both are
    in item_conflicts(), for doctor."""
    names = {found.name for found in loader.entries(root)} if root is not None else {loaded.name for loaded in active}
    owners = {}
    for loaded in active:
        for item in loaded.manifest.statusline:
            if item.name in names and item.name != loaded.name:
                continue
            owners.setdefault(item.name, []).append((loaded, item))
    return [{"name": item.name, "plugin": loaded.name, "claude": item.claude, "tmux": item.tmux}
            for found in owners.values() if len(found) == 1 for loaded, item in found]


def item_conflicts(root):
    """Sentences for status-line items that are not shown because of their name."""
    active = loader.active(root)
    names = {found.name for found in loader.entries(root)}
    problems, owners = [], {}
    for loaded in active:
        for item in loaded.manifest.statusline:
            if item.name in names and item.name != loaded.name:
                problems.append(f"{loaded.name}'s status-line item {item.name!r} is not shown: "
                                f"that name belongs to the {item.name} plugin")
            else:
                owners.setdefault(item.name, []).append(loaded.name)
    problems.extend(f"{' and '.join(plugins)} both declare the status-line item {name!r}; neither is shown"
                    for name, plugins in sorted(owners.items()) if len(plugins) > 1)
    return problems


def status_items(root):
    """The runtime.json `plugins` record of a launch whose adapter applies no plugins: its status-line items only."""
    items = [] if loader.safe_mode() else declared_items(root, loader.active(root))
    return {"plugins": {}, "status_items": items}


def adjust_usage(info, value):
    """`value` (a usage record) after every plugin's adjustment here, in plugin-name order.

    Each adjuster gets its own deep copy, so one that fails, runs out of time
    or returns something unusable (not a JSON object) changes nothing: the
    value stays as the adapter measured it or the last good adjustment left
    it, and the failure is recorded against the plugin. None means no change.
    """
    try:
        current = json.loads(json.dumps(value))
    except (TypeError, ValueError):
        return value
    changed = False
    for loaded in loader.active(info.universe):
        for function in loaded.registrations.usage:
            adjusted = loader.guard(loaded, "usage", function, info, json.loads(json.dumps(current)),
                                    root=info.universe, default=_FAILED, timeout=loader.SYNCHRONOUS_BUDGET)
            if adjusted is _FAILED or adjusted is None:
                continue
            try:
                if not isinstance(adjusted, dict):
                    raise TypeError("a usage adjustment returns the usage record (a dict) or None")
                current = json.loads(json.dumps(adjusted))
                changed = True
            except (TypeError, ValueError) as error:
                loader.record(loaded, "usage", error, info.universe)
    return current if changed else value


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
