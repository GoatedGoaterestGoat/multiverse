"""Focus an agent while it needs the user: only inbox-only messages reach it, others are refused."""
from datetime import datetime
import os

from .. import universe
from ..store import read
from ..universe import agents, focus, mailbox


def configure(parser):
    parser.add_argument("agent", nargs="?", help="Agent name (default: yourself inside an agent session)")
    parser.add_argument("--json", action="store_true", help="Return the focus record as JSON")
    length = parser.add_mutually_exclusive_group()
    length.add_argument("--for", dest="duration", metavar="DURATION",
                        help="End the focus after a duration such as 5m, 20m or 1h (default: until mv2 unfocus)")
    length.add_argument("--turn", action="store_true",
                        help="End the focus when the agent's next turn ends; mid-turn, that is the turn after this one")


def caller():
    """The ID of the agent running this command, or None for the person; an agent that can't be verified is refused."""
    from ..runtime.agent_identity import actor
    agent = actor()
    return agent.identity if agent else os.environ.get("MV2_AGENT_ID")


def target(root, agent, as_user):
    """The agent directory to change, and whether the caller is that agent.

    Agents may change only their own focus: a sender that was refused must not
    unfocus the recipient. Outside an agent session, the person may change any.
    """
    root = universe.resolve(root)
    me = None if as_user else caller()
    if not agent and not me:
        raise ValueError("Name the agent to focus outside an agent session")
    directory = agents.directory(root, agent or me)
    if me and directory.name != me:
        name = read(directory / "properties.json")["name"]
        raise ValueError(f"Agents change only their own focus; {name} or the user can change {name}'s. "
                         f"Send it an inbox-only message instead.")
    return directory, bool(me)


def run(root=None, *, agent=None, duration=None, turn=False, json=False, as_user=False):
    directory, own = target(root, agent, as_user)
    name = read(directory / "properties.json")["name"]
    status = agents.runtime(directory).get("status")
    if turn and status not in ("working", "waiting", "idle"):
        raise ValueError(f"{name}'s harness is not reporting its turns now, so a turn focus would never end; "
                         "focus for a duration with --for, or until mv2 unfocus")
    record = focus.start(directory, duration=focus.seconds(duration) if duration is not None else None, turn=turn,
                         status=status, by="agent" if own else "user")
    return {"agent": name, "focused": True, "ends": focus.ends(record, own), **record}


def cli_run(root=None, *, json=False, **options):
    result = run(root, **options)
    if json:
        return result
    if result["by"] == "agent":
        line = (f"Focused {result['ends']}. Steer and follow-up messages to you are refused and their senders "
                f"are told to send inbox-only.")
        return line + (" Run mv2 unfocus once the user has answered." if result["ends"] == "until unfocused"
                       else " It ends by itself; mv2 unfocus ends it sooner.")
    return (f"Focused {result['agent']} {result['ends']}. Steer and follow-up messages to it are refused; "
            f"inbox-only messages still arrive.")


def unread(directory, since=None):
    """Unread inbox-only messages, and how many of them arrived at or after `since`."""
    try:
        messages = mailbox.inbox(directory, limit=None, peek=True)["messages"]
    except (OSError, ValueError):
        return 0, 0
    if since is None:
        return len(messages), 0
    start = datetime.fromisoformat(since)
    def during(message):
        try:
            return datetime.fromisoformat(message.get("at", "")) >= start
        except (TypeError, ValueError):
            return False
    return len(messages), sum(during(message) for message in messages)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
