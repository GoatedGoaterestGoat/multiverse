#!/usr/bin/env python3
"""Build the private run catalogue from a checked official Codex release source.

This does not authorize a new version. Retest the settings and native requests
before adding it to harnesses/codex/run.py's checked versions.
"""
import argparse
import hashlib
import json
from pathlib import Path


def generate(source, version):
    path = source / 'codex-rs/models-manager/models.json'
    raw = path.read_bytes()
    models = json.loads(raw)['models']
    if not isinstance(models, list) or not models:
        raise ValueError('Unknown native model catalogue')
    result = []
    for model in models:
        if not isinstance(model.get('slug'), str) or not model['slug']:
            raise ValueError('A native model needs its exact slug')
        # Preserve modality, context, compaction identity and wire format. The
        # caller supplies instructions; native capability declarations are gone.
        messages = model.get('model_messages') or {}
        result.append({**model, 'model_messages': {'instructions_template': messages.get('instructions_template')},
                       'experimental_supported_tools': [],
                       'tool_mode': 'direct', 'shell_type': 'disabled', 'apply_patch_tool_type': None,
                       'supports_search_tool': False, 'supports_experimental_context': False,
                       'node_repl_disabled': True, 'include_skills_usage_instructions': False,
                       'include_plugin_usage_instructions': False, 'include_apps_usage_instructions': False})
    return {'codex': version, 'source': str(path.relative_to(source)),
            'default_instructions': (source / 'codex-rs/models-manager/prompt.md').read_text(),
            'sha256': hashlib.sha256(raw).hexdigest(), 'models': result}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    target = Path(__file__).resolve().parents[1] / 'src/multiverse/harnesses/codex/run_models.json'
    target.write_text(json.dumps(generate(args.source, args.version), indent=2) + '\n')
