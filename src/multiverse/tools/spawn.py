"""Launch an agent in a tagged tmux pane."""
from pathlib import Path
from ..display import cause, outcome
from ..runtime import lifecycle
from .. import universe
from ._common import add_wait


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
    return lifecycle.start(root, name, prompt, harness, model, wait=wait, wait_timeout=wait_timeout)


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
