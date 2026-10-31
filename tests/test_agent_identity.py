"""One agent's conversation authority is separate from a terminal generation."""
import contextlib
from copy import deepcopy
from dataclasses import asdict, replace
from functools import partial
import io
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch
from uuid import uuid4
from types import SimpleNamespace

from multiverse import cli
from multiverse.harnesses.types import AgentIdentitySpec, NativeEndpoint, NativeServiceHandle, ServiceIdentity
from multiverse.runtime import agent_identity
from multiverse.store import read, uid, write
from multiverse.tools import _common, catalog, inbox, list_agents, report, runtime_update, session_bind, send, focus, unfocus
from multiverse.universe.mailbox import append_message
from test_multiverse import BaseTest


class AgentIdentityTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.identity, self.native = str(uuid4()), str(uuid4())
        self.directory = self.root / 'state' / 'agents' / self.identity
        self.spec = AgentIdentitySpec('fixture-agent-v1', 'FIXTURE_THREAD', ('FIXTURE_HOME', 'OPTIONAL_HOME'))
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

    def command(self, env, entry, *args):
        """Run a CLI entry point in this process, as its `python -m` runs it, under only `env`."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with patch.dict(os.environ, env, clear=True), patch.object(sys, 'argv', ['mv2', '-u', str(self.root), *args]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                entry()
            except SystemExit as error:
                code = error.code or 0
        return code, out.getvalue(), err.getvalue()

    def test_stale_cli_context_can_read_catalog_and_list_but_cannot_act_as_the_agent(self):
        """Under a stale identity context catalog and list work without resolving the caller while report, inbox
        and send are refused; fragile because every new command must land on the right side, and a stale shell
        writing as the agent is silent."""
        project = Path(__file__).resolve().parents[1]
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('MV2_', 'CODEX_'))}
        # Explicit stable mode with incomplete native proof must not prevent a
        # read-only lookup, but must never become authority for an agent action.
        env.update({**self.env, 'FIXTURE_THREAD': 'wrong', 'PYTHONPATH': str(project / 'src')})
        listed = subprocess.run([sys.executable, '-m', 'multiverse.cli', '-u', str(self.root), 'list'],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertTrue(listed.stdout)
        refused = subprocess.run([sys.executable, '-m', 'multiverse.cli', '-u', str(self.root),
                                  'report', '--summary', 'must not write'],
                                 env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(refused.returncode, 1)
        self.assertIn('identity could not be verified', refused.stderr)
        with patch.object(agent_identity, 'actor', side_effect=AssertionError('identity lookup')):
            for standalone in (False, True):
                for action, module in (('catalog', catalog), ('list', list_agents)):
                    entry = partial(_common.main, module.configure, module.run) if standalone else cli.main
                    with self.subTest(action=action, standalone=standalone):
                        code, out, err = self.command(env, entry, *([] if standalone else [action]))
                        self.assertEqual(code, 0, err)
                        self.assertTrue(out)
        for args in (['inbox'], ['send', 'one-agent', '--text', 'must not save', '--delivery', 'inbox-only']):
            with self.subTest(action=args[0]):
                code, _, err = self.command(env, cli.main, *args)
                self.assertEqual(code, 1)
                self.assertIn('identity could not be verified', err)
        self.assertNotIn('summary', read(self.directory / 'properties.json'))

    def test_child_paths_follow_current_runtime_instead_of_cached_tool_settings(self):
        """A resumed conversation may keep old tool environment, so its children must use the current recorded run directory and socket together."""
        stale = str(self.work / 'cached-run')
        env = {**self.env, 'MV2_RUN_DIR': stale, 'MV2_TMUX_SOCKET': str(self.work / 'cached.sock')}
        for label in ('first', 'replacement'):
            root = self.work / label
            facts = {'launch_id': uid(), 'state': 'running', 'run_directory': str(root),
                     'tmux': {'socket': str(root / 'tmux.sock')}}
            write(self.directory / 'runtime.json', facts)
            with self.subTest(launch=label):
                self.assertEqual(agent_identity.calling_paths(env), (str(root / 'tmux.sock'), root))
                self.assertEqual(agent_identity.calling_socket(env), str(root / 'tmux.sock'))
        facts.pop('run_directory')
        write(self.directory / 'runtime.json', facts)
        self.assertEqual(agent_identity.calling_paths(env)[1], Path(stale))
        with self.assertRaisesRegex(ValueError, 'no recorded run directory'):
            agent_identity.calling_paths(env, require_recorded_run=True)
        for invalid in ('relative', None, [], ''):
            with self.subTest(run_directory=invalid):
                write(self.directory / 'runtime.json', {**facts, 'run_directory': invalid})
                with self.assertRaisesRegex(ValueError, 'run directory'):
                    agent_identity.calling_paths(env)
        (self.directory / 'runtime.json').unlink()
        with self.assertRaisesRegex(ValueError, 'runtime record'):
            agent_identity.calling_paths(env)

    def test_report_and_inbox_follow_the_agent_across_launches(self):
        """report and inbox follow the stable agent across a failed or replaced attachment, and an inbox wait
        survives a resume; fragile because authority checks sit beside launch-generation checks and are easily
        conflated, so agents lose messages after every resume."""
        append_message(self.directory, 'one inbox', {'name': 'user'})
        with patch.dict(os.environ, self.env, clear=True):
            result = report.run(self.root, agent=self.identity, phase='working', summary='same conversation')
            self.assertEqual(result['summary'], 'same conversation')
            self.assertEqual(inbox.run(self.root, agent=self.identity)['messages'][0]['text'], 'one inbox')
            write(self.directory / 'runtime.json', {'launch_id': 'replacement', 'state': 'dormant'})
            self.assertEqual(report.run(self.root, agent=self.identity, phase='done')['phase'], 'done')
        self.assertEqual(read(self.directory / 'runtime.json')['state'], 'dormant')

        def arrival():
            time.sleep(.05)
            write(self.directory / 'runtime.json', {'launch_id': 'resumed', 'state': 'running'})
            append_message(self.directory, 'after resume', {})
        worker = threading.Thread(target=arrival)
        with patch.dict(os.environ, self.env, clear=True):
            worker.start()
            try:
                result = inbox.run(self.root, agent=self.identity, wait=True, timeout='1s')
            finally:
                worker.join()
        self.assertEqual(result['messages'][0]['text'], 'after resume')

    def test_incomplete_or_wrong_proof_never_becomes_authority(self):
        """Each missing or wrong element of an identity proof, in stable and in legacy per-launch mode, is refused,
        and no identity at all means the operator; fragile because the proof has six env elements plus binding and
        scope and env leaks into children and old panes, so a break is a silent privilege escalation or
        misattribution (797bfec)."""
        cases = [{key: value for key, value in self.env.items() if key != missing}
                 for missing in ('MV2_IDENTITY_MODE', 'MV2_AGENT_ID', 'MV2_UNIVERSE', 'MV2_AGENT_DIR',
                                 'FIXTURE_THREAD', 'FIXTURE_HOME')]
        cases += [{**self.env, 'FIXTURE_THREAD': str(uuid4())}, {**self.env, 'FIXTURE_HOME': '/different'},
                  {**self.env, 'MV2_AGENT_DIR': str(self.work)}, {**self.env, 'MV2_IDENTITY_MODE': 'other-v1'}]
        for env in cases:
            with self.subTest(mode='stable', env=env), patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(ValueError, 'identity'):
                    report.run(self.root, agent=self.identity, summary='must not write')
        for props in ({**self.props, 'native_session_id': str(uuid4())},
                      {**self.props, 'native_service_identity': {'provider': 'foreign', 'scope': {}}}):
            write(self.directory / 'properties.json', props)
            with self.subTest(mode='stable', properties=props), patch.dict(os.environ, self.env, clear=True):
                with self.assertRaisesRegex(ValueError, 'identity'):
                    inbox.run(self.root, agent=self.identity)
        write(self.directory / 'properties.json', self.props)
        old = {**self.env, 'MV2_LAUNCH_ID': 'old'}
        old.pop('MV2_IDENTITY_MODE')
        with self.subTest(mode='old launch'), patch.dict(os.environ, old, clear=True):
            self.assertIsNone(agent_identity.actor())
            with self.assertRaisesRegex(ValueError, 'identity could not be verified'):
                report.run(self.root, agent=self.identity)
        self.assertNotIn('summary', read(self.directory / 'properties.json'))

        # A legacy per-launch agent, of a harness whose configuration its adapter refuses.
        directory = self.root / 'state' / 'agents' / uid()
        properties = {'id': directory.name, 'universe_id': read(self.root / 'universe.json')['id'],
                      'name': 'sender', 'harness': 'codex', 'workspace': str(self.work)}
        write(directory / 'properties.json', properties)
        write(directory / 'runtime.json', {'launch_id': 'current'})
        write(self.root / 'harnesses/codex/harness.json', {'schema': 2, 'adapter': 'codex',
              'settings': {'inbox_bridge': True, 'args': ['--remote', 'unix:///custom']}})
        env = {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': directory.name,
               'MV2_AGENT_DIR': str(directory), 'MV2_LAUNCH_ID': 'current'}
        self.assertIsNone(agent_identity.sender_actor({'MV2_UNIVERSE': str(self.root)}))
        self.assertEqual(agent_identity.sender_actor(env).sender()['name'], 'sender')
        for action in (report.run, inbox.run):
            with self.subTest(mode='legacy', action=action), \
                    patch.dict(os.environ, {**env, 'MV2_LAUNCH_ID': 'old'}, clear=True):
                with self.assertRaisesRegex(ValueError, 'Outdated launch'):
                    action(self.root, agent=directory.name)
            with self.subTest(mode='legacy', action=action, proof='incomplete'), patch.dict(os.environ,
                    {**env, 'MV2_LAUNCH_ID': '', 'MV2_IDENTITY_MODE': 'codex-agent-v1'}, clear=True):
                with self.assertRaisesRegex(ValueError, 'Agent identity could not be verified'):
                    action(self.root, agent=directory.name)
        self.assertNotIn('summary', read(directory / 'properties.json'))
        for key in env:
            with self.subTest(mode='legacy', missing=key), self.assertRaises(ValueError):
                agent_identity.sender_actor({name: value for name, value in env.items() if name != key})
        for changed in ({'MV2_AGENT_DIR': str(self.work)}, {'MV2_LAUNCH_ID': 'old'},
                        {'MV2_AGENT_ID': directory.name.upper()}):
            with self.subTest(mode='legacy', changed=changed), self.assertRaises(ValueError):
                agent_identity.sender_actor({**env, **changed})
        write(directory / 'properties.json', {**properties, 'universe_id': uid()})
        with self.subTest(mode='legacy', changed='universe'), self.assertRaises(ValueError):
            agent_identity.sender_actor(env)
        write(directory / 'properties.json', properties)
        (directory / 'runtime.json').unlink()
        with self.subTest(mode='legacy', changed='no runtime'), self.assertRaisesRegex(ValueError, 'Outdated launch'):
            agent_identity.sender_actor(env)
        (directory / 'properties.json').write_text('[]')
        with self.subTest(mode='legacy', changed='damaged'), self.assertRaisesRegex(ValueError, 'properties.json'):
            agent_identity.sender_actor(env)

    def test_recorded_bridge_launch_proves_only_data_callbacks(self):
        """Cached pre-update tools need every historical and current proof; one missing guard silently authorizes another launch."""
        old = '1d4f573b-35a6-4628-99ee-0c23d518f47d'  # QA's measured a8d2f81 launch shape.
        env = {key: value for key, value in self.env.items() if key != 'MV2_IDENTITY_MODE'}
        env['MV2_LAUNCH_ID'] = old
        request = {'env': {key: env[key] for key in ('MV2_UNIVERSE', 'MV2_AGENT_ID', 'MV2_AGENT_DIR', 'MV2_LAUNCH_ID')},
                   'shared_service': True, 'native_mode': {'policy': 'required', 'mode': 'bridge'},
                   'rpc_proxy': {'socket_path': '/old/relay', 'upstream_path': '/old/service',
                                 'settings': {}, 'binds_session': True}}
        path = self.directory / 'launches' / old / 'request.json'
        facts = {'launch_id': uid(), 'state': 'failed', 'reason': 'retained failure',
                 'native_mode': {'mode': 'bridge'}, 'tmux': {'socket': '/current/server'}}
        write(self.directory / 'runtime.json', facts)
        adapter = SimpleNamespace(allow_recorded_launch=lambda session: True,
                                  agent_identity_policy=lambda: self.spec)
        # A current per-launch caller still uses its original generation checks,
        # even if a previous connected launch left a saved stable policy.
        with patch('multiverse.harnesses.load', side_effect=AssertionError('current launch needs no adapter lookup')), \
                patch.dict(os.environ, {**env, 'MV2_LAUNCH_ID': facts['launch_id']}, clear=True):
            self.assertEqual(agent_identity.sender_actor().identity, self.identity)
            self.assertEqual(_common.callback(self.root, self.identity, agent_scoped=True)[2], facts['launch_id'])
        write(path, request)
        append_message(self.directory, 'retained inbox', {'name': 'user'})
        with patch('multiverse.harnesses.load', return_value=adapter), patch.dict(os.environ, env, clear=True):
            # The compatibility route must not turn generic actor()/child spawning into stable authority.
            self.assertIsNone(agent_identity.actor())
            with self.assertRaisesRegex(ValueError, 'Outdated launch'):
                agent_identity.calling_socket()
            with self.assertRaisesRegex(ValueError, 'Outdated launch'):
                runtime_update.run(self.root, agent=self.identity, status='idle')
            before = (self.directory / 'properties.json').read_bytes()
            self.assertEqual(session_bind.run(self.root, agent=self.identity, session_id=self.native)['id'], self.identity)
            self.assertEqual((self.directory / 'properties.json').read_bytes(), before)
            caller = agent_identity.sender_actor()
            with patch.object(session_bind, 'actor', side_effect=[caller, None]), self.assertRaises(ValueError):
                session_bind.run(self.root, agent=self.identity, session_id=self.native)
            with self.assertRaisesRegex(ValueError, 'verify its existing'):
                session_bind.run(self.root, agent=self.identity, session_id=uid())
            self.assertEqual(report.run(self.root, agent=self.identity, summary='cached tools')['summary'], 'cached tools')
            self.assertEqual(inbox.run(self.root, agent=self.identity)['messages'][0]['text'], 'retained inbox')
            self.assertEqual(agent_identity.sender_actor().identity, self.identity)
            self.assertTrue(focus.run(self.root, agent=self.identity)['focused'])
            self.assertFalse(unfocus.run(self.root, agent=self.identity)['focused'])
            self.assertEqual(dict(os.environ), env)  # no MODE or current-LID substitution
            self.assertEqual(read(path), request)
            self.assertEqual(read(self.directory / 'runtime.json'), facts)
            other = self.root / 'state' / 'agents' / uid()
            write(other / 'properties.json', {'id': other.name, 'name': 'other'})
            send.run(self.root, agent='other', text='cached sender', delivery='inbox-only')
            from multiverse.universe.mailbox import inbox as history
            self.assertEqual(history(other, all_messages=True)['messages'][0]['from']['agent_id'], self.identity)
            for action in (report.run, inbox.run, focus.run, unfocus.run):
                with self.subTest(target='another agent', action=action), self.assertRaises(ValueError):
                    action(self.root, agent='other')
            with self.assertRaises(ValueError):
                session_bind.run(self.root, agent='other', session_id=self.native)

        # Each row changes just one proof. Final/current records never repair a missing old request.
        cases = []
        for key in request['env']:
            for value in (None, 'foreign'):
                changed = deepcopy(request)
                if value is None:
                    del changed['env'][key]
                else:
                    changed['env'][key] = value
                cases.append((f'request {key} {value}', {}, self.props, changed, adapter))
        for field, value in (('shared_service', False), ('shared_service', 1),
                             ('native_mode', {'mode': 'plain'}), ('native_mode', []),
                             ('rpc_proxy', {'binds_session': False}), ('rpc_proxy', {'binds_session': 1}),
                             ('rpc_proxy', []), ('env', [])):
            cases.append((f'request {field} {value}', {}, self.props, {**request, field: value}, adapter))
        for key, value in (('MV2_LAUNCH_ID', old.upper()), ('MV2_AGENT_DIR', str(self.work))):
            changed = deepcopy(request)
            changed['env'][key] = value
            cases.append((f'caller and request {key}', {key: value}, self.props, changed, adapter))
        for key, value in (('MV2_IDENTITY_MODE', 'wrong-mode'), ('MV2_IDENTITY_MODE', ''),
                           ('MV2_LAUNCH_ID', uid()), ('MV2_LAUNCH_ID', old.upper()),
                           ('MV2_LAUNCH_ID', '../' + old), ('MV2_LAUNCH_ID', ''),
                           ('FIXTURE_THREAD', uid()), ('FIXTURE_HOME', '/foreign'),
                           ('OPTIONAL_HOME', '/foreign'),
                           ('MV2_AGENT_DIR', str(self.work))):
            cases.append((f'caller {key} {value}', {key: value}, self.props, request, adapter))
        for key, value in (('id', uid()), ('native_session_id', uid()), ('workspace', str(self.work / 'foreign')),
                           ('universe_id', uid()), ('native_service_identity', {'provider': 'foreign', 'scope': {}}),
                           ('agent_identity', {})):
            cases.append((f'properties {key}', {}, {**self.props, key: value}, request, adapter))
        changed = deepcopy(self.props)
        changed['agent_identity']['native_id'] = uid()
        cases.append(('saved policy native id', {}, changed, request, adapter))
        changed = deepcopy(self.props)
        changed['agent_identity']['schema'] = 2
        cases.append(('saved policy schema', {}, changed, request, adapter))
        for label, selected in (
                ('no opt-in', SimpleNamespace(agent_identity_policy=lambda: self.spec)),
                ('false opt-in', SimpleNamespace(allow_recorded_launch=lambda session: False,
                                                agent_identity_policy=lambda: self.spec)),
                ('non-boolean opt-in', SimpleNamespace(allow_recorded_launch=lambda session: 1,
                                                      agent_identity_policy=lambda: self.spec)),
                ('different adapter policy', SimpleNamespace(allow_recorded_launch=lambda session: True,
                    agent_identity_policy=lambda: replace(self.spec, mode='other-v1')))):
            cases.append((label, {}, self.props, request, selected))
        for value in (None, [], 'torn'):
            cases.append((f'unreadable request {value}', {}, self.props, value, adapter))
        for label, changed_env, properties, retained, selected in cases:
            write(self.directory / 'properties.json', properties)
            if retained is None:
                path.unlink(missing_ok=True)
            elif retained == 'torn':
                path.write_text('{')
            else:
                write(path, retained)
            before = (self.directory / 'properties.json').read_bytes()
            with self.subTest(proof=label), patch('multiverse.harnesses.load', return_value=selected), \
                    patch.dict(os.environ, {**env, **changed_env}, clear=True):
                with self.assertRaises(ValueError):
                    report.run(self.root, agent=self.identity, summary='must not write')
                for action in (focus.run, unfocus.run):
                    with self.assertRaises(ValueError):
                        action(self.root, agent=self.identity)
                self.assertEqual((self.directory / 'properties.json').read_bytes(), before)

    def test_an_agent_cannot_rebind_fake_runner_status_or_become_another(self):
        """An agent's identity can neither rebind its session, report runner status, nor act as an agent it
        messages; a break is a silent privilege escalation."""
        before = (self.directory / 'properties.json').read_bytes()
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(session_bind.run(self.root, agent=self.identity, session_id=self.native)['id'], self.identity)
            self.assertEqual((self.directory / 'properties.json').read_bytes(), before)
            with self.assertRaisesRegex(ValueError, 'verify its existing'):
                session_bind.run(self.root, agent=self.identity, session_id=str(uuid4()))
            with self.assertRaisesRegex(ValueError, 'exact terminal launch'):
                runtime_update.run(self.root, agent=self.identity, status='idle')
        self.assertEqual(read(self.directory / 'runtime.json')['reason'], 'latched failure')
        other = str(uuid4())
        target = self.root / 'state' / 'agents' / other
        write(target / 'properties.json', {'id': other, 'name': 'recipient'})
        with patch.dict(os.environ, self.env, clear=True):
            send.run(self.root, agent='recipient', text='from one agent', delivery='inbox-only')
            with self.assertRaisesRegex(ValueError, 'another agent'):
                report.run(self.root, agent='recipient', summary='not mine')
        from multiverse.universe.mailbox import inbox as history
        self.assertEqual(history(target, all_messages=True)['messages'][0]['from']['agent_id'], self.identity)
