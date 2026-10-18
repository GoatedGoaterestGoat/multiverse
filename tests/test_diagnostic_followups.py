"""Upgrade and partial-state diagnostics preserve unrelated work and finality."""
import contextlib
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from multiverse import cli, harnesses
from multiverse.runtime import lifecycle, native
from multiverse.store import lock, now, read, uid, write
from multiverse.tools import doctor, inbox, list_agents, report
from multiverse.ui.backend import harnesses as discover
from multiverse.universe import agents, mailbox
from test_multiverse import BaseTest


class DefinitionFollowupTest(BaseTest):
    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
             patch.object(sys, 'argv', ['mv2', '-u', str(self.root), *args]):
            try:
                cli.main()
            except SystemExit as error:
                code = error.code
        return code, out.getvalue(), err.getvalue()

    def test_damaged_harness_definitions_stay_local(self):
        """A truncated, wrong-typed, unknown-key or future-schema harness.json fails alone: catalog, catalog --json,
        command help, doctor, report and inbox still run and the other harnesses stay discoverable; fragile because
        every command and callback loads all definitions, and the typeahead and hooks swallow failures (50d4567)."""
        broken = self.root / 'harnesses/broken/harness.json'
        write(broken, {})
        broken.write_text('{"schema":\n')
        for args in (('catalog',), ('catalog', '--json'), ('command', 'help')):
            with self.subTest(args=args):
                code, out, err = self.call(*args)
                self.assertEqual(code, 0, err)
                self.assertTrue(out)
                self.assertIn(str(broken), err)
                self.assertEqual(len(err.splitlines()), 1)
        self.assertNotIn('harnesses/broken:', self.call('catalog')[1])
        with self.subTest('doctor names each damaged file and keeps the other problems'):
            second = self.root / 'harnesses/broken-two/harness.json'
            write(second, {})
            second.write_text('{"schema":\n')
            future = self.root / 'harnesses/future/harness.json'
            write(future, {'schema': 7})
            directory = self.root / 'state/agents' / uid()
            write(directory / 'properties.json', {'id': directory.name, 'name': 'other', 'harness': 'claude',
                                                  'workspace': str(self.work / 'missing-workspace')})
            problems = doctor.run(self.root)['problems']
            for path in (broken, second):
                self.assertTrue(any(str(path) in reason for reason in problems), problems)
            self.assertTrue(any(str(future) in reason and 'schema' in reason.lower() for reason in problems), problems)
            self.assertTrue(any('other' in reason and 'missing-workspace' in reason for reason in problems), problems)
        codex = self.root / 'harnesses/codex/harness.json'
        for data in ([], {'schema': 19}, {'schema': 2, 'adapter': 'missing'},
                     {'schema': 2, 'adapter': 'codex', 'settings': []},
                     {'schema': 2, 'adapter': 'codex', 'settings': {'typo': True}},
                     {'schema': 2, 'adapter': 'codex', 'settings': {'args': '--bad'}},
                     {'schema': 2, 'adapter': 'codex', 'settings': {'args': ['{unknown}']}},
                     {'schema': 2, 'adapter': 'claude', 'settings': {'inbox_bridge': 'yes'}},
                     {'schema': 2, 'adapter': 'codex', 'settings': {'env': ['IS_SANDBOX=1']}},
                     {'schema': 2, 'adapter': 'codex', 'settings': {'env': {'IS_SANDBOX': 1}}},
                     {'schema': 2, 'adapter': 'codex', 'settings': {'env': {'BAD-NAME': '1'}}},
                     {'schema': 2, 'adapter': 'codex', 'settings': {'env': {'1BAD': '1'}}},
                     {'schema': 2, 'adapter': 'codex', 'settings': {'env': {'NUL': 'a\0b'}}},
                     {'schema': 1, 'spawn': ['binary'], 'env': {'PATH': '/x'}},
                     {'schema': 1, 'spawn': []}, {'schema': 1, 'spawn': [22]},
                     {'schema': 1, 'spawn': ['binary'], 'bootstrap': []}):
            with self.subTest(definition=data):
                write(codex, data)
                with self.assertRaises(ValueError):
                    harnesses.load(self.root, 'codex')
                discovered = discover(self.root)
                self.assertNotIn('codex', discovered)
                self.assertIn('claude', discovered)
        # An agent's own callbacks still work while its harness definition is damaged.
        identity = str(uuid4())
        directory = self.root / 'state/agents' / identity
        properties = {'id': identity, 'name': 'worker', 'harness': 'codex', 'workspace': str(self.work)}
        write(directory / 'properties.json', properties)
        write(directory / 'runtime.json', {'launch_id': 'current'})
        environment = {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': identity,
                       'MV2_AGENT_DIR': str(directory), 'MV2_LAUNCH_ID': 'current'}
        mailbox.append_message(directory, 'still readable', {}, delivery='inbox-only')
        valid = json.dumps({'schema': 2, 'adapter': 'codex',
                            'settings': {'inbox_bridge': True, 'args': ['--remote', 'unix:///custom']}})
        for config in (valid, '{bad-json', '[]'):
            with self.subTest(callbacks_with=config), patch.dict(os.environ, environment, clear=True):
                codex.write_text(config)
                self.assertEqual(report.run(self.root, agent=identity, phase='working')['phase'], 'working')
                self.assertEqual(inbox.run(self.root, agent=identity, peek=True)['messages'][0]['text'],
                                 'still readable')
        with self.subTest('a wrong-typed harness name'), patch.dict(os.environ, environment, clear=True):
            write(directory / 'properties.json', {**properties, 'harness': []})
            self.assertEqual(report.run(self.root, agent=identity, summary='saved')['summary'], 'saved')
            self.assertEqual([m['text'] for m in inbox.run(self.root, agent=identity)['messages']], ['still readable'])

    def test_progress_and_terminal_writes_preserve_preparing_but_generation_or_state_changes_do_not(self):
        """Only allow-listed progress and terminal writes keep a launch counted as preparing, and a new launch id or
        state ends it; fragile because each new runtime field written during preparation must join the list, or doctor
        raises false alarms and remove or resume races a live launch (fbbdfe2)."""
        with self.subTest('doctor ignores a full starting record only while its launch lock is held'):
            directory = self.root / 'state/agents' / uid()
            write(directory / 'properties.json', {'id': directory.name, 'name': 'preparing',
                'harness': 'claude', 'workspace': str(self.work)})
            launch_id = uid()
            write(directory / 'runtime.json', {'launch_id': launch_id, 'state': 'starting',
                'host': 'fixture', 'started_at': now(), 'native_mode': {'mode': 'plain'}})
            write(directory / 'launches' / launch_id / 'registration.json', {'stage': 'preparing terminal status'})
            with lock(directory / '.launch.lock'):
                self.assertEqual(doctor.run(self.root)['problems'], [])
            self.assertTrue(doctor.run(self.root)['problems'])
        directory = self.root / 'state/agents' / uid()
        facts = {'launch_id': uid(), 'state': 'starting', 'host': 'fixture'}
        write(directory / 'runtime.json', facts)
        (directory / 'launches' / facts['launch_id']).mkdir(parents=True)
        with lock(directory / '.launch.lock'):
            for changes, expected in (({'observed_at': now(), 'native_versions': {'cli': 'fixture'}}, True),
                                      ({'launch_id': uid()}, False), ({'state': 'failed'}, False),
                                      ({'tmux': {'socket': str(self.work / 'absent.sock'),
                                                 'session': '$1', 'pane': '%1'}}, True)):
                with self.subTest(changes=changes):
                    write(directory / 'runtime.json', {**facts, **changes})
                    self.assertEqual(agents.launch_in_progress(directory, facts), expected)


class RefusalUpgradeTest(BaseTest):
    def setUp(self):
        super().setUp()
        fixture = Path(__file__).parent / 'fixtures/delivery/main-52b9df4-launch-only.json'
        self.record = json.loads(fixture.read_text())
        attempt = self.record['tracking']['attempts'][-1]
        self.directory = self.root / 'state/agents' / attempt['agent_id']
        self.path = self.directory / 'inbox-state.json'
        write(self.directory / 'properties.json', {'id': self.directory.name, 'name': 'recipient',
            'harness': 'claude', 'task': '', 'universe_id': read(self.root/'universe.json')['id'],
            'workspace': str(self.work), 'native_session_id': attempt['native_session_id']})
        write(self.directory / 'runtime.json', {'launch_id': attempt['launch_id'], 'state': 'running',
            'native_mode': {'mode': 'plain', 'policy': 'plain'},
            'tmux': {'socket': str(self.work / 'absent.sock'), 'pane': '%1', 'session': '$1'}})
        (self.directory / 'inbox.jsonl').write_text(json.dumps(self.record['message']) + '\n')
        write(self.path, {self.record['message']['id']: self.record['tracking']})
        self.enterContext(patch.object(native, 'observe', return_value={'live': 'alive'}))
        self.enterContext(patch.object(lifecycle, 'observe', return_value={'live': 'alive'}))

    def test_ledgers_written_by_older_mv2_are_read_without_rewriting(self):
        """Ledger records from a real main@52b9df4 fixture are read, counted and finalized without the file being
        rewritten; fragile because the ledger format changed repeatedly and old records persist in real universes, so
        a break miscounts or rewrites delivery evidence."""
        with self.subTest('a refusal main wrote is not counted, and counting does not migrate it'):
            before = self.path.read_bytes()
            self.assertNotIn('not delivered', list_agents.run(self.root, wide=True))
            self.assertFalse(any('not delivered' in reason for reason in doctor.run(self.root)['problems']))
            self.assertEqual(self.path.read_bytes(), before)
        message = self.record['message']
        tracking = self.record['tracking']
        for state in ('accepted', 'unknown', 'submitting'):
            with self.subTest(state=state):
                current = {**tracking, 'submission_state': state}
                write(self.path, {message['id']: current})
                before = self.path.read_bytes()
                self.assertIsNone(native.finalize_launch_only(self.root, self.directory, message['id']))
                self.assertEqual(self.path.read_bytes(), before)
        with self.subTest('a failed wake is counted and keeps its evidence'):
            failed = {**tracking, 'delivery_method': 'resume_prompt'}
            write(self.path, {message['id']: failed})
            self.assertIn('1 message not delivered', list_agents.run(self.root, wide=True))
            before = self.path.read_bytes()
            self.assertIsNone(native.finalize_launch_only(self.root, self.directory, message['id']))
            self.assertEqual(self.path.read_bytes(), before)
