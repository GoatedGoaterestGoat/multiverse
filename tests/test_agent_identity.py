"""One agent's conversation authority is separate from a terminal generation."""
from dataclasses import asdict
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch
from uuid import uuid4

from multiverse.harnesses.types import AgentIdentitySpec, NativeEndpoint, NativeServiceHandle, ServiceIdentity
from multiverse.runtime import agent_identity
from multiverse.store import read, write
from multiverse.tools import inbox, report, runtime_update, session_bind, send
from multiverse.universe import agents
from multiverse.universe.mailbox import append_message
from test_multiverse import BaseTest


class AgentIdentityTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.identity, self.native = str(uuid4()), str(uuid4())
        self.directory = self.root / 'state' / 'agents' / self.identity
        self.spec = AgentIdentitySpec('fixture-agent-v1', 'FIXTURE_THREAD', ('FIXTURE_HOME',))
        service = NativeServiceHandle(ServiceIdentity('fixture', {'FIXTURE_HOME': str(self.work)}),
                                      NativeEndpoint('unix-websocket', str(self.work / 'service.sock')))
        self.props = {'id': self.identity, 'name': 'one-agent', 'harness': 'pi', 'workspace': str(self.work),
                      'universe_id': read(self.root / 'universe.json')['id'], 'native_session_id': self.native,
                      'native_service_identity': asdict(service.identity)}
        self.props['agent_identity'] = agent_identity.record(self.spec, self.props, service)
        write(self.directory / 'properties.json', self.props)
        write(self.directory / 'runtime.json', {'launch_id': 'current', 'state': 'failed', 'reason': 'latched failure'})
        self.env = {'MV2_IDENTITY_MODE': self.spec.mode, 'MV2_UNIVERSE': str(self.root),
                    'MV2_AGENT_ID': self.identity, 'MV2_AGENT_DIR': str(self.directory),
                    'FIXTURE_THREAD': self.native, 'FIXTURE_HOME': str(self.work)}

    def test_each_launch_refreshes_tool_paths_without_changing_agent_authority(self):
        old = {'MV2_CLI': '/removed/checkout/bin/mv2', 'MV2_PYTHON': '/removed/venv/bin/python',
               'PATH': '/removed/bin', 'MV2_LAUNCH_ID': 'must-not-escape'}
        target = {**self.env, 'MV2_CLI': '/current/checkout/bin/mv2',
                  'MV2_PYTHON': '/current/venv/bin/python', 'PATH': '/current/bin:/usr/bin'}
        refreshed = agent_identity.environment(target, self.spec, old)
        for key in ('MV2_CLI', 'MV2_PYTHON', 'PATH'):
            self.assertEqual(refreshed[key], target[key])
        self.assertNotIn('MV2_LAUNCH_ID', refreshed)
        # Executable locations are requested tool configuration, never actor proof.
        with_old_tools = {**self.env, **{key: old[key] for key in ('MV2_CLI', 'MV2_PYTHON', 'PATH')}}
        with_new_tools = {**self.env, **refreshed}
        self.assertEqual(agent_identity.actor(with_old_tools), agent_identity.actor(with_new_tools))
        self.assertEqual(agent_identity.environment(target, None, old), {})
        for damaged in (None, [], {'MV2_CLI': 7, 'MV2_PYTHON': None, 'PATH': []}):
            with self.subTest(saved=damaged):
                self.assertEqual(agent_identity.environment(target, self.spec, damaged), refreshed)

    def test_read_only_cli_does_not_resolve_the_callers_agent_identity(self):
        from multiverse.cli import parser, run
        for action in ('catalog', 'list'):
            with self.subTest(action=action), patch.object(agent_identity, 'actor', side_effect=AssertionError('identity lookup')):
                run(parser().parse_args(['-u', str(self.root), action]))

    def test_stale_cli_context_can_read_catalog_and_list_but_cannot_act_as_the_agent(self):
        project = Path(__file__).resolve().parents[1]
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('MV2_', 'CODEX_'))}
        # Explicit stable mode with incomplete native proof must not prevent a
        # read-only lookup, but must never become authority for an agent action.
        env.update({**self.env, 'FIXTURE_THREAD': 'wrong', 'PYTHONPATH': str(project / 'src')})
        for standalone in (False, True):
            for action in ('catalog', 'list'):
                entry = (['-m', 'multiverse.tools.' + ('list_agents' if action == 'list' else action)]
                         if standalone else ['-m', 'multiverse.cli'])
                args = ['-u', str(self.root), *([] if standalone else [action])]
                with self.subTest(action=action, standalone=standalone):
                    result = subprocess.run([sys.executable, *entry, *args], env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(result.stdout)
        for args in (['report', '--summary', 'must not write'], ['inbox'],
                     ['send', 'one-agent', '--text', 'must not save', '--delivery', 'inbox-only']):
            with self.subTest(action=args[0]):
                result = subprocess.run([sys.executable, '-m', 'multiverse.cli', '-u', str(self.root), *args],
                                        env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 1)
                self.assertIn('identity could not be verified', result.stderr)
        self.assertNotIn('summary', read(self.directory / 'properties.json'))

    def test_report_and_inbox_belong_to_the_agent_even_when_attachment_is_failed_or_replaced(self):
        append_message(self.directory, 'one inbox', {'name': 'user'})
        with patch.dict(os.environ, self.env, clear=True):
            result = report.run(self.root, agent=self.identity, phase='working', summary='same conversation')
            self.assertEqual(result['summary'], 'same conversation')
            self.assertEqual(inbox.run(self.root, agent=self.identity)['messages'][0]['text'], 'one inbox')
            write(self.directory / 'runtime.json', {'launch_id': 'replacement', 'state': 'dormant'})
            self.assertEqual(report.run(self.root, agent=self.identity, phase='done')['phase'], 'done')
        self.assertEqual(read(self.directory / 'runtime.json')['state'], 'dormant')

    def test_inbox_wait_survives_attachment_replacement_but_checks_agent_identity(self):
        def arrival():
            time.sleep(.05)
            write(self.directory / 'runtime.json', {'launch_id': 'replacement', 'state': 'running'})
            append_message(self.directory, 'after resume', {})
        worker = threading.Thread(target=arrival)
        with patch.dict(os.environ, self.env, clear=True):
            worker.start()
            try:
                result = inbox.run(self.root, agent=self.identity, wait=True, timeout='1s')
            finally:
                worker.join()
        self.assertEqual(result['messages'][0]['text'], 'after resume')

    def test_incomplete_or_wrong_proof_never_becomes_operator_authority(self):
        cases = [{key: value for key, value in self.env.items() if key != missing}
                 for missing in ('MV2_IDENTITY_MODE', 'MV2_AGENT_ID', 'MV2_UNIVERSE', 'MV2_AGENT_DIR',
                                 'FIXTURE_THREAD', 'FIXTURE_HOME')]
        cases += [{**self.env, 'FIXTURE_THREAD': str(uuid4())}, {**self.env, 'FIXTURE_HOME': '/different'},
                  {**self.env, 'MV2_AGENT_DIR': str(self.work)}, {**self.env, 'MV2_IDENTITY_MODE': 'other-v1'}]
        for env in cases:
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(ValueError, 'identity'):
                    report.run(self.root, agent=self.identity, summary='must not write')
        self.assertNotIn('summary', read(self.directory / 'properties.json'))

    def test_stable_actor_cannot_mutate_binding_or_report_runner_status(self):
        before = (self.directory / 'properties.json').read_bytes()
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(session_bind.run(self.root, agent=self.identity, session_id=self.native)['id'], self.identity)
            self.assertEqual((self.directory / 'properties.json').read_bytes(), before)
            with self.assertRaisesRegex(ValueError, 'verify its existing'):
                session_bind.run(self.root, agent=self.identity, session_id=str(uuid4()))
            with self.assertRaisesRegex(ValueError, 'exact terminal launch'):
                runtime_update.run(self.root, agent=self.identity, status='idle')
        self.assertEqual(read(self.directory / 'runtime.json')['reason'], 'latched failure')

    def test_sender_can_address_other_agents_without_becoming_them(self):
        other = str(uuid4())
        target = self.root / 'state' / 'agents' / other
        write(target / 'properties.json', {'id': other, 'name': 'recipient'})
        with patch.dict(os.environ, self.env, clear=True):
            send.run(self.root, agent='recipient', text='from one agent', delivery='inbox-only')
            with self.assertRaisesRegex(ValueError, 'another agent'):
                report.run(self.root, agent='recipient', summary='not mine')
        from multiverse.universe.mailbox import inbox as history
        self.assertEqual(history(target, all_messages=True)['messages'][0]['from']['agent_id'], self.identity)

    def test_changed_binding_or_scope_revokes_agent_authority_without_changing_a_launch(self):
        for props in ({**self.props, 'native_session_id': str(uuid4())},
                      {**self.props, 'native_service_identity': {'provider': 'foreign', 'scope': {}}}):
            write(self.directory / 'properties.json', props)
            with patch.dict(os.environ, self.env, clear=True):
                with self.assertRaisesRegex(ValueError, 'identity'):
                    inbox.run(self.root, agent=self.identity)

    def test_old_launch_context_never_acquires_agent_authority(self):
        old = {**self.env, 'MV2_LAUNCH_ID': 'old'}
        old.pop('MV2_IDENTITY_MODE')
        with patch.dict(os.environ, old, clear=True):
            self.assertIsNone(agent_identity.actor())
            with self.assertRaisesRegex(ValueError, 'Outdated launch'):
                report.run(self.root, agent=self.identity)
