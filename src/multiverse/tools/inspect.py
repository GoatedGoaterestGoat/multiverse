"""Show an agent's key facts and the end of its live terminal."""
import re
from ..universe import agents
from ..runtime import lifecycle
from .. import universe
from ..display import error_text, label, mode, state, notice_lines

TAIL_LINES = 30


def configure(parser):
    parser.add_argument("agent")
    parser.add_argument("--lines", type=int, default=TAIL_LINES, help=f"Terminal lines to show (default: {TAIL_LINES})")
    parser.add_argument("--json", action="store_true", help="Print the facts and the whole captured terminal as JSON")


def run(root=None, *, agent, lines=None, json=False):  # `lines` and `json` only shape the command line's text
    return lifecycle.terminal_action(universe.resolve(root), agent, "inspect")


def tail(capture, lines):
    """The last `lines` lines of a capture, without the empty space the pane leaves below the screen
    and with the long rules a terminal draws across its width shortened."""
    rows = [re.sub(r"([─━═│])\1{5,}", r"\1\1\1", row.rstrip()) for row in capture.splitlines()]
    while rows and not rows[-1]:
        rows.pop()
    return rows[-lines:] if lines > 0 else []


def facts(root, agent):
    """The agent's row from the listing: the same facts the dashboard shows."""
    identity = agents.directory(root, agent, allow_damaged=True).name
    return next(row for row in lifecycle.listing(root, only=identity) if row["id"] == identity)


def cli_run(root=None, *, agent, lines=TAIL_LINES, json=False):
    root = universe.resolve(root)
    row = facts(root, agent)
    live = row["live"] == "alive"
    # A pane that is gone answers with the reason it is gone, not with a terminal.
    damage = row.get("damaged")
    damage_reason = f"{damage['file']}: {damage['reason']}" if damage else None
    if damage:
        answer, note = "", damage_reason
    else:
        try:
            answer, note = run(root, agent=agent), None
        except (OSError, ValueError, RuntimeError) as error:
            answer, note = "", str(error)
    capture = answer if live else ""
    note = None if live else (answer or note or row.get("reason") or "it is not running")  # kept for --json
    error = damage_reason or (row.get('runtime', {}).get('native_refusal') or {}).get('message') or row.get("connection_error") or row.get("launch_error")
    fallback = (row.get("runtime", {}).get("native_mode") or {}).get("fallback_reason")
    # The launch's plugin problems (a plugin left out because it broke). A bridged Claude launch also puts them in
    # its fallback reason; a plain one has none, so only the plugins record says why.
    recorded = row.get('runtime', {}).get('plugins')
    problems = recorded.get('problems') if isinstance(recorded, dict) else None
    problems = [item for item in problems if isinstance(item, str) and item not in (fallback or '')] \
        if isinstance(problems, list) else []
    notices = notice_lines(row.get('runtime', {}).get('launch_notices'), problems)
    if json:
        result = {"name": row["name"], "harness": row["harness"], "mode": mode(row), "live": row["live"],
                "status": row.get("status"), "phase": row.get("phase"), "summary": row.get("summary", ""),
                "error": error, "fallback": fallback, "terminal": capture if capture else None, "note": note}
        if damage:
            result['damaged'] = damage
        if notices:
            result['notices'] = notices
        if row.get('delivery_problem'):
            result['delivery_problem'] = row['delivery_problem']
        if row.get('runtime', {}).get('auto_recovery'):
            result['recovery'] = row['runtime']['auto_recovery']
        return result
    head = f'{row["name"]} · {label(row)} · {state(row)}'
    reported = " — ".join(part for part in (row.get("phase"), " ".join(row.get("summary", "").split())) if part)
    out = [head] + ([f"Reported: {reported}"] if reported else []) + ([f"Last error: {error_text(error, tail=5)}"] if error else []) \
        + ([f"Note: {fallback}"] if fallback else [])
    out.extend('Note: ' + notice for notice in notices)
    if row.get('delivery_problem'):
        out.append(row['delivery_problem'])
    if row.get('recovery_note'):
        out.append('Note: Reconnected automatically.')
    shown = tail(capture, lines)
    if shown:
        out += ["", f"Terminal (last {len(shown)} lines):", *shown]
    elif note:
        out += ["", "No live terminal: " + {"dormant": "it is dormant.", "stopped": "it is stopped.", "ending": "it is ending."}.get(
            row["live"], "its state is unknown.")]
    return "\n".join(out)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
