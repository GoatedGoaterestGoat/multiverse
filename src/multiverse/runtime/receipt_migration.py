"""One-time, read-only native evidence scan for acceptances that predate receipts."""
from dataclasses import replace
from pathlib import Path
import sys
import time

from .. import harnesses
from ..harnesses.types import SessionHandle, normalize_session_id
from ..store import lock, now, read, write
from ..universe import agents, mailbox, records, sessions, storage

MARKER = Path('state/migrations/message-id-receipts-v1.json')
SECONDS = 60  # Shared by the tree, leaving room within the shipped updater's 300-second subprocess limit.
IDENTITY = ('id', 'harness', 'workspace', 'native_session_id', 'native_session_home',
            'native_storage_environment', 'native_service_identity', 'native_session_transition')


def _eligible(message, tracking):
    return (message.get('schema') == 2 and message.get('delivery') in ('steer', 'follow-up')
            and tracking.get('submission_state') == 'accepted'
            and tracking.get('delivery_method') != 'resume_prompt' and not tracking.get('wake_launch_id')
            and tracking.get('native_receipt') is None and not tracking.get('transcript_confirmed_at')
            and not tracking.get('dismissed_at') and not tracking.get('uncheckable_reported_at'))


def _home(directory, properties, adapter):
    """No caller defaults: only a saved home, or fully proven launch environment."""
    home = properties.get('native_session_home')
    if home:
        return home if isinstance(home, str) and Path(home).is_absolute() else None
    keys = getattr(adapter, 'storage_env', ())
    environment = storage.recorded_environment(directory, properties, getattr(adapter, 'family', ''), keys)
    if not environment or not keys:
        return None
    selected = environment.get(keys[0]) or environment.get('HOME')
    hook = getattr(adapter, 'session_home', None)
    if not isinstance(selected, str) or not Path(selected).is_absolute() or not callable(hook):
        return None
    return hook(environment)


def _agent(root, directory, deadline):
    # Control authority is irrelevant to positive transcript evidence. Never
    # discover a process, reconcile a session, or derive a home from this shell.
    with lock(directory / '.launch.lock', create_parent=False):
        properties = records.properties(directory / 'properties.json')
        effective, rows = sessions.effective(directory, properties)
        if effective != properties or properties.get('id') != directory.name:
            return 0, 0, False
        adapter = harnesses.load(root, properties['harness'])
        hook = getattr(adapter, 'message_id_evidence', None)
        if not callable(hook):
            return 0, 0, False
        session = SessionHandle.from_records(properties, {}, directory)
        home = _home(directory, properties, adapter)
        claims = sessions.claims(properties, rows)
        if home and session.native_id:
            claims.add((session.native_id, home))
        history = mailbox.inbox(directory, limit=None, all_messages=True)['messages']
    checked = confirmed = 0
    for message in history:
        if time.monotonic() >= deadline:
            return checked, confirmed, True
        tracking = message['tracking']
        if not _eligible(message, tracking):
            continue
        attempts = tracking.get('attempts') or []
        target = tracking.get('target_session') or message.get('target_session') or next(
            (attempt.get('native_session_id') for attempt in reversed(attempts) if attempt.get('native_session_id')), None)
        try:
            if not target or normalize_session_id(target) != target:
                continue
        except ValueError:
            continue
        homes = {candidate for identifier, candidate in claims if identifier == target
                 and isinstance(candidate, str) and Path(candidate).is_absolute()}
        if len(homes) != 1:
            continue
        selected_home = homes.pop()
        checked += 1
        try:
            result = hook(replace(session, native_id=target, home=selected_home), message['id'], selected_home)
        except Exception:
            continue  # A provider failure is never negative or positive proof.
        if result.state != 'ok' or not isinstance(result.value, dict) or result.value.get('delivered') is not True:
            continue
        with lock(directory / '.launch.lock', create_parent=False):
            current = records.properties(directory / 'properties.json')
            effective, _ = sessions.effective(directory, current)
            if effective != current or any(current.get(key) != properties.get(key) for key in IDENTITY):
                continue
            confirmed += int(mailbox.confirm_message_id(directory, message['id'], tracking))
    return checked, confirmed, False


def migrate(root, *, deadline=None):
    """Run once per universe; only an exact-ID positive can change a delivery record."""
    root = Path(root)
    deadline = time.monotonic() + SECONDS if deadline is None else deadline
    with lock(root / 'state/.message-id-receipts.lock'):
        marker = root / MARKER
        if marker.exists():
            saved = read(marker)
            if not isinstance(saved, dict) or saved.get('schema') != 1 or not saved.get('completed_at'):
                raise ValueError(f'{marker}: invalid receipt migration record; restore an intact copy')
            return {**saved, 'already_completed': True}
        checked = confirmed = 0
        skipped = []
        limited = False
        for path in agents.record_paths(root):
            if time.monotonic() >= deadline:
                limited = True
                break
            try:
                count, changed, limited = _agent(root, path.parent, deadline)
                checked += count
                confirmed += changed
                if limited:
                    break
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                skipped.append(f'{path.parent}: {error}')
        result = {'schema': 1, 'completed_at': now(), 'checked': checked, 'confirmed': confirmed,
                  'skipped': skipped, 'limited': limited}
        write(marker, result)
        return result


def after_update(root, *, deadline=None):
    """The old updater reaches new code through plugins migrate --apply. Never alter its result."""
    try:
        result = migrate(root, deadline=deadline)
        if not result.get('already_completed') and result['confirmed']:
            print(f"Receipt migration: confirmed {result['confirmed']} earlier messages in {root}; nothing resent.", file=sys.stderr)
        if not result.get('already_completed') and result.get('limited'):
            print(f'Receipt migration reached its time budget in {root}; unchecked messages stay unchanged. '
                  f'See {Path(root) / MARKER}.', file=sys.stderr)
    except Exception as error:
        print(f'Receipt migration did not complete in {root}: {error}', file=sys.stderr)
