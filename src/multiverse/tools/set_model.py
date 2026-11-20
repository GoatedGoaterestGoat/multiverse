"""Save an agent's model and effort for its next resume, without restarting it."""
from ..runtime import lifecycle
from ..display import clean


def configure(parser):
    parser.add_argument('agent', help='Agent name or ID')
    parser.add_argument('--model', help='Model ID or alias; default clears the recorded model')
    parser.add_argument('--effort', help='Effort level; default clears the recorded effort')
    parser.add_argument('--json', action='store_true', help='Print the saved choice and notices as JSON')


def run(root=None, *, agent, model=None, effort=None, json=False):
    """Save a choice: None keeps the field, an empty string or default clears it."""
    return lifecycle.set_model(root, agent, model='' if model == 'default' else model,
                               effort='' if effort == 'default' else effort)


def cli_run(root=None, *, agent, model=None, effort=None, json=False):
    result = run(root, agent=agent, model=model, effort=effort)
    if json:
        return result
    selected = clean(result['model']) if result['model'] else 'the harness default'
    level = clean(result['effort']) if result['effort'] else 'the harness default'
    return '\n'.join([f"Saved model for {clean(result['name'])}: {selected} (effort: {level}).", *result['notices']])


if __name__ == '__main__':
    from ._common import main
    main(configure, run)
