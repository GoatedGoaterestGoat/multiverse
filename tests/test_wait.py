"""Launch readiness is bound to one generation and never holds the bind lock."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
import threading
import time
from unittest.mock import patch
from uuid import uuid4

from multiverse import agents, harnesses
from multiverse.cli import parser, run
from multiverse.store import read, write
from test_multiverse import BaseTest, TerminalBase


class WaitTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory = self.root / 'state/agents' / str(uuid4())
        self.launch = str(uuid4())
        self.native = str(uuid4())
        write(self.directory / 'properties.json', {'id': self.directory.name, 'name': 'waiting',
            'harness': 'codex', 'workspace': str(self.work), 'universe_id': 'universe'})
        write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'starting', 'pid': 123,
            'control_endpoint': {'transport': 'unix-websocket', 'address': '/fake/socket'}})
        write(self.directory / 'launches' / self.launch / 'request.json', {'shared_service': True, 'rpc_proxy': {'settings': {}}})
        self.enterContext(patch.object(agents, 'observe', return_value={'live': 'alive'}))
        self.adapter = harnesses.load(self.root, 'codex')

    def wait(self, timeout=.08):
        return agents.wait_ready(self.root, self.directory, self.launch, self.adapter, timeout)

    def test_wait_allows_binding_but_requires_validation_before_ready(self):
        with ThreadPoolExecutor() as pool:
            waiting = pool.submit(self.wait, 2)
            bound = threading.Event()
            def bind():
                agents.bind_session(self.root, self.directory.name, self.native, self.launch)
                bound.set()
            binding = pool.submit(bind)
            self.assertTrue(bound.wait(1), 'Wait must not hold the launch lock')
            binding.result()
            self.assertFalse(waiting.done(), 'A bound ID with state=starting is not validated')
            agents.update_runtime(self.directory, self.launch, state='running')
            result = waiting.result(timeout=1)
        self.assertTrue(result['ready'])
        self.assertEqual(result['session']['native_id'], self.native)
        self.assertEqual(result['session']['state'], 'running')

    def test_timeout_keeps_launch_running_and_returns_latest_bound_identity(self):
        agents.bind_session(self.root, self.directory.name, self.native, self.launch)
        before = read(self.directory / 'runtime.json')
        result = self.wait()
        self.assertFalse(result['ok'])
        self.assertFalse(result['ready'])
        self.assertTrue(result['timed_out'])
        self.assertIn('launch was not stopped', result['error'])
        self.assertEqual(result['native_session_id'], self.native)
        self.assertEqual(read(self.directory / 'runtime.json'), before)

    def test_permission_failure_is_returned_without_waiting_for_timeout(self):
        agents.update_runtime(self.directory, self.launch, error='Requested sandbox grant was not applied')
        result = self.wait()
        self.assertFalse(result['ready'])
        self.assertNotIn('timed_out', result)
        self.assertIn('sandbox grant', result['error'])

    def test_plain_readiness_requires_child_pid_but_not_native_binding(self):
        write(self.directory / 'launches' / self.launch / 'request.json', {})
        agents.update_runtime(self.directory, self.launch, state='running', pid=None, control_endpoint=None)
        self.assertTrue(self.wait()['timed_out'])
        agents.update_runtime(self.directory, self.launch, pid=123)
        self.assertTrue(self.wait()['ready'])

    def test_extension_bridge_waits_for_its_registration(self):
        spec = read(self.root / 'harnesses/claude/harness.json')
        spec['settings'] = {'inbox_bridge': True}
        write(self.root / 'harnesses/claude/harness.json', spec)
        self.adapter = harnesses.load(self.root, 'claude')
        write(self.directory / 'launches' / self.launch / 'request.json', {'control_endpoint': {'transport': 'unix-lines'}})
        agents.bind_session(self.root, self.directory.name, self.native, self.launch)
        agents.update_runtime(self.directory, self.launch, state='running',
                              control_endpoint={'transport': 'unix-lines', 'address': '/fake/inbox'})
        self.assertTrue(self.wait()['timed_out'])
        agents.update_runtime(self.directory, self.launch, native_bridge='claude-mod-v1')
        self.assertTrue(self.wait()['ready'])

    def test_runner_plain_fallback_does_not_wait_for_a_native_binding(self):
        # A wait may already have read the original bridge launch request when
        # the runner selects plain after a relay preparation failure.
        agents.update_runtime(self.directory, self.launch, state='running', control_endpoint=None,
                              native_mode={'policy': 'auto', 'mode': 'plain', 'fallback_reason': 'relay unavailable'})
        self.assertTrue(self.wait()['ready'])

    def test_wait_never_follows_replacement_generation(self):
        write(self.directory / 'runtime.json', {**agents.runtime(self.directory), 'launch_id': 'replacement'})
        with self.assertRaisesRegex(ValueError, 'refusing to follow a replacement'):
            self.wait()

    def test_exit_observation_waits_for_runner_to_publish_its_failure(self):
        def exited(*args):
            # The pane snapshot can finish after the runner's final write,
            # while wait_ready still holds its earlier runtime snapshot.
            agents.update_runtime(self.directory, self.launch, state='failed',
                                  reason='Native startup failed: No such file or directory')
            return {'live': 'stopped'}
        agents.update_runtime(self.directory, self.launch, state='running')
        with patch.object(agents, 'observe', side_effect=exited):
            result = self.wait()
        self.assertFalse(result['ready'])
        self.assertEqual(result['runtime']['state'], 'failed')
        self.assertEqual(result['error'], 'Native startup failed: No such file or directory')

    def test_exit_without_runner_finalization_is_bounded_and_preserves_generation_checks(self):
        started = time.monotonic()
        with patch.object(agents, 'observe', return_value={'live': 'dormant'}):
            result = self.wait(timeout=2)
        self.assertFalse(result['ready'])
        self.assertIn('ended before becoming ready', result['error'])
        self.assertLess(time.monotonic() - started, 1, 'A dead runner must not consume the full readiness timeout')
        # A replacement during the finalization grace is never followed.
        def replaced(*args):
            write(self.directory / 'runtime.json', {**agents.runtime(self.directory), 'launch_id': 'replacement'})
            return {'live': 'stopped'}
        with patch.object(agents, 'observe', side_effect=replaced), self.assertRaisesRegex(ValueError, 'replacement'):
            self.wait()

    def test_invalid_timeout_is_rejected_before_creating_an_agent(self):
        before = list((self.root / 'state/agents').iterdir())
        for timeout in (0, -1, float('nan'), float('inf')):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(ValueError, 'finite positive'):
                agents.start(self.root, 'invalid', wait=True, wait_timeout=timeout)
        self.assertEqual(list((self.root / 'state/agents').iterdir()), before)


class WaitTerminalTest(TerminalBase):
    def test_cli_spawn_and_resume_wait_return_ready_child_and_same_identity(self):
        with patch.dict(os.environ, {'MV2_TMUX_SOCKET': self.socket}):
            first = run(parser().parse_args(['-u', str(self.root), 'spawn', '--name', 'waiter',
                '--harness', 'fake', '--wait', '--wait-timeout', '5']))
            self.assertTrue(first['ready'], first)
            self.assertTrue(first['runtime']['pid'])
            identifier = str(uuid4())
            agents.bind_session(self.root, first['id'], identifier)
            agents.kill(self.root, first['id'])
            resumed = run(parser().parse_args(['-u', str(self.root), 'resume', first['id'], '--wait']))
        self.assertTrue(resumed['ready'], resumed)
        self.assertEqual(resumed['session']['native_id'], identifier)
        self.assertNotEqual(resumed['runtime']['launch_id'], first['runtime']['launch_id'])

    def test_runner_failure_returns_failed_launch_record(self):
        executable = self.work / 'cannot-execute'
        executable.write_text('#!/nonexistent/interpreter\n')
        executable.chmod(0o755)
        write(self.root / 'harnesses/fake/harness.json', {'schema': 1, 'spawn': [str(executable)]})
        completed = subprocess.run([sys.executable, '-m', 'multiverse.cli', '-u', str(self.root),
            'spawn', '--name', 'failed', '--harness', 'fake', '--wait', '--wait-timeout', '5', '--json'],
            env={**os.environ, 'MV2_TMUX_SOCKET': self.socket}, capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 1, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertFalse(result['ok'])
        self.assertFalse(result['ready'])
        self.assertEqual(result['runtime']['state'], 'failed')
        self.assertIn('No such file', result['error'])
        # Without --json the failure is one plain sentence on stderr and nothing on stdout.
        plain = subprocess.run([sys.executable, '-m', 'multiverse.cli', '-u', str(self.root),
            'spawn', '--name', 'failed-text', '--harness', 'fake', '--wait', '--wait-timeout', '5'],
            env={**os.environ, 'MV2_TMUX_SOCKET': self.socket}, capture_output=True, text=True, timeout=10)
        self.assertEqual((plain.returncode, plain.stdout), (1, ''), plain.stderr)
        self.assertRegex(plain.stderr, r'^mv2: failed-text did not start: .*No such file')
        self.assertNotRegex(plain.stderr, r'[0-9a-f]{8}-[0-9a-f]{4}-')  # no record ID
