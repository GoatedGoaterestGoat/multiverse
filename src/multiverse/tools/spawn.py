"""Launch an agent in a tagged tmux pane."""
from pathlib import Path
from .. import agents, universe
from ._common import add_wait

def configure(parser):
    parser.add_argument("--name", required=True)
    parser.add_argument("--harness", default="codex", help="Configured harness name (default: codex)")
    task = parser.add_mutually_exclusive_group()
    task.add_argument("--task", "--prompt", help="Initial prompt sent to the new agent")
    task.add_argument("--task-file", type=Path)
    parser.add_argument("--model")
    add_wait(parser)


def run(root=None, *, name, task=None, task_file=None, harness="codex", model=None, wait=False, wait_timeout=60):
    root = universe.resolve(root)
    prompt = Path(task_file).read_text() if task_file is not None else task
    return agents.start(root, name, prompt, harness, model, wait=wait, wait_timeout=wait_timeout)


if __name__ == "__main__":
    from ._common import main
    main(configure, run)
