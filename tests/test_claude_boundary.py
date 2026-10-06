"""Claude in the native-backends model: a terminal-hosted bridge, not a service.

The multiverse mod and the inbox it hosts live inside the Claude process, so a Claude launch declares no shared
service, attachment recovery, status observer or proxy, and its readiness is the mod's registration.
"""
import hashlib
import itertools
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse import agents, harnesses, native, recovery
from multiverse.harnesses.base import ConfiguredAdapter
from multiverse.harnesses.claude import BRIDGE, MOD, NOT_REGISTERED
from multiverse.harnesses.types import LaunchContext, Message, NativeEndpoint, SessionHandle
from multiverse.store import read, write
from multiverse.tools import runtime_update, send
from test_multiverse import BaseTest, TerminalBase, until

FOREIGN = ('other-bridge-v1', 'pi-socket-v1', 'claude-mod-v2')


class Inbox:
    """A local inbox like Claude's: reads JSON lines until EOF and sends no reply."""

    def __init__(self, path):
        self.lines = []
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(path)
        self.server.listen(1)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        connection, _ = self.server.accept()
        data = b''
        with connection:
            while chunk := connection.recv(65536):
                data += chunk
        self.lines = [json.loads(line) for line in data.decode().splitlines()]

    def close(self):
        self.thread.join(5)
        self.server.close()


class BoundaryBase:
    def prepare(self):
        self.config = self.work / 'claude-config'
        self.enterContext(patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.config)}))
        # The version probe never runs a real or stand-in binary here.
        self.enterContext(patch('multiverse.harnesses.claude.claude_version', return_value=(2, 1, 289)))

    def adapter(self, setting=None):
        path = self.root / 'harnesses/claude/harness.json'
        config = read(path)
        config['settings'].pop('inbox_bridge', None)
        if setting is not None:
            config['settings']['inbox_bridge'] = setting
        write(path, config)
        return harnesses.load(self.root, 'claude')


class ClaudeBoundaryTest(BoundaryBase, BaseTest):
    def setUp(self):
        super().setUp()
        self.prepare()
        self.socket = str(self.work / 'inbox.sock')

    def values(self):
        return dict(name='worker', agent_id=str(uuid4()), session_id=str(uuid4()), bootstrap='Hello',
                    bootstrap_json='"Hello"', task='', agent_dir=str(self.work / 'agent'), harness_dir='/h',
                    session_file='', bootstrap_file='', task_file='', workspace=str(self.work), model='',
                    control_socket=self.socket)

    def session(self, **fields):
        base = dict(state='running', native_id=str(uuid4()), endpoint=NativeEndpoint('unix-lines', self.socket),
                    bridge=BRIDGE, native_mode={'policy': 'auto', 'mode': 'bridge', 'fallback_reason': None},
                    session_file=str(self.work / 'agent' / 'session.jsonl'))
        return SessionHandle('agent', 'claude', str(self.work), **{**base, **fields})

    def records(self):
        """Saved records of a running, bridged Claude agent, as core writes them."""
        agent, native_id = str(uuid4()), str(uuid4())
        directory = self.root / 'state/agents' / agent
        properties = {'id': agent, 'name': 'worker', 'harness': 'claude', 'workspace': str(self.work),
                      'native_session_id': native_id}
        facts = {'launch_id': 'current', 'state': 'running', 'status': 'idle',
                 'control_endpoint': {'transport': 'unix-lines', 'address': self.socket},
                 'native_mode': {'policy': 'auto', 'mode': 'bridge', 'fallback_reason': None}}
        write(directory / 'properties.json', properties)
        write(directory / 'runtime.json', facts)
        return agent, directory, properties

    def test_every_launch_shape_is_terminal_hosted_with_no_service_attachment_or_proxy(self):
        values = self.values()
        auto, plain = self.adapter(None), self.adapter(False)
        saved = SessionHandle(values['agent_id'], 'claude', str(self.work), native_id=values['session_id'])
        launches = {'auto spawn': auto.spawn(LaunchContext('spawn', values)),
                    'auto resume': auto.resume(saved, LaunchContext('resume', values)),
                    'plain spawn': plain.spawn(LaunchContext('spawn', values)),
                    'plain resume': plain.resume(saved, LaunchContext('resume', values))}
        with patch('multiverse.harnesses.claude.claude_version', return_value=(2, 1, 270)):
            launches['auto fallback'] = auto.spawn(LaunchContext('spawn', values))
        for name, command in launches.items():
            with self.subTest(name):
                for absent in ('shared_service', 'rpc_proxy', 'attachment', 'fallback'):
                    self.assertIsNone(getattr(command, absent), absent)
                self.assertIsNone(native.launch_attachment(command))
                bridged = name.startswith('auto') and name != 'auto fallback'
                self.assertEqual(command.control_endpoint, NativeEndpoint('unix-lines', self.socket) if bridged else None)
                self.assertEqual(command.native_mode['mode'], 'bridge' if bridged else 'plain')

    def test_core_offers_no_recovery_or_retained_usage_for_a_bridged_session(self):
        adapter = self.adapter(None)
        _, directory, properties = self.records()
        facts = read(directory / 'runtime.json')
        session = SessionHandle.from_records(properties, facts, directory)
        self.assertEqual((session.service, session.attachment), (None, None))
        reason = native.recovery_unavailable(session)
        self.assertIn('has not declared', reason)
        self.assertEqual(recovery.eligibility(properties, facts), reason)
        self.assertEqual(recovery.check(properties, facts, directory).state, 'unavailable')
        command = adapter.spawn(LaunchContext('spawn', self.values()))
        with self.assertRaises(ValueError):
            native.validate_recovery_command(command, session)
        self.assertIsNone(adapter.attachment_recovery(session))
        self.assertIsNone(adapter.retained_usage(session, facts))

    def test_readiness_is_the_mods_registration_and_matches_the_rule_it_replaced(self):
        adapters = {None: self.adapter(None), False: self.adapter(False)}
        launches = {'bridge': {'policy': 'auto', 'mode': 'bridge', 'fallback_reason': None},
                    'plain': {'policy': 'auto', 'mode': 'plain', 'fallback_reason': 'Claude Code 2.1.270 is too old'},
                    None: None}
        wrong, ready = [], 0
        for setting, mode, transport, bridge, state, bound in itertools.product(
                (None, False), launches, (None, 'unix-lines', 'unix-json'), (None, BRIDGE, *FOREIGN),
                ('running', 'starting', 'stopped'), (True, False)):
            session = SessionHandle(
                'agent', 'claude', str(self.work), native_id=str(uuid4()) if bound else None, state=state,
                endpoint=NativeEndpoint(transport, self.socket) if transport else None, bridge=bridge,
                native_mode=launches[mode], session_file=str(self.work / 'agent' / 'session.jsonl'))
            adapter = adapters[setting]
            expected = transport is None or (setting is None and mode != 'plain' and transport == 'unix-lines'
                                             and bridge == BRIDGE and state == 'running' and bound)
            # The old rule asked the whole capability set; the hook must give the same answer without it.
            old = ConfiguredAdapter.attachment_ready(adapter, session)
            new = adapter.attachment_ready(session)
            ready += bool(new)
            if not (bool(old) == bool(new) == bool(expected)):
                wrong.append((setting, mode, transport, bridge, state, bound, bool(old), bool(new), bool(expected)))
        self.assertEqual(wrong[:5], [])
        self.assertGreater(ready, 0)

    def test_readiness_does_not_build_the_capability_set(self):
        adapter = self.adapter(None)
        with patch.object(type(adapter), 'capabilities', side_effect=AssertionError('readiness built capabilities')), \
                patch.object(type(adapter), '_compaction_capability', side_effect=AssertionError('read compaction')):
            self.assertTrue(adapter.attachment_ready(self.session()))
            self.assertFalse(adapter.attachment_ready(self.session(bridge=None)))
            self.assertTrue(adapter.attachment_ready(self.session(endpoint=None, bridge=None)))

    def test_a_foreign_bridge_id_never_makes_a_launch_ready_or_deliverable(self):
        adapter, executor = self.adapter(None), Mock()
        for bridge in (None, *FOREIGN):
            with self.subTest(bridge=bridge):
                session = self.session(bridge=bridge)
                self.assertFalse(adapter.attachment_ready(session))
                capabilities = adapter.capabilities(session)
                for name in ('send_steer', 'send_follow_up'):
                    self.assertEqual((capabilities[name].state, capabilities[name].reason), ('unavailable', NOT_REGISTERED))
                self.assertNotEqual(capabilities['ask'].state, 'supported')
                for delivery in ('steer', 'follow-up'):
                    message = Message('id', 'body', delivery=delivery, framed='Message from a:\nbody')
                    self.assertEqual(adapter.send(session, message, executor).state, 'unavailable')
                self.assertEqual(adapter.ask(session, 'What?', executor).state, 'unavailable')
        executor.write_lines.assert_not_called()
        exact = self.session(bridge=BRIDGE)
        self.assertTrue(adapter.attachment_ready(exact))
        self.assertEqual(adapter.capabilities(exact)['send_follow_up'].state, 'supported')

    def test_the_recorded_bridge_id_reaches_the_session_and_only_the_exact_one_registers(self):
        adapter = self.adapter(None)
        agent, directory, properties = self.records()

        def reported(**changes):
            with patch.dict(os.environ, {'MV2_LAUNCH_ID': 'current'}):
                runtime_update.run(self.root, agent=agent, **changes)
            session = SessionHandle.from_records(properties, read(directory / 'runtime.json'), directory)
            return session, adapter.attachment_ready(session)

        session, ready = reported(status='idle', bridge='other-bridge-v1')
        self.assertEqual((session.bridge, ready), ('other-bridge-v1', False))
        session, ready = reported(status='idle', bridge=BRIDGE)
        self.assertEqual((session.bridge, ready), (BRIDGE, True))
        session, ready = reported(status='idle', bridge='none')
        self.assertEqual((session.bridge, ready), (None, False))

    def test_the_mod_reports_the_bridge_id_the_adapter_waits_for(self):
        source = (MOD / 'hooks/index.ts').read_text()
        self.assertEqual(source.count('"--bridge"'), 1)
        self.assertIn(f'"--bridge", "{BRIDGE}"', source)

    def test_a_plain_fallback_explains_itself_in_every_operation(self):
        adapter, executor = self.adapter(None), Mock()
        reason = 'Claude Code 2.1.270 is older than 2.1.280, which the multiverse mod needs'
        fell_back = self.session(endpoint=None, bridge=None,
                                 native_mode={'policy': 'auto', 'mode': 'plain', 'fallback_reason': reason})
        self.assertTrue(adapter.attachment_ready(fell_back))  # nothing to wait for: the process is the work
        message = Message('id', 'body', framed='Message from a:\nbody')
        for result in (adapter.send(fell_back, message, executor), adapter.ask(fell_back, 'What?', executor)):
            self.assertEqual((result.state, result.reason), ('unavailable', 'Launched without the bridge: ' + reason))
        executor.write_lines.assert_not_called()


FAKE_CLAUDE = '''#!{python}
import json, os, sys, time
from pathlib import Path

Path(os.environ['MV2_AGENT_DIR'], 'seen.json').write_text(json.dumps({{'argv': sys.argv[1:]}}))
print('HARNESS READY', flush=True)
while True:
    time.sleep(1)
'''


class ClaudeBoundaryLaunchTest(BoundaryBase, TerminalBase):
    """A real launch through core with a stand-in `claude`; the test plays the mod when it registers."""

    def setUp(self):
        super().setUp()
        self.prepare()
        self.enterContext(patch('multiverse.native.start_shared_service',
            side_effect=AssertionError('A fake Claude must never prepare a native daemon')))
        directory = self.root / 'harnesses/claude'
        executable = directory / 'fake-cli'
        executable.write_text(FAKE_CLAUDE.format(python=sys.executable))
        executable.chmod(0o755)
        spec = read(directory / 'harness.json')
        spec['settings']['executable'] = str(executable)
        write(directory / 'harness.json', spec)
        self.adapter_ = self.adapter(None)

    def test_a_launched_claude_has_an_inbox_but_no_service_and_waits_for_the_mod(self):
        agent = agents.start(self.root, 'bridged', '', 'claude', socket_path=self.socket)
        self.addCleanup(lambda: agents.terminal_action(self.root, agent['id'], 'stop'))
        directory = agents.directory(self.root, agent['id'])
        until(lambda: (directory / 'seen.json').exists())
        facts = read(directory / 'runtime.json')
        launch = facts['launch_id']
        request = read(directory / 'launches' / launch / 'request.json')

        # Declarations: a unix-lines inbox in the launch's private directory, and nothing else.
        self.assertEqual(request['control_endpoint']['transport'], 'unix-lines')
        self.assertEqual((request['attachment'], request['rpc_proxy'], request['shared_service'], request['fallback']),
                         (None, None, False, None))
        self.assertEqual(facts['control_endpoint'], request['control_endpoint'])
        self.assertEqual((facts.get('native_service'), facts.get('attachment')), (None, None))
        self.assertEqual(facts['native_mode']['mode'], 'bridge')
        self.assertTrue(Path(facts['control_endpoint']['address']).parent.is_dir())

        def reported(**changes):
            with patch.dict(os.environ, {'MV2_LAUNCH_ID': launch}):
                runtime_update.run(self.root, agent=agent['id'], **changes)

        def waited(timeout):
            return agents.wait_ready(self.root, directory, launch, self.adapter_, timeout, include_delivery=False)

        # Readiness: the process running is not enough; a foreign id does not count; the exact id does.
        until(lambda: read(directory / 'runtime.json').get('startup_checked') is True)
        self.assertFalse(waited(1)['ready'])
        reported(status='idle', bridge='other-bridge-v1')
        self.assertFalse(waited(1)['ready'])
        reported(status='idle', bridge=BRIDGE)
        self.assertTrue(waited(10)['ready'])

        # Status: what the mod reports stays; core runs no observer that could reset it to unknown.
        reported(status='waiting')
        time.sleep(1.5)
        facts = read(directory / 'runtime.json')
        self.assertEqual(facts['status'], 'waiting')
        self.assertNotIn('native_relay', facts)

        # Delivery: one auth line and one tagged frame for the exact session, over the same inbox.
        address = facts['control_endpoint']['address']
        digest = hashlib.sha256(os.path.abspath(address).encode()).hexdigest()
        (self.config / 'sessions').mkdir(parents=True, exist_ok=True)
        (self.config / 'sessions' / f'4242.{digest}.key').write_text(json.dumps({'peerToken': '0' * 32}))
        sender = str(uuid4())
        write(self.root / 'state/agents' / sender / 'properties.json', {'id': sender, 'name': 'reviewer'})
        inbox = Inbox(address)
        with patch.object(native, 'owned'):
            result = send.run(self.root, agent=agent['id'], text='Please rebase',
                              sender={'agent_id': sender, 'universe': str(self.root), 'name': 'agent'})
        inbox.close()
        self.assertEqual(result['state'], 'native_queued')
        auth, frame = inbox.lines
        self.assertEqual(auth['type'], 'auth')
        self.assertEqual(frame['session_id'], agent['native_session_id'])
        self.assertEqual(frame['message']['content'].split('>>', 1)[1], 'Message from reviewer:\nPlease rebase<</MV2>>')


if __name__ == '__main__':
    import unittest
    unittest.main()
