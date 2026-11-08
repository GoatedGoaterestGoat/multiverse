"""Typed responses for the blocking requests offered by the checked Codex TUI.

This codec has no transport, storage or caller authority. The adapter and relay
both use it; a native request's offered values are never supplied by an answer.
"""
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json


VERSION = (0, 166, 0)
DETAIL_BYTES = 64 << 10
RESPONSE_BYTES = 128 << 10  # Typed wrappers and offered native values can expand a bounded answer file.
METHODS = {
    'item/commandExecution/requestApproval': 'command',
    'item/fileChange/requestApproval': 'file_change',
    'item/permissions/requestApproval': 'permissions',
    'item/tool/requestUserInput': 'user_input',
    'mcpServer/elicitation/request': 'mcp',
}


def text(value, *, empty=False):
    if not isinstance(value, str) or '\0' in value or not empty and not value:
        raise ValueError('The native prompt contains invalid text; answer it in the terminal')
    return value


def object_value(value):
    if not isinstance(value, dict):
        raise ValueError('The native prompt has an unsupported shape; answer it in the terminal')
    return value


def bounded(value, *, limit=DETAIL_BYTES):
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError):
        raise ValueError('The native prompt cannot be represented safely') from None
    if len(raw) > limit:
        raise ValueError('The native prompt exceeds the 64 KiB answerable detail limit' if limit == DETAIL_BYTES
                         else 'The native answer exceeds the 128 KiB response limit')
    return deepcopy(value)


def public_answer(value):
    """Keep whole nonsecret values that fit, with an explicit truncation marker."""
    if value is None:
        return None
    try:
        return bounded(value)
    except ValueError:
        result = {'truncated': True}
        for field in ('answers', 'notes'):
            kept = {}
            for key, values in value.get(field, {}).items():
                trial = {**result, field: {**kept, key: values}}
                try:
                    bounded(trial)
                except ValueError:
                    continue
                kept[key] = values
            if kept:
                result[field] = kept
        return result


def choice(identifier, label, *, saves_rule=False):
    return {'id': identifier, 'label': label, 'saves_rule': saves_rule, 'takes_message': False}


def question(identifier, prompt, *, header='', options=(), free_text=True, secret=False, required=True, takes_note=False):
    if any(type(value) is not bool for value in (secret, required, free_text, takes_note)):
        raise ValueError('The native question has invalid flags')
    return {'id': text(identifier), 'question': text(prompt, empty=True), 'header': text(header, empty=True),
            'options': tuple(options), 'multiple': False, 'free_text': free_text,
            'secret': secret, 'required': required, 'takes_note': takes_note}


@dataclass
class Offer:
    method: str
    params: dict
    kind: str
    summary: str
    choices: list = field(default_factory=list)
    questions: list = field(default_factory=list)
    results: dict = field(default_factory=dict)
    fields: dict = field(default_factory=dict)
    answerable: bool = True
    reason: str | None = None
    detail: dict = field(default_factory=dict)

    def public(self, identifier):
        stamp = self.params.get('startedAtMs')
        try:
            at = datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat() if type(stamp) is int else None
        except (OverflowError, OSError, ValueError):
            at = None
        return {'id': identifier, 'kind': self.kind, 'asked_at': at, 'tool': METHODS[self.method],
                'summary': self.summary[:500], 'detail': deepcopy(self.detail),
                'choices': tuple(self.choices), 'questions': tuple(self.questions),
                'answerable': self.answerable, 'reason': self.reason, 'subagent': None}

    def response(self, answer):
        """Build only a typed offered result; never accept caller-provided native JSON."""
        if not self.answerable:
            raise ValueError(self.reason or 'Answer this prompt in the terminal')
        answer = object_value(answer)
        selected, values = answer.get('choice'), answer.get('answers', {})
        values = object_value(values)
        notes = object_value(answer.get('notes', {}))
        known = {item['id']: item for item in self.questions}
        if any(key not in known or known[key]['takes_note'] is not True for key in notes):
            raise ValueError('This native question does not offer a note')
        notes = {key: text(value, empty=True).strip() for key, value in notes.items()}
        if selected is not None and any(notes.values()):
            raise ValueError('A decision-only prompt does not take question notes')
        if type(answer.get('allow_rule', False)) is not bool:
            raise ValueError('The answer has an invalid caller permission')
        if answer.get('message') is not None:
            raise ValueError('This Codex prompt does not offer an answer message')
        if selected is not None:
            text(selected)
            offered = next((item for item in self.choices if item['id'] == selected), None)
            if offered is None:
                raise ValueError('This choice is not offered by the native terminal')
            if offered['saves_rule'] and answer.get('allow_rule') is not True:
                raise ValueError('Only the person may choose an answer that saves a rule')
            if selected not in self.results or values:
                raise ValueError('This answer has fields the native prompt does not offer')
            return bounded(self.results[selected], limit=RESPONSE_BYTES)
        elif self.choices and not self.questions:
            raise ValueError('Choose one of the native prompt decisions')
        if set(values) - {item['id'] for item in self.questions}:
            raise ValueError('The answer names an unknown question')
        filled = {}
        for item in self.questions:
            value = values.get(item['id'], ())
            note = notes.get(item['id'], '')
            if not isinstance(value, (list, tuple)) or len(value) > 1:
                raise ValueError('Codex accepts one answer per question')
            if not value:
                if note and not item['options'] and item['free_text']:
                    filled[item['id']] = {'answers': ['user_note: ' + note]}
                    continue
                if item['required']:
                    raise ValueError('A required native question is unanswered')
                continue
            label = text(value[0], empty=True)
            labels = [option[0] for option in item['options']]
            if label not in labels and not item['free_text']:
                raise ValueError('The answer is not an offered question label')
            if self.method == 'item/tool/requestUserInput':
                # The TUI sends typed text as a note. A raw string could be
                # mistaken for an unoffered remembered MCP approval token.
                if label not in labels:
                    if note:
                        raise ValueError('A freeform answer and a note would provide two copies of the same draft')
                    note = label.strip()
                    if not note:
                        raise ValueError('A required native text answer is empty')
                    native = ['None of the above'] if labels else []
                else:
                    native = [label]
                if note:
                    native.append('user_note: ' + note)
                filled[item['id']] = {'answers': native}
            else:
                mapping = self.fields.get(item['id'])
                filled[item['id']] = mapping[label] if mapping is not None else label
        if self.method == 'item/tool/requestUserInput':
            return bounded({'answers': filled}, limit=RESPONSE_BYTES)
        if self.kind == 'form' and selected is None:
            return bounded({'action': 'accept', 'content': filled, '_meta': None}, limit=RESPONSE_BYTES)
        raise ValueError('This prompt has no supported typed response')

    def person_answer(self, result):
        """Return only offered choices/labels, omitting every secret question value."""
        if not isinstance(result, dict):
            return None
        def canonical(value):
            if self.method == 'mcpServer/elicitation/request' and not set(value) - {'action', 'content', '_meta'}:
                return {key: value.get(key) for key in ('action', 'content', '_meta')}
            return value
        matches = [identifier for identifier, expected in self.results.items() if canonical(result) == canonical(expected)]
        if len(matches) == 1:
            return {'choice': matches[0]}
        if self.method == 'item/tool/requestUserInput':
            values = result.get('answers')
            if not isinstance(values, dict):
                return None
            shown, notes = {}, {}
            for item in self.questions:
                if item['secret']:
                    continue
                value = values.get(item['id'])
                labels = value.get('answers') if isinstance(value, dict) else None
                if (isinstance(labels, list) and all(isinstance(label, str) for label in labels)
                        and len(labels) <= 2):
                    offered = {label for label, _ in item['options']}
                    shown[item['id']] = [label for number, label in enumerate(labels)
                                         if not label.startswith('user_note: ') or number == 0 and label in offered]
                    note = next((label[len('user_note: '):] for number, label in enumerate(labels)
                                 if label.startswith('user_note: ') and not (number == 0 and label in offered)), None)
                    if note is not None and item['takes_note']:
                        notes[item['id']] = note
            return public_answer({'answers': shown, **({'notes': notes} if notes else {})})
        if self.kind == 'form' and result.get('action') == 'accept':
            content = result.get('content')
            if not isinstance(content, dict) or set(content) - set(self.fields):
                return None
            shown = {}
            for item in self.questions:
                key = item['id']
                if item['secret'] or key not in content:
                    continue
                value, mapping = content[key], self.fields[key]
                if mapping is None:
                    if isinstance(value, str):
                        shown[key] = [value]
                else:
                    labels = [label for label, actual in mapping.items()
                              if type(actual) is type(value) and actual == value]
                    if len(labels) == 1:
                        shown[key] = labels
            return public_answer({'answers': shown})
        return None

    def question_effect(self, value):
        """Compare complete nonsecret tool answers, independently of public preview truncation."""
        if self.method != 'item/tool/requestUserInput' or not isinstance(value, dict) or set(value) != {'answers'}:
            return None
        values = value['answers']
        if not isinstance(values, dict) or set(values) - {q['id'] for q in self.questions}:
            return None
        for answer in values.values():
            if (not isinstance(answer, dict) or set(answer) != {'answers'}
                    or not isinstance(answer['answers'], list) or len(answer['answers']) > 2
                    or any(not isinstance(item, str) for item in answer['answers'])):
                return None
        secret = {q['id'] for q in self.questions if q['secret']}
        return bounded({'answers': {key: answer for key, answer in values.items() if key not in secret}},
                       limit=RESPONSE_BYTES)


def command(offer):
    decisions = offer.params.get('availableDecisions')
    if not isinstance(decisions, list) or not decisions or len(decisions) > 128:
        raise ValueError('Native command decisions are missing; answer it in the terminal')
    simple = {'accept': ('allow', 'Allow once'), 'acceptForSession': ('allow_session', 'Allow for this session'),
              'decline': ('deny', 'Continue without the command'), 'cancel': ('stop', 'Stop this turn')}
    for number, decision in enumerate(decisions):
        saves = False
        if isinstance(decision, str) and decision in simple:
            identifier, label = simple[decision]
        elif isinstance(decision, dict) and set(decision) == {'acceptWithExecpolicyAmendment'}:
            amendment = object_value(decision['acceptWithExecpolicyAmendment'])
            prefix = object_value(amendment.get('execpolicy_amendment')).get('command')
            if (not isinstance(prefix, list) or not prefix
                    or any(not isinstance(part, str) for part in prefix)):
                raise ValueError('The native command amendment has an unsupported shape')
            if any('\r' in part or '\n' in part for part in prefix):
                continue  # The checked TUI hides these amendments.
            identifier, label, saves = f'allow_rule_{number}', 'Allow and save the offered command rule', True
        elif isinstance(decision, dict) and set(decision) == {'applyNetworkPolicyAmendment'}:
            amendment = object_value(decision['applyNetworkPolicyAmendment'])
            network = object_value(amendment.get('network_policy_amendment'))
            if set(network) != {'host', 'action'} or network.get('action') not in ('allow', 'deny'):
                raise ValueError('The native network amendment has an unsupported shape')
            text(network.get('host'))
            identifier, label, saves = f'network_rule_{number}', 'Save the offered network rule', True
        else:
            raise ValueError('The native command offers an unsupported decision')
        if identifier in offer.results:
            raise ValueError('The native command repeats an ambiguous decision')
        offer.choices.append(choice(identifier, label, saves_rule=saves))
        offer.results[identifier] = {'decision': deepcopy(decision)}
    if not offer.choices:
        raise ValueError('No terminal command decision can be answered here')


def user_input(offer):
    questions = offer.params.get('questions')
    if not isinstance(questions, list) or not 1 <= len(questions) <= 64:
        raise ValueError('The native question list is missing or too large')
    for item in questions:
        item = object_value(item)
        options = item.get('options')
        if options is not None and (not isinstance(options, list) or len(options) > 128):
            raise ValueError('The native question options have an unsupported shape')
        labels = [(text(object_value(value).get('label')), text(value.get('description'), empty=True))
                  for value in options or []]
        if len(set(label for label, _ in labels)) != len(labels):
            raise ValueError('The native question has ambiguous option labels')
        other, secret = item.get('isOther', False), item.get('isSecret', False)
        if type(other) is not bool or type(secret) is not bool:
            raise ValueError('The native question has invalid flags')
        if other and labels:
            if any(label == 'None of the above' for label, _ in labels):
                raise ValueError('The native Other option is ambiguous')
            labels.append(('None of the above', 'Enter another answer'))
        offer.questions.append(question(item.get('id'), item.get('question'), header=item.get('header', ''),
                                        options=labels, free_text=not labels or other, secret=secret, takes_note=True))
    if len({item['id'] for item in offer.questions}) != len(offer.questions):
        raise ValueError('The native question has repeated question IDs')


def permission_profile(value):
    """Validate the native profile shape without expanding or reconstructing it."""
    value = object_value(value)
    if set(value) - {'network', 'fileSystem'}:
        raise ValueError('The native permission profile has unsupported fields')
    network, files = value.get('network'), value.get('fileSystem')
    if network is not None:
        network = object_value(network)
        if set(network) - {'enabled'} or network.get('enabled') is not None and type(network['enabled']) is not bool:
            raise ValueError('The native network permission has an invalid shape')
    if files is not None:
        files = object_value(files)
        if set(files) - {'read', 'write', 'globScanMaxDepth', 'entries'}:
            raise ValueError('The native filesystem permission has unsupported fields')
        for key in ('read', 'write'):
            if files.get(key) is not None:
                if not isinstance(files[key], list):
                    raise ValueError('The native filesystem roots have an invalid shape')
                for path in files[key]:
                    text(path)
        depth = files.get('globScanMaxDepth')
        if depth is not None and (type(depth) is not int or depth <= 0):
            raise ValueError('The native filesystem depth is invalid')
        entries = files.get('entries')
        if entries is not None:
            if not isinstance(entries, list):
                raise ValueError('The native filesystem entries have an invalid shape')
            for item in entries:
                item = object_value(item)
                if set(item) != {'path', 'access'} or item.get('access') not in ('read', 'write', 'deny'):
                    raise ValueError('The native filesystem entry has an invalid shape')
                path = object_value(item['path'])
                tag = path.get('type')
                if tag in ('path', 'glob_pattern'):
                    key = 'path' if tag == 'path' else 'pattern'
                    if set(path) != {'type', key}:
                        raise ValueError('The native filesystem path has an invalid shape')
                    text(path[key])
                elif tag == 'special' and set(path) == {'type', 'value'}:
                    special = object_value(path['value'])
                    kind = special.get('kind')
                    if kind in ('root', 'minimal', 'tmpdir', 'slash_tmp') and set(special) == {'kind'}:
                        continue
                    if kind in ('project_roots', 'current_working_directory', 'unknown'):
                        allowed = {'kind', 'subpath', 'path'} if kind == 'unknown' else {'kind', 'subpath'}
                        if set(special) - allowed:
                            raise ValueError('The native filesystem special path is invalid')
                        if kind == 'unknown':
                            text(special.get('path'))
                        if special.get('subpath') is not None:
                            text(special['subpath'], empty=True)
                    else:
                        raise ValueError('The native filesystem special path is unsupported')
                else:
                    raise ValueError('The native filesystem path is unsupported')
    return {key: deepcopy(item) for key, item in value.items() if item is not None}


def form_fields(offer, schema):
    schema = object_value(schema)
    if set(schema) - {'$schema', 'type', 'properties', 'required'} or schema.get('type') != 'object':
        raise ValueError('This form cannot be filled in by the checked terminal')
    properties = object_value(schema.get('properties'))
    required = schema.get('required')
    required = [] if required is None else required
    if (not isinstance(required, list) or any(not isinstance(key, str) for key in required)
            or set(required) - set(properties) or len(properties) > 64):
        raise ValueError('The native form has invalid required fields')
    for key, native in properties.items():
        native = object_value(native)
        for name in ('title', 'description'):
            if native.get(name) is not None:
                text(native[name], empty=True)
        kind = native.get('type')
        labels, mapping = [], None
        if kind == 'boolean' and not set(native) - {'type', 'title', 'description', 'default'}:
            if native.get('default') is not None and type(native['default']) is not bool:
                raise ValueError('The native form has an invalid boolean default')
            mapping = {'True': True, 'False': False}
        elif kind == 'string':
            common = {'type', 'title', 'description', 'default'}
            if native.get('default') is not None:
                text(native['default'], empty=True)
            if 'enum' in native:
                allowed = common | {'enum', 'enumNames'}
            elif 'oneOf' in native:
                allowed = common | {'oneOf'}
            else:
                allowed = common | {'format', 'minLength', 'maxLength'}
            if set(native) - allowed:
                raise ValueError('The native form has unsupported string fields')
            if native.get('format') is not None and native['format'] not in ('email', 'uri', 'date', 'date-time'):
                raise ValueError('The native form has an unsupported string format')
            for name in ('minLength', 'maxLength'):
                if native.get(name) is not None and (type(native[name]) is not int or not 0 <= native[name] < 2**32):
                    raise ValueError('The native form has invalid string limits')
            if 'enum' in native:
                values, names = native['enum'], native.get('enumNames')
                if (not isinstance(values, list) or not values or len(values) > 128
                        or any(not isinstance(value, str) for value in values)
                        or names is not None and (not isinstance(names, list)
                                                  or any(not isinstance(name, str) for name in names))):
                    raise ValueError('The native form has invalid enum options')
                mapping = {(names[i] if names is not None and i < len(names) else value): value
                           for i, value in enumerate(values)}
                if len(mapping) != len(values):
                    raise ValueError('The native form has ambiguous option labels')
            elif 'oneOf' in native:
                values = native['oneOf']
                if not isinstance(values, list) or not values or len(values) > 128:
                    raise ValueError('The native form has invalid options')
                mapping = {}
                for value in values:
                    value = object_value(value)
                    if set(value) != {'const', 'title'}:
                        raise ValueError('The native form has unsupported options')
                    title, actual = text(value['title']), text(value['const'], empty=True)
                    if title in mapping:
                        raise ValueError('The native form has ambiguous option labels')
                    mapping[title] = actual
        else:
            raise ValueError('Answer this form in the terminal: numeric and multi-select fields are unsupported')
        if mapping is not None:
            labels = [(label, '') for label in mapping]
        offer.fields[key] = mapping
        offer.questions.append(question(key, native.get('description') or native.get('title') or key,
            header=native.get('title') or key, options=labels, free_text=mapping is None,
            required=key in required))


def elicitation(offer):
    mode = offer.params.get('mode')
    if mode not in ('form', 'openai/form', 'openaiForm'):
        raise ValueError('Answer this request in the terminal: URL or device verification is required')
    schema = offer.params.get('requestedSchema')
    meta = offer.params.get('_meta')
    meta = {} if meta is None else meta
    meta = object_value(meta)
    if schema is None or isinstance(schema, dict) and schema.get('type') == 'object' and schema.get('properties') == {}:
        persist = meta.get('persist')
        persist = [] if persist is None else persist
        if isinstance(persist, str):
            persist = [persist]
        if not isinstance(persist, list) or any(not isinstance(value, str) for value in persist):
            raise ValueError('The native approval has an unsupported persistence policy')
        # Special tool-suggestion forms are not ordinary approval choices.
        if meta.get('codex_approval_kind') not in (None, 'mcp_tool_call'):
            raise ValueError('This MCP approval has an unsupported native metadata shape')
        for identifier, label, saved, value in [('allow', 'Allow once', False, None),
                ('allow_session', 'Allow for this session', False, 'session'),
                ('allow_always', 'Always allow', True, 'always')]:
            if value is not None and value not in persist:
                continue
            offer.choices.append(choice(identifier, label, saves_rule=saved))
            offer.results[identifier] = {'action': 'accept', 'content': None,
                                         '_meta': {'persist': value} if value else None}
        if meta.get('codex_approval_kind') != 'mcp_tool_call':
            offer.choices.append(choice('decline', 'Decline'))
            offer.results['decline'] = {'action': 'decline', 'content': None, '_meta': None}
    else:
        offer.kind = 'form'
        form_fields(offer, schema)
    offer.choices.append(choice('cancel', 'Cancel'))
    offer.results['cancel'] = {'action': 'cancel', 'content': None,
        '_meta': {'codex_approval_cancellation_reason': 'user'} if meta.get('codex_approval_kind') == 'mcp_tool_call' else None}


def describe(method, params, *, item=None):
    """Keep unsupported native requests visible, with no fabricated answer path."""
    if method not in METHODS:
        return None
    params = object_value(params)
    tool = METHODS[method]
    summary = (params.get('command') or params.get('reason') or params.get('message')
               or params.get('serverName') or tool.replace('_', ' '))
    questions = params.get('questions')
    if tool == 'user_input' and isinstance(questions, list) and questions and isinstance(questions[0], dict):
        summary = questions[0].get('question') or summary
    offer = Offer(method, {}, 'question' if tool == 'user_input' else 'permission',
                  ' '.join(summary.split())[:500] if isinstance(summary, str) else tool)
    try:
        offer.params = params = bounded(params)
        detail = deepcopy(params)
        if tool == 'file_change':
            item = object_value(item)
            if item.get('id') != params.get('itemId') or item.get('type') != 'fileChange':
                raise ValueError('The exact file-change diff is not available; answer it in the terminal')
            changes = item.get('changes')
            if not isinstance(changes, list) or not changes:
                raise ValueError('The exact file-change diff is not available; answer it in the terminal')
            detail['changes'] = changes
            for value in changes:
                value = object_value(value)
                text(value.get('path'))
                text(value.get('diff'), empty=True)
            for native, identifier, label in [('accept', 'allow', 'Allow once'),
                    ('acceptForSession', 'allow_session', 'Allow for this session'), ('cancel', 'stop', 'Stop this turn')]:
                offer.choices.append(choice(identifier, label))
                offer.results[identifier] = {'decision': native}
        elif tool == 'command':
            command(offer)
        elif tool == 'permissions':
            granted = permission_profile(params.get('permissions'))
            for identifier, label, scope, strict in [('allow', 'Grant for this turn', 'turn', False),
                    ('allow_review', 'Grant with strict review', 'turn', True),
                    ('allow_session', 'Grant for this session', 'session', False), ('deny', 'Deny permissions', 'turn', False)]:
                offer.choices.append(choice(identifier, label))
                offer.results[identifier] = {'permissions': {} if identifier == 'deny' else deepcopy(granted), 'scope': scope}
                if strict:
                    offer.results[identifier]['strictAutoReview'] = True
        elif tool == 'user_input':
            user_input(offer)
        elif tool == 'mcp':
            elicitation(offer)
        offer.detail = bounded(detail)
        bounded(offer.public('preview'))
    except (ValueError, TypeError, RecursionError) as error:
        # A terminal-only listing is deliberately small and contains no partial
        # choices or retained opaque authentication metadata.
        offer.answerable = False
        offer.reason = (str(error) if isinstance(error, ValueError) else
                        'Answer this native prompt in the terminal: its fields or complete detail cannot be verified')
        offer.detail = {'terminal_only': True}
        if isinstance(error, ValueError) and '64 KiB' in str(error):
            offer.detail['truncated'] = True
        offer.choices.clear()
        offer.questions.clear()
        offer.results.clear()
        offer.fields.clear()
        # An oversized native payload is not retained just to display a small
        # terminal-only placeholder, nor used as later response authority.
        if not offer.params:
            offer.params = {key: value for key, value in params.items()
                            if key in ('threadId', 'turnId', 'itemId', 'approvalId')
                            and isinstance(value, str) and len(value) <= 4096}
    return offer
