"""Focus an agent while it needs the user: steer and follow-up messages to it wait in its inbox."""
from datetime import datetime

from .. import display, universe
from ..runtime import queued_prompts
from ..store import read
from ..universe import agents, focus, mailbox


def configure(parser):
    parser.add_argument("agent", nargs="?", help="Agent name (default: yourself inside an agent session)")
    parser.add_argument("--json", action="store_true", help="Return the focus record as JSON")
    length = parser.add_mutually_exclusive_group()
    length.add_argument("--for", dest="duration", metavar="DURATION",
                        help="End the focus after a duration such as 5m, 20m or 1h (default: until mv2 unfocus); "
                             "the user only, not an agent focusing itself")
    length.add_argument("--turn", action="store_true",
                        help="End the focus when the agent's next turn ends; for an agent focusing itself or one that is working, "
                             "that is the turn after the current one")


def caller():
    """The verified agent running this command, or None for the person; an agent that can't be verified is refused.

    The same check as `send`: stable agent proof, opted-in retained bridge proof,
    or the exact current launch, in the agent's own universe and directory.
    """
    from ..runtime.agent_identity import sender_actor
    return sender_actor(acting="change focus")


def target(root, agent, as_user):
    """The agent directory to change, and whether the caller is that agent.

    Agents may change only their own focus: a sender that was refused must not
    unfocus the recipient. Outside an agent session, the person may change any.
    """
    me = None if as_user else caller()
    if not agent and not me:
        raise ValueError("Name the agent to focus outside an agent session")
    if not agent:
        return me.directory, True
    directory = agents.directory(universe.resolve(root), agent)
    # The same folder, not only the same ID: an agent with this ID in another universe is another agent.
    if me and directory.resolve() != me.directory.resolve():
        name = read(directory / "properties.json")["name"]
        raise ValueError(f"Agents change only their own focus; {name} or the user can change {name}'s. "
                         f"Send it an inbox-only message instead.")
    return directory, bool(me)


def run(root=None, *, agent=None, duration=None, turn=False, json=False, as_user=False, check_queue=False):
    """Focus an agent. `check_queue` also says which follow-ups its harness already holds, which the focus won't stop;
    the command line asks for it, the dashboard doesn't, since the read can take up to 2 s."""
    directory, own = target(root, agent, as_user)
    name = read(directory / "properties.json")["name"]
    if own and duration is not None:
        # An agent's sense of time is poor, and a focus that runs out by itself tells it nothing.
        raise ValueError("Only the user can focus you for a time; use --turn, or focus until mv2 unfocus")
    try:
        record = focus.start(directory, duration=focus.seconds(duration) if duration is not None else None, turn=turn,
                             by="agent" if own else "user")
    except focus.NoTurns:
        raise ValueError(f"{name}'s harness is not reporting its turns now, so a turn focus would never end; "
                         + ("focus until mv2 unfocus instead" if own else
                            "focus for a duration with --for, or until mv2 unfocus")) from None
    result = {"agent": name, "focused": True, "ends": focus.ends(record, own), **record}
    if check_queue:
        # Read after the focus began: anything sent from now on is held, so what the harness holds came before.
        result.update(queued(root, directory, name, own, turn))
    return result


def queued(root, directory, name, own, turn):
    """`queued_follow_ups` (a count, or None when unknown) and `queue_note`, from the read send uses.

    Follow-ups only: a steer reaches the agent within its current turn. An unknown queue gets a line only when one
    may be queued, as for send: mv2's records hold an unconfirmed follow-up to the agent, or can't be read.
    """
    try:
        # The target's own universe: an agent focusing itself may have another one selected.
        report, _, unconfirmed = queued_prompts.for_send(directory.parents[2], directory.name)
    except Exception:
        return {"queued_follow_ups": None, "queue_note": None}  # Never fails a focus that has begun.
    complete = report.state == "ok" and report.coverage == "complete"
    count = sum(entry.mode == "follow-up" for entry in report.entries) if report.state == "ok" else 0
    whom, whose = ("you", "your") if own else (name, "its")
    if count:
        noun = "message was" if count == 1 else "messages were"
        note = (f"{'' if complete else 'At least '}{count} follow-up {noun} already queued for {whom} before this focus, "
                f"and it won't stop {'it' if count == 1 else 'them'}: {'it arrives' if count == 1 else 'they arrive'} "
                f"when {whose} turn ends")
        if turn:
            note += (" and start" + ("s" if count == 1 else "") + " a new turn. A turn focus counts any turn end, so it "
                     "will likely end with that turn")
            note += (", before the user answers. To stay focused until the user answers, run mv2 focus instead, "
                     "and mv2 unfocus once they have." if own else ".")
        else:
            note += ". The focus holds only messages sent from now on."
    elif not complete and (unconfirmed is None or unconfirmed):
        reason = display.clean(report.reason or "The harness queue is unknown").rstrip(".")
        note = (f"mv2 couldn't check whether follow-up messages are already queued for {whom}: {reason}. "
                f"If any are, this focus won't stop them.")
    else:
        note = None
    return {"queued_follow_ups": count if complete or count else None, "queue_note": note}


def cli_run(root=None, *, json=False, **options):
    result = run(root, check_queue=True, **options)
    if json:
        return result
    if result["by"] == "agent":
        line = f"Focused {result['ends']}. Steer and follow-up messages to you wait in your inbox."
        line += (" Run mv2 unfocus once the user has answered." if result["ends"] == "until unfocused"
                 else " It ends by itself; mv2 unfocus ends it sooner.")
    else:
        line = (f"Focused {result['agent']} {result['ends']}. Steer and follow-up messages to it wait in its inbox, "
                f"with its inbox-only messages.")
    return line + ("\n" + result["queue_note"] if result.get("queue_note") else "")


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
