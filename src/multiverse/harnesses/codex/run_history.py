"""Materialize selected model history as a private native rollout.

The reader already applies rollbacks. Metadata is used for provenance, never
replayed as a second rollback or as saved tool/session configuration.
"""
import base64
from copy import deepcopy
from dataclasses import dataclass
import json

from ..types import RunFailure, RunImage, RunMessage, RunRow, RunText
from .transcripts import mapping, model_member

FORMAT = 'codex-jsonl'


@dataclass(frozen=True)
class Replay:
    content: bytes
    lost: tuple[str, ...]
    accounts: frozenset[str]
    reasoning_accounts: frozenset[str] = frozenset()

    def check_login(self, account):
        """Native account metadata is sufficient; no credential bytes are requested."""
        current = mapping(account.get('workspaceRouting')).get('chatgptAccountId')
        if self.accounts and self.accounts != {current}:
            raise RunFailure('unavailable', 'The copied encrypted history belongs to an unverified or different Codex login')


def _built(message):
    blocks = []
    for block in message.blocks:
        if isinstance(block, RunText):
            blocks.append({'type': 'input_text' if message.role == 'user' else 'output_text', 'text': block.text})
        elif isinstance(block, RunImage) and message.role == 'user':
            blocks.append({'type': 'input_image', 'image_url': 'data:' + block.media_type + ';base64,' +
                           base64.b64encode(block.data).decode('ascii')})
        else:
            raise RunFailure('invalid_request', 'Unsupported Codex message block')
    return {'type': 'message', 'role': message.role, 'content': blocks}


def session(messages, *, model, session_id, cwd, at, system, own_conversation=False, drop_reasoning=False):
    rows, lost, accounts, reasoning_accounts = [], set(), set(), set()
    writing_model, writing_account = None, None
    built_prefix = False

    def item(payload, metadata):
        if not isinstance(payload, dict) or not isinstance(payload.get('type'), str):
            raise RunFailure('unavailable', 'A copied Codex model item cannot be read')
        kind = payload['type']
        if kind == 'compaction_summary':
            kind = 'compaction'
            payload = {**payload, 'type': kind}
        member = model_member('response_item', kind, payload.get('role'), metadata)
        if member is None:
            raise RunFailure('unavailable', 'A copied Codex model item has unknown history membership')
        if not member:
            return None
        value = deepcopy(payload)
        if kind == 'additional_tools':
            if value.get('tools'):
                lost.add('Historical tool declarations were removed for the tool-free turn')
            return None
        if kind == 'tool_search_output':
            if value.get('tools'):
                lost.add('Historical tool-search declarations were removed for the tool-free turn')
            value['tools'] = []
        encrypted = value.get('encrypted_content')
        if encrypted:
            compaction = kind in ('compaction', 'compaction_summary', 'context_compaction')
            if drop_reasoning and not compaction:
                value.pop('encrypted_content', None)
                lost.add('Encrypted reasoning was omitted because the current login could not verify its source account')
                if kind == 'reasoning':
                    return None
            elif own_conversation:
                pass
            elif writing_model == model and isinstance(writing_account, str) and writing_account:
                accounts.add(writing_account)
                if not compaction:
                    reasoning_accounts.add(writing_account)
            elif compaction:
                raise RunFailure('unavailable', 'Encrypted Codex compaction needs its writing model and login provenance')
            else:
                value.pop('encrypted_content', None)
                lost.add('Encrypted reasoning was omitted because its writing model or login was unverified')
                if kind == 'reasoning':
                    return None
        return value

    for message in messages:
        if isinstance(message, RunMessage):
            rows.append({'type': 'response_item', 'payload': _built(message)})
            built_prefix = True
            continue
        if not isinstance(message, RunRow) or message.format != FORMAT or not isinstance(message.data, dict):
            raise RunFailure('invalid_request', 'A native Codex row needs the codex-jsonl format and its body')
        row = message.data
        outer, payload = row.get('type'), mapping(row.get('payload'))
        if outer == 'session_meta':
            writing_account = payload.get('creator_account_id')
            writing_model = None
            continue
        if outer == 'turn_context':
            writing_model = payload.get('model')
            continue
        # Settings events affect the next turn, not the provenance of items
        # still arriving from the current turn. Only turn_context sets it.
        if outer == 'compacted':
            if built_prefix:
                lost.add('Messages before the Codex compaction checkpoint were replaced by its history')
                built_prefix = False
            replacement = payload.get('replacement_history')
            metadata = payload.get('replacement_history_metadata')
            if (not isinstance(replacement, list) or metadata is not None and
                    (not isinstance(metadata, list) or len(metadata) != len(replacement))):
                raise RunFailure('unavailable', 'The Codex compaction does not contain a complete replacement history')
            accounts.clear()
            reasoning_accounts.clear()
            kept, annotations = [], []
            for index, entry in enumerate(replacement):
                annotation = mapping(metadata[index]) if metadata is not None else {}
                value = item(entry, annotation)
                if value is not None:
                    kept.append(value)
                    annotations.append(annotation)
            # A checkpoint resets earlier selected history, as native replay does.
            # Reviewer state can contain a second history; it is not this model's
            # conversation and cannot restore tools in the private session.
            rows = [{'type': 'compacted', 'payload': {'message': payload.get('message', ''),
                    'replacement_history': kept, 'replacement_history_metadata': annotations}}]
            continue
        if outer == 'response_item':
            annotation = mapping(row.get('metadata'))
            value = item(payload, annotation)
            if value is not None:
                rows.append({'type': 'response_item', 'payload': value, 'metadata': deepcopy(annotation)})
            continue
        if outer == 'inter_agent_communication':
            rows.append({'type': outer, 'payload': deepcopy(payload)})
            continue
        if model_member(outer, payload.get('type'), payload.get('role'), mapping(row.get('metadata'))) is None:
            raise RunFailure('unavailable', 'A copied Codex row has unknown history membership')
        # UI echoes, rollback markers, world state and dynamic-tool configuration
        # are not model messages. The supplied row order has already selected history.
    if not rows:
        return Replay(b'', tuple(sorted(lost)), frozenset())
    from .run import catalogue
    header = {'type': 'session_meta', 'payload': {
        'id': session_id, 'session_id': session_id, 'timestamp': at, 'cwd': str(cwd),
        'runtime_workspace_roots': [], 'originator': 'multiverse-run', 'cli_version': catalogue()['codex'],
        'source': 'exec', 'model_provider': 'openai', 'base_instructions': {'text': system},
        'dynamic_tools': [], 'selected_capability_roots': [], 'history_mode': 'paginated'}}
    content = ''.join(json.dumps({'timestamp': at, 'ordinal': index, **row}, ensure_ascii=False) + '\n'
                      for index, row in enumerate([header, *rows])).encode('utf-8')
    return Replay(content, tuple(sorted(lost)), frozenset(accounts), frozenset(reasoning_accounts))
