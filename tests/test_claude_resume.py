"""Claude resume: a session that was never used has no transcript, so it restarts under its own ID."""
import json
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
from multiverse.harnesses.claude import CONVERSATION_MISSING, FRESH_NOTES, NEVER_USED
from multiverse.harnesses.types import LaunchContext, SessionHandle
from multiverse.store import read, write
from test_multiverse import BaseTest, TerminalBase, until

WAKE = 'Message from lead:\nPlease rebase.'


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

    def test_saved_conversation_resumes_exactly_as_before(self):
        for bridge in (False, True):
            with self.subTest(bridge=bridge):
                values = self.values()
                self.save(values['session_id'])
                command = self.resume(self.adapter(bridge), values)
                self.assertEqual(command.argv[command.argv.index('--resume') + 1], values['session_id'])
                self.assertNotIn('--session-id', command.argv)
                self.assertFalse((command.native_mode or {}).get('fallback_reason'))
                self.assertFalse(any(note in str(command.native_mode) for note in FRESH_NOTES))

    def test_unused_session_restarts_under_its_own_id_with_every_other_flag(self):
        for bridge in (False, True):
            with self.subTest(bridge=bridge):
                adapter, values = self.adapter(bridge), self.values()
                fresh = self.resume(adapter, values)
                self.save(values['session_id'])
                saved = self.resume(adapter, values)
                self.assertEqual(fresh.argv[fresh.argv.index('--session-id') + 1], values['session_id'])
                self.assertNotIn('--resume', fresh.argv)
                # Name, bootstrap, add-dir, model and the bridge flags are all unchanged.
                self.assertEqual(['--session-id' if arg == '--resume' else arg for arg in saved.argv], list(fresh.argv))
                self.assertIn('--name', fresh.argv)
                self.assertIn('--append-system-prompt', fresh.argv)
                self.assertEqual(fresh.argv[fresh.argv.index('--model') + 1], 'chosen')
                self.assertEqual(('--plugin-dir' in fresh.argv), bridge)
                self.assertNotIn('--original task', fresh.argv)  # the first task is never replayed
                # The note is the resume's notice, never a fallback reason (inspect would print it twice).
                self.assertFalse(fresh.native_mode['fallback_reason'])
                self.assertEqual(fresh.environment, saved.environment)
                self.assertEqual(fresh.control_endpoint, saved.control_endpoint)

    def test_wake_prompt_still_follows_the_terminator_when_the_session_starts_fresh(self):
        for prompt in (WAKE, '--resume'):
            with self.subTest(prompt=prompt):
                values = self.values()
                command = self.resume(self.adapter(True), values, resume_prompt=prompt)
                self.assertEqual(command.argv[-2:], ('--', prompt))
                self.assertLess(command.argv.index('--session-id'), command.argv.index('--'))
                self.assertEqual(command.argv.count('--resume'), 1 if prompt == '--resume' else 0)
                self.assertNotIn('--original task', command.argv)

    def test_conversation_is_looked_up_in_the_launch_environment_not_the_callers(self):
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
        self.assertNotIn('--resume', self.resume(adapter, values, environment={'HOME': str(self.work / 'empty')}).argv)

    def test_a_recipe_that_does_not_resume_this_session_is_left_alone(self):
        values = self.values()
        adapter = self.adapter(False)
        session = SessionHandle(values['agent_id'], 'claude', values['workspace'], native_id=values['session_id'])
        command = adapter.resume(session, LaunchContext('resume', values, environment=self.environment))
        self.assertIsNone(adapter._reserve_session(command, str(uuid4())))


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

    def test_stopping_an_unused_agent_no_longer_makes_it_impossible_to_resume(self):
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

    def test_the_resume_result_says_when_it_started_fresh_and_why(self):
        from multiverse.display import outcome
        unused, unused_dir = self.launch('unused', '')
        self.stop_agent(unused)
        (unused_dir / 'seen.json').unlink()
        result = lifecycle.start(self.root, resume=unused['id'], socket_path=self.socket)
        self.assertEqual(result['notices'], [NEVER_USED])
        self.assertEqual(outcome(result, 'Resumed', 'did not resume').splitlines()[1:], [NEVER_USED])
        until(lambda: (unused_dir / 'seen.json').exists())
        self.stop_agent(unused)
        # A used agent whose conversation Claude has since deleted, as cleanupPeriodDays does: Claude's own prompt
        # history still names its session, so the result says the saved conversation is missing.
        used, used_dir = self.launch('used', 'do the work')
        history = self.storage / 'history.jsonl'
        history.write_text(json.dumps({'display': 'do the work', 'pastedContents': {}, 'timestamp': 1,
                                       'project': str(self.work), 'sessionId': used['native_session_id']},
                                      separators=(',', ':')) + '\n')
        self.stop_agent(used)
        for transcript in (self.storage / 'projects').glob(f"*/{used['native_session_id']}.jsonl"):
            transcript.unlink()
        (used_dir / 'seen.json').unlink()
        result = lifecycle.start(self.root, resume=used['id'], socket_path=self.socket)
        self.assertEqual(result['notices'], [CONVERSATION_MISSING])
        until(lambda: (used_dir / 'seen.json').exists())
        argv = read(used_dir / 'seen.json')['argv']
        self.assertEqual(argv[argv.index('--session-id') + 1], used['native_session_id'])  # the fresh start stays
        self.assertEqual(read(used_dir / 'runtime.json')['launch_notices'], [CONVERSATION_MISSING])
        self.stop_agent(used)

    def test_a_used_agent_still_resumes_its_conversation(self):
        agent, directory = self.launch('used', 'do the work')
        self.assertTrue(list((self.storage / 'projects').glob('*/*.jsonl')))
        argv, runtime = self.restart(agent, directory)
        self.assertEqual(argv[argv.index('--resume') + 1], agent['native_session_id'])
        self.assertNotIn('--session-id', argv)
        self.assertFalse((runtime.get('native_mode') or {}).get('fallback_reason'))
        self.stop_agent(agent)
