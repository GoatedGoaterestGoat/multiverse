"""Attach to the owned live terminal pane."""
import os
import subprocess
from .. import agents, universe

def configure(parser):
    parser.add_argument("agent")


def run(root=None, *, agent):
    backend, pane = agents.terminal_action(universe.resolve(root), agent, "attach")
    # Switch only inside the owning server; otherwise attach without nesting.
    current = os.environ.get("TMUX", "").split(",")[0]
    if current == backend.prefix[2]:
        backend.run("switch-client", "-t", pane)
    else:
        env = dict(os.environ)
        env.pop("TMUX", None)
        subprocess.run([*backend.prefix, "attach-session", "-t", pane], env=env, check=True)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
