"""Core transcript identity and paging, over metadata without native row parsing."""
from dataclasses import replace
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from multiverse.harnesses.types import ObservedBinding, RowFact, TranscriptSource, TranscriptView, TranscriptRowsChanged
from multiverse.plugins.access import within, Denied
from multiverse.plugins.agent_files import AgentGone
from multiverse.runtime import transcripts as t
from multiverse.store import read, write
from multiverse.universe import agents, sessions
from test_multiverse import BaseTest


class Provider:
    family = 'claude'
    storage_env = ('CLAUDE_CONFIG_DIR',)
    name = 'fixture'

    def __init__(self, home):
        self.home, self.views, self.bodies, self.calls = home, {}, {}, []
        self.observation = None
        self.during_body = None

    def session_home(self, environment):
        return environment.get('CLAUDE_CONFIG_DIR')

    def observed_binding(self, session):
        return self.observation

    def transcript_view(self, session, *, tip=None, reader=None):
        reader.check()
        self.calls.append(('view', session.native_id, session.home))
        return self.views.get((session.native_id, tip), self.views[session.native_id])

    def transcript_bodies(self, session, view, indexes, *, max_bytes, reader=None):
        # Body reads must run after all launch/namespace locks were released.
        directory = Path(session.session_file).parent
        for path in (directory / '.launch.lock', directory.parents[1] / '.sessions.lock'):
            with path.open('a') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(stream, fcntl.LOCK_UN)
        self.calls.append(('bodies', tuple(indexes)))
        if self.during_body:
            self.during_body()
        reader.check()
        return [deepcopy(self.bodies[i]) for i in indexes]

    def transcript_readable(self, session, *, reader=None):
        return session.native_id in self.views


class TranscriptCoreTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory = self.root / 'state' / 'agents' / str(uuid4())
        self.native_id = str(uuid4())
        self.home = str(self.work / 'saved-home')
        write(self.directory / 'properties.json', {'id': self.directory.name, 'name': 'fixture', 'harness': 'claude',
              'workspace': str(self.work), 'native_session_id': self.native_id, 'native_session_home': self.home})
        write(self.directory / 'runtime.json', {'launch_id': 'launch', 'state': 'running', 'status': 'idle'})
        self.provider = Provider(self.home)
        self.enterContext(patch.object(t.harnesses, 'load', return_value=self.provider))
        self.set_rows(['prompt', 'response', 'prompt', 'response'])

    def set_rows(self, labels, *, history=None, context_from=0, sizes=None, refs=True, **changes):
        facts = tuple(RowFact(position=f'{i}:original', ref=f'r{i}' if refs else None,
                              kind='fixture', label=label, size=(sizes or [20] * len(labels))[i],
                              at=f'2026-10-25T10:{i:02d}:00Z', preview=f'preview {i}', **changes)
                      for i, label in enumerate(labels))
        self.provider.views[self.native_id] = TranscriptView(source=TranscriptSource(state='supported', home=self.home,
             format='fixture-json', size=sum(r.size for r in facts)), rows=facts,
             history=tuple(range(len(facts))) if history is None else tuple(history), context_from=context_from,
             tip=facts[-1].ref if facts else None, gap=None, history_version=0)
        self.provider.bodies = {i: {'body': [f'body {i}']} for i in range(len(labels))}
        return facts

    def test_pages_apply_bounds_before_reading_bodies_and_keep_each_cursor_boundary(self):
        """Filters and reverse pages can otherwise skip or repeat rows while oversized bodies bypass the read cap."""
        self.set_rows(['meta', 'prompt', 'meta', 'response', 'prompt'], sizes=[9000, 1200, 9000, t.MAX_BYTES + 1, 20])
        info = t.info(self.root, self.directory)
        page = t.rows(self.root, self.directory, after=info.start, limit=1, labels={'prompt'})
        self.assertEqual([row.ref for row in page.rows], ['r1'])
        self.assertEqual(self.provider.calls[-1], ('bodies', (1,)))
        self.assertEqual((page.remaining, page.at_end), (1, False))
        last = t.rows(self.root, self.directory, after=page.next, labels={'prompt'})
        self.assertEqual([row.ref for row in last.rows], ['r4'])
        self.assertTrue(last.at_end)
        backwards = t.rows(self.root, self.directory, before=info.end, limit=2)
        self.assertEqual([row.ref for row in backwards.rows], ['r4'])  # huge preceding row comes alone
        previous = t.rows(self.root, self.directory, before=backwards.previous, limit=2)
        self.assertEqual([row.ref for row in previous.rows], ['r3'])
        self.assertIsNone(previous.rows[0].data)
        self.assertNotIn(('bodies', (3,)), self.provider.calls)
        # A row above the page's byte bound but below the hard cap is returned alone, with a body.
        one = t.rows(self.root, self.directory, after='r0', max_bytes=1024)
        self.assertEqual([row.ref for row in one.rows], ['r1'])
        self.assertIsNotNone(one.rows[0].data)
        # Metadata traversal ignores byte bounds, and never asks for any body.
        self.provider.calls.clear()
        metadata = t.rows(self.root, self.directory, bodies=False, max_bytes=1024)
        self.assertEqual(len(metadata.rows), 5)
        self.assertFalse(any(call[0] == 'bodies' for call in self.provider.calls))
        one.rows[0].data['body'].append('caller edit')
        self.assertEqual(t.rows(self.root, self.directory, after='r0', limit=1).rows[0].data, {'body': ['body 1']})
        empty = t.rows(self.root, self.directory, labels={'not-a-label'})
        self.assertEqual((empty.rows, empty.next, empty.at_end), ((), info.end, True))
        for kwargs in ({'limit': 0}, {'limit': 2001}, {'limit': True}, {'max_bytes': 1023},
                       {'max_bytes': t.MAX_BYTES + 1}, {'after': info.start, 'before': info.end}):
            with self.subTest(kwargs), self.assertRaises(ValueError):
                t.rows(self.root, self.directory, **kwargs)

    def test_unchanged_metadata_is_reused_without_reusing_authority_or_bodies(self):
        """Large paged histories otherwise repeat a full analysis per page; caching must not skip current identity,
        changes to a view, or fresh bodies, since those failures can return another generation's data silently."""
        original_view = self.provider.views[self.native_id]
        with patch.object(t, '_validate_view', wraps=t._validate_view) as validate, \
                patch.object(t, '_build_groups', wraps=t._build_groups) as group, \
                patch.object(t, '_snapshot', wraps=t._snapshot) as snapshot:
            first = t.rows(self.root, self.directory, history=True, limit=1)
            second = t.rows(self.root, self.directory, history=True, after=first.next, limit=1)
            third = t.rows(self.root, self.directory, history=True, after=second.next, limit=1)
            self.assertEqual([first.rows[0].turn, second.rows[0].turn, third.rows[0].turn], ['r0', 'r0', 'r2'])
            self.assertEqual((validate.call_count, group.call_count, snapshot.call_count), (1, 1, 3))
            # The provider's fresh body is handed through once, not copied again at each layer.
            body = {'fresh': ['value']}
            with patch.object(self.provider, 'transcript_bodies', return_value=[body]):
                page = t.rows(self.root, self.directory, limit=1)
            self.assertIs(page.rows[0].data, body)
            # A new object is always validated and regrouped, even if its history_version and tip stayed the same.
            self.provider.views[self.native_id] = replace(original_view, rows=tuple(
                replace(row, label='response') if i == 2 else row for i, row in enumerate(original_view.rows)))
            changed = t.rows(self.root, self.directory)
            self.assertEqual([row.turn for row in changed.rows], ['r0'] * 4)
            self.assertEqual((validate.call_count, group.call_count), (2, 2))
            broken = replace(original_view, rows=(original_view.rows[0], original_view.rows[0]))
            self.provider.views[self.native_id] = broken
            with self.assertRaises(t.Unavailable): t.rows(self.root, self.directory)
            self.assertEqual(validate.call_count, 3)
            self.provider.views[self.native_id] = original_view
            # Reusing the exact view still cannot bypass a removed agent's snapshot.
            self.directory.rename(self.directory.with_suffix('.moved'))
            with self.assertRaises(AgentGone): t.rows(self.root, self.directory)
        # The bound retains metadata only, never every old view of a growing file.
        for version in range(t.VIEW_CACHE_SIZE + 2):
            t._analyze(replace(original_view, history_version=version))
        self.assertLessEqual(len(t._VIEW_CACHE), t.VIEW_CACHE_SIZE)

    def test_history_reorders_rows_and_distinguishes_rewrite_from_branch_divergence(self):
        """Compaction history order and a rewound branch must never be confused with raw-file positions."""
        self.set_rows(['prompt', 'response', 'compaction', 'prompt'], history=[2, 0, 3], context_from=1)
        info = t.info(self.root, self.directory)
        page = t.rows(self.root, self.directory, history=True, limit=1)
        self.assertEqual([r.ref for r in page.rows], ['r2'])
        self.assertEqual([r.ref for r in t.rows(self.root, self.directory, history=True, after=page.next).rows], ['r0', 'r3'])
        self.assertEqual([r.ref for r in t.rows(self.root, self.directory, history=True, after=info.context_start).rows], ['r0', 'r3'])
        self.assertEqual((info.context_rows, info.rows), (2, 4))
        raw = t.rows(self.root, self.directory)
        self.assertTrue(raw.rows[1].abandoned)
        self.assertIsNone(raw.rows[1].turn)
        self.assertFalse(raw.rows[2].in_context)
        old = self.provider.views[self.native_id]
        self.provider.views[(self.native_id, 'r3')] = old
        self.provider.views[self.native_id] = replace(old, history=(2, 0, 1), tip='r1', history_version=1)
        with self.assertRaises(t.Changed) as caught:
            t.rows(self.root, self.directory, history=True, after=raw.rows[3].cursor)
        self.assertEqual(caught.exception.reason, 'diverged')
        resumed = t.rows(self.root, self.directory, history=True, after=caught.exception.resume)
        self.assertEqual([r.ref for r in resumed.rows], ['r1'])
        # A raw cursor survives a rewind: the native bytes didn't change.
        self.assertEqual(t.rows(self.root, self.directory, after=raw.rows[3].cursor).rows, ())
        changed_rows = tuple(replace(r, position=r.position + '-rewrite') for r in old.rows)
        self.provider.views[self.native_id] = replace(old, rows=changed_rows)
        with self.assertRaises(t.Changed) as rewritten:
            t.rows(self.root, self.directory, after=raw.rows[1].cursor)
        self.assertEqual(rewritten.exception.reason, 'rewritten')
        self.assertEqual([r.ref for r in t.rows(self.root, self.directory, after=rewritten.exception.resume).rows], ['r2', 'r3'])
        for bad in ('mv2-row-v1:bad', t._pack(t.CURSOR_PREFIX, {'u': 'someone else'}), 'missing ref'):
            with self.subTest(bad), self.assertRaises(ValueError):
                t.rows(self.root, self.directory, after=bad)

    def test_turns_group_only_labels_and_need_native_end_evidence(self):
        """A mid-turn command must not take the prompt's later response or mark that prompt finished from idle status."""
        facts = self.set_rows(['meta', 'prompt', 'response', 'command', 'command_output', 'compaction', 'response'])
        old = self.provider.views[self.native_id]
        facts = tuple(replace(r, command='/compact', args='keep tests') if i == 3 else r for i, r in enumerate(facts))
        self.provider.views[self.native_id] = replace(old, rows=facts)
        info = t.info(self.root, self.directory)
        page = t.turns(self.root, self.directory)
        self.assertEqual([(r.id, r.kind, r.rows, r.finished) for r in page.turns],
                         [('preamble', 'preamble', 1, None), ('r1', 'prompt', 3, False), ('r3', 'command', 3, True)])
        self.assertEqual((page.turns[2].command, page.turns[2].args), ('/compact', 'keep tests'))
        self.assertEqual(info.turns, 3)
        self.assertEqual([row.turn for row in t.rows(self.root, self.directory).rows],
                         ['preamble', 'r1', 'r1', 'r3', 'r3', 'r3', 'r1'])
        self.assertEqual(info.context_start, info.start)
        self.assertEqual([r.id for r in t.turns(self.root, self.directory, before=info.end).turns],
                         ['preamble', 'r1', 'r3'])
        self.assertEqual([r.ref for r in t.turn(self.root, self.directory, 'r1', after=page.turns[1].start).rows],
                         ['r1', 'r2', 'r6'])
        prompt = t.turn(self.root, self.directory, 'r1', limit=2)
        self.assertEqual([r.ref for r in prompt.rows], ['r1', 'r2'])
        self.assertEqual([r.ref for r in t.turn(self.root, self.directory, 'r1', after=prompt.next).rows], ['r6'])
        first = t.turns(self.root, self.directory, limit=2)
        second = t.turns(self.root, self.directory, after=first.next)
        self.assertEqual([r.id for r in second.turns], ['r3'])
        self.assertEqual([r.id for r in t.turns(self.root, self.directory, before=second.previous).turns], ['preamble', 'r1'])
        for at, expected in ((None, None), ('not-time', None), ('2026-10-25T10:06:00', None),
                             ('2026-10-25T10:00:00Z', False), ('2026-10-25T10:06:00Z', True)):
            with self.subTest(at):
                self.provider.views[self.native_id] = replace(old, rows=(*facts[:-1], replace(facts[-1], ends_turn=True, at=at)))
                self.assertEqual(t.turns(self.root, self.directory).turns[1].finished, expected)
        self.set_rows(['prompt', 'response', 'prompt'])
        self.assertEqual([r.finished for r in t.turns(self.root, self.directory).turns], [True, False])
        self.set_rows(['prompt', 'response'], refs=False)
        no_ref = t.turns(self.root, self.directory).turns[0]
        self.assertTrue(no_ref.id.startswith('@' + t.CURSOR_PREFIX))
        self.assertEqual(t.turn(self.root, self.directory, no_ref.id).rows[0].ref, None)
        for labels, history, expected in (
            (['command', 'meta', 'command_output', 'command', 'meta', 'prompt', 'response'], None,
             [('r0', 'command', 2), ('preamble', 'preamble', 2), ('r3', 'command', 1), ('r5', 'prompt', 2)]),
            (['meta', 'prompt', 'command', 'response'], [2, 0, 3, 1],
             [('r2', 'command', 1), ('preamble', 'preamble', 2), ('r1', 'prompt', 1)]),
        ):
            with self.subTest('preamble after the first command', history=history):
                self.set_rows(labels, history=history)
                grouped = t.turns(self.root, self.directory)
                self.assertEqual([(r.id, r.kind, r.rows) for r in grouped.turns], expected)
                first = t.turns(self.root, self.directory, limit=1)
                following = t.turns(self.root, self.directory, after=first.next, limit=1)
                self.assertEqual((first.turns[0].id, following.turns[0].id), (expected[0][0], 'preamble'))
                previous = t.turns(self.root, self.directory, before=following.previous)
                self.assertEqual([r.id for r in previous.turns], [expected[0][0]])

    def test_generations_are_explicit_and_old_cursors_cannot_select_them(self):
        """A /clear can otherwise make an old cursor silently read the previous conversation or the caller's home."""
        original = t.info(self.root, self.directory)
        old_page = t.rows(self.root, self.directory)
        new_id = str(uuid4())
        self.provider.views[new_id] = self.provider.views[self.native_id]
        agents.rebind_session(self.root, self.directory.name, self.native_id, new_id, 'clear', 'launch', home=self.home)
        current = t.info(self.root, self.directory)
        self.assertEqual([(r.native_id, r.reason, r.readable) for r in current.earlier], [(self.native_id, 'clear', True)])
        self.assertEqual(current.earlier[0].generation, original.generation)
        for selected, expected in ((None, current.generation), (original.generation, original.generation)):
            page = t.turns(self.root, self.directory, generation=selected)
            self.assertTrue(all(t._unpack(turn.start, t.CURSOR_PREFIX)['g'] == expected for turn in page.turns))
        with self.assertRaises(t.Changed) as replaced:
            t.rows(self.root, self.directory, after=old_page.next)
        self.assertEqual((replaced.exception.reason, replaced.exception.resume), ('replaced', current.start))
        old = t.rows(self.root, self.directory, generation=original.generation)
        self.assertEqual(old.generation, original.generation)
        duplicate = self.root / 'state' / 'agents' / str(uuid4())
        write(duplicate / 'properties.json', {**read(self.directory / 'properties.json'), 'id': duplicate.name,
              'name': 'conflicting old owner', 'native_session_id': self.native_id})
        with self.assertRaises(t.Unavailable):
            t.rows(self.root, self.directory, generation=original.generation)
        (duplicate / 'properties.json').unlink()
        duplicate.rmdir()
        with self.assertRaisesRegex(ValueError, 'another generation'):
            t.rows(self.root, self.directory, generation=original.generation, after=current.start)
        self.assertEqual(t.info(self.root, self.directory, generation=t._pack(t.GENERATION_PREFIX, {'bad': True})).state,
                         'unavailable')

    def test_snapshot_refuses_busy_changed_damaged_foreign_and_unowned_records(self):
        """A transcript's content must never be exposed under another agent, generation or caller-selected home."""
        original = read(self.directory / 'properties.json')
        token = t.info(self.root, self.directory).start
        foreign = t._unpack(token, t.CURSOR_PREFIX)
        foreign['a'] = str(uuid4())
        with self.assertRaisesRegex(ValueError, 'another agent'):
            t.rows(self.root, self.directory, after=t._pack(t.CURSOR_PREFIX, foreign))
        with (self.directory / '.launch.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(t.Retry):
                t.rows(self.root, self.directory)
        self.provider.during_body = lambda: write(self.directory / 'runtime.json', {'launch_id': 'new-launch'})
        with self.assertRaises(t.Retry):
            t.rows(self.root, self.directory)
        self.provider.during_body = None
        write(self.directory / 'runtime.json', {'launch_id': 'launch'})
        with (self.root / 'state' / '.sessions.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(t.Retry):
                t.rows(self.root, self.directory)
        with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': '/not-recorded', 'MV2_AGENT_DIR': '/another-agent'}):
            write(self.directory / 'properties.json', {k: v for k, v in original.items() if k != 'native_session_home'})
            self.assertEqual(t.info(self.root, self.directory).state, 'unavailable')
            with self.assertRaises(t.Unavailable):
                t.rows(self.root, self.directory)
        with patch.dict(os.environ, {'MV2_AGENT_DIR': str(self.directory)}):
            own = t.info(self.root, self.directory)
            self.assertEqual(own.state, 'supported')
            self.assertNotIn('native_session_home', read(self.directory / 'properties.json'))
        # A storage map proves the same home without borrowing this process's environment.
        write(self.directory / 'properties.json', {**{k: v for k, v in original.items() if k != 'native_session_home'},
              'native_storage_environment': {'CLAUDE_CONFIG_DIR': self.home}})
        with patch.dict(os.environ, {'MV2_AGENT_DIR': '/not-this-agent', 'CLAUDE_CONFIG_DIR': '/not-recorded'}):
            self.assertEqual(t.info(self.root, self.directory).state, 'supported')
        write(self.directory / 'properties.json', original)
        good = self.provider.views[self.native_id]
        self.provider.views[self.native_id] = replace(good, source=replace(good.source, home='/another-home'))
        self.assertEqual(t.info(self.root, self.directory).state, 'unavailable')
        self.provider.views[self.native_id] = good
        self.assertEqual(own.generation, t.info(self.root, self.directory).generation)
        with within('observe', 'fixture'):
            self.assertEqual(t.info(self.root, self.directory).state, 'supported')
        # There is no transport or mutation surface for a provider to use.
        with patch.object(t, 'READ_SECONDS', -1):
            with self.assertRaises(t.Retry):
                t.rows(self.root, self.directory)
        reader = t._snapshot(self.root, self.directory).reader
        self.assertFalse(any(hasattr(reader, name) for name in ('request', 'rpc', 'command', 'write_lines', 'start', 'stop')))
        for name in ('runtime.json', 'sessions.jsonl'):
            path = self.directory / name
            before = path.read_bytes() if path.exists() else None
            path.write_text('{')
            try:
                result = t.info(self.root, self.directory)
                self.assertNotEqual(result.state, 'supported')
                self.assertIn(name, result.reason)
            finally:
                path.write_bytes(before) if before is not None else path.unlink()
        other = self.root / 'state' / 'agents' / str(uuid4())
        write(other / 'properties.json', {**original, 'id': other.name, 'name': 'other'})
        self.assertEqual(t.info(self.root, self.directory).state, 'unavailable')
        (other / 'properties.json').unlink()
        other.rmdir()
        gone = self.directory.with_name(str(uuid4()))
        self.directory.rename(gone)
        with self.assertRaises(AgentGone):
            t.info(self.root, self.directory)

    def test_observed_clear_is_reconciled_before_a_lock_free_read(self):
        """Observe may journal a proved /clear, but no native command or caller storage can choose the new binding."""
        before = t.info(self.root, self.directory)
        new_id = str(uuid4())
        self.provider.views[new_id] = self.provider.views[self.native_id]
        self.provider.observation = ObservedBinding(native_id=new_id, home=self.home, evidence='fixture')
        after = t.info(self.root, self.directory)
        self.assertEqual((after.state, after.native_id), ('supported', new_id))
        self.assertEqual(after.earlier[0].generation, before.generation)
        journal = sessions.journal(self.directory)
        self.assertEqual((len(journal), journal[0]['reason']), (1, 'detected'))
        page = t.rows(self.root, self.directory)
        self.assertEqual(len(page.rows), 4)
        self.assertEqual(len(sessions.journal(self.directory)), 1)

    def test_a_rewrite_during_body_read_returns_a_safe_resume_without_native_content(self):
        """A page cannot combine old facts with rewritten bodies or leak a provider exception containing transcript text."""
        page = t.rows(self.root, self.directory, limit=1)
        def rewrite():
            old = self.provider.views[self.native_id]
            self.provider.views[self.native_id] = replace(old, rows=tuple(
                replace(r, position=r.position + '-rewrite') for r in old.rows))
            self.provider.during_body = None
            raise TranscriptRowsChanged()
        self.provider.during_body = rewrite
        with self.assertRaises(t.Changed) as caught:
            t.rows(self.root, self.directory, after=page.next)
        self.assertEqual(caught.exception.reason, 'rewritten')
        self.assertEqual([r.ref for r in t.rows(self.root, self.directory, after=caught.exception.resume).rows],
                         ['r1', 'r2', 'r3'])
        self.provider.during_body = rewrite
        with self.assertRaises(t.Changed) as start:
            t.rows(self.root, self.directory)
        self.assertIsNone(start.exception.resume)
        def failure():
            raise RuntimeError('private native content')
        self.provider.during_body = failure
        with self.assertRaises(t.Unavailable) as failed:
            t.rows(self.root, self.directory)
        self.assertNotIn('private native content', str(failed.exception))

    def test_explicit_boundaries_cross_off_history_metadata_without_false_divergence(self):
        """A tail or previous-page cursor must not name an off-history neighbour and reject an unchanged history."""
        self.set_rows(['prompt', 'meta', 'response', 'meta'], history=[0, 2], context_from=1)
        before = t.info(self.root, self.directory)
        tail = t.rows(self.root, self.directory, before=before.end, history=True, limit=1)
        self.assertEqual([r.ref for r in tail.rows], ['r2'])
        earlier = t.rows(self.root, self.directory, before=tail.previous, history=True)
        self.assertEqual([r.ref for r in earlier.rows], ['r0'])
        self.assertEqual([r.ref for r in t.rows(self.root, self.directory, after=before.context_start, history=True).rows], ['r2'])
        # Both raw and history end cursors pin the old prefix, rather than silently following appends.
        self.set_rows(['prompt', 'meta', 'response', 'meta', 'prompt'], history=[0, 2, 4], context_from=1)
        for history in (False, True):
            with self.subTest(history=history):
                page = t.rows(self.root, self.directory, after=before.end, history=history)
                self.assertEqual([r.ref for r in page.rows], ['r4'])
        # Row-side cursors still detect a real divergence when their own row leaves history.
        old = self.provider.views[self.native_id]
        self.provider.views[(self.native_id, 'r2')] = old
        self.provider.views[self.native_id] = replace(old, history=(0, 4), context_from=1)
        with self.assertRaises(t.Changed) as changed:
            t.rows(self.root, self.directory, before=tail.previous, history=True)
        self.assertEqual(changed.exception.reason, 'diverged')
