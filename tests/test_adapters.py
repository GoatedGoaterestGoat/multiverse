"""Independent adapter contracts and core native execution with no provider calls."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse import agents, harnesses, native, universe, usage
from multiverse.dashboard import LocalBackend, harnesses as discover
from multiverse.harnesses.types import CAPABILITIES, Capability, CommandSpec, LaunchContext, NativeContext, NativeResult
from multiverse.store import append_message, inbox, read, write
from multiverse.tools import capabilities, deliver, native_status
from test_multiverse import BaseTest


class AdapterContractTest(BaseTest):
    def values(self):
        return dict(name='literal', agent_id=str(uuid4()), session_id=str(uuid4()), bootstrap='Hello "quoted"',
                    bootstrap_json='"Hello"', task='--literal $(false) `false` {literal}',
                    agent_dir=str(self.work / 'agent'), harness_dir='/local/harness', session_file='/agent/session.jsonl',
                    bootstrap_file='/launch/bootstrap.md', task_file='/launch/task.md', workspace=str(self.work), model='chosen')

    def test_builtins_and_legacy_have_equivalent_launches(self):
        for name in harnesses.BUILTINS:
            with self.subTest(name=name):
                adapter = harnesses.load(self.root, name)
                legacy = harnesses.LegacyAdapter(name, read(harnesses.PACKAGES / name / 'defaults.json'))
                for mode in ('spawn', 'resume'):
                    context = LaunchContext(mode, self.values(), 'chosen')
                    self.assertEqual(adapter.launch(context), legacy.launch(context))
                self.assertEqual(set(adapter.capabilities(NativeContext({}))), set(CAPABILITIES))

    def test_configuration_is_declarative_and_never_rewritten(self):
        config = self.root / 'harnesses/codex/harness.json'
        data = read(config)
        data['settings'] = {'model': 'local-model', 'executable': '/custom/codex',
                            'args': ['--sandbox', 'read-only'], 'resume_args': ['--add-dir', '/custom/worktree'],
                            'unset_env': ['CUSTOM_PARENT']}
        write(config, data)
        original = config.read_bytes()
        adapter = harnesses.load(self.root, 'codex')
        result = adapter.launch(LaunchContext('resume', self.values(), adapter.default_model))
        self.assertEqual(result.argv[0], '/custom/codex')
        self.assertIn('/custom/worktree', result.argv)
        self.assertIn('read-only', result.argv)
        self.assertIn('local-model', result.argv)
        self.assertEqual(set(result.unset_env), {'CODEX_THREAD_ID', 'CLAUDECODE', 'CUSTOM_PARENT'})
        self.assertEqual(config.read_bytes(), original)

    def test_legacy_preserves_every_custom_argument_and_asset(self):
        path = self.root / 'harnesses/codex/harness.json'
        config = read(harnesses.PACKAGES / 'codex/defaults.json')
        config['spawn'][1:1] = ['--profile', 'my-settings', '--sandbox', 'read-only']
        config['bootstrap'] += '\nA local instruction.'
        config['private_preference'] = {'anything': 'retained'}
        write(path, config)
        before = path.read_bytes()
        adapter = harnesses.load(self.root, 'codex')
        self.assertIs(type(adapter), harnesses.LegacyAdapter)
        self.assertEqual(adapter.spec, config)
        self.assertEqual(path.read_bytes(), before)
        self.assertIn('my-settings', adapter.launch(LaunchContext('spawn', self.values())).argv)

    def test_malformed_and_unknown_settings_fail_cleanly_and_discovery_survives(self):
        path = self.root / 'harnesses/codex/harness.json'
        cases = [[], {'schema': 19}, {'schema': 2, 'adapter': 'missing'},
                 {'schema': 2, 'adapter': 'codex', 'settings': []},
                 {'schema': 2, 'adapter': 'codex', 'settings': {'typo': True}},
                 {'schema': 2, 'adapter': 'codex', 'settings': {'args': '--bad'}},
                 {'schema': 2, 'adapter': 'codex', 'settings': {'args': ['{unknown}']}},
                 {'schema': 2, 'adapter': 'codex', 'settings': {'inbox_bridge': True}},
                 {'schema': 1, 'spawn': []}, {'schema': 1, 'spawn': [22]},
                 {'schema': 1, 'spawn': ['binary'], 'bootstrap': []}]
        for data in cases:
            with self.subTest(data=data):
                write(path, data)
                with self.assertRaises(ValueError):
                    harnesses.load(self.root, 'codex')
                self.assertNotIn('codex', discover(self.root))

    def test_pi_exact_session_file_required_and_native_id_preserved(self):
        adapter = harnesses.load(self.root, 'pi')
        directory = self.work / 'agent'
        directory.mkdir()
        with self.assertRaisesRegex(ValueError, 'missing'):
            adapter.validate_resume({}, directory)
        for contents in ('', 'not json', '{}', '{"type":"session","id":"bad"}'):
            (directory / 'session.jsonl').write_text(contents)
            with self.assertRaises(ValueError):
                adapter.validate_resume({}, directory)
        native_id = str(uuid4())
        write(directory / 'session.jsonl', {'type': 'session', 'id': native_id})
        # Native files are JSONL, not pretty-printed JSON.
        (directory / 'session.jsonl').write_text(json.dumps({'type': 'session', 'id': native_id}) + '\n')
        adapter.validate_resume({'native_session_id': native_id}, directory)
        with self.assertRaisesRegex(ValueError, 'different native'):
            adapter.validate_resume({'native_session_id': str(uuid4())}, directory)

    def test_pi_bridge_is_source_owned_opt_in_and_requires_current_handshake(self):
        path = self.root / 'harnesses/pi/harness.json'
        self.assertFalse((path.parent / 'extension.ts').exists())
        config = read(path)
        config['settings']['inbox_bridge'] = True
        write(path, config)
        adapter = harnesses.load(self.root, 'pi')
        argv = adapter.launch(LaunchContext('spawn', self.values())).argv
        self.assertEqual(argv[argv.index('--extension') + 1], str(harnesses.PACKAGES / 'pi/extension.ts'))
        for facts, expected in (({}, 'unavailable'), ({'state': 'stopped', 'native_bridge': 'pi-inbox-v1'}, 'unavailable'),
                                ({'state': 'running', 'native_bridge': 'pi-inbox-v1'}, 'supported')):
            caps = adapter.capabilities(NativeContext({}, facts))
            self.assertEqual(caps['steer'].state, expected)
            self.assertEqual(caps['follow_up'].state, expected)
            self.assertEqual(caps['ask'].state, 'unavailable')

    def test_queue_probe_is_explicit_and_never_claims_steering_or_wake(self):
        adapter = harnesses.load(self.root, 'codex')
        props = {'native_session_id': str(uuid4()), 'workspace': str(self.work)}
        with patch.object(native, 'execute') as execute:
            caps = adapter.capabilities(NativeContext(props))
            execute.assert_not_called()
            self.assertEqual(caps['native_queue'].state, 'unavailable')
            execute.return_value = subprocess.CompletedProcess([], 0, 'Usage: queue --thread UUID --message TEXT', '')
            ctx = NativeContext(props, probes=native.probe(adapter, self.work))
            caps = adapter.capabilities(ctx)
            self.assertEqual(caps['native_queue'].state, 'supported')
            for name in ('steer', 'follow_up', 'wake', 'usage', 'interrupt', 'ask'):
                self.assertEqual(caps[name].state, 'unavailable')
            command = adapter.control('native_queue', ctx, 'literal $(false)')
            self.assertEqual(command.argv[command.argv.index('--thread') + 1], props['native_session_id'])
            self.assertEqual(command.argv[command.argv.index('--message') + 1], 'literal $(false)')
            execute.return_value = subprocess.CompletedProcess([], 2, '', 'unrecognized subcommand queue')
            self.assertEqual(native.probe(adapter, self.work)['native_queue'].state, 'unavailable')
            execute.return_value = subprocess.CompletedProcess([], 1, '', 'connection failed')
            self.assertEqual(native.probe(adapter, self.work)['native_queue'].state, 'error')
            execute.side_effect = subprocess.TimeoutExpired('queue', 5)
            self.assertEqual(native.probe(adapter, self.work)['native_queue'].state, 'error')


class NativeDeliveryTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.agent = str(uuid4())
        self.directory = self.root / 'state/agents' / self.agent
        self.native_id = str(uuid4())
        write(self.directory / 'properties.json', {'id': self.agent, 'name': 'worker', 'harness': 'codex',
               'native_session_id': self.native_id, 'workspace': str(self.work)})
        write(self.directory / 'runtime.json', {'launch_id': 'current', 'state': 'running'})

    def test_queue_receipt_keeps_exact_message_recipient_reply_and_does_not_ack(self):
        message = append_message(self.directory, 'literal $(false)', {'name': 'sender'}, 'reply-id')
        with patch.object(native, 'owned'), patch.object(native, 'execute') as execute:
            execute.side_effect = [subprocess.CompletedProcess([], 0, '--thread --message', ''),
                                   subprocess.CompletedProcess([], 0, 'queued', '')]
            receipt = deliver.run(self.root, agent=self.agent, message_id=message['id'], mode='native-queue')
            self.assertEqual(receipt['state'], 'native_queued')
            self.assertEqual(receipt['native_session_id'], self.native_id)
            command = execute.call_args.args[0]
            payload = command.argv[command.argv.index('--message') + 1]
            for expected in (message['id'], 'reply-id', 'literal $(false)'):
                self.assertIn(expected, payload)
            self.assertEqual(inbox(self.directory)['messages'], [message])
            execute.side_effect = None
            execute.return_value = subprocess.CompletedProcess([], 0, '--thread --message', '')
            repeated = deliver.run(self.root, agent=self.agent, message_id=message['id'], mode='native-queue')
            self.assertTrue(repeated['reused'])
            self.assertEqual(len(read(self.directory / 'native-delivery.json')[message['id']]), 1)

    def test_failed_delivery_keeps_inbox_and_requires_explicit_retry(self):
        message = append_message(self.directory, 'retry me', {})
        with patch.object(native, 'owned'), patch.object(native, 'execute') as execute:
            execute.side_effect = [subprocess.CompletedProcess([], 0, '--thread --message', ''),
                                   subprocess.TimeoutExpired('queue', 10)]
            result = native.deliver(self.root, self.agent, message['id'], mode='native-queue')
            self.assertEqual(result['state'], 'error')
            self.assertIn('unknown', result['reason'])
            self.assertEqual(len(inbox(self.directory)['messages']), 1)
            execute.side_effect = None
            execute.return_value = subprocess.CompletedProcess([], 0, '--thread --message', '')
            self.assertTrue(native.deliver(self.root, self.agent, message['id'], mode='native-queue')['reused'])
            result = native.deliver(self.root, self.agent, message['id'], retry=True, mode='native-queue')
            self.assertEqual(result['state'], 'native_queued')
            self.assertEqual(len(read(self.directory / 'native-delivery.json')[message['id']]), 2)

    def test_unsupported_steering_never_falls_back_to_queue_or_terminal_input(self):
        message = append_message(self.directory, 'steer me', {})
        with patch.object(native, 'probe', return_value={}), patch.object(native, 'execute') as execute:
            result = native.deliver(self.root, self.agent, message['id'])
            self.assertEqual(result['state'], 'unavailable')
            execute.assert_not_called()
        self.assertEqual(len(inbox(self.directory)['messages']), 1)
        self.assertEqual(native_status.run(self.root, agent=self.agent, operation='ask')['state'], 'unavailable')

    def test_wrong_recipient_and_stale_terminal_never_receive(self):
        with patch.object(native, 'probe', return_value={}):
            with self.assertRaisesRegex(ValueError, 'unread inbox'):
                native.deliver(self.root, self.agent, str(uuid4()))
        message = append_message(self.directory, 'not to stale pane', {})
        with patch.object(native, 'probe', return_value={'native_queue': Capability('supported')}), \
             patch.object(native, 'owned', side_effect=ValueError('wrong launch')), patch.object(native, 'execute') as execute:
            result = native.deliver(self.root, self.agent, message['id'], mode='native-queue')
            self.assertEqual(result['state'], 'error')
            execute.assert_not_called()

    def test_shared_tools_and_backend_return_same_capabilities(self):
        self.assertEqual(capabilities.run(self.root, agent=self.agent), LocalBackend().capabilities(self.root, self.agent))
        self.assertEqual(native_status.run(self.root, agent=self.agent), LocalBackend().native_status(self.root, self.agent))

    def test_optional_usage_failure_is_diagnosable_and_does_not_break_snapshot(self):
        for result in (NativeResult('unavailable', 'not installed'), NativeResult('error', 'broken pipe')):
            with patch.object(harnesses.CodexAdapter, 'control', return_value=result):
                snapshot = LocalBackend().snapshot(self.root)
                row = snapshot['agents'][0]
                self.assertEqual(row['tokens'], 'N/A')
                self.assertEqual(row['usage']['state'], result.state)
                self.assertIn(result.reason, row['usage_detail'])
        with patch.object(harnesses.CodexAdapter, 'control', side_effect=RuntimeError('adapter failed')):
            self.assertEqual(LocalBackend().snapshot(self.root)['agents'][0]['usage']['state'], 'error')
        self.assertEqual(usage.display({'state': 'ok', 'value': {'total_tokens': 0}}), '0')
        self.assertEqual(usage.display({'state': 'ok', 'value': {'total_tokens': None}}), 'N/A')

    def test_native_subprocess_scrubs_identity_and_preserves_literal_argv(self):
        script = 'import json,os,sys; print(json.dumps([sys.argv[1:], sorted(k for k in os.environ if k.startswith("MV2_") or k in ("CODEX_THREAD_ID","CLAUDECODE"))]))'
        with patch.dict(os.environ, MV2_AGENT_ID='parent', CODEX_THREAD_ID='parent', CLAUDECODE='parent'):
            result = native.execute(CommandSpec((sys.executable, '-c', script, '$(false)\n{literal}')), self.work)
        self.assertEqual(json.loads(result.stdout), [['$(false)\n{literal}'], []])


if __name__ == '__main__':
    unittest.main()
