"""Attach to the owned live terminal pane."""
import os
import shlex
import subprocess
from ..runtime import lifecycle
from .. import universe
from ..terminal.tmux import Tmux


def configure(parser):
    parser.add_argument("agent")


def run(root=None, *, agent):
    backend, pane = lifecycle.terminal_action(universe.resolve(root), agent, "attach")
    command = backend.command("attach-session", "-t", pane)
    current = os.environ.get("TMUX", "").split(",")[0]
    if current:
        origin = os.environ.get("TMUX_PANE")
        if not origin:
            raise RuntimeError("Cannot identify the originating tmux pane")
        host = Tmux(current)
        tty, client = host.run("display-message", "-p", "-t", origin,
                               "#{pane_tty}\t#{client_name}").split("\t")
        # A separate terminal can inherit TMUX from its launcher. It must not
        # replace that launcher's client just because its environment matches.
        if os.isatty(0) and os.ttyname(0) == tty:
            visible = host.run("list-clients", "-t", origin, "-F", "#{client_name}\t#{pane_id}")
            if not client or f"{client}\t{origin}" not in visible.splitlines():
                raise RuntimeError("No tmux client is attached to the originating dashboard")
            # Replace the host client, not the dashboard process. A nested attach
            # lets the host consume prefix+d; switch-client detaches the whole host.
            return_command = [*host.prefix, "attach-session", "-t", origin]
            route = "unset TMUX TMUX_PANE; " + shlex.join(command) + "; exec " + shlex.join(return_command)
            host.run("detach-client", "-t", client, "-E", route)
            return
    env = dict(os.environ)
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    # This is the interactive terminal client, not one of Tmux.run's inspection helpers.
    subprocess.run(command, env=env, check=True, stdin=None)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
