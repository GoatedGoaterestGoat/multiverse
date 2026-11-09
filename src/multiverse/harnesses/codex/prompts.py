"""Primary-terminal prompt inventory and one response on its existing socket."""
import asyncio
from collections import OrderedDict
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4

from ..types import NativeResult
from . import prompt_codec as codec
from .prompt_files import Candidate, Files, identifier


POLL = .1
HEARTBEAT = 2
FRESH = 10
RESOLUTION_WAIT = 30
MAX_REQUESTS = 128
MAX_RETAINED = 512
REPLAY_BYTES = 1 << 20  # The relay's bounded native message, never retained prompt content.
UNKNOWN = ('The answer may have been sent, but Codex does not confirm which reply it took. '
           'Another client may have answered, the prompt may have closed, or the connection may have ended. '
           'Read the result again; do not send this answer again.')


class Jobs:
    """Keep only the latest operation per private pathname while file I/O waits."""
    def __init__(self):
        self.values = OrderedDict()

    def append(self, value):
        key = ('answer' if value[0] == 'discard' else 'record', value[1])
        self.values[key] = value
        self.values.move_to_end(key)

    def extend(self, values):
        for value in values:
            self.append(value)

    def __iter__(self):
        return iter(self.values.values())

    def __bool__(self):
        return bool(self.values)


def request_id(value):
    valid = (type(value) is int and -(2**63) <= value < 2**63
             or isinstance(value, str) and len(value) <= 1024 and '\0' not in value)
    return (type(value).__name__, value) if valid else None


def replay_digest(params):
    digest, size = hashlib.sha256(), 0
    try:
        encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        for part in encoder.iterencode(params):
            raw = part.encode()
            size += len(raw)
            if size > REPLAY_BYTES:
                return None
            digest.update(raw)
    except (ValueError, TypeError, RecursionError):
        return None
    return digest.hexdigest()


def scoped(value, files, native_id, id=None):
    return (isinstance(value, dict) and type(value.get('schema')) is int and value['schema'] == 1
            and value.get('launch_id') == files.launch and value.get('native_id') == native_id
            and (id is None or value.get('id') == id))


def files_for(session):
    if not session.session_file or not session.launch_id or not session.native_id:
        raise ValueError('No exact Codex launch and conversation is recorded')
    return Files(str(Path(session.session_file).parent), session.launch_id)


def available(session):
    versions = session.native_versions or {}
    return (session.state == 'running' and session.native_id and session.launch_id and session.session_file
            and session.endpoint is not None and session.endpoint.transport == 'unix-websocket'
            and (session.native_mode or {}).get('mode') == 'bridge'
            and (session.native_relay or {}).get('state') == 'ready'
            and isinstance(versions.get('cli'), str)
            and versions.get('cli') in {'.'.join(map(str, known)) for known in codec.VERSIONS}
            and versions.get('daemon') == versions.get('cli'))


def supported(session):
    if not available(session):
        return False
    try:
        files = files_for(session)
        value = files.read('index.json')
        return (scoped(value, files, session.native_id) and value.get('active') is True
                and type(value.get('updated')) in (int, float) and 0 <= time.time() - value['updated'] <= FRESH)
    except (OSError, ValueError, TypeError):
        return False


def catalog(session):
    if not available(session):
        return NativeResult('unavailable', 'Waiting prompts need a verified Codex bridge on matching checked CLI and daemon releases; resume with it')
    try:
        files = files_for(session)
        index = files.read('index.json')
        if (not scoped(index, files, session.native_id) or index.get('active') is not True
                or type(index.get('updated')) not in (float, int)
                or not 0 <= time.time() - index['updated'] <= FRESH
                or index.get('coverage') not in ('complete', 'unknown')
                or not isinstance(index.get('open'), list) or len(index['open']) > MAX_REQUESTS):
            raise ValueError('The relay prompt inventory is not current')
        prompts = []
        for id in index['open']:
            identifier(id)
            record = files.read(id + '.open.json')
            if (not scoped(record, files, session.native_id, id)
                    or record.get('connection_id') != index.get('connection_id')
                    or not isinstance(record.get('prompt'), dict) or record['prompt'].get('id') != id):
                raise ValueError('The relay prompt belongs to a different connection')
            prompts.append(record['prompt'])
        after = files.read('index.json')
        if after != index:
            raise ValueError('The relay prompt inventory changed while being read')
        return NativeResult('ok', value={'prompts': prompts, 'coverage': index['coverage'],
                                        'reason': index.get('reason')})
    except (OSError, ValueError, TypeError, KeyError):
        return NativeResult('ok', value={'prompts': [], 'coverage': 'unknown',
            'reason': 'The relay has no current prompt inventory. It may not have seen the request, or its connection may have ended; answer in the terminal.'})


def submit(session, answer, executor):
    found = catalog(session)
    if found.state != 'ok':
        return found
    if session.status != 'waiting':
        return NativeResult('unavailable', 'The agent is not waiting on this prompt; nothing was sent')
    try:
        identifier(answer.id)
        if not any(row.get('id') == answer.id and row.get('answerable') is True for row in found.value['prompts']):
            return NativeResult('unavailable', 'That exact prompt is no longer known to be open; nothing was sent')
        files = files_for(session)
        record = files.read(answer.id + '.open.json')
        if not scoped(record, files, session.native_id, answer.id) or request_id(record.get('request_id')) is None:
            raise ValueError('The prompt changed launch or conversation')
        offer = codec.describe(record['method'], record['params'], item=record.get('item'))
        if (offer is None or json.dumps(offer.public(answer.id), sort_keys=True)
                != json.dumps(record['prompt'], sort_keys=True)):
            raise ValueError('The native prompt choices changed')
        offer.response(asdict(answer))
        executor.check_session()
        return executor.write_prompt_answer(session, answer)
    except (OSError, ValueError, TypeError, KeyError):
        return NativeResult('unavailable', 'The exact prompt or its offered response cannot be verified; nothing was sent')


def outcome(session, answer):
    try:
        files = files_for(session)
        id = identifier(answer.id)
        result = files.read(id + '.result.json')
        if not scoped(result, files, session.native_id, id):
            raise ValueError('The prompt result belongs to a different conversation')
        if result.get('state') not in ('sent_first', 'person', 'gone', 'refused', 'submitted', 'unknown'):
            raise ValueError('The prompt result is not a known Codex outcome')
        return NativeResult('ok', value={key: result.get(key) for key in
            ('state', 'reason', 'cause', 'person_answer', 'saved_rule', 'choices', 'questions')})
    except (OSError, ValueError, TypeError, KeyError):
        return NativeResult('ok', value={'state': 'unknown', 'cause': 'unconfirmed', 'reason': UNKNOWN})


class Relay:
    """All native state mutates on one relay loop; only file I/O leaves it."""
    def __init__(self, connection, context):
        self.connection = connection
        self.files = Files(context.get('agent_directory'), context.get('launch_id'))
        self.records, self.by_request, self.items = OrderedDict(), {}, OrderedDict()
        self.jobs = Jobs()
        self.partial = False
        self.disabled = False
        self.closed = False
        self.native_id = None
        self.binding_mode = None
        self.mode, self.mode_turn = 'unknown', None
        self.mode_requests, self.review_items, self.retired_reviews = {}, set(), set()
        self.review_turns = set()
        self.revision = 0
        self.heartbeat = 0

    def eligible(self):
        root, connection = self.connection.root, self.connection
        cli = root.settings.get('navigation_cli')
        return (not self.disabled and not connection.ended and root.bound
                and connection.owns_primary() is True and root.launch_refusal is None
                and isinstance(cli, (list, tuple)) and all(type(part) is int for part in cli)
                and tuple(cli) in codec.VERSIONS
                and connection.version == tuple(cli))

    def envelope(self, native_id, id=None):
        return {'schema': 1, 'launch_id': self.files.launch, 'native_id': native_id,
                'connection_id': self.connection.connection_id, **({'id': id} if id else {})}

    def publication(self, record):
        params = record['offer'].params
        item = record.get('item')
        if not record['offer'].answerable:
            params, item = None, None
        public = record['offer'].public(record['id'])
        if record.get('attempted'):
            public['answerable'] = False
            public['reason'] = 'An answer was already attempted for this prompt; read its result or use the terminal, never send it again'
        value = {**self.envelope(record['native_id'], record['id']), 'request_id': record['request'], 'method': record['method'],
                 'params': params, 'item': item, 'prompt': public}
        self.jobs.append(('write', record['id'] + '.open.json', value))

    def index(self):
        if self.native_id is None or self.connection.owns_primary() is not True:
            return
        open_ids = [r['id'] for r in self.records.values() if r['open'] and r['native_id'] == self.native_id]
        self.revision += 1
        reason = ('Some native prompts were not captured by this owning connection; answer those in the terminal'
                  if self.partial else None)
        if not open_ids:
            reason = 'No primary waiting request has been observed on this relay connection; answer in the terminal'
        value = {**self.envelope(self.native_id), 'active': self.eligible(), 'revision': self.revision,
                 'updated': time.time(), 'open': open_ids,
                 'coverage': 'unknown' if self.partial or not open_ids else 'complete', 'reason': reason}
        self.jobs.append(('write', 'index.json', value))
        self.heartbeat = time.monotonic()

    def result(self, record, state, reason, *, cause=None, person=None):
        value = {**self.envelope(record['native_id'], record['id']), 'state': state, 'reason': reason,
                 'cause': cause, 'person_answer': person,
                 'choices': deepcopy(record['offer'].choices), 'questions': deepcopy(record['offer'].questions),
                 'saved_rule': state == 'sent_first' and record.get('saved_rule', False)}
        record['result'] = value
        self.jobs.append(('write', record['id'] + '.result.json', value))

    def close_record(self, record, cause, reason):
        if record['open']:
            record['open'] = False
            self.jobs.extend([('remove', record['id'] + '.open.json', None),
                              ('discard', record['id'] + '.answer.json', None)])
        if record.get('local'):
            if not record.get('resolved') or not record.get('effect'):
                record['contradiction'] = True
                self.result(record, 'unknown', reason + ' ' + UNKNOWN, cause=cause)
        elif record.get('result', {}).get('state') != 'person':
            self.result(record, 'gone', reason + ' No mv2 answer was sent.', cause=cause)

    def disable(self):
        """Release the optional feature without disabling the native terminal."""
        self.disabled = True
        for record in self.records.values():
            if record['open']:
                record['open'] = False
                self.jobs.extend([('remove', record['id'] + '.open.json', None),
                                  ('discard', record['id'] + '.answer.json', None)])
                if not record['local']:
                    record['terminal_only'] = True
                    self.result(record, 'refused', 'The prompt relay can no longer verify pickup; '
                                'no mv2 answer was sent. The prompt may still be available in the terminal.', cause='unconfirmed')
            if record['local'] and (not record['resolved'] or not record.get('effect')):
                record['contradiction'] = True
                self.result(record, 'unknown', 'The prompt relay can no longer observe this submission. ' + UNKNOWN,
                            cause='connection_lost')
        self.index()

    def synchronize(self):
        current = self.connection.root.native_id
        if self.native_id != current:
            first = self.native_id is None
            for record in self.records.values():
                if record['native_id'] == self.native_id and (record['open'] or record.get('local')):
                    self.close_record(record, 'switched', 'The agent changed conversations.')
            self.items.clear()
            self.native_id = current
            self.mode_requests.clear()
            self.review_items.clear()
            self.retired_reviews.clear()
            self.review_turns.clear()
            self.mode_turn = None
            self.mode = 'normal' if self.binding_mode == current else 'unknown'
            self.binding_mode = None
            if not first:
                self.partial = False
            self.index()

    def observe(self, direction, value):
        if not self.eligible():
            primary = getattr(self.connection, 'primary', None)
            pending = getattr(primary, 'pending', None)
            if (direction == 'incoming' and 'method' not in value and pending is not None
                    and request_id(value.get('id')) == request_id(pending)
                    and getattr(primary, 'pending_method', None) in ('thread/start', 'thread/resume')):
                result = value.get('result')
                thread = result.get('thread') if isinstance(result, dict) else None
                if (isinstance(thread, dict) and isinstance(thread.get('status'), dict)
                        and thread['status'].get('type') == 'idle' and isinstance(thread.get('id'), str)):
                    # Only a later accepted owning bind can use this candidate.
                    self.binding_mode = thread['id']
            if direction == 'incoming' and value.get('method') in codec.METHODS:
                self.partial = True  # A request observed before binding is not a complete inventory.
            return
        self.synchronize()
        key = request_id(value.get('id'))
        if direction == 'incoming' and 'method' not in value and key in self.mode_requests:
            method = self.mode_requests.pop(key)
            result = value.get('result')
            turn = result.get('turn') if isinstance(result, dict) else None
            turn = turn.get('id') if isinstance(turn, dict) else None
            if isinstance(turn, str) and 1 <= len(turn) <= 4096 and (method != 'review/start' or result.get('reviewThreadId') == self.native_id):
                if method == 'review/start' and turn not in self.retired_reviews:
                    self.enter_review(turn)
                elif method == 'turn/start' and not self.review_turns:
                    self.mode, self.mode_turn = 'normal', turn
            elif 'error' not in value:
                self.mode, self.mode_turn, self.partial = 'unknown', None, True
            return
        params = value.get('params')
        if not isinstance(params, dict):
            return
        target = params.get('threadId')
        primary = target == self.native_id
        side = target is not None and target == getattr(self.connection, 'side_id', None)
        known = any(record['target_id'] == target for record in self.records.values())
        if not primary and not side and not known:
            return
        method = value.get('method')
        if direction == 'outgoing':
            if primary and method in ('review/start', 'turn/start') and key is not None:
                if key in self.mode_requests or len(self.mode_requests) >= MAX_REQUESTS:
                    self.mode, self.mode_turn, self.partial = 'unknown', None, True
                else:
                    self.mode_requests[key] = method
            if method == 'turn/interrupt':
                for record in self.records.values():
                    if (record['target_id'] == target and record['turn'] in (None, params.get('turnId'))
                            and (record['open'] or record['local'])):
                        self.close_record(record, 'interrupted', 'An interruption was requested; this prompt may be closing.')
                self.index()
            return
        if primary and method in ('item/started', 'item/completed', 'item/fileChange/patchUpdated'):
            item = params.get('item')
            if method == 'item/fileChange/patchUpdated':
                item = {'id': params.get('itemId'), 'type': 'fileChange', 'changes': params.get('changes')}
            if isinstance(item, dict) and isinstance(item.get('id'), str):
                if item.get('type') in ('enteredReviewMode', 'exitedReviewMode'):
                    self.review_marker(params, item)
                if item.get('type') == 'fileChange':
                    try:
                        self.items[(params.get('turnId'), item['id'])] = codec.bounded(item)
                        while len(self.items) > MAX_REQUESTS:
                            self.items.popitem(last=False)
                    except ValueError:
                        pass
                self.effect(method, params, item)
        elif primary and method == 'rawResponseItem/completed':
            self.effect(method, params, params.get('item'))
        elif method in codec.METHODS and 'id' in value:
            if primary or side:
                reason = ('/btw side conversation' if side else 'review' if self.review_turns or self.mode == 'review'
                          or 'review/start' in self.mode_requests.values() else
                          'conversation whose current mode is unknown' if self.mode != 'normal' else None)
                if self.mode == 'unknown' and primary:
                    self.partial = True
                self.request(value, terminal_only=reason)
        elif method == 'serverRequest/resolved':
            key = request_id(params.get('requestId'))
            record = self.records.get(self.by_request.get(key))
            if record is not None and record['target_id'] == target:
                record['resolved'] = True
                if record['open']:
                    record['open'] = False
                    self.jobs.extend([('remove', record['id'] + '.open.json', None),
                                      ('discard', record['id'] + '.answer.json', None)])
                self.finish(record)
                self.index()
        elif method in ('turn/completed', 'turn/started'):
            turn = params.get('turn')
            turn = turn if isinstance(turn, dict) else {}
            if primary and method == 'turn/completed' and turn.get('id') in self.review_turns:
                self.leave_review(turn['id'])
            for record in self.records.values():
                if record['target_id'] == target and (method == 'turn/completed' and record['turn'] in (None, turn.get('id'))
                        or method == 'turn/started' and record['turn'] != turn.get('id') and not record['review']):
                    cause = 'interrupted' if turn.get('status') == 'interrupted' else 'turn_ended'
                    self.close_record(record, cause, 'The native turn was interrupted.' if cause == 'interrupted'
                                      else 'The native turn ended or was replaced.')
            self.index()
        elif primary and 'id' in value:
            self.partial = True
            self.index()

    def review_marker(self, params, item):
        turn = params.get('turnId')
        if not isinstance(turn, str) or not turn or len(turn) > 4096 or len(item['id']) > 4096:
            self.mode, self.mode_turn, self.partial = 'unknown', None, True
            return
        key = (turn, item['id'], item['type'])
        if key in self.review_items or turn in self.retired_reviews:
            return
        if len(self.review_items) >= MAX_RETAINED:
            self.disable()
            return
        self.review_items.add(key)
        if item['type'] == 'enteredReviewMode':
            self.enter_review(turn)
            for record in self.records.values():
                if record['open'] and record['review'] and record['review_turn'] is None:
                    record['review_turn'] = turn
        elif turn in self.review_turns:
            self.leave_review(turn)

    def enter_review(self, turn):
        if turn not in self.review_turns and len(self.review_turns | self.retired_reviews) >= MAX_RETAINED:
            self.disable()
            return
        self.review_turns.add(turn)
        self.mode, self.mode_turn = 'review', turn

    def leave_review(self, turn):
        self.retired_reviews.add(turn)
        self.review_turns.discard(turn)
        self.mode, self.mode_turn = ('review', next(iter(self.review_turns))) if self.review_turns else ('normal', None)
        if not self.review_turns and 'review/start' not in self.mode_requests.values():
            for record in self.records.values():
                if record['open'] and record['review']:
                    self.close_record(record, 'turn_ended', 'The native review ended.')
        self.index()

    def request(self, value, *, terminal_only=None):
        key = request_id(value.get('id'))
        params = value['params']
        mcp = value['method'] == 'mcpServer/elicitation/request'
        turn, item = params.get('turnId'), params.get('itemId')
        if (key is None or turn is not None and (not isinstance(turn, str) or not 1 <= len(turn) <= 4096)
                or not mcp and (turn is None or not isinstance(item, str) or not 1 <= len(item) <= 4096)
                or mcp and (not isinstance(params.get('serverName'), str) or not 1 <= len(params['serverName']) <= 4096)):
            self.partial = True
            self.index()
            return
        existing = self.records.get(self.by_request.get(key))
        replay = replay_digest(params)
        if existing is not None:
            if (existing['method'] == value['method'] and replay is not None
                    and existing['replay'] == replay):
                return  # Native replay does not create a different public prompt.
            self.close_record(existing, 'unconfirmed', 'The native request ID was reused with different prompt data.')
            self.partial = True
            self.disable()
            return
        if len(self.records) >= MAX_RETAINED:
            # Keep the tombstones that suppress late terminal responses. Do not
            # silently evict them and allow a reused ID to inherit an answer.
            self.partial = True
            self.disable()
            return
        if sum(r['open'] for r in self.records.values()) >= MAX_REQUESTS:
            self.partial = True
            self.index()
            return
        item = None if terminal_only else self.items.get((params.get('turnId'), params.get('itemId')))
        if terminal_only:
            # Recognized side traffic is visible only as a small placeholder;
            # neither its question content nor a response path belongs to mv2.
            offer = codec.Offer(value['method'], {}, 'question' if value['method'] == 'item/tool/requestUserInput'
                else 'permission', 'Waiting in a ' + terminal_only, answerable=False,
                reason='Answer this ' + terminal_only + ' prompt in the terminal', detail={'terminal_only': True})
        else:
            offer = codec.describe(value['method'], params, item=item)
        id = str(uuid4())
        record = {'id': id, 'request': deepcopy(value['id']), 'method': value['method'],
                  'replay': replay,
                  'native_id': self.native_id, 'target_id': params['threadId'], 'turn': params.get('turnId'), 'offer': offer,
                  'review': terminal_only == 'review', 'review_turn': self.mode_turn if terminal_only == 'review' else None,
                  'item': deepcopy(item), 'open': True, 'local': False, 'resolved': False,
                  'attempted': False, 'contradiction': False, 'sent': False, 'effect': None}
        self.records[id] = record
        self.by_request[key] = id
        self.publication(record)
        self.index()

    def terminal(self, value):
        if 'method' in value or 'id' not in value:
            return True
        self.synchronize()
        record = self.records.get(self.by_request.get(request_id(value['id'])))
        if record is None:
            return True
        if record['local']:
            return False  # A late duplicate must not consume another native callback.
        if record.get('terminal_only'):
            self.result(record, 'person', 'The person replied in the terminal; mv2 did not send an answer.',
                        cause='person', person=record['offer'].person_answer(value.get('result')))
            return True
        if not self.eligible() or not record['open']:
            return False
        record['open'] = False
        self.jobs.extend([('remove', record['id'] + '.open.json', None),
                          ('discard', record['id'] + '.answer.json', None)])
        self.result(record, 'person', 'The person replied to this prompt in the terminal first; mv2 did not send an answer.',
                    cause='person', person=record['offer'].person_answer(value.get('result')))
        self.index()
        return True

    def claim(self, candidate):
        record = self.records.get(candidate.id)
        answer = candidate.answer
        if not self.eligible() or record is None:
            candidate.answer.clear()
            return None
        self.synchronize()
        if not record['open'] or record['native_id'] != self.native_id or record['attempted']:
            candidate.answer.clear()
            return None
        record['attempted'] = True
        try:
            # Caller is attribution only; it never supplies allow_rule authority.
            if (not scoped(answer, self.files, record['native_id'], candidate.id)
                    or set(answer) - {'notes', 'caller'} != {'schema', 'id', 'launch_id', 'native_id', 'choice', 'message', 'answers', 'allow_rule'}):
                raise ValueError('The answer belongs to another launch or request')
            result = record['offer'].response(answer)
        except (ValueError, TypeError, KeyError):
            self.result(record, 'refused', 'The answer does not match this exact native request or its offered choices; nothing was sent.',
                        cause='invalid_answer')
            candidate.answer.clear()
            self.publication(record)
            return None
        record['local'] = True
        record['started'] = time.monotonic()
        # Compare complete nonsecret question values in memory, never a truncated public preview.
        record['expected'] = record['offer'].question_effect(result)
        selected = next((v for v in record['offer'].choices if v['id'] == answer.get('choice')), {})
        record['saved_rule'] = selected.get('saves_rule') is True
        record['choice'] = answer.get('choice')
        self.result(record, 'submitted', 'mv2 is sending this exact response from the owning terminal connection; its outcome is not confirmed.')
        self.publication(record)
        return {'jsonrpc': '2.0', 'id': record['request'], 'result': result}

    def sent(self, candidate, successful):
        record = self.records.get(candidate.id)
        if record is None:
            return
        candidate.answer.clear()  # In particular discard all secret answer bytes.
        record['sent'] = successful
        if not successful:
            record['contradiction'] = True
            self.result(record, 'unknown', 'The terminal connection did not confirm the write. ' + UNKNOWN, cause='connection_lost')
        elif record['resolved']:
            self.finish(record)

    def finish(self, record):
        if record.get('result', {}).get('state') == 'person':
            return
        if not record['local']:
            self.result(record, 'gone', 'Codex closed this prompt before mv2 sent an answer. '
                        'It may have been answered in another client or cancelled.', cause='closed')
        elif record['sent'] and record['resolved'] and not record['contradiction']:
            observed = (' Codex reported ' + record['effect'] + '.') if record['effect'] else ''
            self.result(record, 'sent_first', 'mv2 sent its answer first from this terminal connection.' + observed +
                        ' Codex does not confirm it took mv2\'s choice, including any session grant or saved-policy scope.')
        elif record['contradiction']:
            if record.get('contradicted'):
                observed = ('Codex reported ' + record['effect'] + '. ') if record['effect'] else ''
                self.result(record, 'unknown', observed + 'The observed result differs from the submitted choice. ' + UNKNOWN,
                            cause='contradicted')
            elif record.get('result', {}).get('state') != 'unknown':
                self.result(record, 'unknown', 'An observed native event does not establish the submitted choice. ' + UNKNOWN,
                            cause='unconfirmed')

    def effect(self, method, params, item):
        if not isinstance(item, dict):
            return
        for record in self.records.values():
            if (not record['local'] or record['native_id'] != self.native_id
                    or record['turn'] != params.get('turnId')):
                continue
            native = record['offer'].params
            expected_type = {'item/commandExecution/requestApproval': 'commandExecution',
                             'item/fileChange/requestApproval': 'fileChange'}.get(record['method'])
            if (expected_type is not None and item.get('id') == native.get('itemId') and item.get('type') == expected_type
                    and method == 'item/completed' and item.get('status') in ('completed', 'failed', 'declined')
                    and native.get('approvalId') is None and native.get('networkApprovalContext') is None
                    and native.get('kind', 'command') == 'command'):
                kind = 'the command' if item['type'] == 'commandExecution' else 'the file change'
                record['effect'] = kind + ' as ' + item['status']
                refused = record.get('choice') in ('deny', 'stop')
                if item['status'] == 'declined' and not refused or item['status'] == 'completed' and refused:
                    record['contradiction'] = True
                    record['contradicted'] = True
            elif (method == 'rawResponseItem/completed' and item.get('type') == 'function_call_output'
                    and item.get('call_id') == native.get('itemId')
                    and record['method'] == 'item/tool/requestUserInput' and isinstance(item.get('output'), str)
                    and not any(q['id'].startswith('mcp_tool_call_approval_') for q in record['offer'].questions)):
                try:
                    value = json.loads(item['output']) if len(item['output'].encode()) <= codec.RESPONSE_BYTES else None
                    safe = record['offer'].question_effect(value)
                    if safe is not None:
                        record['effect'] = 'a question result with secret values omitted'
                        if safe != record.get('expected'):
                            record['contradiction'] = True
                            record['contradicted'] = True
                except (ValueError, TypeError, RecursionError):
                    pass
            if record['resolved']:
                self.finish(record)

    async def run(self, send):
        while not self.closed:
            if self.eligible():
                self.synchronize()
                if time.monotonic() - self.heartbeat >= HEARTBEAT:
                    for record in self.records.values():
                        if not record['open'] or record['attempted']:
                            # Core may publish just after a terminal-close
                            # unlink. Discard that late file without reading
                            # its answer or applying it to the next prompt.
                            self.jobs.append(('discard', record['id'] + '.answer.json', None))
                    self.index()
                for record in list(self.records.values()):
                    if (record['local'] and not record['resolved'] and not record.get('resolution_waited')
                            and time.monotonic() - record['started'] >= RESOLUTION_WAIT):
                        record['resolution_waited'] = True
                        self.result(record, 'unknown', 'No matching native resolution has been observed. ' + UNKNOWN,
                                    cause='unconfirmed')
                    if not record['open'] or record['attempted']:
                        continue
                    if not record['offer'].answerable:
                        self.jobs.append(('discard', record['id'] + '.answer.json', None))
                        continue
                    try:
                        candidate = await asyncio.to_thread(self.files.take, record['id'])
                    except (OSError, ValueError):
                        record['attempted'] = True
                        self.result(record, 'refused', 'The exclusive answer file could not be verified; nothing was sent.',
                                    cause='invalid_answer')
                        self.publication(record)
                        self.jobs.append(('discard', record['id'] + '.answer.json', None))
                        continue
                    if candidate is not None:
                        try:
                            await send(candidate)
                        finally:
                            candidate.answer.clear()
            jobs, self.jobs = self.jobs, Jobs()
            if jobs:
                await asyncio.to_thread(self.files.apply, jobs)
            await asyncio.sleep(POLL)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.files.stop_publications()
        self.disable()
        for record in self.records.values():
            self.jobs.extend([('remove', record['id'] + '.open.json', None),
                              ('discard', record['id'] + '.answer.json', None)])
            if record.get('result'):
                self.jobs.append(('write', record['id'] + '.result.json', record['result']))
        jobs, self.jobs = self.jobs, Jobs()
        if jobs:
            try:
                await asyncio.to_thread(self.files.apply, jobs, final=True)
            except (OSError, ValueError):
                pass  # Absent/stale files cannot be used as proof of a live prompt.
