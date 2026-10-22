"""Offline harness contracts: no tmux server or model calls."""
from contextlib import ExitStack
from dataclasses import replace
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from native_safety import install as install_native_safety
install_native_safety()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse import harnesses
from multiverse.harnesses.types import LaunchContext, SessionHandle
from multiverse.universe import templates
from multiverse.store import read, write
from isolation import isolate


class HarnessContracts(unittest.TestCase):
    def setUp(self):
        isolate(self)
        temporary = tempfile.TemporaryDirectory(prefix='mv2-offline-')
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.root = templates.create(self.work / 'universe', 'offline', workdir=self.work)
        # These are argv/state contracts, not native daemon integration tests.
        path = self.root / 'harnesses/codex/harness.json'
        definition = read(path)
        definition['settings']['inbox_bridge'] = False
        write(path, definition)
        self.socket = '/offline-test/tmux.sock'
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.backend = self.patches.enter_context(patch('multiverse.runtime.lifecycle.Tmux'))
        self.backend.return_value.create.return_value = {
            'socket': self.socket, 'session': '$offline', 'pane': '%offline'}
        self.patches.enter_context(patch('multiverse.runtime.lifecycle.shutil.which', return_value=sys.executable))
        self.patches.enter_context(patch.dict(os.environ, MV2_TMUX_SOCKET=self.socket))
        self.patches.enter_context(patch('multiverse.runtime.native.start_shared_service',
            side_effect=AssertionError('Offline tests must never prepare a native daemon')))

    def start(self, name='offline', **kwargs):
        return lifecycle.start(self.root, name=name, task='literal $(false) `false`\nquoted',
                            model='gpt-6-astra', socket_path=self.socket, **kwargs)

    def request(self, row):
        return read(self.root / 'state/agents' / row['id'] / 'launches' /
                    row['runtime']['launch_id'] / 'request.json')

    def values(self):
        values = dict(name='literal', agent_id=str(uuid4()), session_id=str(uuid4()), bootstrap='Hello "quoted"',
                      bootstrap_json='"Hello"', task='--literal $(false) `false` {literal}',
                      agent_dir=str(self.work / 'agent'), harness_dir='/local/harness',
                      session_file=str(self.work / 'agent/session.jsonl'), bootstrap_file='/launch/bootstrap.md',
                      task_file='/launch/task.md', workspace=str(self.work), model='chosen',
                      control_socket='/tmp/owned/rpc.sock')
        session_file = Path(values['session_file'])
        session_file.parent.mkdir(exist_ok=True)
        session_file.write_text(json.dumps({'type': 'session', 'id': values['session_id']}) + '\n')
        return values

    def session(self, name, values):
        if name == 'claude':
            saved = Path(os.environ['CLAUDE_CONFIG_DIR']) / 'projects' / '-workspace' / f"{values['session_id']}.jsonl"
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_text('{"type": "user"}\n')
        return SessionHandle(values['agent_id'], name, values['workspace'],
                             native_id=values['session_id'], session_file=values['session_file'])

    def test_resume_targets_the_recorded_native_session_for_every_adapter(self):
        """Resume through lifecycle.start targets the recorded session for Codex (id), Claude (id) and Pi (session
        file), never the latest or the template's, and refuses when it cannot be exact, naming the file or the way out
        (Pi: --start-fresh, a damaged header's file; qa PR #26); fragile because each adapter
        builds resume differently, and a wrong target silently resumes another conversation."""
        for harness in ('codex', 'claude', 'pi'):
            with self.subTest('through lifecycle.start', harness=harness):
                row = self.start(name=f'offline-{harness}', harness=harness)
                directory = self.root / 'state/agents' / row['id']
                native = row.get('native_session_id')
                if harness == 'codex':
                    native = str(uuid4())
                    agents.bind_session(self.root, row['id'], native, row['runtime']['launch_id'])
                if harness == 'pi':
                    (directory / 'session.jsonl').write_text(json.dumps({'type': 'session', 'id': str(uuid4())}) + '\n')
                with patch('multiverse.runtime.lifecycle.observe', return_value={'live': 'dormant'}):
                    resumed = lifecycle.start(self.root, resume=row['id'], socket_path=self.socket)
                argv = self.request(resumed)['argv']
                self.assertEqual(resumed['id'], row['id'])
                self.assertNotEqual(resumed['runtime']['launch_id'], row['runtime']['launch_id'])
                self.assertNotIn('--last', argv)
                self.assertNotIn(row['task'], argv)
                self.assertIn(native if harness != 'pi' else str(directory / 'session.jsonl'), argv)
                self.assertEqual(argv[argv.index('--model') + 1], 'gpt-6-astra')
                with self.assertRaisesRegex(ValueError, 'Outdated'):
                    agents.update_runtime(directory, row['runtime']['launch_id'], status='idle')
        with self.subTest('a Codex agent that never bound a session'):
            row = self.start()
            with patch('multiverse.runtime.lifecycle.observe', return_value={'live': 'dormant'}), \
                    self.assertRaisesRegex(ValueError, 'No native session ID recorded'):
                lifecycle.start(self.root, resume=row['id'], socket_path=self.socket)
        # Claude resumes only a session whose transcript exists; keep that storage out of the real one.
        self.patches.enter_context(patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.work / 'claude-config')}))
        for name in harnesses.BUILTINS:
            adapter = harnesses.BUILTINS[name].configured(name, {})
            legacy = harnesses.LegacyAdapter(name, {
                'spawn': ['custom'],
                'resume': ['custom', '{agent_id}', '{session_id}', '{session_file}', '{workspace}'],
            })
            values = self.values()
            session = self.session(name, values)
            other = {**values, 'agent_id': 'wrong-agent', 'session_id': str(uuid4()),
                     'session_file': '/wrong/session.jsonl', 'workspace': '/wrong/worktree'}
            for implementation in (adapter, legacy):
                with self.subTest('the session, not the template values', name=name,
                                  implementation=type(implementation).__name__):
                    expected = implementation.resume(session, LaunchContext('resume', values, 'chosen'))
                    actual = implementation.resume(session, LaunchContext('resume', other, 'chosen'))
                    self.assertEqual(actual, expected)
                    with self.assertRaisesRegex(ValueError, 'different harness'):
                        implementation.resume(replace(session, harness='another'), LaunchContext('resume', values))
            with self.subTest('no recorded session', name=name):
                missing = replace(session, session_file=None) if name == 'pi' else replace(session, native_id=None)
                with self.assertRaisesRegex(ValueError, 'native session ID|session file is missing'):
                    adapter.resume(missing, LaunchContext('resume', values))
            if name == 'pi':
                with self.subTest('a missing Pi session file names the way to start fresh'):
                    gone = replace(session, session_file=str(self.work / 'gone.jsonl'))
                    with self.assertRaisesRegex(ValueError, 'mv2 resume literal --start-fresh'):
                        adapter.resume(gone, LaunchContext('resume', values))
                for number, (label, first_line) in enumerate((
                        ('no id', json.dumps({'type': 'session'})), ('an id that is not text', json.dumps({'type': 'session', 'id': 7})),
                        ('an id that is not a UUID', json.dumps({'type': 'session', 'id': 'x'})),
                        ('another type', json.dumps({'type': 'message', 'id': values['session_id']})),
                        ('a list', json.dumps(['session'])), ('no JSON', 'not json'))):
                    with self.subTest('a damaged Pi session header names its file', header=label):
                        damaged = self.work / f'damaged-{number}.jsonl'
                        damaged.write_text(first_line + '\n')
                        with self.assertRaisesRegex(ValueError, 'Pi session file ' + re.escape(json.dumps(str(damaged)))):
                            adapter.resume(replace(session, session_file=str(damaged)), LaunchContext('resume', values))
                alias = self.work / 'pi-alias'
                alias.symlink_to(self.work, target_is_directory=True)
                for recorded, shell, notice in (
                        ('/pi/a', '/pi/b', 'Kept its recorded PI_CODING_AGENT_DIR "/pi/a"; ignored PI_CODING_AGENT_DIR="/pi/b".'),
                        (None, '/pi/b', 'Kept its recorded PI_CODING_AGENT_DIR unset; ignored PI_CODING_AGENT_DIR="/pi/b".'),
                        ('/pi/a', '/pi/a', None), (str(self.work), str(alias), None), ('/pi/a', None, None), ('/pi/a', '', None)):
                    with self.subTest('Pi names a shell PI_CODING_AGENT_DIR it ignored', recorded=recorded, shell=shell):
                        caller = {} if shell is None else {'PI_CODING_AGENT_DIR': shell}
                        effective = {} if recorded is None else {'PI_CODING_AGENT_DIR': recorded}
                        self.assertEqual(adapter.resume_notice(session, caller, effective), notice)
            if name in ('codex', 'claude'):
                with self.subTest('a saved id that is not a lowercase UUID', name=name):
                    context = LaunchContext('resume', values)
                    command = adapter.resume(replace(session, native_id=session.native_id.upper()), context)
                    self.assertIn(session.native_id, command.argv)
                    with self.assertRaisesRegex(ValueError, 'must be a UUID'):
                        adapter.resume(replace(session, native_id='invalid'), context)
        with self.subTest('the exact Pi session file'):
            adapter = harnesses.BUILTINS['pi'].configured('pi', {})
            values = self.values()
            session_file = Path(values['session_file'])
            session = replace(self.session('pi', values), native_id=None)
            context = LaunchContext('resume', values)
            session_file.unlink()
            with self.assertRaisesRegex(ValueError, 'missing'):
                adapter.resume(session, context)
            for contents in ('', 'not json', '{}', '{"type":"session","id":"bad"}', '{"type":"session","id":123}'):
                session_file.write_text(contents)
                with self.assertRaises(ValueError):
                    adapter.resume(session, context)
            native_id = str(uuid4())
            # Native files are JSONL, not pretty-printed JSON.
            session_file.write_text(json.dumps({'type': 'session', 'id': native_id}) + '\n')
            command = adapter.resume(replace(session, native_id=native_id.upper()), context)
            self.assertIn(str(session_file), command.argv)
            adapter.resume(session, context)  # A valid Pi file can precede UUID binding.
            with self.assertRaisesRegex(ValueError, 'different native'):
                adapter.resume(replace(session, native_id=str(uuid4())), context)


if __name__ == '__main__':
    unittest.main()
