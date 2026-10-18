"""Ask an isolated side question without changing the main conversation."""
from ..runtime import native
from .. import universe


def configure(parser):
    parser.add_argument("agent")
    parser.add_argument("--question", required=True)
    parser.add_argument("--json", action="store_true", help="Print the full result as JSON")


def run(root=None, *, agent, question, json=False):  # `json`: presentation only
    return native.operation(universe.resolve(root), agent, "ask", question=question)


def cli_run(root=None, *, agent, question, json=False):
    """The answer alone; anything else is a failure that says why (exit 1)."""
    from ..display import agent_name, clean
    result = run(root, agent=agent, question=question)
    if json:
        return {**result, "ok": result.get("state") == "ok"}
    if result.get("state") == "ok":
        return clean(str((result.get("value") or {}).get("answer") or "")).strip() or "(no text in the answer)"
    reason = " ".join(clean(str(result.get("reason") or "it gave no reason")).split())
    raise ValueError(f"{agent_name(universe.resolve(root), agent)} can't answer: {reason}")


if __name__ == '__main__':
    from ._common import main
    main(configure, run)
