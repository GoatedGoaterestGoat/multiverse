"""Canonical multiverse state paths.

Historically the plugin derived ``~/.multiverse`` from ``HOME``. That's
unsafe in deployments where the monitor and the claude processes it
spawns have different ``HOME`` values — e.g. container setups where a
wrapper function sets ``HOME=$SOMEWHERE_ELSE`` for ``claude`` so that
``~/.claude`` lands on persistent storage. In such cases the monitor
writes to one ``~/.multiverse`` and the MCP server writes to another,
and nothing that relies on inter-process file coordination (inbox
delivery, spawn-request brokering, the dashboard's inbox counter)
works correctly even though session state itself is fine (because the
monitor already passes ``MULTIVERSE_STATUS`` and friends as absolute
paths via ``tmux new-session -e``).

The rest of this module makes the multiverse root follow that same
"always pass an absolute path across process boundaries" rule. The
precedence is:

  1. ``$MULTIVERSE_ROOT`` — explicit override; the monitor sets this via
     ``tmux -e`` when spawning sessions, and ``bin/multiverse`` sets a
     default before launching the monitor itself so any user-set value
     is preserved.
  2. Grandparent of ``$MULTIVERSE_STATUS`` — derived per-session env;
     handy when a plugin process somehow missed step 1 but is running
     inside a multiverse-managed session.
  3. ``~/.multiverse`` — final fallback for genuinely standalone
     invocations (e.g. a user running a one-off script outside the
     wrapper). HOME-dependent, which is exactly what we're trying to
     avoid across process boundaries, but reasonable as a last resort.
"""

from __future__ import annotations

import os
from pathlib import Path


def multiverse_dir() -> Path:
    """Return the canonical ``~/.multiverse`` directory for this process."""
    explicit = os.environ.get("MULTIVERSE_ROOT")
    if explicit:
        return Path(explicit)
    status = os.environ.get("MULTIVERSE_STATUS")
    if status:
        # .../.multiverse/sessions/<id>/status.json → .../.multiverse/
        return Path(status).parent.parent.parent
    return Path(os.path.expanduser("~/.multiverse"))
