"""Exact waiting prompts and one answer attempt; outcomes never send an answer again."""
from collections.abc import Mapping
from dataclasses import asdict, replace
import json
import math
import os
from pathlib import Path
import re
import stat
from types import SimpleNamespace
import time
from uuid import uuid4

from .. import harnesses, universe
from ..harnesses.types import (AttachmentSpec, Message, NativeEndpoint, NativeResult, NativeServiceHandle,
                              PromptAnswer, PromptChoice, PromptClosed, PromptInput, PromptList, PromptOpened,
                              PromptQuestion, PromptResult, SessionHandle, WaitingPrompt)
from ..plugins import access, agent_files
from ..store import lock, now, retry_missing
from ..universe import agents, records
from . import delivery_evidence, effects, native, prompt_events

LIMIT = 64 * 1024
INPUT_LIMIT = 128 * 1024
POLL = .5
FINAL = frozenset(('answered', 'sent_first', 'person', 'gone', 'refused'))
STATES = FINAL | {'submitted', 'unknown'}
AGENT_REASON = "Agents can't read or answer prompts yet; ask the person."
UNKNOWN_REASON = ('The answer may have been sent, but its outcome is unconfirmed. The person may have answered, '
                  'the prompt may have closed, or the connection may have ended. Read --result; do not send it again.')
STOPPED_UNANSWERED_REASON = 'The agent stopped while this prompt was open; mv2 sent no answer to it.'
INPUT_ANSWERED_REASON = ('Delivered to the conversation as the answer; the agent acts on it as on an answer '
                         "in the terminal. The terminal's question card may still show it open.")
INPUT_UNKNOWN_REASON = ('The answer may have reached the conversation, but its delivery is unconfirmed. '
                        'It may still be queued, or the connection may have ended. '
                        'Read --result; do not send it again.')


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ValueError('Give the complete prompt ID shown by mv2 answer')
    return value


def _caller(caller, root=None):
    if caller is not None and (not isinstance(caller, dict) or set(caller) != {'kind', 'name'}
                              or caller['kind'] != 'plugin' or not isinstance(caller['name'], str)
                              or not caller['name']):
        raise ValueError('An explicit prompt caller must name its plugin')
    # Do not turn a stale or partial agent identity, or an agent's pane, into permission to answer as a person.
    from .agent_identity import runs_in_agent
    if runs_in_agent():
        return None  # a marker refuses before any universe is looked up
    try:
        selected = universe.resolve(root)
    except (OSError, ValueError, RuntimeError):
        selected = None  # no universe to find agents' panes in: the call fails on its own after this
    if selected is not None and runs_in_agent(root=selected):
        return None
    return caller or {'kind': 'person'}


def _refused(id, reason, cause='invalid_answer'):
    return PromptResult(id=id, state='refused', cause=cause, reason=reason)


def _connect(root, directory, *, reconcile=True):
    _, adapter, session, executor = native.context(root, directory.name, reconcile=reconcile)
    state = native.observe(executor.properties, executor.facts)['live']
    if state not in ('alive', 'stopped', 'dormant'):
        raise ValueError('The agent\'s current launch cannot be verified')
    return (adapter, session, executor) if state == 'alive' else None


def _reader(root, record, session):
    try:
        ended = delivery_evidence.launch_ended(Path(session.session_file).parent, session.launch_id)
    except (OSError, ValueError, TypeError, KeyError):
        ended = False
    return harnesses.load(root, session.harness), native.NativeExecutor(session.workspace), ended


def _text(value, *, empty=True):
    return isinstance(value, str) and (empty or bool(value)) and '\0' not in value and len(value.encode()) <= LIMIT


def _prompt(value):
    if isinstance(value, WaitingPrompt):
        value = asdict(value)
    item = WaitingPrompt(**value)
    _id(item.id)
    if (not _text(item.kind, empty=False) or not _text(item.summary) or type(item.answerable) is not bool
            or type(item.requires_waiting) is not bool or not item.requires_waiting and item.kind != 'question'
            or any(v is not None and not _text(v) for v in (item.asked_at, item.tool, item.reason, item.subagent))
            or not isinstance(item.detail, Mapping) or len(item.choices) > 128 or len(item.questions) > 128):
        raise ValueError('Invalid waiting prompt')
    choices, questions, ids = [], [], set()
    for value in item.choices:
        choice = value if isinstance(value, PromptChoice) else PromptChoice(**value)
        if (not _text(choice.id, empty=False) or choice.id in ids or not _text(choice.label)
                or type(choice.saves_rule) is not bool or type(choice.takes_message) is not bool):
            raise ValueError('Invalid prompt choice')
        ids.add(choice.id)
        choices.append(choice)
    ids = set()
    for value in item.questions:
        question = value if isinstance(value, PromptQuestion) else PromptQuestion(**value)
        if (not _text(question.id, empty=False) or question.id in ids and item.answerable or not _text(question.question)
                or not _text(question.header) or len(question.options) > 128
                or any(type(v) is not bool for v in (question.multiple, question.free_text, question.secret,
                                                    question.required, question.takes_note))):
            raise ValueError('Invalid prompt question')
        options, labels = [], set()
        for pair in question.options:
            if (not isinstance(pair, (list, tuple)) or len(pair) != 2 or not all(_text(v) for v in pair)
                    or pair[0] in labels):
                raise ValueError('Invalid prompt question options')
            labels.add(pair[0])
            options.append(tuple(pair))
        ids.add(question.id)
        questions.append(replace(question, options=tuple(options)))
    encoded = json.dumps(item.detail, ensure_ascii=False, allow_nan=False).encode()
    detail = (dict(item.detail) if len(encoded) <= LIMIT else
              {'truncated': True, 'text': encoded[:(LIMIT - 128) // 2].decode('utf-8', errors='ignore')})
    return replace(item, choices=tuple(choices), questions=tuple(questions), detail=detail)


def _catalog(adapter, session, executor):
    if not all(callable(native.adapter_hook(adapter, method)) for method in
               ('waiting_prompts', 'answer_prompt', 'prompt_outcome')):
        return PromptList(state='unavailable', reason='This harness cannot read or answer waiting prompts')
    executor.check_session()
    reply = effects.call(adapter, 'waiting_prompts', session, executor)
    if not isinstance(reply, NativeResult) or reply.state != 'ok':
        return PromptList(state='unavailable', reason=getattr(reply, 'reason', '') or 'Waiting prompts are unavailable')
    try:
        value = reply.value
        if not isinstance(value, dict) or value.get('coverage') not in ('complete', 'unknown'):
            raise ValueError('Invalid prompt coverage')
        values = value.get('prompts')
        if not isinstance(values, (list, tuple)) or len(values) > 128:
            raise ValueError('Invalid prompt list')
        prompts = tuple(_prompt(v) for v in values)
        if len({v.id for v in prompts}) != len(prompts):
            raise ValueError('Repeated prompt ID')
        reason = value.get('reason')
        if reason is not None and not _text(reason):
            raise ValueError('Invalid coverage reason')
        return PromptList(state='ok', prompts=prompts, coverage=value['coverage'],
                          reason=reason or ('Some waiting prompts may only be visible in the terminal'
                                            if value['coverage'] == 'unknown' else None))
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
        return PromptList(state='unavailable', reason='The harness returned an unreadable waiting-prompt list')


def waiting(root, agent, *, caller=None):
    effects.require('waiting_prompts', agent)
    who = _caller(caller, root)
    if who is None:
        return PromptList(state='refused', reason=AGENT_REASON)
    root = universe.resolve(root)
    directory = agents.directory(root, agent)
    try:
        with lock(directory / '.launch.lock', create_parent=False, blocking=False):
            connected = _connect(root, directory)
            if connected is None:
                launch = agents.runtime(directory).get('launch_id')
                prompt_events.finish(directory, launch, previous=True)
                prompt_events.finish(directory, launch)
                prompt_events.publish(directory)
                return PromptList(state='unavailable', reason='The agent is not running')
            adapter, session, executor = connected
            found = _catalog(adapter, session, executor)
            found = replace(found, prompts=tuple(p for p in found.prompts
                                                if session.status == 'waiting' or not p.requires_waiting))
            prompt_events.record(directory, session.launch_id, session.native_id, tuple(
                PromptOpened(id=p.id, kind=p.kind, requires_waiting=p.requires_waiting)
                for p in found.prompts))
            found = replace(found, prompts=tuple(
                replace(p, answerable=False, reason='An answer was already recorded; read --result. Do not answer again.')
                if not p.requires_waiting and _record_present(directory / 'prompts' / (p.id + '.json')) else p
                for p in found.prompts))
            if who['kind'] == 'plugin':
                found = replace(found, prompts=tuple(replace(p, choices=tuple(c for c in p.choices if not c.saves_rule))
                                                     for p in found.prompts))
            return found
    except access.Denied:
        raise
    except (OSError, ValueError, RuntimeError):
        return PromptList(state='unavailable', reason='The agent or its waiting prompts changed; read them again')


def _validate(prompt, id, choice, message, answers, notes, who):
    if not prompt.answerable:
        return _refused(id, prompt.reason or 'Answer this prompt in the terminal', 'terminal_only')
    if (choice is not None and not _text(choice, empty=False)
            or message is not None and (not isinstance(message, str) or '\0' in message)):
        return _refused(id, 'Choice and message must be bounded text without NUL')
    if message is not None and len(message.encode('utf-8')) > LIMIT:
        return _refused(id, 'The message is longer than 64 KiB; shorten it. Nothing was sent.')
    offered = next((c for c in prompt.choices if c.id == choice), None)
    if choice is not None and offered is None or prompt.choices and not prompt.questions and choice is None:
        return _refused(id, 'Choose one of the decisions this prompt offers; nothing was sent', 'not_offered')
    if offered and offered.saves_rule and who['kind'] != 'person':
        return _refused(id, 'This choice saves a rule in the person\'s settings; ask the person to answer', 'person_only')
    if message is not None and (offered is None or not offered.takes_message):
        return _refused(id, 'This choice does not accept a message; nothing was sent', 'not_offered')
    if answers is None:
        answers = {}
    if not isinstance(answers, Mapping) or len(answers) > 128:
        return _refused(id, 'Answers must map question IDs to text or lists of text')
    known = {q.id: q for q in prompt.questions}
    if any(not isinstance(key, str) or key not in known for key in answers):
        return _refused(id, 'An answer names a question this prompt does not offer; nothing was sent', 'not_offered')
    if notes is None:
        notes = {}
    if not isinstance(notes, Mapping) or len(notes) > 128:
        return _refused(id, 'Notes must map question IDs to bounded text')
    normalized_notes = {}
    for key, text in notes.items():
        if not isinstance(key, str) or key not in known or not known[key].takes_note:
            return _refused(id, 'A note names a question that does not offer one; nothing was sent', 'not_offered')
        if not _text(text):
            return _refused(id, 'Each note must be bounded text without NUL; nothing was sent')
        if text.strip():
            normalized_notes[key] = text.strip()
    normalized = {}
    # A decision (decline/cancel, for example) can be an alternative to filling the form.
    questions = known if choice is None or answers else {}
    for id_, question in questions.items():
        values = answers.get(id_, ())
        values = (values,) if isinstance(values, str) else values
        if (not isinstance(values, (tuple, list)) or len(values) > 128 or not all(_text(v) for v in values)
                or len(set(values)) != len(values)):
            return _refused(id, 'Each answer must be text or a list of distinct text values')
        note_only = id_ in normalized_notes and not question.options and question.free_text
        if question.required and not values and not note_only:
            return _refused(id, 'Answer every required question; nothing was sent', 'missing_answer')
        if len(values) > 1 and not question.multiple:
            return _refused(id, 'This question accepts only one answer; nothing was sent', 'not_offered')
        if not question.free_text and any(v not in {label for label, _ in question.options} for v in values):
            return _refused(id, 'Choose an offered label for this question; nothing was sent', 'not_offered')
        if id_ in normalized_notes and any(v not in {label for label, _ in question.options} for v in values):
            return _refused(id, 'Give typed text as an answer or a note, not both; nothing was sent', 'ambiguous_answer')
        if values:
            normalized[id_] = tuple(values)
    if choice is not None and normalized_notes and not normalized:
        return _refused(id, 'Notes belong to question answers, not a separate decision; nothing was sent', 'not_offered')
    if not prompt.choices and not prompt.questions:
        return _refused(id, 'The prompt has no answerable choice or question; use its terminal', 'terminal_only')
    answer = PromptAnswer(id=id, choice=choice, message=message, answers=normalized,
                          notes=normalized_notes, allow_rule=who['kind'] == 'person')
    if len(json.dumps(asdict(answer), ensure_ascii=False).encode()) > LIMIT:
        return _refused(id, 'The answer exceeds 64 KiB; shorten it and answer the same prompt again. Nothing was sent.')
    return answer


def _exclusive(path, value, *, published=None):
    """Publish one complete private file; never replace an earlier attempt or follow its symlink."""
    agent_files.ensure_dir(path.parent)
    folder = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = '.answer-' + str(uuid4())
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=folder)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(value, output, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path.name, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)
        if published is not None:
            published()  # The native reader can take it even if the directory fsync fails next.
        os.fsync(folder)
    finally:
        try:
            try:
                os.unlink(temporary, dir_fd=folder)
            except FileNotFoundError:
                pass
        finally:
            os.close(folder)


def _encode_answer(session, answer, caller):
    payload = {'schema': 1, **asdict(answer), 'launch_id': session.launch_id, 'native_id': session.native_id,
               'caller': caller}
    return payload, json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True)


def write_answer(executor, session, answer):
    """Executor implementation: core grants this one payload only during its admitted adapter call."""
    effects.require('answer_prompt', session.agent_id)
    payload, encoded = _encode_answer(session, answer, getattr(executor, '_prompt_caller', None))
    if encoded != getattr(executor, '_prompt_payload', None):
        raise ValueError('No matching prompt answer was admitted by core')
    executor.check_session()
    _exclusive(Path(session.session_file).parent / 'prompts' / (answer.id + '.answer.json'), payload,
               published=lambda: setattr(executor, '_prompt_written', True))
    return NativeResult('pending', value={'state': 'submitted'})


def _session(record):
    values = dict(record['session'])
    if values.get('endpoint'):
        values['endpoint'] = NativeEndpoint(**values['endpoint'])
    if values.get('attachment'):
        values['attachment'] = AttachmentSpec(**values['attachment'])
    if values.get('service'):
        values['service'] = NativeServiceHandle.from_record(values['service'])
    return SessionHandle(**values)


def _safe_person(value, record):
    """A terminal answer as the harness reports it, in the shape mv2's own answers take: choice {id, label, short},
    the message (only for a choice the prompt shows takes one), answers {question: [offered labels]}, typed
    {question: [text]}, notes {question: text}. A withheld question's entry is {"secret": true}, added first and
    kept whatever text is cut to stay within LIMIT."""
    if not isinstance(value, dict):
        return None
    room = LIMIT - 32
    safe = {'truncated': True} if value.get('truncated') is True else {}
    answers, notes = value.get('answers'), value.get('notes')
    for category, given in (('answers', answers), ('notes', notes)):
        for key in record.get('withheld_ids', ()):
            if isinstance(given, dict) and key in given:
                safe.setdefault(category, {})[key] = {'secret': True}

    def size():
        return len(json.dumps(safe, ensure_ascii=False).encode())
    if size() > room:
        return {'truncated': True}
    choice = value.get('choice')
    if (choice in record.get('choices', ()) or 'choices' not in record and isinstance(choice, str)
            and re.fullmatch(r'[A-Za-z0-9_:-]{1,128}', choice)):
        labels = record.get('choice_labels', {}).get(choice, {})
        safe['choice'] = {'id': choice, 'label': labels.get('label'), 'short': labels.get('short')}
        message = value.get('message')
        if choice in record.get('message_choices', ()) and _text(message, empty=False):
            safe['message'] = message
            while size() > room and safe['message']:
                safe['message'] = safe['message'][:len(safe['message']) // 2]
                safe['truncated'] = True
            if not safe['message']:
                del safe['message']
        if size() > room:
            return {key: value for key, value in safe.items() if key in ('answers', 'notes')} | {'truncated': True}

    def keep(category, key, text, single=False):
        group = safe.setdefault(category, {})
        if single:
            group[key] = text
        else:
            group.setdefault(key, []).append(text)
        if size() <= room:
            return True
        if single:
            del group[key]
        else:
            group[key].pop()
            if not group[key]:
                del group[key]
        if not group:
            del safe[category]
        safe['truncated'] = True
        return False

    if isinstance(answers, dict):
        for key, labels in record.get('public_options', {}).items():
            selected = answers.get(key)
            if isinstance(selected, str):
                selected = [selected]
            if not isinstance(selected, (tuple, list)):
                continue
            for text in selected[:128]:
                if not _text(text):
                    continue
                category = 'answers' if text in labels else 'typed' if key in record.get('public_free_text', ()) else None
                if category is None:
                    continue
                if not keep(category, key, text):
                    return safe
            if len(selected) > 128:
                safe['truncated'] = True
    if isinstance(notes, dict):
        for key in record.get('public_notes', ()):
            text = notes.get(key)
            if _text(text, empty=False) and not keep('notes', key, text, single=True):
                return safe
    return safe or None


def _kept_answer(prompt, request, schema, budget):
    """The answer mv2 sends, as its record keeps it before anything is sent: the choice (its id, label and short),
    the message sent with it, offered labels, typed text and notes. A withheld question's answer is left out and
    marked {"secret": true}; long text is cut to fit `budget` encoded bytes (what the record has room for), marked
    truncated."""
    withheld = set(schema['withheld_ids'])
    answer = {}
    if request.choice is not None:
        labels = schema['choice_labels'].get(request.choice, {})
        answer['choice'] = {'id': request.choice, 'label': labels.get('label'), 'short': labels.get('short')}
    if _text(request.message, empty=False):
        answer['message'] = request.message
    for key, values in request.answers.items():
        if key in withheld:
            answer.setdefault('answers', {})[key] = {'secret': True}
            continue
        offered = schema['public_options'].get(key, ())
        chosen = [value for value in values if value in offered]
        typed = [value for value in values if value not in offered]
        if chosen:
            answer.setdefault('answers', {})[key] = chosen
        if typed:
            answer.setdefault('typed', {})[key] = typed
    for key, text in request.notes.items():
        answer.setdefault('notes', {})[key] = {'secret': True} if key in withheld else text
    return _fitted(answer, budget)


def _fitted(answer, budget):
    """`answer` cut to `budget` encoded bytes: its longest text halved until it fits, marked truncated."""
    def size():
        return len(json.dumps(answer, ensure_ascii=False).encode())

    def texts():  # (container, key) of every text that may be cut
        if isinstance(answer.get('message'), str):
            yield answer, 'message'
        for category in ('typed', 'notes'):
            for key, value in (answer.get(category) or {}).items():
                if isinstance(value, str):
                    yield answer[category], key
                elif isinstance(value, list):
                    for index in range(len(value)):
                        yield value, index
    while size() > budget:
        place = max(texts(), key=lambda item: len(item[0][item[1]]), default=None)
        if place is None or not place[0][place[1]]:
            # Nothing left to cut: the choice's ID (what was sent) and the secret markers stay, the rest goes.
            kept = {category: {key: value for key, value in (answer.get(category) or {}).items()
                               if value == {'secret': True}} for category in ('answers', 'notes')}
            choice = answer.get('choice')
            return {**({'choice': {'id': choice['id'], 'label': None, 'short': None}}
                       if isinstance(choice, dict) and isinstance(choice.get('id'), str) else {}),
                    **{category: values for category, values in kept.items() if values}, 'truncated': True}
        container, key = place
        container[key] = container[key][:len(container[key]) // 2]
        answer['truncated'] = True
    return answer


def _record_size(record):
    """A prompt record's size on disk, as agent_files.write_json writes it."""
    return len(json.dumps(record, indent=2, ensure_ascii=False).encode()) + 1


def _fit_in(record, place, value, margin=0):
    """`value` cut (as _fitted cuts) until `record` with `value` set at `place` (a key path) stays `margin` bytes
    under LIMIT on disk; returns the value that fits."""
    target = record
    for key in place[:-1]:
        target = target[key]
    budget = len(json.dumps(value, ensure_ascii=False).encode())
    while True:
        fitted = _fitted(json.loads(json.dumps(value)), budget)
        target[place[-1]] = fitted
        over = _record_size(record) - (LIMIT - margin)
        if over <= 0 or budget <= 0:
            return fitted
        budget = max(budget - over - 64, 0)


def _handed_over(record, directory, scope):
    """Whether this attempt's answer file was published (or taken by the native reader): proof it was handed over,
    which an attempt cut off before its record said so still leaves. It is read only from `directory`, the agent
    folder the record was read from, in its own prompts folder (never through a link), and only when the record's
    saved session file is in that folder too. Only a regular file within LIMIT naming this attempt (its id, caller,
    launch and conversation) counts; its content is never passed on. An acknowledgement alone proves nothing."""
    if directory is None or scope is None:
        return False  # proof only from the folder the record itself was read through
    try:
        session = record['session']
        agent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            # The folder as it is opened now, and the saved session's, must be one: proof is read through this
            # descriptor, so a folder put in its place meanwhile supplies nothing.
            own, saved = os.fstat(agent), os.stat(Path(session['session_file']).parent)
            if (saved.st_dev, saved.st_ino) != (own.st_dev, own.st_ino):
                return False  # the saved session names some other folder: no proof from there
            folder = os.open('prompts', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=agent)
        finally:
            os.close(agent)
        found = os.fstat(folder)
        if ((own.st_dev, own.st_ino), (found.st_dev, found.st_ino)) != scope:
            os.close(folder)
            return False  # not the prompts folder, in the agent folder, the record was read from
        try:
            for suffix in ('.answer.json', '.answer.taken.json'):
                payload = _answer_file(folder, record['id'] + suffix)
                if (isinstance(payload, dict) and type(payload.get('schema')) is int and payload['schema'] == 1
                        and WIRE_KEYS == set(payload) and _wire_matches(payload, record)
                        and payload.get('id') == record['id']
                        and payload.get('caller') == record.get('caller')
                        and payload.get('launch_id') == session.get('launch_id')
                        and payload.get('native_id') == session.get('native_id')):
                    return True
        finally:
            os.close(folder)
    except (KeyError, TypeError, OSError, ValueError):
        pass
    return False


WIRE_KEYS = frozenset(('schema', 'id', 'choice', 'message', 'answers', 'notes', 'allow_rule', 'launch_id',
                       'native_id', 'caller'))  # what _encode_answer writes: an answer file names all of them


def _witness(payload, withheld):
    """A SHA-256 over an answer envelope as mv2 sends it, with every withheld question's value left out but its key
    kept: it binds each public value and both key sets exactly, whatever the kept answer cut for display, and holds
    no secret."""
    import hashlib

    def public(values):
        return {key: (None if key in withheld else value) for key, value in values.items()}
    bound = {key: payload.get(key) for key in ('schema', 'id', 'choice', 'message', 'allow_rule', 'launch_id',
                                               'native_id', 'caller')}
    bound.update(answers=public(payload.get('answers') or {}), notes=public(payload.get('notes') or {}))
    text = json.dumps(bound, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


KEPT_KEYS = frozenset(('choice', 'message', 'answers', 'typed', 'notes', 'truncated'))


def _kept_shape(kept, payload):
    """Whether a kept answer has exactly the shape core writes, for the choice `payload` sent; a damaged one proves
    no handoff."""
    if not isinstance(kept, dict) or not set(kept) <= KEPT_KEYS or kept.get('truncated', True) is not True:
        return False
    choice = kept.get('choice')
    if 'choice' in kept and choice is None:
        return False  # core leaves the key out when no choice was sent
    if choice is None:
        if payload.get('choice') is not None:
            return False
    elif not (isinstance(choice, dict) and set(choice) == {'id', 'label', 'short'}
              and choice['id'] == payload.get('choice') and isinstance(choice['id'], str)
              and all(choice[key] is None or isinstance(choice[key], str) for key in ('label', 'short'))):
        return False
    if 'message' in kept and not isinstance(kept['message'], str):
        return False
    def secret(value):  # exactly as core writes it: {"secret": true}
        return type(value) is dict and set(value) == {'secret'} and value['secret'] is True

    def strings(value):
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    for field, valid in (('answers', lambda value: secret(value) or strings(value)), ('typed', strings),
                         ('notes', lambda value: secret(value) or isinstance(value, str))):
        group = kept.get(field, {})
        if not isinstance(group, dict) or not all(isinstance(key, str) and valid(value) for key, value in group.items()):
            return False
    return True


def _wire_matches(payload, record):
    """Whether an answer file is the envelope this reservation sent: the wire's types, its witness (every public
    value and both key sets, exactly) and a kept answer of the shape core writes."""
    answers, notes = payload.get('answers'), payload.get('notes')
    if not (isinstance(answers, dict) and isinstance(notes, dict) and type(payload.get('allow_rule')) is bool
            and (payload.get('choice') is None or isinstance(payload.get('choice'), str))
            and (payload.get('message') is None or isinstance(payload.get('message'), str))
            and all(isinstance(key, str) and isinstance(value, list) and all(isinstance(item, str) for item in value)
                    for key, value in answers.items())
            and all(isinstance(key, str) and isinstance(value, str) for key, value in notes.items())):
        return False
    withheld = record.get('withheld_ids')
    witness = record.get('witness')
    if not (isinstance(witness, str) and isinstance(withheld, list) and all(isinstance(key, str) for key in withheld)):
        return False
    if 'answer' not in record or not _kept_shape(record['answer'], payload):  # a witnessed record always keeps one
        return False
    if not _kept_matches(record['answer'], payload, record):
        return False
    return _witness(payload, set(withheld)) == witness


def _kept_matches(kept, payload, record):
    """Whether the kept answer shows what this envelope sent, as core keeps it: its public values projected through
    the record's schema (secrets as markers), and cut only as fitting cuts. Normal fitting keeps every field, the
    choice, offered labels and markers exactly, and shortens only the message, typed answers and public note text,
    each to a prefix of itself; when nothing else could be cut, it keeps exactly core's minimal form: the choice's
    id, every marker, and truncated. Nothing the wire didn't send is shown."""
    try:
        schema = {'withheld_ids': record['withheld_ids'],
                  'choice_labels': record.get('choice_labels') if isinstance(record.get('choice_labels'), dict) else {},
                  'public_options': record.get('public_options') if isinstance(record.get('public_options'), dict)
                  else {}}
        request = SimpleNamespace(choice=payload.get('choice'), message=payload.get('message'),
                                  answers=payload['answers'], notes=payload['notes'])
        expected = _kept_answer(None, request, schema, float('inf'))
    except (KeyError, TypeError, AttributeError):
        return False
    if kept == expected:
        return True
    if kept.get('truncated') is not True:
        return False
    marker = {'secret': True}
    minimal = {**({'choice': {'id': expected['choice']['id'], 'label': None, 'short': None}}
                  if 'choice' in expected else {}),
               **{field: markers for field, markers in (
                   (field, {key: value for key, value in (expected.get(field) or {}).items() if value == marker})
                   for field in ('answers', 'notes')) if markers},
               'truncated': True}
    if kept == minimal:
        return True

    def prefix(value, sent):
        return isinstance(value, str) and isinstance(sent, str) and sent.startswith(value)
    if set(kept) != set(expected) | {'truncated'}:
        return False
    if kept.get('choice') != expected.get('choice') or kept.get('answers') != expected.get('answers'):
        return False  # offered labels and markers are never cut
    if 'message' in expected and not prefix(kept['message'], expected['message']):
        return False
    typed, sent_typed = kept.get('typed', {}), expected.get('typed', {})
    if set(typed) != set(sent_typed) or not all(
            isinstance(typed[key], list) and len(typed[key]) == len(sent_typed[key])
            and all(prefix(item, wanted) for item, wanted in zip(typed[key], sent_typed[key])) for key in typed):
        return False
    notes, sent_notes = kept.get('notes', {}), expected.get('notes', {})
    return set(notes) == set(sent_notes) and all(
        notes[key] == marker if sent_notes[key] == marker else prefix(notes[key], sent_notes[key]) for key in notes)


def _unique(pairs):
    names = [name for name, _ in pairs]
    if len(set(names)) != len(names):
        raise ValueError('a name appears twice')  # JSON would keep only the last: what was sent can't be told
    return dict(pairs)


def _answer_file(folder, name):
    """An answer file's JSON when it is a regular file within LIMIT in `folder` (a descriptor) and no object in it
    repeats a name, else None; never follows a link or blocks."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=folder)
    except OSError:
        return None
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_size > LIMIT:
            return None
        data = os.read(descriptor, LIMIT + 1)
    finally:
        os.close(descriptor)
    if len(data) > LIMIT:  # grew since it was looked at
        return None
    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=_unique)  # the wire is UTF-8, and only that
    except (ValueError, UnicodeError, RecursionError):
        return None


def _caller_of(record):
    """Who answered, from a record's caller, as core writes it: the person through mv2, or a named plugin. A
    damaged caller names nobody."""
    caller = record.get('caller')
    if caller == {'kind': 'person'}:
        return {'kind': 'mv2'}
    from ..plugins.manifest import NAME
    if (isinstance(caller, dict) and set(caller) == {'kind', 'name'} and caller['kind'] == 'plugin'
            and isinstance(caller['name'], str) and NAME.fullmatch(caller['name'])):
        return {'kind': 'plugin', 'name': caller['name']}
    return None


def _rebuilt(answer, record):
    """A kept answer as its record's schema allows it: the choice it offered (its label from the schema), a message
    only with a choice that takes one, offered labels, typed text where the question takes it, notes where it takes
    one; a withheld question's entry {"secret": true} whatever was stored. Whatever else a damaged record holds is
    left out; None when nothing is left."""
    def texts(value):
        return isinstance(value, list) and all(isinstance(item, str) for item in value)

    def names(key):  # a stored list of names, as core writes it; a damaged one counts as empty
        value = record.get(key)
        return set(value) if texts(value) else set()
    if not isinstance(answer, dict) or 'withheld_ids' in record and not texts(record['withheld_ids']):
        return None  # which questions are secret can't be told: nothing is shown
    withheld = names('withheld_ids')
    raw = record.get('public_options')
    options = ({key: value for key, value in raw.items() if isinstance(key, str) and texts(value)}
               if isinstance(raw, dict) else {})
    free, noted = names('public_free_text'), names('public_notes')
    labels = record.get('choice_labels') if isinstance(record.get('choice_labels'), dict) else {}
    rebuilt = {}
    choice = answer.get('choice')
    choice = choice.get('id') if isinstance(choice, dict) else None
    allowed = record.get('choices')
    if isinstance(choice, str) and (choice in allowed if texts(allowed) else 'choices' not in record
                                    and re.fullmatch(r'[A-Za-z0-9_:-]{1,128}', choice)):
        named = labels.get(choice) if isinstance(labels.get(choice), dict) else {}
        rebuilt['choice'] = {'id': choice, 'label': named.get('label') if isinstance(named.get('label'), str) else None,
                             'short': named.get('short') if isinstance(named.get('short'), str) else None}
        if _text(answer.get('message'), empty=False) and choice in names('message_choices'):
            rebuilt['message'] = answer['message']
    for key, value in (answer.get('answers') or {}).items() if isinstance(answer.get('answers'), dict) else ():
        if key in withheld:
            rebuilt.setdefault('answers', {})[key] = {'secret': True}
        elif texts(value) and value and all(item in options.get(key, ()) for item in value):
            rebuilt.setdefault('answers', {})[key] = list(value)
    for key, value in (answer.get('typed') or {}).items() if isinstance(answer.get('typed'), dict) else ():
        if key not in withheld and key in free and texts(value) and value:
            rebuilt.setdefault('typed', {})[key] = list(value)
    for key, value in (answer.get('notes') or {}).items() if isinstance(answer.get('notes'), dict) else ():
        if key in withheld:
            rebuilt.setdefault('notes', {})[key] = {'secret': True}
        elif key in noted and isinstance(value, str):
            rebuilt.setdefault('notes', {})[key] = value
    if answer.get('truncated') is True:
        rebuilt['truncated'] = True
    return rebuilt or None


def _common(answer):
    """A terminal answer kept before 1.21 in today's shape: a bare choice id becomes {id, label, short}."""
    if isinstance(answer, dict) and isinstance(answer.get('choice'), str):
        return {**answer, 'choice': {'id': answer['choice'], 'label': None, 'short': None}}
    return answer


def _attributed(outcome, record, directory=None, scope=None):
    """`outcome` with whose answer its state describes and that answer: the person's (answered in the terminal),
    or mv2's, kept in `record` (from mv2 answer or the dashboard: {"kind": "mv2"}; from a plugin: its name). None
    for a state where no answer is known to have taken."""
    if outcome.state == 'person':
        answer = _common(outcome.person_answer)
        if isinstance(record, dict):  # as stored: read again through its schema, never as it stands
            answer = _rebuilt(answer, record)
        return replace(outcome, by={'kind': 'person'}, answer=answer, person_answer=answer)
    # answered and sent_first are the harness's word that it took; submitted counts only once mv2 recorded that it
    # handed the answer over (a record read mid-attempt, or left by an interrupted one, is a candidate, not an answer).
    if (isinstance(record, dict) and (outcome.state in ('answered', 'sent_first')
                                      or outcome.state == 'submitted' and (record.get('dispatched') is True
                                                                           or _handed_over(record, directory, scope)))):
        by = _caller_of(record)
        kept = record.get('answer')
        if not isinstance(kept, dict):  # a record from before 1.21: its choice, and the labels it proved public
            kept = {**({'choice': {'id': record['choice'], 'label': None, 'short': None}}
                       if isinstance(record.get('choice'), str) else {}),
                    **({'answers': record['answers']} if isinstance(record.get('answers'), dict) and record['answers']
                       else {})} or None
        return replace(outcome, by=by, answer=_rebuilt(kept, record) if by is not None else None, person_answer=None)
    return replace(outcome, by=None, answer=None, person_answer=None)  # a person's answer belongs to state person


def _public_schema(prompt):
    # A duplicate question ID cannot safely identify which question's answer is public.
    counts = {}
    for question in prompt.questions:
        counts[question.id] = counts.get(question.id, 0) + 1
    return {'choices': [c.id for c in prompt.choices],
            'public_options': {q.id: [label for label, _ in q.options] for q in prompt.questions
                               if not q.secret and counts[q.id] == 1},
            'public_free_text': [q.id for q in prompt.questions if q.free_text and not q.secret and counts[q.id] == 1],
            'public_notes': [q.id for q in prompt.questions if q.takes_note and not q.secret and counts[q.id] == 1],
            # A secret question's answer, or one a duplicate ID can't attribute, is never kept; a kept answer says so.
            'withheld_ids': list(dict.fromkeys(q.id for q in prompt.questions if q.secret or counts[q.id] > 1)),
            'choice_labels': {c.id: {'label': c.label, 'short': getattr(c, 'short', None)} for c in prompt.choices},
            'message_choices': [c.id for c in prompt.choices if c.takes_message],
            'has_secret': any(q.secret for q in prompt.questions)}


def _closed_schema(id, reply):
    """A closed receipt may retain its question schema, even when mv2 never attempted an answer."""
    if not isinstance(reply, NativeResult) or not isinstance(reply.value, dict):
        return {}
    try:
        return _public_schema(_prompt({'id': id, 'kind': 'closed', 'summary': '', 'answerable': False,
                                       'choices': reply.value['choices'], 'questions': reply.value['questions']}))
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return {}


def _outcome(id, reply, record):
    if not isinstance(reply, NativeResult) or reply.state != 'ok' or not isinstance(reply.value, dict):
        return None
    value = reply.value
    state = value.get('state')
    if state not in STATES:
        return None
    cause = value.get('cause')
    cause = cause if isinstance(cause, str) and re.fullmatch(r'[a-z_]{1,64}', cause) else None
    reasons = {'person': 'This prompt was answered in its terminal first; mv2 did not send its answer.',
               'sent_first': 'The answer was sent first from the agent\'s terminal connection; the harness does not confirm it took it.',
               'gone': 'The prompt closed before this answer took; nothing will be sent again.',
               'submitted': 'The answer was submitted; its outcome is pending. Read --result; do not send it again.',
               'unknown': UNKNOWN_REASON,
               'refused': 'The harness refused this answer; nothing was sent.'}
    causes = {'interrupted': 'The prompt was interrupted before this answer took.',
              'turn_ended': 'The turn ended before this answer took.',
              'stopped': 'The agent stopped before this answer took.',
              'switched': 'The agent switched conversations before this answer took.'}
    reason = causes.get(cause, reasons.get(state))
    if state == 'unknown':
        observed = {'interrupted': 'An interruption was observed.',
                    'turn_ended': 'The native turn ended.',
                    'stopped': 'The agent stopped.',
                    'switched': 'The agent changed conversations.',
                    'connection_lost': 'The prompt connection was lost.'}
        reason = (observed.get(cause, '') + ' ' + UNKNOWN_REASON).strip()
    if state == 'refused' and cause == 'person':
        reason = 'The person refused this prompt in the terminal first. Nothing was sent.'
    # Adapter explanations must be authored diagnostics, never copied prompt/answer text.
    # For secret questions (or a stale ID with no known schema), use only structured causes.
    reported = value.get('reason')
    if record.get('has_secret') is False and _text(reported, empty=False):
        reason = reported
    return PromptResult(id=id, state=state, cause=cause or ('person' if state == 'person' else None),
                        reason=reason,
                        saved_rule=state in ('answered', 'sent_first') and record.get('saves_rule') is True,
                        person_answer=_safe_person(value.get('person_answer'), record) if state == 'person' else None)


REASON_LIMIT = 2048  # the most of an outcome's reason a record keeps, in characters


def _save(directory, record, outcome):
    previous = record.get('result')  # what changed is judged against this, before fitting writes into the record
    as_it_was = json.loads(json.dumps(record))
    if isinstance(outcome.reason, str) and len(outcome.reason) > REASON_LIMIT:
        outcome = replace(outcome, reason=outcome.reason[:REASON_LIMIT - 1] + '…')
    if isinstance(outcome.person_answer, dict):
        # The record's LIMIT holds mv2's kept answer and the person's together: the person's gets the room left.
        record['result'] = {key: value for key, value in asdict(outcome).items() if key not in ('by', 'answer')}
        outcome = replace(outcome, person_answer=_fit_in(record, ('result', 'person_answer'), outcome.person_answer))
    stored = {key: value for key, value in asdict(outcome).items() if key not in ('by', 'answer')}
    record['result'] = stored
    while _record_size(record) > LIMIT and stored.get('reason'):  # the whole record stays within LIMIT
        stored['reason'] = stored['reason'][:len(stored['reason']) // 2]
        outcome = replace(outcome, reason=stored['reason'])
    if _record_size(record) > LIMIT and stored.get('person_answer'):  # its markers alone don't fit: say it's cut
        stored['person_answer'] = {'truncated': True}
        outcome = replace(outcome, person_answer=stored['person_answer'])
    if _record_size(record) > LIMIT and isinstance(record.get('answer'), dict):  # a record kept too full before
        _fit_in(record, ('answer',), record['answer'])
        if _record_size(record) > LIMIT:
            record['answer'] = {'truncated': True}
    if _record_size(record) > LIMIT:  # what can't be cut still won't fit: the reservation stays as it was
        record.clear()
        record.update(as_it_was)
        return outcome
    changed = previous != stored
    agent_files.write_json(directory / 'prompts' / (record['id'] + '.json'), record)
    if outcome.state in FINAL or outcome.cause in ('stopped', 'switched'):
        _discard_answer(directory, record['id'])
    if changed:
        launch = record['session']['launch_id']
        if (record.get('requires_waiting') is False
                and (record.get('input_reserved') is True or outcome.state in ('person', 'gone'))):
            prompt_events.stage(directory, launch, record['session']['native_id'], (
                PromptClosed(id=record['id'], kind=record['kind'], state=outcome.state, cause=outcome.cause,
                             requires_waiting=False),))
        data = {'id': record['id'], 'kind': record['kind'], 'tool': record['tool'], 'caller': record['caller'],
                'state': outcome.state, 'outcome': outcome.state, 'cause': outcome.cause}
        if not prompt_events.publish_ordered(directory, 'agent.prompt', launch, data):
            agents._event(directory, 'agent.prompt', launch, data)
    return outcome


def _discard_answer(directory, id):
    """Clean transient files; only winning removal of the unclaimed name proves nothing was taken."""
    try:
        folder = os.open(directory / 'prompts', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    try:
        unclaimed = False
        try:
            os.unlink(id + '.answer.json', dir_fd=folder)
            unclaimed = True
        except FileNotFoundError:
            pass
        try:
            os.unlink(id + '.answer.taken.json', dir_fd=folder)
        except FileNotFoundError:
            pass
        return unclaimed
    finally:
        os.close(folder)


def _settle(root, directory, record):
    current = PromptResult(**record['result'])
    if current.state in FINAL - {'sent_first'}:
        return current
    if 'input_id' in record:
        return _settle_input(root, directory, record)
    session = _session(record)
    try:
        adapter, executor, ended = _reader(root, record, session)
        reply = effects.call(adapter, 'prompt_outcome', session, PromptAnswer(id=record['id']), executor)
    except access.Denied:
        raise
    except Exception:
        if current.state == 'sent_first':
            return current
        return _save(directory, record, replace(current, state='unknown', reason=UNKNOWN_REASON))
    completed = _outcome(record['id'], reply, record)
    if current.state == 'sent_first':
        # It ends the wait, but a later explicit read can expose a cancellation or contradiction.
        # Missing/unreadable receipts cannot erase the already-qualified native send.
        if (completed is not None and completed.state == 'unknown'
                and completed.cause in ('contradicted', 'interrupted', 'turn_ended', 'switched', 'connection_lost')):
            return _save(directory, record, completed)
        return current
    if completed is not None and completed.state in FINAL:
        return _save(directory, record, completed)
    # Native must take the file atomically before acting. Winning its removal
    # proves this answer was not taken; merely observing its presence would race pickup.
    if ended:
        if _discard_answer(directory, record['id']):
            return _save(directory, record, replace(current, state='gone', cause='stopped',
                         reason='The agent stopped before taking this answer. Nothing was sent.'))
        return _save(directory, record, replace(current, state='unknown', cause='stopped', reason=UNKNOWN_REASON))
    properties = records.properties(directory / 'properties.json')
    facts = agents.runtime(directory)
    if facts.get('launch_id') != session.launch_id or properties.get('native_session_id') != session.native_id:
        if _discard_answer(directory, record['id']):
            return _save(directory, record, replace(current, state='gone', cause='switched',
                         reason='The agent changed launch or conversation before taking this answer. Nothing was sent.'))
        return _save(directory, record, replace(current, state='unknown', cause='switched',
                     reason='The agent changed launch or conversation. ' + UNKNOWN_REASON))
    return _save(directory, record, completed or current)


def _input_outcome(id, reply):
    """Native acceptance is not transcript proof, and transport errors never prove non-delivery."""
    if (isinstance(reply, NativeResult) and isinstance(reply.value, dict)
            and reply.value.get('definite_failure') is True):
        if reply.state == 'error' and reply.value.get('transcript_confirmed') is not True:
            return _refused(id, 'The native conversation did not accept this answer. Nothing reached the agent.',
                            'not_delivered')
        return PromptResult(id=id, state='unknown', reason=INPUT_UNKNOWN_REASON)
    if isinstance(reply, NativeResult) and reply.state in ('ok', 'native_queued', 'pending'):
        if isinstance(reply.value, dict) and reply.value.get('transcript_confirmed') is True:
            return PromptResult(id=id, state='answered', reason=INPUT_ANSWERED_REASON)
        return PromptResult(id=id, state='submitted', reason='The answer was accepted as conversation input; '
                            'delivery is unconfirmed. Read --result; do not send it again.')
    return PromptResult(id=id, state='unknown', reason=INPUT_UNKNOWN_REASON)


def _submit_input(directory, adapter, session, executor, record, recipe):
    if executor._prompt_written:
        return PromptResult(id=record['id'], state='unknown', reason=INPUT_UNKNOWN_REASON)
    try:
        valid = (record.get('requires_waiting') is False and record['kind'] == 'question'
                 and isinstance(recipe.id, str) and recipe.id == record.get('input_id')
                 and isinstance(recipe.text, str) and recipe.text and '\0' not in recipe.text
                 and len(recipe.text.encode('utf-8')) <= INPUT_LIMIT)
    except UnicodeError:
        valid = False
    if not valid:
        return _refused(record['id'], 'The harness returned an invalid answer input; nothing was sent.', 'invalid_input')
    # The adapter has committed this question to one input; delivery may still fail or remain uncertain.
    record['input_reserved'] = True
    agent_files.write_json(directory / 'prompts' / (record['id'] + '.json'), record)
    try:
        executor.check_session()
    except access.Denied:
        raise
    except Exception:
        return _refused(record['id'], 'The exact session cannot take the answer; nothing was sent.', 'unconfirmed')
    try:
        message = Message(recipe.id, recipe.text, delivery='steer', framed=recipe.text)
        executor._prompt_input_started = True
        reply = native.submit_input(directory, adapter, session, message, executor, restart_id=recipe.id)
    except access.Denied:
        raise
    except Exception:
        reply = None  # An adapter error can contain the answer; never publish it.
    return _input_outcome(record['id'], reply)


def _settle_input(root, directory, record):
    """Read only this reserved input's receipt, without a restart claim or a new target."""
    outcome = PromptResult(id=record['id'], state='unknown', reason=INPUT_UNKNOWN_REASON)
    try:
        connected = _connect(root, directory, reconcile=False)
        if connected is None:
            return _save(directory, record, replace(outcome, cause='stopped'))
        adapter, session, executor = connected
        expected = _session(record)
        fields = ('agent_id', 'harness', 'workspace', 'launch_id', 'native_id', 'session_file',
                  'home', 'endpoint', 'service', 'native_mode', 'native_versions', 'pid', 'pid_started')
        generation = executor.facts.get('session_generation', 0)
        if (any(getattr(session, key) != getattr(expected, key) for key in fields)
                or type(generation) is not int or generation != record['session_generation']):
            return _save(directory, record, replace(outcome, cause='switched'))
        executor.check_session()
        reply = effects.call(adapter, 'get_delivery', session, record['input_id'], executor)
        outcome = _input_outcome(record['id'], reply)
        if outcome.state == 'answered':
            record['input_reserved'] = True  # An exact transcript receipt also proves a legacy input went through.
    except access.Denied:
        raise
    except Exception:
        pass  # Unreadable evidence is uncertainty, never proof that the input was not submitted.
    return _save(directory, record, outcome)


def pending_inputs(directory, launch_id, native_id, generation):
    """Navigation admission only reads; it cannot settle receipts or restart queued work."""
    for path in sorted((directory / 'prompts').glob('*.json')):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', path.stem):
            continue  # Private callback answer files have dotted suffixes.
        record = _record(directory, path.stem)
        if ('input_id' in record and record['session']['launch_id'] == launch_id
                and record['session']['native_id'] == native_id and record['session_generation'] == generation
                and record['result']['state'] in ('submitted', 'unknown')):
            return record['id']
    return None


def result(root, agent, id, *, caller=None):
    effects.require('prompt_outcome', agent)
    if _caller(caller, root) is None:
        return _refused(id, AGENT_REASON, 'agent_caller')
    id = _id(id)
    root = universe.resolve(root)
    directory = agents.directory(root, agent)
    try:
        with lock(directory / '.launch.lock', create_parent=False, blocking=False), \
                lock(directory / '.prompts.lock', create_parent=False, blocking=False):
            record, scope = _record(directory, id, missing_ok=True, scope=True)
            return _attributed(_settle(root, directory, record) if record is not None else
                               _unanswered(root, directory, id), record, directory, scope)
    except BlockingIOError:
        # Atomic records can still be read. Contention is not evidence that an unknown ID was submitted.
        record, scope = _record(directory, id, missing_ok=True, scope=True)
        if record is None:
            return _refused(id, 'The agent is busy changing state; read --result again. Nothing was sent by this check.', 'busy')
        current = PromptResult(**record['result'])
        busy = 'The agent is busy changing state; read --result again. Nothing was resent.'
        return _attributed(current if current.state in FINAL else replace(current, cause='busy', reason=busy),
                           record, directory, scope)


def _record_present(path):
    try:
        retry_missing(path.lstat)  # A dangling link or unreadable record is damage, not an absent attempt.
    except FileNotFoundError:
        return False
    return True


def _record_at(folder, path, *, missing_ok):
    """A prompt record read through `folder`, a descriptor of its prompts folder: never through a link, within
    records.MAX_BYTES, a JSON object with no name repeated. None when it isn't there and `missing_ok`."""
    try:  # a record replaced as it's opened is briefly missing: retried as every record read is
        descriptor = retry_missing(lambda: os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                                   dir_fd=folder), path=path)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise records.RecordError(path, 'is missing') from None
    except OSError as error:
        raise records.RecordError(path, f'cannot be read: {error.strerror or type(error).__name__}') from None
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_size > records.MAX_BYTES:
            raise records.RecordError(path, 'is not a record file')
        raw = os.read(descriptor, records.MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    try:
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
    except (ValueError, UnicodeError, RecursionError):
        raise records.RecordError(path, 'is not a prompt record') from None
    if len(raw) > records.MAX_BYTES or not isinstance(data, dict):
        raise records.RecordError(path, 'is not a prompt record')
    return data


def _record(directory, id, *, missing_ok=False, scope=False):
    """The prompt record `id` of the agent at `directory`, checked. With `scope`, (record, the prompts folder's
    identity, with its agent folder's): read through one descriptor of each, so a proof read later counts only from
    that same prompts folder, still in that same agent folder."""
    path = directory / 'prompts' / (id + '.json')
    if scope:
        try:
            agent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as error:
            raise records.RecordError(path, f'cannot be read: {error.strerror or type(error).__name__}') from None
        try:  # the record, and whose it is, both read through this one folder
            try:
                folder = os.open('prompts', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=agent)
            except FileNotFoundError:
                if missing_ok:
                    return None, None
                raise records.RecordError(path, 'is missing') from None
            except OSError as error:
                raise records.RecordError(path, f'cannot be read: {error.strerror or type(error).__name__}') from None
            try:
                mine, found = os.fstat(agent), os.fstat(folder)
                identity = ((mine.st_dev, mine.st_ino), (found.st_dev, found.st_ino))  # the agent's and its prompts'
                record = _record_at(folder, path, missing_ok=missing_ok)
            finally:
                os.close(folder)
            if record is None:
                return None, None
            owner = _record_at(agent, directory / 'properties.json', missing_ok=False).get('id')
            if not isinstance(owner, str) or not owner:  # read through this folder, and says no owner: damage
                raise records.RecordError(directory / 'properties.json', 'names no agent id')
        finally:
            os.close(agent)
        return _checked(directory, id, record, owner), identity
    if missing_ok and not _record_present(path):
        return None
    return _checked(directory, id, records._object(path))


_UNREAD = object()


def _checked(directory, id, record, owner=_UNREAD):
    """`record`, checked: a prompt record of this agent (`owner`, its properties' id, read by path here only when
    the caller didn't read it through the record's own folder)."""
    if owner is _UNREAD:
        owner = records.properties(directory / 'properties.json')['id']
    try:
        value = PromptResult(**record['result'])
        session = _session(record)
        if (record.get('schema') != 1 or record.get('id') != id or value.id != id or value.state not in STATES
                or record.get('agent') != owner
                or session.agent_id != record['agent'] or not session.native_id or not session.launch_id):
            raise ValueError('Invalid prompt record')
        if ('input_id' in record or record.get('requires_waiting') is False) and (record.get('input_id') != 'prompt-' + id
                or record.get('requires_waiting') is not False or record.get('kind') != 'question'
                or type(record.get('session_generation')) is not int or record['session_generation'] < 0):
            raise ValueError('Invalid prompt input record')
    except (KeyError, TypeError, ValueError):
        raise ValueError('The prompt result is damaged or does not belong to this agent') from None
    return record


def _unanswered(root, directory, id):
    """Observe an unattempted prompt without reserving, consuming or repairing anything."""
    try:
        connected = _connect(root, directory, reconcile=False)
    except access.Denied:
        raise
    except (OSError, ValueError, RuntimeError):
        return _refused(id, 'The current launch cannot be verified; read its prompts again. Nothing was sent.', 'unconfirmed')
    if connected is None:
        return _stopped(root, directory, id)
    adapter, session, executor = connected
    if not session.native_id or not session.launch_id or not session.session_file:
        return _refused(id, 'The current conversation is not bound; nothing was sent.', 'unconfirmed')
    found = None
    try:
        found = _catalog(adapter, session, executor)
    except access.Denied:
        raise
    except Exception:
        pass  # A closed receipt may still explain a prompt absent from the current inventory.
    return _closed(adapter, session, executor, id, found)


def _stopped(root, directory, id):
    """Name an unknown ID without treating a retained prompt as an attempted answer."""
    known, outcome = False, None
    try:
        _, adapter, session, _ = native.context(root, directory.name, reconcile=False)
        if session.native_id and session.launch_id and session.session_file:
            reply = effects.call(adapter, 'prompt_outcome', session, PromptAnswer(id=id),
                                 native.NativeExecutor(session.workspace))
            schema = _closed_schema(id, reply)
            outcome = _outcome(id, reply, schema)
            known = outcome is not None and (outcome.state != 'unknown' or outcome.cause == 'open' or bool(schema))
    except access.Denied:
        raise
    except Exception:
        pass  # No readable evidence identifies this prompt; a stopped launch cannot take an answer.
    reason = ('The agent is not running. Nothing was sent.' if known else
              f'The agent is not running, and no prompt with ID {id} is known for it. Nothing was sent.')
    if outcome is not None and (outcome.state, outcome.cause) in (('unknown', 'open'), ('gone', 'stopped')):
        reason = STOPPED_UNANSWERED_REASON
    return PromptResult(id=id, state='gone', cause='stopped', reason=reason)


def _closed(adapter, session, executor, id, found):
    prompt = next((p for p in found.prompts if p.id == id), None) if found and found.state == 'ok' else None
    if prompt is not None:
        if prompt.requires_waiting and session.status != 'waiting':
            return _refused(id, 'The agent is not waiting on this prompt; nothing was sent.', 'not_waiting')
        name = (executor.properties or {}).get('name') or session.agent_id
        return _refused(id, f'mv2 sent no answer to this prompt; it is still open. '
                        f'Answer it with mv2 answer {name} {id} and its answer options.', 'not_answered')
    try:
        reply = effects.call(adapter, 'prompt_outcome', session, PromptAnswer(id=id), executor)
        outcome = _outcome(id, reply, _closed_schema(id, reply))
        if outcome is not None and outcome.state in ('person', 'gone', 'refused'):
            reason = outcome.reason or 'That prompt closed.'
            if outcome.state == 'gone' and outcome.cause == 'stopped':
                return replace(outcome, reason=STOPPED_UNANSWERED_REASON)
            if outcome.state == 'gone' and outcome.cause not in ('interrupted', 'turn_ended', 'switched', 'replaced'):
                reason = 'That prompt is no longer open. Nothing was sent.'
            if 'nothing was sent' not in reason.lower():
                reason = reason.rstrip('. ') + '. Nothing was sent.'
            return replace(outcome, reason=reason)
        if outcome is not None and outcome.state in ('answered', 'sent_first', 'submitted'):
            return _refused(id, 'The harness outcome has no matching recorded mv2 answer; '
                            'read the prompt again. Nothing was sent by this check.', 'unconfirmed')
    except access.Denied:
        raise
    except Exception:
        pass
    if found and found.state == 'ok' and found.prompts:
        return PromptResult(id=id, state='gone', cause='replaced',
                            reason=f'No prompt with ID {id} is known for this agent; it now waits on ' +
                            ', '.join(p.id for p in found.prompts) + '. Nothing was sent.')
    if found and found.state == 'ok' and found.coverage == 'complete':
        return PromptResult(id=id, state='gone', cause='closed',
                            reason=f'No prompt with ID {id} is known for this agent. Nothing was sent.')
    if session.status != 'waiting':
        return _refused(id, f'No prompt with ID {id} is known for this agent; nothing was sent.', 'not_waiting')
    return _refused(id, f'No prompt with ID {id} is known for this agent. It may have closed or changed, '
                       'or the connection may not have seen it. Nothing was sent; read the current prompts.', 'unconfirmed')


def answer(root, agent, id, *, choice=None, message=None, answers=None, notes=None, timeout=30, wait=True, caller=None):
    effects.require('answer_prompt', agent)
    who = _caller(caller, root)
    if who is None:
        return _refused(id, AGENT_REASON, 'agent_caller')
    id = _id(id)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0 or type(wait) is not bool:
        raise ValueError('The timeout must be a finite nonnegative number of seconds, and wait a boolean')
    deadline = time.monotonic() + timeout
    root = universe.resolve(root)
    directory = agents.directory(root, agent)
    try:
        with lock(directory / '.launch.lock', create_parent=False, blocking=False), \
                lock(directory / '.delivery.lock', create_parent=False, blocking=False), \
                lock(directory / '.prompts.lock', create_parent=False, blocking=False):
            path = directory / 'prompts' / (id + '.json')
            if _record_present(path):
                return _refused(id, 'An answer was already recorded for this prompt; read --result. Nothing was resent.', 'already_answered')
            try:
                connected = _connect(root, directory)
            except (OSError, ValueError, RuntimeError):
                return _refused(id, 'The current launch cannot be verified; nothing was sent. Read its prompts again.', 'unconfirmed')
            if connected is None:
                return _attributed(_stopped(root, directory, id), None)
            adapter, session, executor = connected
            if not session.native_id or not session.launch_id or not session.session_file:
                return _refused(id, 'The current conversation is not bound; nothing was sent.', 'unconfirmed')
            try:
                found = _catalog(adapter, session, executor)
            except access.Denied:
                raise
            except Exception:
                return _refused(id, 'The open prompts cannot be verified; nothing was sent.', 'unconfirmed')
            if found.state != 'ok':
                return _refused(id, found.reason or 'Waiting prompts are unavailable', 'unavailable')
            prompt = next((p for p in found.prompts if p.id == id), None)
            if prompt is None:
                return _attributed(_closed(adapter, session, executor, id, found), None)
            if prompt.requires_waiting and session.status != 'waiting':
                return _refused(id, 'The agent is not waiting on this prompt; nothing was sent.', 'not_waiting')
            request = _validate(prompt, id, choice, message, answers, notes, who)
            if isinstance(request, PromptResult):
                return request
            sent, encoded = _encode_answer(session, request, who)
            if len(encoded.encode('utf-8')) > LIMIT:
                return _refused(id, 'The complete answer file exceeds 64 KiB; shorten the answer. Nothing was sent.')
            executor.check_session()
            input_record = {}
            if not prompt.requires_waiting:
                facts = executor.facts
                generation = facts.get('session_generation', 0) if isinstance(facts, Mapping) else None
                if type(generation) is not int or generation < 0:
                    return _refused(id, 'The conversation generation cannot be verified; nothing was sent.', 'unconfirmed')
                input_record = {'input_id': 'prompt-' + id, 'session_generation': generation}
            initial = PromptResult(id=id, state='submitted', reason='Answer submission is in progress; read --result, do not resend.')
            schema = _public_schema(prompt)
            public_options = schema['public_options']
            selected = next((c for c in prompt.choices if c.id == choice), None)
            record = {'schema': 1, 'id': id, 'agent': session.agent_id, 'session': asdict(session), 'requested_at': now(),
                      'kind': prompt.kind, 'tool': prompt.tool, 'caller': who, 'choice': request.choice,
                      'question_ids': list(dict.fromkeys((*request.answers, *request.notes))),
                      'requires_waiting': prompt.requires_waiting, **input_record,
                      **schema, 'saves_rule': bool(selected and selected.saves_rule),
                      'answers': {key: list(values) for key, values in request.answers.items()
                                  if key in public_options and all(v in public_options[key] for v in values)},
                      'answer': {}, 'witness': _witness(sent, set(schema['withheld_ids'])),
                      'result': {key: value for key, value in asdict(initial).items() if key not in ('by', 'answer')}}
            # The whole nonsecret answer, kept before anything is sent, for prompt_result's answer: in the room the
            # rest of the record leaves under LIMIT on disk, less a margin for the outcome it records later.
            if _record_size(record) > LIMIT - 8192:  # the prompt's schema alone leaves no safe room
                return _refused(id, "The prompt's choices and questions are too large to keep a safe record of the "
                                    "answer; nothing was sent.", 'too_large')
            _fit_in(record, ('answer',), _kept_answer(prompt, request, schema, LIMIT), margin=4096)
            if _record_size(record) > LIMIT - 4096:  # what can't be cut (secret markers, the schema) still won't fit
                return _refused(id, "The prompt's choices and questions are too large to keep a safe record of the "
                                    "answer; nothing was sent.", 'too_large')
            try:
                _exclusive(path, record)
            except FileExistsError:
                return _refused(id, 'An answer was already recorded; read --result. Nothing was resent.', 'already_answered')
            except OSError:
                return _refused(id, 'The answer could not be recorded safely; nothing was sent.', 'storage')
            executor._prompt_caller = dict(who)
            executor._prompt_payload = encoded if prompt.requires_waiting else None
            executor._prompt_written = False
            executor._prompt_input_started = False
            dispatched = False
            try:
                reply = effects.call(adapter, 'answer_prompt', session, request, executor)
                recipe = reply.value if isinstance(reply, NativeResult) and reply.state == 'ok' else None
                if isinstance(recipe, PromptInput):
                    outcome = _submit_input(directory, adapter, session, executor, record, recipe)
                elif 'input_id' in record:
                    outcome = _outcome(id, reply, {**record, 'has_secret': True})
                    if outcome is not None and outcome.state not in ('person', 'gone', 'refused'):
                        outcome = replace(initial, state='unknown', reason=INPUT_UNKNOWN_REASON)
                    if outcome is None:
                        outcome = _refused(id, 'The harness did not supply a valid answer input; nothing was sent.',
                                           'invalid_input')
                else:
                    outcome = _outcome(id, reply, record)
                if (outcome is None and isinstance(reply, NativeResult) and reply.state in ('unavailable', 'error')
                        and not executor._prompt_written):
                    outcome = _refused(id, 'The harness refused this answer before submission; nothing was sent.', 'unavailable')
                outcome = outcome or (initial if isinstance(reply, NativeResult) and reply.state in ('pending', 'ok') else
                                      replace(initial, state='unknown', reason=UNKNOWN_REASON))
                # Handed over: the answer file published, or the input started; an acknowledgement alone isn't proof.
                dispatched = bool(executor._prompt_written or executor._prompt_input_started)
            except access.Denied:
                uncertain = executor._prompt_written or executor._prompt_input_started
                reason = INPUT_UNKNOWN_REASON if 'input_id' in record else UNKNOWN_REASON
                denied = (replace(initial, state='unknown', reason=reason) if uncertain else
                          _refused(id, 'Answer access was refused before submission', 'access'))
                _save(directory, record, denied)
                raise
            except Exception:
                outcome = replace(initial, state='unknown',
                                  reason=INPUT_UNKNOWN_REASON if 'input_id' in record else UNKNOWN_REASON)
            finally:
                del executor._prompt_payload
                del executor._prompt_caller
            # Publish the first outcome event even if the adapter only acknowledges submission.
            record.pop('result')
            record['dispatched'] = dispatched
            outcome = _attributed(_save(directory, record, outcome), record, directory)
    except BlockingIOError:
        return _refused(id, 'The agent is changing state or another answer is in progress; nothing was sent.', 'busy')
    if outcome.state in FINAL or not wait:
        return outcome
    while time.monotonic() < deadline:
        try:
            outcome = result(root, agent, id, caller=caller)
        except access.Denied:
            raise
        except (OSError, ValueError, RuntimeError):
            return replace(outcome, state='unknown', cause='unreadable', reason=UNKNOWN_REASON, by=None, answer=None)
        if outcome.state in FINAL:
            return outcome
        time.sleep(min(POLL, max(0, deadline - time.monotonic())))
    return outcome
