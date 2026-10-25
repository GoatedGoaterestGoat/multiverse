"""Access levels: what a plugin may do in a universe, as the user grants it.

Every plugin API call, contribution and adapter method has one effect class:
observe (reads), spend (uses tokens, money or time, changing nothing) or act
(changes a session, a launch, an inbox or a universe's files). A plugin works
under the level granted to it in each universe, at most what its manifest
requests, and nothing where it is off. A call above the plugin's level, or
above the class its own code declared (effect= on a contribution,
@mv.effect), is refused with Denied, which the API raises as api.AccessDenied.

This is a guard against mistakes that makes a plugin's reach readable, not a
sandbox: a plugin's code runs with the user's permissions. The declared class
follows a context variable, so it does not follow threads the plugin starts
itself. Core's own callers set no limit, so require() passes for them.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import os

LEVELS = ("observe", "spend", "act")
# One line each, for `mv2 plugins enable`, `info` and the dashboard's grant prompt.
MEANING = {
    "observe": "reads agents, messages and transcripts, and changes nothing",
    "spend": "also uses model tokens, money or time (ask, one-shot runs), still changing nothing",
    "act": "also changes agents, inboxes and files: spawns, sends, stops",
}
# How `mv2 plugins enable` says what a plugin asks for, first.
ASKS = {"observe": "observes only", "spend": "spends model tokens", "act": "acts on agents"}
PROCESS_CEILING = "MV2_EFFECTS"  # MV2_EFFECTS=observe: every plugin in this process observes at most


class Denied(ValueError):
    """A call above the level a plugin has here, or above the class its own code declared."""


def check(level, what="access level"):
    if level not in LEVELS:
        raise ValueError(f"{what} must be observe, spend or act")
    return level


def rank(level):
    return LEVELS.index(level)


def lowest(*levels):
    """The lowest of the levels given; None (nothing allowed) when any is None."""
    if any(level is None for level in levels):
        return None
    return min(levels, key=rank) if levels else None


def process_ceiling():
    """The ceiling MV2_EFFECTS sets for every plugin in this process, or None. An unknown value is observe."""
    value = os.environ.get(PROCESS_CEILING)
    if not value:
        return None
    return value if value in LEVELS else "observe"


# (level, who, declared): the limit the running plugin code is under, or None outside any plugin call. declared:
# it comes from the plugin's own effect= or @mv.effect, not from its grant.
_limit = ContextVar("mv2_plugin_access", default=None)


_NONE = "none"  # no access at all: a plugin off in that universe


def _held(level):
    return -1 if level == _NONE else rank(level)


@contextmanager
def within(level, who, declared=False):
    """Run the block under `level` (None: no access) for `who`; nesting keeps the lower limit and its name.
    `declared`: the limit is the class the plugin's own code declared, not its grant."""
    level = _NONE if level is None else level
    current = _limit.get()
    token = _limit.set(current if current is not None and _held(current[0]) <= _held(level)
                       else (level, who, declared))
    try:
        yield
    finally:
        _limit.reset(token)


def current():
    """(level, who, declared) of the running plugin code, or None."""
    return _limit.get()


def require(level, what):
    """Refuse `what`, which needs `level`, when the running plugin code is limited below it."""
    limit = _limit.get()
    if limit is None:
        return
    held, who, declared = limit
    if _held(held) < rank(level):
        if declared:
            raise Denied(f"{who} is declared {held}; {what} needs {level}")
        raise Denied(f"{who} has {'no' if held == _NONE else held} access here; {what} needs {level}")
