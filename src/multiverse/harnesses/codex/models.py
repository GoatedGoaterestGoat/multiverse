"""Release model metadata, independent of login state, caches and native services.

The table describes the bundled catalog, not account availability or a custom
provider's configuration. Codex accepts free-form IDs; unknown IDs remain
unverified. scripts/gen_codex_models.py refreshes this at each version check.
"""
from functools import lru_cache
import json
from pathlib import Path

from ..types import ModelCatalog, ModelCheck, ModelInfo


@lru_cache(maxsize=1)
def table():
    return json.loads(Path(__file__).with_name('models.json').read_text())


def catalog(environment):
    # No environment variable identifies Codex's effective provider/auth mode:
    # config profiles and login state can override them. Do not guess from keys.
    data = table()
    return ModelCatalog(models=tuple(ModelInfo(
        id=item['id'], name=item['name'], efforts=tuple(item['efforts']),
        default_effort=item['default_effort'], inputs=tuple(item['inputs']),
        context_window=item['context_window'], hidden=item['hidden'], default=item['default'])
        for item in data['models']), free_form=True, source='table', version=data['codex'])


def check(model, effort, environment):
    text = (model or '').strip()
    if not text:
        return ModelCheck(ok=False, reason='No model given')
    item = next((item for item in table()['models'] if item['id'] == text), None)
    if item is None:
        return ModelCheck(ok=True, model=text, effort=effort,
                          reason=f"{text} isn't in mv2's table of Codex {table()['codex']}'s models; Codex decides")
    if effort is not None and effort not in item['efforts']:
        return ModelCheck(ok=False, model=text, verified=True,
                          reason=f'{text} does not take effort {effort}')
    return ModelCheck(ok=True, model=text, effort=effort if effort is not None else item['default_effort'],
                      verified=True)
