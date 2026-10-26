"""Read Codex 0.164.0 rollout rows without connecting to a native service.

Only the exact recorded thread and home are searched. Recovery's stronger
absence proof in storage.py is independent of this display reader.
"""
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from ..types import (RowFact, TranscriptSource, TranscriptView,
                     TranscriptRowsChanged, normalize_session_id)
from .storage import session_home, _header, _metadata_object, MAX_ENTRIES

BODY_LIMIT = 16 << 20
_indexes = OrderedDict()  # bounded metadata only; never retain row bodies
NO_STORAGE = "mv2 has no record of this agent's Codex storage; resume it once"


class Unreadable(ValueError):
    pass


def check(reader):
    if reader is not None:
        reader.check()


def reading_home(session):
    if session.home:
        return session.home if Path(session.home).is_absolute() else None
    own = os.environ.get('MV2_AGENT_DIR')
    if (not own or not session.session_file
            or os.path.realpath(own) != os.path.realpath(Path(session.session_file).parent)):
        return None
    return session_home(os.environ)


def open_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Unreadable('The Codex rollout is not a regular file')
        handle = os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise
    return handle


def copies(home, native_id, reader=None):
    """Find all exact-thread copies, including archives; uncertainty never means absence."""
    try:
        native_id = normalize_session_id(native_id)
    except (TypeError, ValueError):
        raise Unreadable("No valid Codex session ID recorded") from None
    uuid = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
    name = re.compile(r'rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-' + re.escape(native_id)
                      + r'(?:_' + uuid + r')?\.jsonl(?:\.zst)?')
    roots = [Path(home) / part for part in ('sessions', 'archived_sessions')]
    pending, found, scanned = list(roots), set(), 0
    while pending:
        check(reader)
        directory = pending.pop()
        try:
            if directory.is_symlink():
                raise Unreadable('A Codex sessions directory is linked')
            with os.scandir(directory) as entries:
                for entry in entries:
                    check(reader)
                    scanned += 1
                    if scanned > MAX_ENTRIES:
                        raise Unreadable('The Codex rollout scan exceeded its entry limit')
                    path = Path(entry.path)
                    mode = path.lstat().st_mode
                    if name.fullmatch(entry.name):
                        if entry.name.endswith('.zst'):
                            path = path.with_suffix('')
                            if not path.exists():
                                raise Unreadable('Compressed Codex rollouts are not supported')
                        evidence = _header(path, native_id)
                        if evidence.state != 'ok':
                            raise Unreadable('The Codex rollout header cannot verify this thread')
                        found.add(path)
                    elif entry.name.startswith('rollout-') and native_id in entry.name:
                        raise Unreadable('An exact-thread Codex rollout has an unsupported name')
                    elif stat.S_ISLNK(mode):
                        raise Unreadable('A Codex sessions entry is linked')
                    elif stat.S_ISDIR(mode):
                        pending.append(path)
        except FileNotFoundError:
            if directory not in roots:
                raise Unreadable('A Codex sessions directory disappeared') from None
        except OSError:
            raise Unreadable('Codex rollout storage cannot be read') from None
    return sorted(found, key=lambda path: (path.stat().st_mtime_ns, str(path)))


def mapping(value):
    return value if isinstance(value, dict) else {}


def string(value):
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class Entry:
    fact: RowFact
    offset: int
    digest: str
    chain: str
    role: str | None = None
    text_hash: str | None = None
    turn: str | None = None
    rollback: int = 0
    version: str | None = None
    trigger: bool = False
    input_kind: str | None = None
    echo: bool = False
    history_base: bool = False
    native_id: str | None = None
    rollback_boundary: bool | None = False
    trim_context: bool | None = False
    rollback_echo: bool = False
    model_member: bool | None = False


# Codex 0.164.0 core/src/event_mapping.rs and context/contextual_user_message.rs.
# These predicates govern native rollback only; public labels still use provenance.
DEV_CONTEXT_PREFIXES = (
    '<permissions instructions>', 'Approved command prefix saved:', '<model_switch>',
    '<managed_developer_instructions>', '<persistent_mode>', '<apps_instructions>',
    '<collaboration_mode>', '<multi_agent_role>', '<multi_agent_mode>',
    '<environments_instructions>', '<git_attribution>', '<plugins_instructions>',
    '<realtime_conversation>', '<skills_instructions>', '<tools>', '<personality_spec>',
    '<token_budget>', '<context_window>', '<context_window_guidance>', '<rollout_budget>',
)
USER_CONTEXT_PAIRS = (
    ('# AGENTS.md instructions', '</INSTRUCTIONS>'),
    ('<environment_context>', '</environment_context>'),
    ('<agent_message_board_notification>', '</agent_message_board_notification>'),
    ('<skill>', '</skill>'), ('<user_shell_command>', '</user_shell_command>'),
    ('<turn_aborted>', '</turn_aborted>'), ('<subagent_notification>', '</subagent_notification>'),
    ('<recommended_plugins>', '</recommended_plugins>'),
)
ASCII_CASE = str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')


def user_context(text):
    trimmed = text.strip()
    folded = trimmed.translate(ASCII_CASE)
    if any(folded.startswith(start.translate(ASCII_CASE)) and folded.endswith(end.translate(ASCII_CASE))
           for start, end in USER_CONTEXT_PAIRS):
        return True
    return (trimmed.startswith('<goal_context>') and trimmed.endswith('</goal_context>')
            or re.fullmatch(r'<external_([^>]*)>.*</external_\1>', trimmed, re.DOTALL) is not None
            or re.fullmatch(r'<codex_internal_context source="[a-z][a-z0-9_]*">.*</codex_internal_context>', trimmed, re.DOTALL) is not None
            or trimmed.startswith('Warning: The maximum number of unified exec processes you can keep open is')
            or trimmed.startswith('Warning: Your account was flagged for potentially high-risk cyber activity')
            or trimmed.startswith('Warning: apply_patch was requested via ')
            and trimmed.endswith('Use the apply_patch tool instead of exec_command.'))


def clear_ordinary_text(content):
    """An older echo cannot settle unknown contextual/summary-looking input."""
    return isinstance(content, list) and bool(content) and all(
        isinstance(block, dict) and block.get('type') == 'input_text' and isinstance(block.get('text'), str)
        and not block['text'].lstrip().startswith((
            '<', '# AGENTS.md instructions',
            'Another language model started to solve this problem and produced a summary'))
        for block in content)


def context_update(role, content, input_kind):
    """Known pre-input context, known barrier, or None where trimming cannot be proved."""
    if role not in ('developer', 'user'):
        return False
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict):
            return None
        kind = block.get('type')
        if kind in ('input_text', 'output_text'):
            if not isinstance(block.get('text'), str):
                return None
        elif kind == 'input_image':
            if not any(isinstance(block.get(key), str) for key in ('image_url', 'file_id')):
                return None
        elif kind == 'input_audio':
            if not isinstance(block.get('audio_url'), str):
                return None
        else:
            return None
    texts = [block['text'] for block in content if block.get('type') == 'input_text']
    if role == 'developer':
        return any(text.lstrip().translate(ASCII_CASE).startswith(prefix.translate(ASCII_CASE))
                   for text in texts for prefix in DEV_CONTEXT_PREFIXES)
    if any(user_context(text) for text in texts):
        return True
    # Unknown/dynamic user fragments need the native parser. Proven ordinary
    # input can stop trimming; an older input may gain that proof from its echo.
    ordinary = content and all(block.get('type') != 'input_text' or clear_ordinary_text([block]) for block in content)
    return False if input_kind == 'user' and ordinary else None


def checkpoint_context(payload):
    """A raw compaction row cannot expose only part of its replacement history."""
    replacement = payload.get('replacement_history')
    if replacement is None:
        return False  # Legacy compaction ends with its non-contextual summary.
    if not isinstance(replacement, list) or not replacement:
        return None
    metadata = payload.get('replacement_history_metadata')
    if metadata is not None and (not isinstance(metadata, list) or len(metadata) != len(replacement)):
        return None
    last = None
    for index in range(len(replacement) - 1, -1, -1):
        candidate = mapping(replacement[index])
        member = model_member('response_item', string(candidate.get('type')), string(candidate.get('role')),
                              mapping(metadata[index]) if metadata is not None else {})
        if member is None:
            return None
        if member:
            last = candidate
            break
    if last is None:
        return None
    if last.get('type') == 'message':
        parts = mapping(last.get('internal_chat_message_metadata_passthrough')).get('content_item_metadata')
        ordinary = (isinstance(parts, list) and parts and isinstance(last.get('content'), list)
                    and len(parts) == len(last['content']) and all(
                        mapping(mapping(part).get('provenance')).get('type') == 'user' for part in parts))
        status = context_update(last.get('role'), last.get('content'), 'user' if ordinary else None)
        return False if status is False else None
    if last.get('type') in ('agent_message', 'reasoning', 'function_call', 'function_call_output',
                            'custom_tool_call', 'custom_tool_call_output', 'local_shell_call',
                            'tool_search_call', 'tool_search_output', 'web_search_call', 'image_generation_call'):
        return False
    return None


def model_member(outer, inner, role, metadata):
    """Codex 0.164.0 history.rs is_api_message, separate from stored-row labels."""
    if outer in ('compacted', 'inter_agent_communication'):
        return True
    if outer != 'response_item':
        return None if outer == 'unreadable' else False
    if inner == 'message':
        return role != 'system' if role is not None else None
    if inner == 'configuration_update':
        authored = metadata.get('harness_authored_configuration', False)
        return authored if type(authored) is bool else None
    if inner in ('additional_tools', 'agent_message', 'function_call_output', 'function_call',
                 'tool_search_call', 'tool_search_output', 'custom_tool_call', 'custom_tool_call_output',
                 'local_shell_call', 'reasoning', 'web_search_call', 'image_generation_call',
                 'compaction', 'context_compaction'):
        return True
    # Unknown named variants deserialize to native Other; missing type is damage.
    return False if inner is not None else None


def row_value(line):
    """Every identity record uses the header's strict duplicate-key interpretation."""
    try:
        return json.loads(line, object_pairs_hook=_metadata_object)
    except (ValueError, RecursionError):
        root_types = ()
        def recover(pairs):
            nonlocal root_types
            # Object hooks run inside-out; for a top-level object this ends
            # with every root type value, including one hidden by a duplicate.
            root_types = tuple(value for key, value in pairs if key == 'type')
            return dict(pairs)
        try:
            value = json.loads(line, object_pairs_hook=recover)
        except (ValueError, RecursionError):
            return None
        if isinstance(value, dict) and 'session_meta' in root_types:
            # Preserve identity-bearing classification with no usable ID, so
            # verify_thread refuses it instead of treating it as generic damage.
            return {'type': 'session_meta', 'payload': {}}
        return None  # Ambiguous non-identity fields cannot prove native history facts.


def entry(line, offset, previous):
    digest = hashlib.sha256(line).hexdigest()
    chain = hashlib.sha256((previous + ':' + digest).encode()).hexdigest()
    row = mapping(row_value(line))
    payload = mapping(row.get('payload'))
    outer, inner = string(row.get('type')) or 'unreadable', string(payload.get('type'))
    kind = outer + ('/' + inner if inner else '')
    role = string(payload.get('role'))
    content = payload.get('content')
    text = '\n'.join(block['text'] for block in content if isinstance(block, dict)
                     and isinstance(block.get('text'), str)) if isinstance(content, list) else ''
    if outer == 'event_msg' and inner == 'user_message':
        text = payload.get('message') if isinstance(payload.get('message'), str) else ''
    passthrough = mapping(payload.get('internal_chat_message_metadata_passthrough'))
    parts = passthrough.get('content_item_metadata')
    provenance = [mapping(mapping(part).get('provenance')).get('type') for part in parts] if isinstance(parts, list) else []
    metadata = mapping(row.get('metadata'))
    input_kind = ('user' if 'user' in provenance else 'injected' if provenance and all(
        value in ('harness', 'skills', 'agents_md', 'developer_instructions', 'tool') for value in provenance) else None)
    rollback_boundary = False
    if outer == 'response_item' and inner == 'message' and role == 'user':
        complete = (isinstance(content, list) and bool(content) and isinstance(parts, list)
                    and len(parts) == len(content) and all(value in (
                        'user', 'harness', 'skills', 'agents_md', 'developer_instructions', 'tool')
                        for value in provenance))
        rollback_boundary = (True if all(value == 'user' for value in provenance) else
                             False if 'user' not in provenance else None) if complete else None
    elif outer == 'inter_agent_communication' or outer == 'response_item' and inner == 'agent_message':
        rollback_boundary = True
    elif outer == 'response_item' and inner == 'message' and role == 'assistant' and isinstance(content, list) and len(content) == 1:
        try:
            legacy = json.loads(text)
        except (ValueError, RecursionError):
            legacy = None
        if isinstance(legacy, dict) and {'author', 'recipient', 'content', 'trigger_turn'} <= legacy.keys():
            # Older structured assistant instructions need native AgentPath and
            # payload parsing; refuse rollback rather than treating them as prose.
            rollback_boundary = None
    elif outer == 'unreadable':
        rollback_boundary = None
    if metadata.get('compaction_output') is True:
        rollback_boundary = False
    trim_context = (context_update(role, content, input_kind) if outer == 'response_item' and inner == 'message'
                    else checkpoint_context(payload) if outer == 'compacted' else False)
    if role == 'user':
        if trim_context is True:
            rollback_boundary = False
        elif trim_context is None:
            rollback_boundary = None
    if metadata.get('inherited_user_message') is True:
        input_kind = 'injected'
    completed_item = mapping(payload.get('item'))
    echo = outer == 'event_msg' and (inner == 'user_message' or inner == 'item_completed'
                                                   and completed_item.get('type') == 'UserMessage')
    if echo and inner == 'item_completed':
        content = completed_item.get('content')
        text = '\n'.join(block['text'] for block in content if isinstance(block, dict)
                         and isinstance(block.get('text'), str)) if isinstance(content, list) else ''
    label = 'meta'
    if outer == 'compacted':
        label = 'compaction'
    elif outer == 'inter_agent_communication':
        label = 'prompt' if payload.get('trigger_turn') is True else 'mid_turn'
        text = payload.get('content') if isinstance(payload.get('content'), str) else ''
    elif outer == 'response_item':
        if inner == 'additional_tools':
            label = 'injected'
        elif inner in ('compaction', 'context_compaction', 'compaction_trigger'):
            label = 'compaction'
        elif inner == 'message':
            label = 'response' if role == 'assistant' else 'injected'
        elif inner in ('function_call_output', 'custom_tool_call_output', 'local_shell_call_output',
                        'tool_search_output', 'computer_call_output'):
            label = 'tool_result'
        elif inner in ('reasoning', 'function_call', 'custom_tool_call', 'local_shell_call',
                        'web_search_call', 'computer_call', 'image_generation_call', 'tool_search_call', 'agent_message'):
            label = 'response'
    elif outer == 'event_msg' and inner == 'turn_aborted':
        label = 'injected'
    if metadata.get('compaction_output') is True:
        label = 'compaction'
    member = model_member(outer, inner, role, metadata)
    if member is None:
        rollback_boundary = None
    call = string(payload.get('call_id'))
    fact = RowFact(position=f'{offset + len(line)}:{chain}', ref=string(payload.get('id'))
                   if outer in ('response_item', 'inter_agent_communication') else None,
                   kind=kind, label=label, source='peer' if outer == 'inter_agent_communication' else None,
                   call_id=call, at=string(row.get('timestamp')), size=len(line),
                   preview=text[:200] if label == 'prompt' or role == 'user' else '')
    rollback = payload.get('num_turns') if outer == 'event_msg' and inner == 'thread_rolled_back' else 0
    plain_text = (isinstance(content, list) and bool(content) and all(
        isinstance(block, dict) and block.get('type') in ('input_text', 'text', 'output_text')
        and isinstance(block.get('text'), str) for block in content))
    if outer == 'event_msg' and inner == 'user_message':
        plain_text = not any(payload.get(key) for key in ('images', 'local_images', 'file_ids', 'audio'))
    # Historical UI echoes flatten media differently from model input. Only
    # plain text can be paired exactly; current provenance covers media inputs.
    text_hash = hashlib.sha256(text.encode()).hexdigest() if text and plain_text else None
    return Entry(fact, offset, digest, chain, role, text_hash,
                 string(payload.get('turn_id')) or string(passthrough.get('turn_id')), rollback if type(rollback) is int and rollback > 0 else 0,
                 string(payload.get('cli_version')) if outer == 'session_meta' else None,
                 payload.get('trigger_turn') is True, input_kind, echo,
                 outer == 'session_meta' and payload.get('history_base') is not None,
                 string(payload.get('id')) if outer == 'session_meta' else None, rollback_boundary, trim_context,
                 clear_ordinary_text(content) if outer == 'response_item' and role == 'user' else False,
                 member)


def signature(status):
    return status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns


def verify_thread(entries, native_id):
    try:
        expected = normalize_session_id(native_id)
        if not entries or entries[0].fact.kind.partition('/')[0] != 'session_meta':
            raise Unreadable('The Codex rollout has no identity header')
        for item in entries:
            if item.fact.kind.partition('/')[0] == 'session_meta' and normalize_session_id(item.native_id) != expected:
                raise Unreadable('The Codex rollout changed its native thread')
    except (TypeError, ValueError):
        raise Unreadable('The Codex rollout changed its native thread') from None


def validate_scan(handle, entries, before, reader=None):
    """Never publish a mixed scan; appends are allowed only after an open-file prefix proof."""
    after = signature(os.fstat(handle.fileno()))
    if after != before:
        if not same_file_prefix(before, after):
            raise Unreadable('The Codex rollout changed during its metadata scan')
        try:
            # Use the same open inode, independently of the iterator's buffer.
            with os.fdopen(os.dup(handle.fileno()), 'rb') as proof:
                prove_prefix(proof, entries, len(entries) - 1, reader)
        except TranscriptRowsChanged:
            raise Unreadable('The Codex rollout changed during its metadata scan') from None
        if signature(os.fstat(handle.fileno())) != after:
            raise Unreadable('The Codex rollout changed during prefix validation')
    return after


def read_copy(path, reader=None):
    # Verify the whole prefix on a changed file. Reuse parsed metadata only for
    # byte-identical lines, so a cold scan parses once and an append parses its tail.
    old_signature, old = _indexes.get(path, (None, ()))
    with open_regular(path) as handle:
        status = os.fstat(handle.fileno())
        stamp = signature(status)
        if stamp == old_signature:
            return old, old_signature
        scan_stamp = stamp
        out, offset, previous = [], 0, ''
        for number, line in enumerate(handle):
            check(reader)
            if not line.endswith(b'\n'):
                break
            digest = hashlib.sha256(line).hexdigest()
            if (number < len(old) and old[number].digest == digest
                    and old[number].offset == offset and (number == 0 or old[number - 1].chain == previous)):
                item = old[number]
            else:
                item = entry(line, offset, previous)
            out.append(item)
            previous, offset = item.chain, offset + len(line)
        stamp = validate_scan(handle, out, stamp, reader)
    # A tail appended after iterator EOF may not be in `out`: keep the initial
    # cache stamp so the next read indexes that tail even if the writer stops.
    _indexes[path] = (scan_stamp, tuple(out))
    _indexes.move_to_end(path)
    while len(_indexes) > 32:
        _indexes.popitem(last=False)
    return tuple(out), stamp


def newest(home, native_id, reader=None):
    paths = copies(home, native_id, reader)
    if not paths:
        return None, [], None
    path = paths[-1]
    entries, stamp = read_copy(path, reader)
    verify_thread(entries, native_id)
    if any(item.history_base for item in entries):
        raise Unreadable('Referenced Codex rollout history is not supported')
    for older in paths[:-1]:
        prior, _ = read_copy(older, reader)
        verify_thread(prior, native_id)
        if any(item.history_base for item in prior):
            raise Unreadable('Referenced Codex rollout history is not supported')
        if len(prior) > len(entries) or any(a.chain != b.chain for a, b in zip(prior, entries)):
            raise Unreadable('Copies of the Codex rollout disagree')
    return path, entries, stamp


def trim_pre_input_context(entries, history, cut, first_input, native_inputs, reader=None):
    """Mirror trim_pre_turn_context_updates over model items, not rollout bookkeeping."""
    cursor = cut - 1
    while cursor >= first_input:
        check(reader)
        number = history[cursor]
        item = entries[number]
        if item.model_member is None:
            raise Unreadable('Codex model-item membership cannot be established')
        if item.model_member:
            if number in native_inputs:
                break
            if item.trim_context is None:
                raise Unreadable('Codex pre-input context trimming cannot be established')
            if not item.trim_context:
                break
            cut = cursor
        cursor -= 1
    return cut


def analyse(entries, tip=None, reader=None):
    facts = [item.fact for item in entries]
    segments, segment, active = [], [], None
    # Segment by native turn ID, including repeated starts for a recovered turn.
    for number, item in enumerate(entries):
        check(reader)
        if item.fact.kind in ('event_msg/task_started', 'event_msg/turn_started'):
            if item.turn != active and segment:
                segments.append(segment)
                segment = []
            active = item.turn
        segment.append(number)
    if segment:
        segments.append(segment)
    active = None
    native_inputs = {i for i, item in enumerate(entries) if item.rollback_boundary is True}
    uncertain_inputs = {i for i, item in enumerate(entries) if item.rollback_boundary is None}
    for group in segments:
        candidates = [i for i in group if entries[i].fact.kind == 'response_item/message'
                      and entries[i].role == 'user' and entries[i].input_kind != 'injected'
                      and facts[i].label != 'compaction']
        authenticated = {i for i in candidates if entries[i].input_kind == 'user'}
        by_text, echoes = defaultdict(list), defaultdict(lambda: defaultdict(int))
        for i in candidates:
            if entries[i].text_hash:
                by_text[entries[i].text_hash].append(i)
        for number in group:
            check(reader)
            item = entries[number]
            if item.echo and item.text_hash:
                echoes[item.text_hash][item.fact.kind] += 1
        for key, matches in by_text.items():
            # With fewer echoes than indistinguishable inputs, no unproven
            # occurrence can be selected safely. Native provenance still stands.
            # Old and new echo formats may describe the same input. Count
            # each stream separately, so mixed formats cannot invent a prompt.
            if max(echoes[key].values(), default=0) >= len(matches):
                authenticated.update(matches)
        native_inputs.update(i for i in authenticated if entries[i].input_kind is None
                             and entries[i].rollback_boundary is None and entries[i].rollback_echo)
        uncertain_inputs.difference_update(native_inputs)
        prompted = False
        for number in group:
            item = entries[number]
            kind = item.fact.kind
            if kind in ('event_msg/task_started', 'event_msg/turn_started'):
                active = item.turn
            if number in authenticated:
                label = 'mid_turn' if prompted else 'prompt'
                facts[number] = replace(facts[number], label=label,
                                        preview=facts[number].preview if label == 'prompt' else '')
                prompted = True
            elif kind == 'inter_agent_communication' and item.trigger:
                prompted = True
            if kind in ('event_msg/task_complete', 'event_msg/turn_complete', 'event_msg/turn_aborted'):
                # Stop hooks can finish after a newer turn starts. Only exact
                # native attribution can finish this segment's current prompt.
                if item.turn is not None and item.turn == active:
                    facts[number] = replace(facts[number], ends_turn=True)
    facts = [replace(fact, preview='') if fact.label not in ('prompt', 'command') else fact for fact in facts]
    # Replay rollback markers in stored order. A marker removes the newest N
    # non-contextual input boundaries, including steers within a public turn.
    cutoff = len(entries)
    if tip is not None:
        index = next((i for i, fact in enumerate(facts) if fact.ref == tip), None)
        if index is not None:
            cutoff = index + 1
            # Ref-less rows follow their preceding item, up to a new turn or rollback.
            while cutoff < len(entries):
                following = entries[cutoff]
                if (facts[cutoff].ref is not None or following.rollback
                        or following.fact.kind in ('event_msg/task_started', 'event_msg/turn_started')):
                    break
                cutoff += 1
    history, starts, version = [], [], 0
    for number, item in enumerate(entries[:cutoff]):
        check(reader)
        if number in native_inputs:
            starts.append(len(history))
        if item.rollback:
            if any(i in uncertain_inputs for i in history):
                raise Unreadable('Codex rollback input boundaries cannot be established')
            count = min(item.rollback, len(starts))
            if count:
                cut = trim_pre_input_context(entries, history, starts[-count], starts[0], native_inputs, reader)
                del history[cut:]
                del starts[-count:]
            version += 1
        history.append(number)
    context = max((n for n, i in enumerate(history) if facts[i].kind == 'compacted'), default=0)
    latest = next((facts[i].ref for i in reversed(history) if facts[i].ref), None)
    return tuple(facts), tuple(history), context, latest, None, version


def empty(state, reason):
    return TranscriptView(source=TranscriptSource(state=state, reason=reason), rows=(), history=(),
                          context_from=0, tip=None, gap=None, history_version=0)


def view(session, *, tip=None, reader=None):
    check(reader)
    if not session.native_id:
        return empty('unavailable', 'No Codex session ID recorded')
    home = reading_home(session)
    if home is None:
        return empty('unavailable', NO_STORAGE)
    try:
        path, entries, _ = newest(home, session.native_id, reader)
    except (Unreadable, OSError):
        return empty('error', 'Codex rollout storage cannot be read consistently')
    if path is None:
        return empty('unavailable', 'Codex has not written a rollout for this session yet')
    check(reader)
    try:
        facts, history, context, latest, gap, version = analyse(entries, tip, reader)
    except Unreadable:
        return empty('error', 'Codex rollback input boundaries cannot be established')
    return TranscriptView(source=TranscriptSource(state='supported', home=home, format='codex-jsonl',
                          version=next((item.version for item in reversed(entries) if item.version), None),
                          size=sum(item.fact.size for item in entries),
                          updated_at=next((item.fact.at for item in reversed(entries) if item.fact.at), None)),
                          rows=facts, history=history, context_from=context, tip=latest, gap=gap,
                          history_version=version)


def same_file_prefix(before, after):
    """An open snapshot may grow while its indexed prefix stays addressable."""
    return before[:2] == after[:2] and after[2] >= before[2]


def prove_prefix(handle, entries, last, reader=None):
    """Re-prove indexed bytes on this open file when a concurrent write grew it."""
    handle.seek(0)
    for item in entries[:last + 1]:
        digest, remaining = hashlib.sha256(), item.fact.size
        while remaining:
            check(reader)
            chunk = handle.read(min(65536, remaining))
            if not chunk:
                raise TranscriptRowsChanged()
            digest.update(chunk)
            remaining -= len(chunk)
        if digest.hexdigest() != item.digest:
            raise TranscriptRowsChanged()


def bodies(session, view, indexes, *, max_bytes, reader=None):
    check(reader)
    home = reading_home(session)
    try:
        path, entries, expected = newest(home, session.native_id, reader) if home else (None, [], None)
        if path is None:
            raise TranscriptRowsChanged()
        out = []
        for number in indexes:
            if number >= len(entries) or entries[number].fact.position != view.rows[number].position:
                raise TranscriptRowsChanged()
        last = max(indexes, default=-1)
        with open_regular(path) as handle:
            stamp = signature(os.fstat(handle.fileno()))
            if not same_file_prefix(expected, stamp):
                raise TranscriptRowsChanged()
            if stamp != expected:
                prove_prefix(handle, entries, last, reader)
            for number in indexes:
                check(reader)
                item = entries[number]
                handle.seek(item.offset)
                line = handle.read(item.fact.size)
                if hashlib.sha256(line).hexdigest() != item.digest:
                    raise TranscriptRowsChanged()
                if view.rows[number].kind == 'unreadable' or item.fact.size > min(max_bytes, BODY_LIMIT):
                    out.append(None)
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, RecursionError):
                    row = None
                out.append(row if isinstance(row, dict) else None)
            after = signature(os.fstat(handle.fileno()))
            if after != stamp:
                prove_prefix(handle, entries, last, reader)
                if not same_file_prefix(stamp, signature(os.fstat(handle.fileno()))):
                    raise TranscriptRowsChanged()
        return out
    except (OSError, Unreadable):
        raise TranscriptRowsChanged() from None


def readable(session, *, reader=None):
    check(reader)
    home = reading_home(session)
    if not home or not session.native_id:
        return None
    state = view(session, reader=reader).source.state
    return True if state == 'supported' else False if state == 'unavailable' else None
