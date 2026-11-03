"""A read-only snapshot of one exact recorded Codex conversation at its current tip."""
from dataclasses import dataclass
from pathlib import Path

from ..types import RunRequest, RunRow, TranscriptRowsChanged
from .transcripts import BODY_LIMIT, mapping, model_member


class AskUnavailable(ValueError):
    pass


class AskRequest(RunRequest):
    """Internal own-conversation request; public plugin requests never use this marker.

    Codex replays this history as the recorded agent's next turn would, under the
    same login and model. In particular, optional encrypted history is not lost
    merely because an older header omitted account provenance.
    """


@dataclass(frozen=True, kw_only=True)
class AskSnapshot:
    messages: tuple[RunRow, ...]
    model: str
    effort: str | None
    base_instructions: str | None
    native_id: str
    tip: str | None
    history_version: int


def snapshot(adapter, session, *, reader=None):
    if not session.native_id or not session.home or not Path(session.home).is_absolute():
        raise AskUnavailable('No exact Codex conversation and recorded home are available')
    for _ in range(3):
        view = adapter.transcript_view(session, tip=None, reader=reader)
        if view.source.state != 'supported':
            raise AskUnavailable(view.source.reason or 'The Codex conversation cannot be read')
        if view.gap:
            raise AskUnavailable('Part of this Codex conversation was never stored')
        indexes = list(view.history[view.context_from:])
        # Provenance must precede the first retained item, even when another turn
        # context later in the suffix describes the current model.
        before = view.history[:view.context_from]
        context = next((index for index in reversed(before) if view.rows[index].kind == 'turn_context'), None)
        header = next((index for index in view.history if view.rows[index].kind == 'session_meta'), None)
        # Idle updates publish complete settings snapshots without a new turn.
        # The current settings can precede compaction; the writing turn_context
        # must still stay intact for encrypted-history provenance.
        current = next((index for index in reversed(view.history) if view.rows[index].kind in
                        ('turn_context', 'event_msg/thread_settings_applied')), None)
        indexes = sorted(set(indexes + ([context] if context is not None else []) +
                             ([header] if header is not None else []) + ([current] if current is not None else [])))
        try:
            bodies = adapter.transcript_bodies(session, view, indexes, max_bytes=BODY_LIMIT, reader=reader)
        except TranscriptRowsChanged:
            continue
        if len(bodies) != len(indexes):
            raise AskUnavailable('The Codex snapshot is incomplete')
        selected, latest, base, provider, turn_context = [], None, None, None, False
        for index, body in zip(indexes, bodies):
            fact = view.rows[index]
            if body is None:
                outer, _, inner = fact.kind.partition('/')
                if (index == current or outer in ('session_meta', 'turn_context') or
                        model_member(outer, inner, None, {}) is not False):
                    raise AskUnavailable('A Codex context row is unreadable or exceeds the snapshot limit')
                continue
            payload = mapping(body.get('payload'))
            if body.get('type') == 'session_meta':
                provider = payload.get('model_provider')
                instructions = payload.get('base_instructions')
                if instructions is not None:
                    if not isinstance(instructions, dict) or not isinstance(instructions.get('text'), str):
                        raise AskUnavailable('The recorded Codex base instructions cannot be read')
                    base = instructions['text']
            if body.get('type') == 'turn_context':
                turn_context = True
            if index == current:
                if body.get('type') == 'turn_context':
                    latest = payload
                    effort = payload.get('effort', payload.get('reasoning_effort'))
                else:
                    latest = mapping(payload.get('thread_settings'))
                    if payload.get('thread_id') != session.native_id:
                        raise AskUnavailable('The latest Codex settings have no proven owner for this conversation')
                    if latest.get('model_provider_id') != 'openai':
                        raise AskUnavailable('The latest Codex settings have no proven standard ChatGPT provider')
                    # This is a full snapshot: an omitted effort means default.
                    effort = latest.get('reasoning_effort')
            selected.append(RunRow(format='codex-jsonl', data=body))
        if provider != 'openai':
            raise AskUnavailable('The conversation has no proven standard ChatGPT provider')
        if not turn_context or latest is None or not isinstance(latest.get('model'), str) or not latest['model']:
            raise AskUnavailable('The Codex conversation has no recorded turn model')
        if effort is not None and not isinstance(effort, str):
            raise AskUnavailable('The recorded Codex effort cannot be read')
        return AskSnapshot(messages=tuple(selected), model=latest['model'], effort=effort,
                           base_instructions=base, native_id=session.native_id, tip=view.tip,
                           history_version=view.history_version)
    raise AskUnavailable('The Codex conversation kept changing while it was read')
