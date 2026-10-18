"""Claude resume: a session that was never used has no transcript, so it restarts under its own ID."""
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse import harnesses
from multiverse.harnesses.claude import FRESH_NOTES, NEVER_USED
from multiverse.harnesses.types import LaunchContext, SessionHandle
from multiverse.store import read, write
from test_multiverse import BaseTest, TerminalBase, until


class ClaudeResumeTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.storage = self.work / 'claude-config'
        self.environment = {'CLAUDE_CONFIG_DIR': str(self.storage)}

    def adapter(self, bridge):
        path = self.root / 'harnesses/claude/harness.json'
        config = read(path)
        config['settings']['inbox_bridge'] = bridge
        write(path, config)
        return harnesses.load(self.root, 'claude')

    def values(self):
        return dict(name='worker', agent_id=str(uuid4()), session_id=str(uuid4()), bootstrap='Hello',
                    bootstrap_json='"Hello"', task='--original task', agent_dir=str(self.work / 'agent'),
                    harness_dir='/local/harness', session_file='', bootstrap_file='', task_file='',
                    workspace=str(self.work), model='', control_socket=str(self.work / 'inbox.sock'))

    def save(self, session_id, storage=None):
        path = (storage or self.storage) / 'projects' / '-some-workspace' / f'{session_id}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"type": "user"}\n')

    def resume(self, adapter, values, environment=None, **context):
        session = SessionHandle(values['agent_id'], 'claude', values['workspace'], native_id=values['session_id'])
        return adapter.resume(session, LaunchContext('resume', values, model='chosen',
                                                     environment=environment or self.environment, **context))

    def test_resume_or_session_id_is_chosen_from_the_launch_storage(self):
        """--resume is used only when the transcript exists in the launch's own storage, else --session-id with every
        other flag unchanged; fragile because Claude refuses --resume without a transcript and --session-id with one,
        so a wrong choice makes the agent impossible to resume."""
        with self.subTest('a saved conversation'):
            for bridge in (False, True):
                with self.subTest(bridge=bridge):
                    values = self.values()
                    self.save(values['session_id'])
                    command = self.resume(self.adapter(bridge), values)
                    self.assertEqual(command.argv[command.argv.index('--resume') + 1], values['session_id'])
                    self.assertNotIn('--session-id', command.argv)
                    self.assertFalse((command.native_mode or {}).get('fallback_reason'))
                    self.assertFalse(any(note in str(command.native_mode) for note in FRESH_NOTES))
        with self.subTest('a session that was never used'):
            for bridge in (False, True):
                with self.subTest(bridge=bridge):
                    adapter, values = self.adapter(bridge), self.values()
                    fresh = self.resume(adapter, values)
                    self.save(values['session_id'])
                    saved = self.resume(adapter, values)
                    self.assertEqual(fresh.argv[fresh.argv.index('--session-id') + 1], values['session_id'])
                    self.assertNotIn('--resume', fresh.argv)
                    # Name, bootstrap, add-dir, model and the bridge flags are all unchanged.
                    self.assertEqual(['--session-id' if arg == '--resume' else arg for arg in saved.argv],
                                     list(fresh.argv))
                    self.assertIn('--name', fresh.argv)
                    self.assertIn('--append-system-prompt', fresh.argv)
                    self.assertEqual(fresh.argv[fresh.argv.index('--model') + 1], 'chosen')
                    self.assertEqual(('--plugin-dir' in fresh.argv), bridge)
                    self.assertNotIn('--original task', fresh.argv)  # the first task is never replayed
                    # The note is the resume's notice, never a fallback reason (inspect would print it twice).
                    self.assertFalse(fresh.native_mode['fallback_reason'])
                    self.assertEqual(fresh.environment, saved.environment)
                    self.assertEqual(fresh.control_endpoint, saved.control_endpoint)
        with self.subTest("the launch's storage, not the caller's"):
            values = self.values()
            elsewhere = self.work / 'elsewhere'
            self.save(values['session_id'], elsewhere)
            adapter = self.adapter(False)
            # Saved in a different storage than the launch uses: this launch has none.
            self.assertIn('--session-id', self.resume(adapter, values).argv)
            home = self.work / 'home'
            self.save(values['session_id'], home / '.claude')
            # Without CLAUDE_CONFIG_DIR, storage is under the launch's HOME.
            self.assertIn('--resume', self.resume(adapter, values, environment={'HOME': str(home)}).argv)
            empty = {'HOME': str(self.work / 'empty')}
            self.assertNotIn('--resume', self.resume(adapter, values, environment=empty).argv)


FAKE_CLAUDE = '''#!{python}
import json, os, re, sys, time
from pathlib import Path

args = sys.argv[1:]
options = args[:args.index('--')] if '--' in args else args
flag = '--resume' if '--resume' in options else '--session-id'
session = options[options.index(flag) + 1]
transcript = Path({storage!r}) / 'projects' / re.sub(r'[^a-zA-Z0-9]', '-', os.getcwd()) / (session + '.jsonl')
if flag == '--resume' and not transcript.is_file():
    print('No conversation found with session ID: ' + session, flush=True)
    sys.exit(1)
if flag == '--session-id' and transcript.is_file():
    print('Error: Session ID ' + session + ' is already in use.', flush=True)
    sys.exit(1)
if '--' in args and args[args.index('--') + 1:]:
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(json.dumps({{'type': 'user', 'sessionId': session}}) + '\\n')
Path(os.environ['MV2_AGENT_DIR'], 'seen.json').write_text(json.dumps({{'argv': args}}))
print('HARNESS READY', flush=True)
while True:
    time.sleep(1)
'''


class ClaudeResumeLaunchTest(TerminalBase):
    """A stand-in `claude` that, like the real one, refuses --resume without a saved conversation."""

    def setUp(self):
        super().setUp()
        self.enterContext(patch('multiverse.runtime.native.start_shared_service',
            side_effect=AssertionError('A fake Claude must never prepare a native daemon')))
        self.storage = self.work / 'claude-config'
        self.enterContext(patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.storage)}))
        directory = self.root / 'harnesses/claude'
        executable = directory / 'fake-cli'
        executable.write_text(FAKE_CLAUDE.format(python=sys.executable, storage=str(self.storage)))
        executable.chmod(0o755)
        spec = read(directory / 'harness.json')
        spec['settings'].update(executable=str(executable), inbox_bridge=False)
        write(directory / 'harness.json', spec)

    def stop_agent(self, agent):
        lifecycle.terminal_action(self.root, agent['id'], 'stop')
        time.sleep(0.1)

    def launch(self, name, task):
        agent = lifecycle.start(self.root, name, task, 'claude', socket_path=self.socket)
        directory = agents.directory(self.root, agent['id'])
        until(lambda: (directory / 'seen.json').exists())
        return agent, directory

    def restart(self, agent, directory):
        self.stop_agent(agent)
        (directory / 'seen.json').unlink()
        lifecycle.start(self.root, resume=agent['id'], socket_path=self.socket)
        until(lambda: (directory / 'seen.json').exists())
        return read(directory / 'seen.json')['argv'], read(directory / 'runtime.json')

    def test_stop_and_restart_of_used_and_unused_agents(self):
        """A real launch, stop and restart, with a stand-in claude that refuses like the real one, picks the right flag
        for a used and an unused agent; fragile because the unit table cannot catch lifecycle passing the wrong
        environment or storage to the adapter."""
        with self.subTest('an unused agent'):
            agent, directory = self.launch('unused', '')
            self.assertIn('--session-id', read(directory / 'seen.json')['argv'])
            self.assertFalse(list((self.storage / 'projects').glob('*/*.jsonl')))  # Claude has saved nothing
            argv, runtime = self.restart(agent, directory)
            self.assertEqual(argv[argv.index('--session-id') + 1], agent['native_session_id'])
            self.assertNotIn('--resume', argv)
            self.assertFalse(runtime['native_mode'] and runtime['native_mode'].get('fallback_reason'))
            self.assertEqual(runtime['launch_notices'], [NEVER_USED])
            self.assertNotEqual(runtime['state'], 'failed')
            self.stop_agent(agent)
        with self.subTest('a used agent'):
            agent, directory = self.launch('used', 'do the work')
            self.assertTrue(list((self.storage / 'projects').glob('*/*.jsonl')))
            argv, runtime = self.restart(agent, directory)
            self.assertEqual(argv[argv.index('--resume') + 1], agent['native_session_id'])
            self.assertNotIn('--session-id', argv)
            self.assertFalse((runtime.get('native_mode') or {}).get('fallback_reason'))
            self.stop_agent(agent)
