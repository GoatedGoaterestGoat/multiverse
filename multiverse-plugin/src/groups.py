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
from typing import Any, Iterable, Optional

from .atomic_files import atomic_write_json, exclusive_file_lock
from .paths import encode_cwd as _encode_cwd, multiverse_dir


STATE_VERSION = 1

# Display suffixes build_dashboard_rows appends to a row's name. Stripped
# before the name is used as a membership key.
_FOLD_SUFFIX_RE = re.compile(r" \(folded [^)]+\)$")
_BG_FORK_SUFFIX = " (bg fork)"

# Guards against a pathological (or hand-edited) file turning tree walks
# into an infinite loop. Deeper than anyone will nest on purpose.
_MAX_DEPTH = 64


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
    # The suffixes nest in either order — build_dashboard_rows wraps a
    # background fork's title in the fold suffix, so a folded fork reads
    # "name (bg fork) (folded 02:33)". Strip until nothing changes rather
    # than assuming one layering.
    while True:
        stripped = _FOLD_SUFFIX_RE.sub("", name)
        if stripped.endswith(_BG_FORK_SUFFIX):
            stripped = stripped[: -len(_BG_FORK_SUFFIX)]
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


# Finder's name for a folder you just made and haven't named yet. The
# dashboard creates the folder first and edits the name in place, so this
# is what shows for the half-second before the user types.
DEFAULT_FOLDER_NAME = "New folder"


def _prune_contained(
    state: GroupState,
    group_ids: Iterable[str],
    agent_names: Iterable[str],
) -> tuple[list[str], list[str]]:
    """Drop items that a selected folder already carries.

    Selecting a folder and something inside it and then moving both would
    move the inner item twice — out of its folder and into the destination
    separately — quietly flattening the structure the user could see on
    screen. Anything with a selected ancestor comes along with that
    ancestor, so it is removed from the operation.
    """
    ordered = list(group_ids)
    selected = {gid for gid in ordered if gid in state.groups}
    # Keep the caller's order rather than the set's: a batch move renames
    # whichever of two same-named folders it visits second, and iterating
    # a set of ids would pick a different loser on every run.
    seen: set[str] = set()
    keep_groups = []
    for gid in ordered:
        if gid not in selected or gid in seen:
            continue
        seen.add(gid)
        if not (selected & set(state.ancestors_of(gid))):
            keep_groups.append(gid)
    keep_agents = []
    for name in agent_names:
        gid = state.group_of(name)
        if gid is None:
            keep_agents.append(name)
            continue
        chain = {gid} | set(state.ancestors_of(gid))
        if not (selected & chain):
            keep_agents.append(name)
    return keep_groups, keep_agents


def common_parent(
    state: GroupState,
    group_ids: Iterable[str],
    agent_names: Iterable[str],
) -> Optional[str]:
    """The folder holding every item, or ``None`` when they disagree.

    Used to decide where a "wrap these in a new folder" lands: beside the
    things being wrapped, the way Finder's New Folder with Selection puts
    it in the current directory.
    """
    parents: set[Optional[str]] = set()
    for gid in group_ids:
        group = state.groups.get(gid)
        if group is not None:
            parents.add(group.parent)
    for name in agent_names:
        parents.add(state.group_of(name))
    if len(parents) == 1:
        return parents.pop()
    return None


@dataclass
class Wrap:
    """A "put these in a new folder" operation, and how to take it back.

    The dashboard creates the folder before the user has named it, so Esc
    has to be able to unwind the whole thing. Restoring from the recorded
    parents rather than just deleting the folder matters when the wrapped
    items came from *different* places: deleting alone would dump them all
    at the level the folder happened to be created at.
    """

    group: Group
    previous_group_parents: dict[str, Optional[str]]
    previous_agent_groups: dict[str, Optional[str]]
    # Only for subfolders the wrap had to rename to keep sibling names
    # unique; undoing puts the original name back.
    previous_group_names: dict[str, str] = field(default_factory=dict)


def wrap_in_new_folder(
    cwd: str,
    *,
    group_ids: Iterable[str] = (),
    agent_names: Iterable[str] = (),
    name: Optional[str] = None,
    parent: Optional[str] = None,
) -> Wrap:
    """Create a folder beside the given items and move them all into it.

    With nothing to wrap this is just "make a folder", and ``parent`` says
    where — the folder the dashboard's cursor is in. It is ignored when
    there are items, which land beside themselves instead.
    """
    with mutate(cwd) as state:
        gids, names = _prune_contained(state, group_ids, agent_names)
        if gids or names:
            parent = common_parent(state, gids, names)
        elif parent is not None and parent not in state.groups:
            raise ValueError("That folder no longer exists.")
        base = (name or DEFAULT_FOLDER_NAME).strip() or DEFAULT_FOLDER_NAME
        group = Group(
            id=new_group_id(),
            name=unique_sibling_name(state, parent, base),
            parent=parent,
            collapsed=False,
            created_at=time.time(),
        )
        previous_groups: dict[str, Optional[str]] = {}
        previous_names: dict[str, str] = {}
        previous_agents: dict[str, Optional[str]] = {}
        state.groups[group.id] = group
        for gid in gids:
            child = state.groups.get(gid)
            if child is not None:
                previous_groups[gid] = child.parent
                # Wrapping can pull same-named folders together out of
                # different parents, so uniquify like every other move.
                if child.name.casefold() in _sibling_names(state, group.id):
                    previous_names[gid] = child.name
                    child.name = unique_sibling_name(
                        state, group.id, child.name
                    )
                child.parent = group.id
        for agent in names:
            key = member_key(agent)
            if key:
                previous_agents[key] = state.members.get(key)
                state.members[key] = group.id
        return Wrap(group, previous_groups, previous_agents, previous_names)


def undo_wrap(cwd: str, wrap: Wrap) -> None:
    """Take back a :func:`wrap_in_new_folder` — folder gone, items home.

    Tolerates the folder having changed underneath (another monitor, an
    agent spawning into it): anything found inside it that this wrap did
    not put there is promoted to where the folder was, rather than
    disappearing with it.
    """
    with mutate(cwd) as state:
        removed = state.groups.pop(wrap.group.id, None)
        parent = removed.parent if removed is not None else wrap.group.parent
        for gid, previous in wrap.previous_group_parents.items():
            group = state.groups.get(gid)
            if group is None:
                continue
            group.parent = previous if previous in state.groups else None
            original = wrap.previous_group_names.get(gid)
            if original and original.casefold() not in _sibling_names(
                state, group.parent
            ):
                group.name = original
        for name, previous in wrap.previous_agent_groups.items():
            key = member_key(name)
            if not key:
                continue
            if previous is not None and previous in state.groups:
                state.members[key] = previous
            else:
                state.members.pop(key, None)
        for group in state.groups.values():
            if group.parent == wrap.group.id:
                group.parent = parent
        for key, gid in list(state.members.items()):
            if gid != wrap.group.id:
                continue
            if parent is None:
                state.members.pop(key, None)
            else:
                state.members[key] = parent


def move_into(
    cwd: str,
    target: Optional[str],
    *,
    group_ids: Iterable[str] = (),
    agent_names: Iterable[str] = (),
) -> int:
    """Move items into ``target`` (``None`` = top level). Returns the count.

    Skips a folder that would end up inside itself rather than failing the
    whole batch — the caller has usually already excluded the subtree, and
    a partial move the user can see beats an error they can't act on.
    """
    with mutate(cwd) as state:
        if target is not None and target not in state.groups:
            raise ValueError("That folder no longer exists.")
        gids, names = _prune_contained(state, group_ids, agent_names)
        moved = 0
        for gid in gids:
            group = state.groups.get(gid)
            if group is None or group.parent == target:
                continue
            if _would_cycle(state, gid, target):
                continue
            if group.name.casefold() in _sibling_names(state, target):
                group.name = unique_sibling_name(state, target, group.name)
            group.parent = target
            moved += 1
        for agent in names:
            key = member_key(agent)
            if not key or state.members.get(key) == target:
                continue
            if target is None:
                state.members.pop(key, None)
            else:
                state.members[key] = target
            moved += 1
        return moved


def lift_out(
    cwd: str,
    *,
    group_ids: Iterable[str] = (),
    agent_names: Iterable[str] = (),
) -> int:
    """Move each item one level up. Returns how many actually moved."""
    with mutate(cwd) as state:
        gids, names = _prune_contained(state, group_ids, agent_names)
        moved = 0
        for gid in gids:
            group = state.groups.get(gid)
            if group is None or group.parent is None:
                continue
            grandparent = state.groups.get(group.parent)
            target = grandparent.parent if grandparent is not None else None
            if group.name.casefold() in _sibling_names(state, target):
                group.name = unique_sibling_name(state, target, group.name)
            group.parent = target
            moved += 1
        for agent in names:
            key = member_key(agent)
            gid = state.members.get(key) if key else None
            group = state.groups.get(gid) if gid else None
            if group is None:
                continue
            if group.parent is None:
                state.members.pop(key, None)
            else:
                state.members[key] = group.parent
            moved += 1
        return moved


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
    def is_draft(self) -> bool:
        return self.kind == "draft"

    @property
    def collapsed(self) -> bool:
        return bool(self.group is not None and self.group.collapsed)


def group_row_key(group_id: str) -> str:
    return f"grp:{group_id}"


# The placeholder row for an agent that hasn't been created yet. A session
# can't be created and renamed afterwards the way a folder can — the name
# is baked in at spawn (custom title, tmux env, inbox routing) — so the
# dashboard draws a draft in the tree and only calls create_session once
# the user presses Enter.
DRAFT_ROW_KEY = "draft:new-agent"

# Distinguishes "no draft" from "a draft at the top level", which is a
# perfectly ordinary place to put one.
NO_DRAFT = object()


def build_display_rows(
    entries: list,
    state: GroupState,
    *,
    draft_in: Any = NO_DRAFT,
) -> list[DisplayRow]:
    """Lay the dashboard out as a tree.

    ``entries`` is the flat, already-sorted list of ``DashboardRow``. Their
    relative order is preserved within each folder, so the existing sort
    (status, then recency, ghosts last) still governs — grouping only
    decides *where* a row is drawn, never how agents rank among themselves.

    Folders come before loose agents at every level, the way a file pane
    lists directories first.

    ``draft_in`` places an unnamed, not-yet-created agent in a folder (or
    at the top level with ``None``). It is laid out as the last child of
    that folder so the connectors around it close correctly — the row
    above it becomes a ``├─`` while the draft takes the ``└─``, exactly as
    if the agent already existed.
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
        holds_draft = draft_in is not NO_DRAFT and (
            draft_in == group.id or draft_in in set(state.descendants_of(group.id))
        )
        if (group.collapsed and not holds_draft) or depth >= _MAX_DEPTH:
            return
        # Bars for this group's children: a continuation bar only if this
        # group itself has a sibling below it.
        child_bars = ancestor_bars + ("" if depth == 0 else (_GAP if last else _BAR))
        emit_level(group.id, child_bars, depth + 1)

    def emit_level(group_id: Optional[str], bars: str, depth: int) -> None:
        subgroups = state.children_of(group_id)
        agents = by_group.get(group_id, [])
        draft_here = draft_in is not NO_DRAFT and draft_in == group_id
        total = len(subgroups) + len(agents) + (1 if draft_here else 0)
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
        if draft_here:
            connector = "" if depth == 0 else _ELBOW
            rows.append(
                DisplayRow(
                    kind="draft",
                    key=DRAFT_ROW_KEY,
                    depth=depth,
                    prefix=bars + connector,
                )
            )

    emit_level(None, "", 0)
    return rows
