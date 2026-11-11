"""Replacement gaps at optional harness, terminal and UI record readers, without native processes."""
from contextlib import contextmanager
import errno
import io
import json
import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from test_multiverse import BaseTest
from multiverse import harnesses, store
from multiverse.harnesses import claude
from multiverse.harnesses.codex import prompt_files, usage
from multiverse.harnesses.codex import plugins as codex_plugins
from multiverse.harnesses.types import CommandSpec, LaunchContext, SessionHandle
from multiverse.terminal import recovery, statusline
from multiverse.tools import inbox, send
from multiverse.ui import views


@contextmanager
def missing(path, failures, number=errno.ENOENT, *, probe=True):
    """Fail only reads of this pathname, including descriptor-relative prompt reads."""
    calls = []
    def wrap(original):
        def read(candidate, *args, **kwargs):
            selected = (isinstance(candidate, (str, os.PathLike)) and
                        (Path(candidate) == path or kwargs.get('dir_fd') is not None and candidate == path.name))
            if selected:
                calls.append(candidate)
                if failures is None or len(calls) <= failures:
                    raise OSError(number, os.strerror(number), str(path))
            return original(candidate, *args, **kwargs)
        return read
    with patch('io.open', side_effect=wrap(io.open)), patch('os.open', side_effect=wrap(os.open)), \
            patch('os.stat', side_effect=wrap(os.stat) if probe else os.stat), patch('time.sleep'):
        yield calls


class NetworkEdgeRecordsTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory = self.work / 'agent'
        self.directory.mkdir()
        self.native = str(uuid4())
        self.session = SessionHandle('agent', 'claude', str(self.work), native_id=self.native, launch_id='launch',
                                     session_file=str(self.directory / 'session.jsonl'))

    def assert_gaps(self, path, reader, present, absent, *, probe=True):
        self.assertEqual(reader(), present)
        for failures in (1, 3, None):
            with self.subTest(path=path.name, failures=failures), missing(path, failures, probe=probe) as calls:
                if failures is None and isinstance(absent, type) and issubclass(absent, Exception):
                    with self.assertRaises(absent):
                        reader()
                else:
                    self.assertEqual(reader(), absent if failures is None else present)
                self.assertGreater(len(calls), 1)
                # A stat followed by an open may add one successful read after its probe.
                self.assertLessEqual(len(calls), store.MISSING_ATTEMPTS + 1)

    def test_terminal_records_survive_gaps_without_inventing_missing_values(self):
        """A replacement gap must not hide status, consume unread counts or discard saved server identity."""
        path = self.directory / 'focus.json'
        store.write(path, {'since': '2026-11-10T12:00:00Z', 'by': 'agent', 'turns': 1})
        self.assert_gaps(path, lambda: statusline.focus_text(self.directory), 'focused (turn)', '')
        path = self.directory / 'status-items' / 'cost.txt'
        path.parent.mkdir()
        path.write_text('Cost: 7\n')
        self.assert_gaps(path, lambda: statusline.plugin_item(self.directory, 'cost'), 'Cost: 7', '')

        manual = {'id': 'manual', 'schema': 2, 'delivery': 'inbox-only'}
        line = (json.dumps(manual) + '\n').encode()
        (self.directory / 'inbox.jsonl').write_bytes(line)
        path = self.directory / 'inbox.cursor'
        path.write_text(str(len(line)))
        self.assert_gaps(path, lambda: statusline.unread(self.directory), 0, 1)
        path.write_text('0')
        path = self.directory / 'inbox-read.json'
        store.write(path, {'manual': '2026-11-10T12:00:00Z'})
        self.assert_gaps(path, lambda: statusline.unread(self.directory), 0, 1)
        store.write(path, {})
        follow_up = {'id': 'copy', 'schema': 2, 'delivery': 'follow-up', 'inbox_only_text': 'Please review'}
        (self.directory / 'inbox.jsonl').write_text(json.dumps(follow_up) + '\n')
        path = self.directory / 'inbox-state.json'
        store.write(path, {'copy': {'inbox_copy_read_at': '2026-11-10T12:00:00Z'}})
        self.assert_gaps(path, lambda: statusline.unread(self.directory), 0, 1)

        socket = self.work / 'tmux.sock'
        path = recovery.registry(socket)
        proof = {'socket': str(socket), 'pid': 42, 'started': 'Tue Nov 10 12:00:00 2026'}
        store.write(path, proof)
        self.assert_gaps(path, lambda: recovery._saved(socket), proof, None)
        with missing(path, None, errno.EACCES) as calls, self.assertRaises(recovery.Unreachable):
            recovery._saved(socket)
        self.assertEqual(len(calls), 1)
        path.unlink()
        path.symlink_to(self.directory / 'focus.json')
        with self.assertRaises(recovery.Unreachable):
            recovery._saved(socket)

    def status_fixture(self, plugins=1):
        directory = self.root / 'state' / 'agents' / 'reader'
        store.write(directory / 'properties.json', {
            'id': 'reader', 'universe_id': store.read(self.root / 'universe.json')['id'], 'name': 'reader',
        })
        store.write(directory / 'runtime.json', {'launch_id': 'launch', 'plugins': {
            'status_items': [{'name': f'item-{index}', 'tmux': True} for index in range(plugins)],
        }})
        (directory / 'inbox.jsonl').write_text(json.dumps({'id': 'manual', 'schema': 2, 'delivery': 'inbox-only'}) + '\n')
        return directory, lambda: statusline.render(self.root, 'reader', 'launch')

    def test_render_batches_brief_and_lasting_optional_absences(self):
        """Batched absence must recover each optional reader without changing its genuine-absence fallback."""
        directory, render = self.status_fixture()
        manual = json.dumps({'id': 'manual', 'schema': 2, 'delivery': 'inbox-only'}) + '\n'
        copy = json.dumps({'id': 'copy', 'schema': 2, 'delivery': 'follow-up', 'inbox_only_text': 'review'}) + '\n'
        defaults = {
            self.root / 'statusline.json': '{}', directory / 'focus.json': '{}', directory / 'inbox.cursor': '0',
            directory / 'inbox-state.json': '{}', directory / 'inbox-read.json': '{}',
            directory / 'status-items' / 'item-0.txt': '',
        }
        rows = (
            (self.root / 'statusline.json', '{"tmux":{"items":{"agent":false}}}', manual, 'inbox 1'),
            (directory / 'focus.json', '{"since":"2026-11-10T12:00:00Z","by":"agent","turns":1}', manual,
             'reader · focused (turn) · inbox 1'),
            (directory / 'inbox.cursor', str(len(manual.encode())), manual, 'reader'),
            (directory / 'inbox-state.json', '{"copy":{"inbox_copy_read_at":"2026-11-10T12:00:00Z"}}', copy, 'reader'),
            (directory / 'inbox-read.json', '{"manual":"2026-11-10T12:00:00Z"}', manual, 'reader'),
            (directory / 'status-items' / 'item-0.txt', 'Cost: 7', manual, 'reader · inbox 1 · Cost: 7'),
        )
        for path, value, log, present in rows:
            with self.subTest(record=path.name):
                for file, text in defaults.items():
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_text(text)
                (directory / 'inbox.jsonl').write_text(log)
                path.write_text(value)
                self.assert_gaps(path, render, present, 'reader · inbox 1')
                # A successful batch stat is only a hint: a later missing open still gets its bounded retry.
                self.assert_gaps(path, render, present, 'reader · inbox 1', probe=False)

    def test_render_absence_delay_does_not_grow_with_plugin_item_count(self):
        """Unpublished optional files must share one wait or every new plugin item slows every status refresh."""
        (self.root / 'statusline.json').unlink(missing_ok=True)
        for count in (0, 1, 32):
            with self.subTest(plugins=count):
                directory, render = self.status_fixture(count)
                now, delays = [0.0], []
                def delay(seconds):
                    delays.append(seconds)
                    now[0] += seconds
                with patch.object(statusline.time, 'monotonic', side_effect=lambda: now[0]), \
                        patch.object(statusline.time, 'sleep', side_effect=delay):
                    self.assertEqual(render(), 'reader · inbox 1')
                self.assertEqual(delays, [.005] * 4)
                self.assertLessEqual(sum(delays), .05)
                self.assertFalse((directory / 'inbox.cursor').exists())

    def test_render_absence_snapshot_never_caches_optional_files_or_current_identity(self):
        """A prior render must not hide new optional data or authorize output for a replaced agent or launch."""
        directory, render = self.status_fixture()
        self.assertEqual(render(), 'reader · inbox 1')
        item = directory / 'status-items' / 'item-0.txt'
        item.parent.mkdir()
        item.write_text('Cost: 7')
        self.assertEqual(render(), 'reader · inbox 1 · Cost: 7')
        item.unlink()
        self.assertEqual(render(), 'reader · inbox 1')
        for path, key in ((directory / 'properties.json', 'id'), (directory / 'runtime.json', 'launch_id'),
                          (self.root / 'universe.json', 'id')):
            with self.subTest(required=path.name):
                saved = store.read(path)
                store.write(path, {**saved, key: 'replacement'})
                self.assertEqual(render(), '')
                store.write(path, saved)
                self.assert_gaps(path, render, 'reader · inbox 1', '', probe=False)

    def test_render_batch_preserves_nonmissing_errors_and_guarded_reads(self):
        """A batch probe cannot turn damaged or inaccessible inbox evidence into a valid absent ledger."""
        directory, render = self.status_fixture(0)
        path = directory / 'inbox-state.json'
        store.write(path, {})
        for error in (errno.EACCES, errno.EIO):
            with self.subTest(error=error), missing(path, None, error) as calls:
                self.assertEqual(render(), 'reader · inbox ?')
                self.assertEqual(len(calls), 2)  # One probe, then the reader's existing error policy.
        for contents in ('[]', '{', ' ' * (statusline.READ_LIMIT + 1)):
            with self.subTest(contents=contents[:10]):
                path.write_text(contents)
                self.assertEqual(render(), 'reader · inbox ?')
        path.unlink()
        os.mkfifo(path)
        self.assertEqual(render(), 'reader · inbox ?')

    def test_harness_reports_keep_replaced_record_evidence(self):
        """Briefly missing reports must not erase stored-message, context or queued-prompt evidence."""
        adapter = claude.ClaudeAdapter.configured('claude', {})
        path = self.directory / 'properties.json'
        store.write(path, {'name': 'reader'})
        self.assert_gaps(path, lambda: claude._agent_name(self.session), 'reader', None)
        path = self.directory / 'inbox-state.json'
        store.write(path, {str(uuid4()): {'target_session': self.native}})
        with patch.object(claude, 'mod_receipt', return_value=('delivered', {})):
            self.assert_gaps(path, lambda: claude._stored_message(self.directory, self.native), True, False)

        path = self.directory / 'runtime.json'
        measurement = {'context': {'tokens': 123}, 'reported_at': '2026-11-10T12:00:00Z',
                       'native_session_id': self.native, 'source': 'codex-app-server'}
        store.write(path, {'launch_id': 'launch', 'state': 'running', 'usage': measurement})
        self.assert_gaps(path, lambda: claude._context_since_move(self.directory, self.native), True, False)
        with patch.object(claude, 'launch_storage', return_value={}), \
                patch.object(claude, 'measured_usage', return_value=None), \
                patch.object(claude, 'context_reading', return_value=None):
            self.assert_gaps(path, lambda: adapter.get_usage(self.session).state, 'ok', 'unavailable')
        self.assert_gaps(path, lambda: usage.measured(self.session).state, 'ok', 'unavailable')

        path = self.directory / claude.QUEUE_FILE
        store.write(path, {'schema': 1, 'launch_id': 'launch', 'session_id': self.native,
                           'coverage': 'complete', 'entries': []})
        with patch.object(adapter, '_queue_bridge', return_value=None):
            self.assert_gaps(path, lambda: adapter.queued_prompts(self.session).value['coverage'], 'complete', 'unknown')
        # Receipts and exclusive answers keep their own first-absence protocol.
        with missing(path, 1) as calls:
            self.assertIsNone(claude._json_record(path))
        self.assertEqual(len(calls), 1)

    def test_codex_replaced_prompts_keep_descriptor_guards_and_exclusive_claims(self):
        """A prompt publication gap must not hide an open/result record or weaken no-follow and claim ownership."""
        files = prompt_files.Files(str(self.directory), 'launch')
        value = {'schema': 1, 'known': True}
        folder = self.directory / 'codex-prompts' / 'launch'
        for name in ('index.json', 'question.open.json', 'question.result.json'):
            with self.subTest(name=name):
                files.write(name, value)
                path = folder / name
                self.assert_gaps(path, lambda: files.read(name), value, FileNotFoundError)
                with missing(path, None, errno.EACCES) as calls, self.assertRaises(PermissionError):
                    files.read(name)
                self.assertEqual(len(calls), 1)
                path.unlink()
                path.symlink_to(self.directory / 'elsewhere')
                with self.assertRaises(OSError) as error:
                    files.read(name)
                self.assertEqual(error.exception.errno, errno.ELOOP)
        claim = folder / 'question.input.json'
        with missing(claim, 1) as calls:
            self.assertEqual(files.input_claim('question', self.native).disposition, 'unclaimed')
        self.assertEqual(len(calls), 1)
        (self.directory / 'prompts').mkdir()
        with missing(self.directory / 'prompts' / 'question.answer.json', 1) as calls:
            self.assertIsNone(files.take('question'))
        self.assertEqual(len(calls), 1)

    def test_optional_ui_and_cli_reads_preserve_their_absence_policy(self):
        """A missing open or stat must not reset the view, end inbox waiting or hide readable send advice."""
        path = self.root / 'views' / 'default.json'
        store.write(path, {'title': 'Review', 'accent': '#123456'})
        self.assert_gaps(path, lambda: views.view(self.root)['title'], 'Review', '')
        self.assert_gaps(path, lambda: views.view(self.root)['title'], 'Review', '', probe=False)
        store.write(path, {'title': 'Review'})
        self.assert_gaps(self.root / 'universe.json', lambda: views.view(self.root)['title'], 'Review', '')
        # A non-file view was optional before retries too, and must never be opened as a FIFO.
        path.unlink()
        os.mkfifo(path)
        self.assertEqual(views.view(self.root)['widgets'], [])

        path = self.directory / 'properties.json'
        store.write(path, {'name': 'reader'})
        with patch.object(send.agents, 'runtime', return_value={}), \
                patch.object(send.native, 'observe', return_value={'live': 'alive'}):
            self.assert_gaps(path, lambda: send._running(self.directory), True, False)
        with patch.object(inbox, 'callback', return_value=(self.root, self.directory, None)), \
                patch.object(inbox.mailbox, 'inbox', return_value={'messages': [{'text': 'ready'}], 'cursor': 0}):
            self.assert_gaps(path, lambda: inbox.run(self.root, wait=True)['messages'][0]['text'], 'ready', ValueError)
        path = self.root / 'harnesses' / 'claude' / 'harness.json'
        self.assert_gaps(path, lambda: harnesses.load(self.root, 'claude').name, 'claude', ValueError)
        context = LaunchContext('spawn', {'agent_dir': str(self.root / 'state' / 'agents' / 'worker'),
                                         'workspace': str(self.work), 'bootstrap_json': '"bootstrap"'})
        command = CommandSpec(argv=('fake-codex',))
        plan = codex_plugins.harness.LaunchPlan(environment={'KNOWN': 'yes'})
        with patch.object(codex_plugins.harness, 'launch_plan', return_value=plan):
            self.assert_gaps(self.root / 'universe.json',
                             lambda: codex_plugins.prepare(context, command).plan.environment.get('KNOWN'), 'yes', None)
