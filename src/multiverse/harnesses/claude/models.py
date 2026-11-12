"""Claude's models, from mv2's table of the catalog built into Claude Code.

`models.json` is written by `scripts/gen_claude_models.py` at each Claude Code version check; nothing here reads the
binary or Claude's own caches. A model is named for a provider, which Claude Code picks from its environment, and an
alias means a different model there: `sonnet` is Sonnet 5.5 on the Anthropic API and Sonnet 4.5 on Amazon Bedrock. A
cloud gateway, which Claude picks from its login state rather than a variable, isn't visible here.
"""
from functools import lru_cache
import json
from pathlib import Path

from ..types import ModelCatalog, ModelCheck, ModelInfo

TABLE = Path(__file__).with_name('models.json')
SUFFIX = '[1m]'
# Claude Code's own order (2.1.312): the first of these set to 1, true, yes or on, in any case, else the Anthropic API.
CHOICE = (('CLAUDE_CODE_USE_BEDROCK', 'bedrock'), ('CLAUDE_CODE_USE_FOUNDRY', 'foundry'),
          ('CLAUDE_CODE_USE_ANTHROPIC_AWS', 'anthropic_aws'),
          ('CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD', 'anthropic_google_cloud'),
          ('CLAUDE_CODE_USE_MANTLE', 'mantle'), ('CLAUDE_CODE_USE_VERTEX', 'vertex'))
TRUE = {'1', 'true', 'yes', 'on'}
PLACES = {'first_party': 'the Anthropic API', 'bedrock': 'Amazon Bedrock', 'vertex': 'Google Vertex AI',
          'foundry': 'Microsoft Foundry', 'anthropic_aws': 'Claude Platform on AWS',
          'anthropic_google_cloud': 'Claude Platform on Google Cloud', 'mantle': 'Amazon Bedrock (Mantle)'}


@lru_cache(maxsize=1)
def table():
    return json.loads(TABLE.read_text())


def provider(environment):
    """The table's key for the provider a Claude launched with `environment` uses. Core passes what a launch from
    this process would see (the harness's settings env over this process's environment); this never reads
    os.environ itself."""
    for name, key in CHOICE:
        value = environment.get(name)
        if value is not None and str(value).strip().lower() in TRUE:
            return key
    return 'first_party'


def _aliases(data, key):
    """Each alias to the table ID it means on this provider."""
    return {name: alias['per_provider'].get(key, alias['default']) for name, alias in data['aliases'].items()}


def catalog(environment):
    """The models offered on this launch's provider, each under that provider's ID and aliases."""
    data, key = table(), provider(environment)
    aliases = _aliases(data, key)
    return ModelCatalog(models=tuple(
        ModelInfo(id=item['provider_ids'][key], name=item['name'], family=item['family'],
                  aliases=tuple(sorted(name for name, target in aliases.items() if target == item['id'])),
                  efforts=tuple(item['efforts']), default_effort=item['default_effort'], inputs=tuple(item['inputs']),
                  context_window=item['context_window'], long_context_window=item['long_context_window'],
                  max_output_tokens=item['max_output_tokens'], default=item['default'], hidden=key not in item['picker'])
        for item in data['models'] if item['provider_ids'].get(key)),
        free_form=True, source='table', version=data['claude_code'], provider=key.replace('_', '-'))


def names(environment):
    """Display labels for the model spellings mv2 may have recorded: each model's table ID and every provider's ID,
    whatever the provider is now, and this launch's provider's aliases, plus each spelling with "[1m]" where the
    model takes it, labelled "NAME [1m]". A label names the recorded choice, not the model Claude served: Claude
    runs the old Opus 4 and 4.1 IDs as the current Opus, and settings or policy can swap a model, none of which is
    read here. Keys are the table's spellings, lower case; Claude matches a spelling trimmed and in any case."""
    data, key = table(), provider(environment)
    labels = {}

    def add(spelling, item):
        labels[spelling] = item['name']
        if item['long_context_window']:
            labels[spelling + SUFFIX] = f"{item['name']} {SUFFIX}"

    for item in data['models']:
        for spelling in (item['id'], *item['provider_ids'].values()):
            if spelling:
                add(spelling, item)
    known = {item['id']: item for item in data['models']}
    for alias, target in _aliases(data, key).items():
        item = known.get(target)
        if item is not None and item['provider_ids'].get(key):  # as check(): an alias not offered here names nothing
            add(alias, item)
    return labels


def takes_effort(model, effort):
    """Whether `model`, by any provider's ID and with or without the long-context suffix, takes `effort` by the
    table: Claude saves an /effort level even for a model that doesn't use it, so that alone proves nothing."""
    name = (model or '').strip()
    name = name[:-len(SUFFIX)] if name.endswith(SUFFIX) else name
    item = next((item for item in table()['models'] if name == item['id'] or name in item['provider_ids'].values()), None)
    return item is not None and effort in item['efforts']


def check(model, effort, environment):
    """Whether a launch with `environment` can use `model` at `effort`: Claude Code never refuses an effort itself
    (it falls back to the model's default), so this has to."""
    data, key = table(), provider(environment)
    text = (model or '').strip()
    if not text:
        return ModelCheck(ok=False, reason='No model given')
    long = text.endswith(SUFFIX)
    name = text[:-len(SUFFIX)] if long else text
    target = _aliases(data, key).get(name)
    item = next((item for item in data['models']
                 if (item['id'] == target if target else name in (item['id'], item['provider_ids'].get(key)))), None)
    if item is None:
        return ModelCheck(ok=True, model=text, effort=effort, reason=(
            f"{text} isn't in mv2's table of Claude Code {data['claude_code']}'s models; Claude decides"))
    ident = item['provider_ids'].get(key)
    if not ident:
        return ModelCheck(ok=False, reason=f"{item['id']} isn't offered on {PLACES[key]}")
    resolved = ident + SUFFIX if long else ident
    if long and not item['long_context_window']:
        return ModelCheck(ok=False, model=resolved, reason=f"{item['id']} does not take the {SUFFIX} suffix")
    if effort is not None and effort not in item['efforts']:
        reason = (f"{item['id']} does not take effort {effort}" if item['efforts']
                  else f"{item['id']} does not take an effort")
        return ModelCheck(ok=False, model=resolved, reason=reason)
    return ModelCheck(ok=True, model=resolved, effort=effort if effort is not None else item['default_effort'],
                      verified=True)
