"""Independent adapter contracts and core native execution with no provider calls."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse import harnesses
from multiverse.runtime import native
from multiverse.harnesses.types import SessionHandle, CommandSpec, LaunchContext, Message
from multiverse.universe.mailbox import append_message, inbox
from multiverse.store import read, write
from multiverse.tools import native_status
from test_multiverse import BaseTest


class AdapterContractTest(BaseTest):
    def setUp(self):
        super().setUp()
        # Claude resumes only a session whose transcript exists; keep that storage out of the real one.
        self.claude_storage = self.work / 'claude-config'
        self.enterContext(patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.claude_storage)}))

    def values(self):
        values = dict(name='literal', agent_id=str(uuid4()), session_id=str(uuid4()), bootstrap='Hello "quoted"',
                    bootstrap_json='"Hello"', task='--literal $(false) `false` {literal}',
                    agent_dir=str(self.work / 'agent'), harness_dir='/local/harness', session_file=str(self.work / 'agent/session.jsonl'),
                    bootstrap_file='/launch/bootstrap.md', task_file='/launch/task.md', workspace=str(self.work), model='chosen', control_socket='/tmp/owned/rpc.sock')
        session_file = Path(values['session_file'])
        session_file.parent.mkdir(exist_ok=True)
        session_file.write_text(json.dumps({'type': 'session', 'id': values['session_id']}) + '\n')
        return values

    def session(self, name, values):
        if name == 'claude':
            saved = self.claude_storage / 'projects' / '-workspace' / f"{values['session_id']}.jsonl"
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_text('{"type": "user"}\n')
        return SessionHandle(values['agent_id'], name, values['workspace'],
                             native_id=values['session_id'], session_file=values['session_file'])

    def test_wake_prompt_is_the_last_literal_argument(self):
        """For each harness, bridge mode and fresh or saved session, the wake prompt is the last argv item after --,
        with no -p, and an empty task adds nothing; fragile because each adapter builds argv itself and a prompt
        starting with - becomes a flag, so the wake fails as a startup error."""
        for name in harnesses.BUILTINS:
            with self.subTest(name=name, task=''):
                adapter = harnesses.load(self.root, name)
                values = {**self.values(), 'task': ''}
                command = adapter.spawn(LaunchContext('spawn', values, 'chosen'))
                self.assertNotIn('', command.argv)
                self.assertNotIn('@' + values['task_file'], command.argv)
                self.assertIn('chosen', command.argv)
        for name in ('codex', 'claude', 'pi'):
            for bridge in (False, True):
                path = self.root / 'harnesses' / name / 'harness.json'
                spec = read(path)
                spec['settings']['inbox_bridge'] = bridge
                write(path, spec)
                adapter = harnesses.load(self.root, name)
                # Claude resumes a session that was never used under --session-id, and the prompt must still follow --.
                for saved in ((True, False) if name == 'claude' else (True,)):
                    for prompt in ('Message from reviewer:\n--literal $(false) `false` {text}', '--resume'):
                        with self.subTest(name=name, bridge=bridge, saved=saved, prompt=prompt):
                            values = self.values()
                            session = (self.session(name, values) if saved else
                                       SessionHandle(values['agent_id'], name, values['workspace'],
                                                     native_id=values['session_id']))
                            command = adapter.resume(session, LaunchContext('resume', values, 'chosen',
                                                                            resume_prompt=prompt))
                            argv = command.argv
                            self.assertEqual(argv[-1], prompt)
                            # Codex takes the session after the terminator; the others take only the prompt.
                            self.assertEqual(argv[-3 if name == 'codex' else -2], '--')
                            self.assertNotIn('-p', argv)
                            self.assertNotIn('--print', argv)
                            self.assertNotIn(values['task'], argv)
                            self.assertIn(values['session_file'] if name == 'pi' else session.native_id, argv)
                            if not saved:
                                self.assertLess(argv.index('--session-id'), argv.index('--'))
                                self.assertEqual(argv.count('--resume'), 1 if prompt == '--resume' else 0)
                            self.assertEqual(adapter.capabilities(session)['wake'].state, 'supported')
        legacy = harnesses.LegacyAdapter('custom', {'spawn': ['custom'], 'resume': ['custom', '{session_id}']})
        self.assertEqual(legacy.capabilities(SessionHandle('agent', 'custom', '/tmp'))['wake'].state, 'unavailable')


class NativeDeliveryTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.agent = str(uuid4())
        self.directory = self.root / 'state/agents' / self.agent
        self.native_id = str(uuid4())
        write(self.directory / 'properties.json', {'id': self.agent, 'name': 'worker', 'harness': 'codex',
               'native_session_id': self.native_id, 'workspace': str(self.work)})
        write(self.directory / 'runtime.json', {'launch_id': str(uuid4()), 'state': 'running',
            'tmux': {'socket': str(self.work / 'absent.sock'), 'session': '$1', 'pane': '%1'}})

    def test_unsupported_delivery_never_falls_back_to_terminal_input(self):
        """An unsupported steer, or a terminal whose ownership check fails, never runs a native command or types into a
        pane, and the message stays saved; a break goes unnoticed because text lands in whatever the agent is
        typing."""
        with self.subTest('an unsupported steer'):
            message = append_message(self.directory, 'steer me', {}, delivery='steer')
            with patch.object(native, 'owned'), patch.object(native, 'execute') as execute:
                result = native.deliver(self.root, self.agent, message['id'])
                self.assertEqual(result['state'], 'unavailable')
                execute.assert_not_called()
            self.assertEqual(inbox(self.directory)['messages'], [])
            self.assertEqual(len(inbox(self.directory, all_messages=True)['messages']), 1)
            self.assertEqual(native_status.run(self.root, agent=self.agent, operation='ask')['state'], 'unavailable')
        with self.subTest("a terminal that is not this launch's"):
            message = append_message(self.directory, 'not to stale pane', {}, delivery='steer')
            with patch.object(native, 'owned', side_effect=ValueError('wrong launch')), \
                    patch.object(native, 'execute') as execute:
                result = native.deliver(self.root, self.agent, message['id'])
                self.assertIn(result['state'], ('error', 'unknown'))
                execute.assert_not_called()
        with self.subTest('plain Codex'):
            adapter = harnesses.load(self.root, 'codex')
            session = SessionHandle('agent', 'codex', str(self.work), native_id=str(uuid4()))
            executor = Mock()
            self.assertEqual(adapter.capabilities(session)['send_steer'].state, 'unavailable')
            self.assertEqual(adapter.send(session, Message('id', 'text'), executor).state, 'unavailable')
            executor.rpc.assert_not_called()
            executor.command.assert_not_called()

    def test_native_subprocess_scrubs_identity_and_preserves_literal_argv(self):
        """Native helper commands run without the caller's MV2_*, CODEX_THREAD_ID or CLAUDECODE and with literal argv;
        a break goes unnoticed because the command succeeds, but a daemon it starts carries one agent's identity into
        every agent's tool calls."""
        script = 'import json,os,sys; print(json.dumps([sys.argv[1:], sorted(k for k in os.environ if k.startswith("MV2_") or k in ("CODEX_THREAD_ID","CLAUDECODE"))]))'
        with patch.dict(os.environ, MV2_AGENT_ID='parent', CODEX_THREAD_ID='parent', CLAUDECODE='parent'):
            result = native.execute(CommandSpec((sys.executable, '-c', script, '$(false)\n{literal}')), self.work)
        self.assertEqual(json.loads(result.stdout), [['$(false)\n{literal}'], []])


if __name__ == '__main__':
    unittest.main()
