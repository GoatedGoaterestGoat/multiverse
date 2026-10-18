"""End an agent's focus: messages reach it normally again. Says what waits in its inbox."""
from ..store import read
from ..universe import focus
from .focus import caller, target, unread


def configure(parser):
    parser.add_argument("agent", nargs="?", help="Agent name (default: yourself inside an agent session)")
    parser.add_argument("--json", action="store_true", help="Return the result as JSON")


def run(root=None, *, agent=None, json=False, as_user=False):
    directory, _ = target(root, agent, as_user)
    record = focus.end(directory)
    count, during = unread(directory, record["since"] if record else None)
    return {"agent": read(directory / "properties.json")["name"], "focused": False, "was_focused": record is not None,
            "unread": count, "unread_while_focused": during}


def cli_run(root=None, *, json=False, **options):
    result = run(root, **options)
    return result if json else summary(result, own=not options.get("as_user") and caller() is not None)


def summary(result, own=False):
    """`Unfocused; …` and what waits in the inbox, for the agent itself or for the user."""
    name, count, during = result["agent"], result["unread"], result["unread_while_focused"]
    if own:
        line = ("Unfocused; messages reach you normally again." if result["was_focused"]
                else "You were not focused; messages already reach you normally.")
        if count:
            noun = "message" if count == 1 else "messages"
            sent = "" if not during else (", all sent while you were focused" if during == count
                                          else f", {during} sent while you were focused")
            line += f" You have {count} unread inbox {noun}{sent}: read them now with mv2 inbox."
        return line
    line = (f"Unfocused {name}; messages reach it normally again." if result["was_focused"]
            else f"{name} was not focused.")
    if count:
        line += f" It has {count} unread inbox {'message' if count == 1 else 'messages'}."
    return line


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
