#!/usr/bin/env python3
"""Extract public model metadata from a Codex release's bundled models.json.

Usage: python scripts/gen_codex_models.py CODEX_SOURCE --version 0.164.0
Never accepts a home/cache file: CODEX_SOURCE is a release source checkout.
"""
import argparse
import hashlib
import json
from pathlib import Path


def generate(source, version):
    path = source / 'codex-rs/models-manager/models.json'
    raw = path.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get('models'), list) or not data['models']:
        raise ValueError('Unknown Codex model catalog shape')
    models = []
    for item in data['models']:
        for key in ('slug', 'display_name', 'visibility'):
            if not isinstance(item[key], str) or not item[key]:
                raise ValueError(f'Invalid model {key}')
        if item['visibility'] not in ('list', 'hide', 'hidden'):
            raise ValueError('Unknown model visibility')
        if type(item['priority']) is not int or type(item['supported_in_api']) is not bool:
            raise ValueError('Unknown model availability shape')
        efforts = [level['effort'] for level in item['supported_reasoning_levels']]
        if not all(isinstance(effort, str) and effort for effort in efforts):
            raise ValueError('Unknown reasoning effort shape')
        default = item['default_reasoning_level']
        if default is not None and default not in efforts:
            raise ValueError('Default reasoning effort is unsupported')
        inputs = item['input_modalities']
        if not isinstance(inputs, list) or not all(value in ('text', 'image') for value in inputs):
            raise ValueError('Unknown input modality')
        window = item['context_window']
        if window is not None and (type(window) is not int or window <= 0):
            raise ValueError('Invalid context window')
        models.append(dict(id=item['slug'], name=item['display_name'], efforts=efforts,
                           default_effort=default, inputs=inputs, context_window=window,
                           hidden=item['visibility'] != 'list', priority=item['priority'],
                           supported_in_api=item['supported_in_api']))
    if len({item['id'] for item in models}) != len(models):
        raise ValueError('Duplicate model ID')
    models.sort(key=lambda item: item['priority'])
    default = next((item['id'] for item in models if not item['hidden']), models[0]['id'])
    for item in models:
        item['default'] = item['id'] == default
        del item['priority']
    return dict(codex=version, source='codex-rs/models-manager/models.json',
                sha256=hashlib.sha256(raw).hexdigest(), models=models)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    target = Path(__file__).resolve().parents[1] / 'src/multiverse/harnesses/codex/models.json'
    target.write_text(json.dumps(generate(args.source, args.version), indent=2) + '\n')
