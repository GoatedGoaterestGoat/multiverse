"""Antigravity CLI release model metadata, independent of login state, caches and live services.

The table reflects the checked Antigravity CLI release's models and effort levels. Which ones an account may run
depends on its plan, so a launch also checks the model against `agy models` (see `account_models`). Antigravity
accepts both base IDs with `--effort` (e.g. `gemini-4-hydrogen` + `--effort medium`) and effort-suffixed spellings
(e.g. `gemini-4-hydrogen-medium`). Nothing here spawns `agy` or reads user caches.
"""
from functools import lru_cache
import json
from pathlib import Path

from ..types import ModelCatalog, ModelCheck, ModelInfo

TABLE = Path(__file__).with_name('models.json')


@lru_cache(maxsize=1)
def table() -> dict:
    return json.loads(TABLE.read_text())


@lru_cache(maxsize=1)
def _suffixed_index() -> dict[str, tuple[dict, str]]:
    """Map every known `<base>-<effort>` spelling to `(model_entry, effort)`."""
    index = {}
    for item in table()['models']:
        for level in item['efforts']:
            index[f"{item['id']}-{level}"] = (item, level)
    return index


def catalog(environment) -> ModelCatalog:
    """Checked Antigravity CLI model catalog; ignores os.environ and account state."""
    data = table()
    return ModelCatalog(
        models=tuple(
            ModelInfo(
                id=item['id'],
                name=item['name'],
                family=item.get('family'),
                efforts=tuple(item['efforts']),
                default_effort=item.get('default_effort'),
                inputs=tuple(item.get('inputs') or ('text',)),
                context_window=item.get('context_window'),
                default=bool(item.get('default')),
                hidden=bool(item.get('hidden')),
            )
            for item in data['models']
        ),
        free_form=True,
        source='table',
        version=data['agy'],
    )


def names(environment) -> dict[str, str]:
    """Human display labels for base IDs and effort-suffixed spellings recorded by Antigravity."""
    labels = {}
    for item in table()['models']:
        labels[item['id']] = item['name']
        for level in item['efforts']:
            labels[f"{item['id']}-{level}"] = f"{item['name']} ({level})"
    return labels


def split_model_effort(model: str | None) -> tuple[str | None, str | None]:
    """Split a known Antigravity `<base>-<effort>` transcript model into `(base_id, effort)`.

    Base IDs and unknown spellings keep `effort=None` so unverified names are never altered.
    """
    if not isinstance(model, str):
        return None, None
    text = model.strip()
    if not text or any(c in text for c in ('\0', '\n', '\r')):
        return None, None
    found = _suffixed_index().get(text)
    if found is not None:
        item, level = found
        return item['id'], level
    return text, None


def check(model: str | None, effort: str | None, environment) -> ModelCheck:
    """Validate a requested model and optional session effort against the checked Antigravity table."""
    text = (model or '').strip()
    if not text:
        return ModelCheck(ok=False, reason='No model given')
    data = table()
    item = next((entry for entry in data['models'] if entry['id'] == text), None)
    if item is not None:
        if effort is not None and effort not in item['efforts']:
            reason = (
                f"{text} does not take effort {effort}" if item['efforts']
                else f"{text} does not take an effort"
            )
            return ModelCheck(ok=False, model=text, verified=True, reason=reason)
        return ModelCheck(
            ok=True,
            model=text,
            effort=effort if effort is not None else item.get('default_effort'),
            verified=True,
        )

    suffixed = _suffixed_index().get(text)
    if suffixed is not None:
        base_item, embedded = suffixed
        base_id = base_item['id']
        if effort is not None:
            return ModelCheck(
                ok=False,
                model=text,
                verified=True,
                reason=f"{text} already sets effort {embedded}; pass {base_id} with --effort {effort} instead",
            )
        return ModelCheck(
            ok=True,
            model=text,
            effort=embedded,
            verified=False,
            reason=f"{text} embeds effort {embedded}; use {base_id} with --effort {embedded} when a subset is set",
        )

    return ModelCheck(
        ok=True,
        model=text,
        effort=effort,
        verified=False,
        reason=f"{text} isn't in mv2's table of Antigravity CLI {data['agy']}'s models; Antigravity decides",
    )
