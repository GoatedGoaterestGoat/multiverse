"""Launch an agent in a tagged tmux pane."""
from pathlib import Path
from .. import agents, universe
from ._common import add_wait

HARNESS_NAMES = {"codex": "Codex", "claude": "Claude", "pi": "Pi"}

def configure(parser):
    parser.add_argument("--name", required=True)
    parser.add_argument("--harness", default="codex", help="Configured harness name (default: codex)")
    task = parser.add_mutually_exclusive_group()
    task.add_argument("--task", "--prompt", help="Initial prompt sent to the new agent")
    task.add_argument("--task-file", type=Path)
    parser.add_argument("--model")
    add_wait(parser)
    parser.add_argument("--json", action="store_true", help="Print the full launch record as JSON")


def run(root=None, *, name, task=None, task_file=None, harness="codex", model=None, wait=False, wait_timeout=60,
        json=False):  # `json` is the command line's presentation flag: the record is always returned here
    root = universe.resolve(root)
    prompt = Path(task_file).read_text() if task_file is not None else task
    return agents.start(root, name, prompt, harness, model, wait=wait, wait_timeout=wait_timeout)


def mode(record):
    """How the agent is connected: `bridge` (native delivery) or `plain`."""
    runtime = record.get("runtime") or {}
    return (runtime.get("native_mode") or {}).get("mode") or ("bridge" if runtime.get("control_endpoint") else "plain")


def label(record):
    """`Codex, bridge`: the harness and how it is connected, as a person would say it."""
    harness = record.get("harness") or "agent"
    return f"{HARNESS_NAMES.get(harness, harness)}, {mode(record)}"


def outcome(record, verb, failure):
    """One plain line for a finished launch; a failed wait raises with its reason."""
    name = record["name"]
    if record.get("ok") is False:
        raise ValueError(f"{name} {failure}: {record.get('error') or 'the launch did not finish'}")
    return f"{verb} {name} ({label(record)})" + (": ready." if record.get("ready") else ".")


def cause(error, name, failure):
    """The reason behind a failed launch, without the record ID and recovery advice around it."""
    return ValueError(f"{name} {failure}: {error.__cause__ or error}")


def cli_run(root=None, *, name, task=None, task_file=None, harness="codex", model=None, wait=False,
            wait_timeout=60, json=False):
    try:
        record = run(root, name=name, task=task, task_file=task_file, harness=harness, model=model,
                     wait=wait, wait_timeout=wait_timeout)
    except RuntimeError as error:
        if json:
            raise
        raise cause(error, name, "did not start") from error
    return record if json else outcome(record, "Started", "did not start")


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
