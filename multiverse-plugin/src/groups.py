"""Agent groups — folders for the monitor dashboard.

A workspace accumulates agents faster than a flat list can stay readable:
every spawn appends one more row, and after an afternoon of delegation the
dashboard is a wall of names with no visible structure. Groups give that
wall a shape. A group is a folder: it holds agents and other groups, it
collapses, and it survives monitor restarts.

Membership is keyed by **agent name**, not by session id or transcript
UUID. Names are the one identifier that stays put across everything the
dashboard does to a row: a dormant agent resumed under a new multiverse
session id keeps its name, a compaction that rewrites the live JSONL keeps
its name, a context fold that supersedes a UUID keeps its name. Keying on
anything else would silently drop agents out of their folder at exactly the
moments the user is least likely to be watching. Display suffixes the
dashboard appends (``(folded HH:MM)``, ``(bg fork)``) are stripped by
:func:`member_key` so a ghost row files itself next to the live agent it
came from.

On-disk layout, one file per workspace under ``~/.multiverse/groups/``::

    <encoded-cwd>.json   — {"version": 1, "groups": {...}, "members": {...}}
    <encoded-cwd>.lock   — flock target for read-modify-write

Every mutation takes the lock and rewrites the file atomically, because two
monitors on the same workspace are a supported configuration (the spawn
broker already assumes it) and a lost update here means an agent quietly
leaving its folder.

The module deliberately depends on nothing from ``sessions`` or Textual:
the store, the tree layout, and the connector glyphs are all plain data, so
they can be tested without a terminal or a live workspace.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

from .atomic_files import atomic_write_json, exclusive_file_lock
from .paths import multiverse_dir


STATE_VERSION = 1

# Display suffixes build_dashboard_rows appends to a row's name. Stripped
# before the name is used as a membership key.
_FOLD_SUFFIX_RE = re.compile(r" \(folded [^)]+\)$")
_BG_FORK_SUFFIX = " (bg fork)"

# Guards against a pathological (or hand-edited) file turning tree walks
# into an infinite loop. Deeper than anyone will nest on purpose.
_MAX_DEPTH = 64


def _encode_cwd(cwd: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


def groups_dir() -> Path:
    """Resolved lazily so tests (and re-rooted processes) can set
    ``MULTIVERSE_ROOT`` after import."""
    return multiverse_dir() / "groups"


def state_path(cwd: str) -> Path:
    return groups_dir() / f"{_encode_cwd(cwd)}.json"


def _lock_path(cwd: str) -> Path:
    return groups_dir() / f"{_encode_cwd(cwd)}.lock"


def member_key(display_name: Optional[str]) -> str:
    """Normalize a dashboard row name to its membership key.

    Idempotent, and safe on ``None`` so callers can pass a row name
    straight through.
    """
    if not display_name:
        return ""
    name = display_name
    while name.endswith(_BG_FORK_SUFFIX):
        name = name[: -len(_BG_FORK_SUFFIX)]
    while True:
        stripped = _FOLD_SUFFIX_RE.sub("", name)
        if stripped == name:
            break
        name = stripped
    return name.strip()


@dataclass
class Group:
    id: str
    name: str
    parent: Optional[str] = None
    collapsed: bool = False
    created_at: float = 0.0

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "parent": self.parent,
            "collapsed": self.collapsed,
            "created_at": self.created_at,
        }


@dataclass
class GroupState:
    """The whole grouping picture for one workspace."""

    groups: dict[str, Group] = field(default_factory=dict)
    # membership key (normalized agent name) → group id
    members: dict[str, str] = field(default_factory=dict)

    # -- queries -------------------------------------------------------

    def get(self, group_id: Optional[str]) -> Optional[Group]:
        if not group_id:
            return None
        return self.groups.get(group_id)

    def group_of(self, agent_name: str) -> Optional[str]:
        gid = self.members.get(member_key(agent_name))
        return gid if gid in self.groups else None

    def children_of(self, group_id: Optional[str]) -> list[Group]:
        """Direct subgroups of ``group_id`` (``None`` for the root level),
        ordered the way a folder pane orders folders: by name."""
        out = [g for g in self.groups.values() if g.parent == group_id]
        out.sort(key=lambda g: (g.name.casefold(), g.created_at, g.id))
        return out

    def ancestors_of(self, group_id: str) -> list[str]:
        """Ids from the group's parent up to the root, nearest first."""
        out: list[str] = []
        seen = {group_id}
        current = self.groups.get(group_id)
        while current is not None and current.parent:
            if current.parent in seen or len(out) > _MAX_DEPTH:
                break
            seen.add(current.parent)
            out.append(current.parent)
            current = self.groups.get(current.parent)
        return out

    def descendants_of(self, group_id: str) -> list[str]:
        """Every subgroup id beneath ``group_id``, at any depth."""
        out: list[str] = []
        stack = [group_id]
        seen = {group_id}
        while stack:
            for child in self.children_of(stack.pop()):
                if child.id in seen:
                    continue
                seen.add(child.id)
                out.append(child.id)
                stack.append(child.id)
        return out

    def path_names(self, group_id: str) -> list[str]:
        """Group names from the root down to ``group_id``."""
        g = self.groups.get(group_id)
        if g is None:
            return []
        names = [g.name]
        for ancestor in self.ancestors_of(group_id):
            parent = self.groups.get(ancestor)
            if parent is not None:
                names.append(parent.name)
        names.reverse()
        return names

    def members_of(self, group_id: str, *, recursive: bool = False) -> set[str]:
        wanted = {group_id}
        if recursive:
            wanted |= set(self.descendants_of(group_id))
        return {name for name, gid in self.members.items() if gid in wanted}

    # -- serialization -------------------------------------------------

    def to_json(self) -> dict:
        return {
            "version": STATE_VERSION,
            "groups": {gid: g.to_json() for gid, g in self.groups.items()},
            "members": dict(self.members),
        }

    @classmethod
    def from_json(cls, data: Any) -> "GroupState":
        """Rebuild from disk, discarding anything malformed.

        This runs on every dashboard tick, so it repairs rather than
        raises: a hand-edited file with a dangling parent pointer or a
        parent cycle produces a usable (if reparented) tree instead of a
        monitor that won't start.
        """
        state = cls()
        if not isinstance(data, dict):
            return state
        raw_groups = data.get("groups")
        if isinstance(raw_groups, dict):
            for gid, raw in raw_groups.items():
                if not isinstance(gid, str) or not isinstance(raw, dict):
                    continue
                name = raw.get("name")
                if not isinstance(name, str) or not name.strip():
                    continue
                parent = raw.get("parent")
                if not isinstance(parent, str):
                    parent = None
                try:
                    created_at = float(raw.get("created_at") or 0.0)
                except (TypeError, ValueError):
                    created_at = 0.0
                state.groups[gid] = Group(
                    id=gid,
                    name=name.strip(),
                    parent=parent,
                    collapsed=bool(raw.get("collapsed")),
                    created_at=created_at,
                )
        # Dangling and cyclic parents both become root groups — visible
        # and fixable in the UI, rather than invisible.
        for g in state.groups.values():
            if g.parent is not None and g.parent not in state.groups:
                g.parent = None
        for g in state.groups.values():
            if g.parent is not None and _would_cycle(state, g.id, g.parent):
                g.parent = None

        raw_members = data.get("members")
        if isinstance(raw_members, dict):
            for name, gid in raw_members.items():
                if not isinstance(name, str) or not isinstance(gid, str):
                    continue
                key = member_key(name)
                if key and gid in state.groups:
                    state.members[key] = gid
        return state


def _would_cycle(state: GroupState, group_id: str, new_parent: Optional[str]) -> bool:
    """True if reparenting ``group_id`` under ``new_parent`` closes a loop."""
    if new_parent is None:
        return False
    if new_parent == group_id:
        return True
    seen = {group_id}
    current: Optional[str] = new_parent
    depth = 0
    while current is not None:
        if current in seen or depth > _MAX_DEPTH:
            return True
        seen.add(current)
        g = state.groups.get(current)
        if g is None:
            return False
        current = g.parent
        depth += 1
    return False


def new_group_id() -> str:
    return f"g-{secrets.token_hex(4)}"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def load(cwd: str) -> GroupState:
    """Read the workspace's groups. Never raises — a missing or corrupt
    file reads as "no groups"."""
    try:
        raw = state_path(cwd).read_text()
    except (OSError, UnicodeDecodeError):
        return GroupState()
    try:
        return GroupState.from_json(json.loads(raw))
    except json.JSONDecodeError:
        return GroupState()


def save(cwd: str, state: GroupState) -> None:
    atomic_write_json(state_path(cwd), state.to_json(), indent=2)


class _Mutation:
    """Read-modify-write under the workspace's group lock.

    Two monitors watching one workspace both write this file; without the
    lock, "collapse a folder here" and "move an agent there" race and one
    of them vanishes.
    """

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd

    def __enter__(self) -> GroupState:
        self._lock = exclusive_file_lock(_lock_path(self.cwd))
        self._lock.__enter__()
        self.state = load(self.cwd)
        return self.state

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                save(self.cwd, self.state)
        finally:
            self._lock.__exit__(exc_type, exc, tb)


def mutate(cwd: str) -> _Mutation:
    """``with mutate(cwd) as state:`` — saved on clean exit only."""
    return _Mutation(cwd)


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def _sibling_names(state: GroupState, parent: Optional[str]) -> set[str]:
    return {g.name.casefold() for g in state.groups.values() if g.parent == parent}


def unique_sibling_name(
    state: GroupState, parent: Optional[str], base: str
) -> str:
    """``base``, or ``base 2`` / ``base 3`` … if taken among siblings."""
    base = base.strip() or "group"
    taken = _sibling_names(state, parent)
    if base.casefold() not in taken:
        return base
    n = 2
    while f"{base} {n}".casefold() in taken:
        n += 1
    return f"{base} {n}"


def create_group(
    cwd: str, name: str, parent: Optional[str] = None
) -> Group:
    """Create a group. Raises ``ValueError`` on an empty name, an unknown
    parent, or a name already used by a sibling."""
    name = (name or "").strip()
    if not name:
        raise ValueError("Group name cannot be empty.")
    with mutate(cwd) as state:
        if parent is not None and parent not in state.groups:
            raise ValueError("That parent group no longer exists.")
        if name.casefold() in _sibling_names(state, parent):
            where = (
                f"inside '{state.groups[parent].name}'"
                if parent is not None
                else "at the top level"
            )
            raise ValueError(f"A group named '{name}' already exists {where}.")
        group = Group(
            id=new_group_id(),
            name=name,
            parent=parent,
            collapsed=False,
            created_at=time.time(),
        )
        state.groups[group.id] = group
        return group


def rename_group(cwd: str, group_id: str, new_name: str) -> Group:
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("Group name cannot be empty.")
    with mutate(cwd) as state:
        group = state.groups.get(group_id)
        if group is None:
            raise ValueError("That group no longer exists.")
        siblings = _sibling_names(state, group.parent) - {group.name.casefold()}
        if new_name.casefold() in siblings:
            raise ValueError(f"A group named '{new_name}' already exists here.")
        group.name = new_name
        return group


def delete_group(cwd: str, group_id: str) -> None:
    """Remove a group, promoting everything inside it one level up.

    Deleting a folder never deletes agents, and never orphans a subgroup —
    both land where the folder itself lived. Nothing about the underlying
    sessions is touched.
    """
    with mutate(cwd) as state:
        group = state.groups.pop(group_id, None)
        if group is None:
            return
        for child in state.groups.values():
            if child.parent == group_id:
                child.parent = group.parent
        for name, gid in list(state.members.items()):
            if gid != group_id:
                continue
            if group.parent is None:
                del state.members[name]
            else:
                state.members[name] = group.parent


def move_group(cwd: str, group_id: str, new_parent: Optional[str]) -> None:
    """Reparent a group. Refuses to move a group into its own subtree."""
    with mutate(cwd) as state:
        group = state.groups.get(group_id)
        if group is None:
            raise ValueError("That group no longer exists.")
        if new_parent is not None and new_parent not in state.groups:
            raise ValueError("That destination group no longer exists.")
        if _would_cycle(state, group_id, new_parent):
            raise ValueError("A group can't be moved inside itself.")
        if group.parent == new_parent:
            return
        siblings = _sibling_names(state, new_parent)
        if group.name.casefold() in siblings:
            group.name = unique_sibling_name(state, new_parent, group.name)
        group.parent = new_parent


def assign_agent(cwd: str, agent_name: str, group_id: Optional[str]) -> None:
    """Put an agent in a group, or take it out (``group_id=None``)."""
    key = member_key(agent_name)
    if not key:
        return
    with mutate(cwd) as state:
        if group_id is None:
            state.members.pop(key, None)
            return
        if group_id not in state.groups:
            raise ValueError("That group no longer exists.")
        state.members[key] = group_id


def set_collapsed(cwd: str, group_id: str, collapsed: bool) -> None:
    with mutate(cwd) as state:
        group = state.groups.get(group_id)
        if group is not None:
            group.collapsed = collapsed


def toggle_collapsed(cwd: str, group_id: str) -> bool:
    """Flip one group's collapsed flag; returns the new value."""
    with mutate(cwd) as state:
        group = state.groups.get(group_id)
        if group is None:
            return False
        group.collapsed = not group.collapsed
        return group.collapsed


def set_all_collapsed(cwd: str, collapsed: bool) -> None:
    with mutate(cwd) as state:
        for group in state.groups.values():
            group.collapsed = collapsed


def auto_group_spawn(
    cwd: str, parent_agent: str, child_agent: str
) -> Optional[str]:
    """File a freshly spawned agent alongside the agent that spawned it.

    Two cases:

    * the spawner is already in a folder → the child joins that folder;
    * the spawner is loose → a folder named after the spawner is created
      and **both** move into it.

    The second case is what keeps the dashboard from degenerating back
    into a flat list: a delegation lineage becomes a folder the first time
    it branches, without the user having to anticipate it. Everything it
    does is reversible by hand (rename, move out, delete the folder), so
    guessing wrong costs a keystroke.

    Returns the group id the child ended up in, or ``None`` if there was
    nothing to do.
    """
    parent_key = member_key(parent_agent)
    child_key = member_key(child_agent)
    if not parent_key or not child_key or parent_key == child_key:
        return None
    with mutate(cwd) as state:
        existing = state.members.get(parent_key)
        if existing in state.groups:
            state.members[child_key] = existing
            return existing
        group = Group(
            id=new_group_id(),
            name=unique_sibling_name(state, None, parent_key),
            parent=None,
            collapsed=False,
            created_at=time.time(),
        )
        state.groups[group.id] = group
        state.members[parent_key] = group.id
        state.members[child_key] = group.id
        return group.id


# ---------------------------------------------------------------------------
# Display tree
# ---------------------------------------------------------------------------

# Box-drawing connectors. Nesting is drawn only *inside* folders: top-level
# rows sit flush against the left edge, so a workspace with no groups looks
# exactly like it did before groups existed.
_TEE = "├─ "
_ELBOW = "└─ "
_BAR = "│  "
_GAP = "   "

CHEVRON_OPEN = "▼"
CHEVRON_CLOSED = "▶"


@dataclass
class DisplayRow:
    """One rendered dashboard line: a folder header or an agent."""

    kind: str  # "group" | "agent"
    key: str
    depth: int = 0
    prefix: str = ""
    group: Optional[Group] = None
    entry: Any = None  # DashboardRow, kept untyped to avoid a cycle
    # For groups: agents anywhere beneath, and the rows that would show if
    # expanded. Both are computed even when collapsed, for the rollup.
    agent_count: int = 0
    agents: list = field(default_factory=list)

    @property
    def is_group(self) -> bool:
        return self.kind == "group"

    @property
    def collapsed(self) -> bool:
        return bool(self.group is not None and self.group.collapsed)


def group_row_key(group_id: str) -> str:
    return f"grp:{group_id}"


def build_display_rows(entries: list, state: GroupState) -> list[DisplayRow]:
    """Lay the dashboard out as a tree.

    ``entries`` is the flat, already-sorted list of ``DashboardRow``. Their
    relative order is preserved within each folder, so the existing sort
    (status, then recency, ghosts last) still governs — grouping only
    decides *where* a row is drawn, never how agents rank among themselves.

    Folders come before loose agents at every level, the way a file pane
    lists directories first.
    """
    by_group: dict[Optional[str], list] = {}
    for entry in entries:
        gid = state.group_of(getattr(entry, "name", ""))
        by_group.setdefault(gid, []).append(entry)

    def subtree_agents(group_id: str) -> list:
        out = list(by_group.get(group_id, ()))
        for child in state.children_of(group_id):
            out.extend(subtree_agents(child.id))
        return out

    rows: list[DisplayRow] = []

    def emit_group(group: Group, ancestor_bars: str, last: bool, depth: int) -> None:
        connector = "" if depth == 0 else (_ELBOW if last else _TEE)
        agents = subtree_agents(group.id)
        rows.append(
            DisplayRow(
                kind="group",
                key=group_row_key(group.id),
                depth=depth,
                prefix=ancestor_bars + connector,
                group=group,
                agent_count=len(agents),
                agents=agents,
            )
        )
        if group.collapsed or depth >= _MAX_DEPTH:
            return
        # Bars for this group's children: a continuation bar only if this
        # group itself has a sibling below it.
        child_bars = ancestor_bars + ("" if depth == 0 else (_GAP if last else _BAR))
        emit_level(group.id, child_bars, depth + 1)

    def emit_level(group_id: Optional[str], bars: str, depth: int) -> None:
        subgroups = state.children_of(group_id)
        agents = by_group.get(group_id, [])
        total = len(subgroups) + len(agents)
        for i, sub in enumerate(subgroups):
            emit_group(sub, bars, last=(i == total - 1), depth=depth)
        for j, entry in enumerate(agents):
            index = len(subgroups) + j
            connector = "" if depth == 0 else (
                _ELBOW if index == total - 1 else _TEE
            )
            rows.append(
                DisplayRow(
                    kind="agent",
                    key=entry.row_key,
                    depth=depth,
                    prefix=bars + connector,
                    entry=entry,
                )
            )

    emit_level(None, "", 0)
    return rows


def iter_groups_depth_first(state: GroupState) -> Iterator[tuple[Group, int]]:
    """Every group with its nesting depth, in display order. Used by the
    move-into picker so it reads like the dashboard."""

    def walk(parent: Optional[str], depth: int) -> Iterator[tuple[Group, int]]:
        if depth > _MAX_DEPTH:
            return
        for group in state.children_of(parent):
            yield group, depth
            yield from walk(group.id, depth + 1)

    yield from walk(None, 0)
