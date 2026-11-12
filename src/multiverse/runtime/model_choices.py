"""Universe model choices, shared by launches, one-shot runs and catalog readers."""
from dataclasses import asdict, replace
from pathlib import Path

from ..harnesses.types import ModelCatalog, ModelCheck, ModelInfo
from . import effects


class ModelChoicesError(ValueError):
    """A safe configuration/catalog diagnostic, without native file contents."""


def restricted(adapter):
    return 'models' in (getattr(adapter, 'settings', None) or {})


def _problem(root, adapter, reason):
    path = Path(root) / 'harnesses' / adapter.name / 'harness.json'
    return ModelChoicesError(f'{path}: settings.models: {reason}. Fix or remove this setting')


def _catalog(adapter, environment):
    if not callable(getattr(adapter, 'models', None)):
        return None
    value = effects.call(adapter, 'models', environment=environment)
    if not isinstance(value, ModelCatalog) or any(not isinstance(item, ModelInfo) for item in value.models):
        raise ModelChoicesError(f'Harness {adapter.name} returned an invalid model catalog')
    return value


def _check(adapter, model, effort, environment):
    if not callable(getattr(adapter, 'check_model', None)):
        return ModelCheck(ok=effort is None, model=model, effort=effort,
                          reason=None if effort is None else f'Harness {adapter.name} cannot validate effort')
    value = effects.call(adapter, 'check_model', model, effort=effort, environment=environment)
    if not isinstance(value, ModelCheck) or type(value.ok) is not bool or type(value.verified) is not bool:
        return ModelCheck(ok=False, reason=f'Harness {adapter.name} returned an invalid model check')
    return value


def _policy(root, adapter, environment, full=None):
    """Resolve every entry before permitting any choice; stale entries never disappear silently."""
    setting = adapter.settings['models']
    if not isinstance(setting, dict) or not setting:
        raise _problem(root, adapter, 'expected a nonempty object of model IDs or aliases to options')
    full = _catalog(adapter, environment) if full is None else full
    if full is None or not callable(getattr(adapter, 'check_model', None)):
        raise _problem(root, adapter, 'this harness has no model catalog and checker')
    available = {item.id: item for item in full.models}
    allowed, effort_unset = {}, {}
    for name, options in setting.items():
        if not isinstance(name, str) or not name.strip() or '\0' in name:
            raise _problem(root, adapter, 'each model must have a nonempty name without NUL')
        if not isinstance(options, dict) or set(options) - {'efforts'}:
            raise _problem(root, adapter, f'{name!r} must be an object with only the optional efforts list')
        selected = _check(adapter, name, None, environment)
        if selected.ok is not True or selected.verified is not True or selected.model not in available:
            raise _problem(root, adapter, f'model {name!r} is not in the available catalog')
        if selected.model in allowed:
            raise _problem(root, adapter, f'{name!r} names a model already listed as {selected.model!r}')
        item = available[selected.model]
        levels = options.get('efforts', item.efforts)
        if 'efforts' in options:
            if (not isinstance(levels, list) or not levels or any(not isinstance(level, str) for level in levels)
                    or len(set(levels)) != len(levels)):
                raise _problem(root, adapter, f'{name!r}.efforts must be a nonempty list of distinct level names')
            obsolete = [level for level in levels if level not in item.efforts]
            if obsolete:
                raise _problem(root, adapter, f'{name!r} no longer offers effort {", ".join(map(repr, obsolete))}')
        levels = tuple(level for level in item.efforts if level in levels)
        allowed[item.id] = replace(item, efforts=levels,
                                   default_effort=item.default_effort if item.default_effort in levels else None)
        effort_unset[item.id] = 'efforts' not in options
    return replace(full, models=tuple(allowed[item.id] for item in full.models if item.id in allowed),
                   free_form=False), effort_unset


def catalog(root, adapter, *, environment):
    """The adapter's catalog in table order, narrowed only when this harness has a subset."""
    if restricted(adapter):
        return _policy(root, adapter, environment)[0]
    found = _catalog(adapter, environment)
    if found is None:
        raise ModelChoicesError(f'Harness {adapter.name} does not provide its models')
    return found


def validate_settings(root, adapter, *, environment):
    """Check a configured subset and model default without requiring an omitted effort to be chosen."""
    if not restricted(adapter):
        return
    found, _ = _policy(root, adapter, environment)
    if adapter.default_model:
        default = _check(adapter, adapter.default_model, None, environment)
        if not default.ok or not default.verified or default.model not in {item.id for item in found.models}:
            raise _problem(root, adapter, f'configured settings.model {adapter.default_model!r} is outside this subset')


def check(root, adapter, model, *, effort=None, environment):
    """Validate requested choices, without claiming native defaults or served settings are observed."""
    narrowed = restricted(adapter)
    try:
        policy = _policy(root, adapter, environment) if narrowed else None
    except ModelChoicesError as error:
        return ModelCheck(ok=False, model=model, effort=effort, verified=True, reason=str(error))
    if not model:
        if narrowed:
            names = ', '.join(item.id for item in policy[0].models)
            return ModelCheck(ok=False, reason=f'Choose a model from {names}; the native default cannot be verified '
                              f'against {Path(root) / "harnesses" / adapter.name / "harness.json"}')
        if effort is None:
            return ModelCheck(ok=True)
    found = _check(adapter, model, effort, environment)
    if not narrowed or not found.ok:
        return found
    chosen, effort_unset = policy
    item = next((item for item in chosen.models if item.id == found.model), None)
    path = Path(root) / 'harnesses' / adapter.name / 'harness.json'
    if not found.verified or item is None:
        return replace(found, ok=False, reason=f'Model {model!r} is not allowed by {path}; choose a model from '
                       + ', '.join(item.id for item in chosen.models))
    if effort is None and not effort_unset[item.id]:
        return replace(found, ok=False, effort=None,
                       reason=f'Choose an effort for {item.id} from {", ".join(item.efforts)}; '
                       f'the native default cannot be verified against {path}')
    if effort is not None and effort not in item.efforts:
        return replace(found, ok=False, reason=f'Effort {effort!r} for {item.id} is not allowed by {path}; '
                       f'choose an effort from {", ".join(item.efforts)}')
    # A default_effort from the table is not an observation of an omitted native effort.
    return replace(found, effort=effort)


def for_launch(root, harness):
    """Private dashboard projection; public catalog types keep their existing shape."""
    from .. import harnesses
    from .run_environment import launch
    adapter = harnesses.load(root, harness)
    environment = launch(adapter)
    full = _catalog(adapter, environment)
    narrowed = restricted(adapter)
    if narrowed:
        found, effort_unset = _policy(root, adapter, environment, full)
    elif full is None:
        return None
    else:
        found, effort_unset = full, {item.id: True for item in full.models}
    configured = _check(adapter, adapter.default_model, None, environment) if adapter.default_model else None
    default_model = (configured.model if configured and configured.ok and configured.verified
                     and any(item.id == configured.model for item in found.models) else None)
    # A configured model can supply the model while the person still has to choose an effort.
    unset = not narrowed or default_model is not None
    return {'models': [{**asdict(item), 'effort_unset': effort_unset[item.id]} for item in found.models],
            'free_form': found.free_form, 'narrowed': narrowed, 'unset': unset,
            'default_model': default_model, 'problems': []}


def display_names(root, harness):
    """Labels for recorded choices, independent of the allowed subset and native running settings."""
    from .. import harnesses
    from .run_environment import launch
    adapter = harnesses.load(root, harness)
    environment = launch(adapter)
    names = {}
    try:
        found = _catalog(adapter, environment)
        names.update({key: item.name for item in found.models for key in (item.id, *item.aliases)} if found else {})
        if callable(getattr(adapter, 'model_names', None)):
            names.update(effects.call(adapter, 'model_names', environment=environment))
    except (OSError, ValueError, TypeError, KeyError):
        pass  # Explicit display settings remain usable when table metadata cannot be read.
    def labels(values):
        return {key: value for key, value in values.items() if isinstance(key, str) and isinstance(value, str)
                and value.strip() and not any(char in value for char in ('\0', '\n', '\r'))}

    return labels(names), labels((getattr(adapter, 'settings', None) or {}).get('model_names', {}))


def recorded_name(root, properties, cache):
    """Read metadata once per scope/harness in this listing; a broken table never damages an agent row."""
    model, harness = properties.get('model'), properties.get('harness')
    if not isinstance(model, str) or not model or not isinstance(harness, str) or not harness:
        return None
    key = (root, harness)
    if key not in cache:
        try:
            cache[key] = display_names(root, harness)
        except Exception:
            cache[key] = ({}, {})
    names, overrides = cache[key]
    return overrides.get(model, names.get(model.strip()))


def require_command(root, adapter, model, effort, environment, command):
    """Final preparation and automatic fallback must keep the admitted canonical choice."""
    if not restricted(adapter):
        return
    for candidate in (command, command.fallback):
        if candidate is None:
            continue
        actual = {**environment, **dict.fromkeys(candidate.unset_env, None), **candidate.environment}
        result = check(root, adapter, model, effort=effort, environment=actual)
        if result.ok is not True:
            raise ValueError(result.reason or 'The prepared command is outside the model subset')
        if result.model != model:
            raise ValueError('The prepared command changes the resolved model; choose its allowed canonical ID')
