"""Read properties and observe matching tmux panes."""
from .. import agents, universe

def configure(parser):
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--json", action="store_true")


def run(root=None, *, recursive=False, json=False):
    rows = agents.listing(universe.resolve(root), recursive)
    if json:
        return rows
    return "\n".join(f'{r["id"][:8]}  {r["name"]:16} {r["live"]:8} {r["status"]:8} {r.get("phase", "?"):9} {r.get("summary", "")} [{r["universe"]}]' for r in rows) or "No agents."


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
