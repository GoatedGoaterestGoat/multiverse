"""Read measured usage for the exact native session."""
from ..runtime import native
from .. import universe


def configure(parser):
    parser.add_argument("agent")
    parser.add_argument("--json", action="store_true", help="Print the full result as JSON")


def run(root=None, *, agent, json=False):  # `json`: presentation only
    return native.operation(universe.resolve(root), agent, "get_usage")


def cli_run(root=None, *, agent, json=False):
    """Context, cost and the cumulative tokens on one line; no measurement is a failure that says why (exit 1)."""
    from ..display import agent_name, clean, context_line, usage_total
    result = run(root, agent=agent)
    if json:
        return {**result, "ok": result.get("state") == "ok"}
    name = agent_name(universe.resolve(root), agent)
    if result.get("state") == "ok":
        total = usage_total(result)
        return f"{name}: {context_line(result)}" + (f" · Total: {total} tokens" if total != "N/A" else "")
    reason = " ".join(clean(str(result.get("reason") or "it gave no reason")).split())
    raise ValueError(f"{name}: usage unavailable: {reason}")


if __name__ == '__main__':
    from ._common import main
    main(configure, run)
