"""Launch readiness is bound to one generation and never holds the bind lock."""
from concurrent.futures import ThreadPoolExecutor
import threading
import io
import sys
from contextlib import redirect_stdout, redirect_stderr
import time
from unittest.mock import patch
from uuid import uuid4

from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse import cli, harnesses
from multiverse.tools import spawn, resume
from multiverse.store import read, write
from test_multiverse import BaseTest


class WaitTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(lifecycle, 'observe', return_value={'live': 'alive'}))
        self.starting()

    def starting(self):
        """A new Codex bridge agent whose launch is starting, with a child process but no bound session."""
        self.directory = self.root / 'state/agents' / str(uuid4())
        self.launch = str(uuid4())
        self.native = str(uuid4())
        write(self.directory / 'properties.json', {'id': self.directory.name, 'name': 'waiting',
            'harness': 'codex', 'workspace': str(self.work), 'universe_id': 'universe'})
        write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'starting', 'pid': 123,
            'control_endpoint': {'transport': 'unix-websocket', 'address': '/fake/socket'}})
        write(self.directory / 'launches' / self.launch / 'request.json', {'shared_service': True, 'rpc_proxy': {'settings': {}}})
        self.adapter = harnesses.load(self.root, 'codex')

    def wait(self, timeout=.08):
        return lifecycle.wait_ready(self.root, self.directory, self.launch, self.adapter, timeout)

    def test_wait_allows_binding_but_requires_validation_before_ready(self):
        """wait never holds the launch lock the binding hook needs, and a bound but unvalidated session is not
        ready; fragile because holding the lock deadlocks binding until timeout, which only a concurrent test
        shows."""
        agents.update_runtime(self.directory, self.launch, attachment={'work_owner': 'service', 'readiness': 'binding'})
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
            agents.update_runtime(self.directory, self.launch, state='running', native_relay={'state': 'ready'})
            result = waiting.result(timeout=1)
        self.assertTrue(result['ready'])
        self.assertEqual(result['session']['native_id'], self.native)
        self.assertEqual(result['session']['state'], 'running')

    def test_readiness_signal_per_launch_mode(self):
        """Plain, extension bridge, Codex bridge, late plain fallback, error and timeout each use their own
        readiness signal; fragile because a wrong one returns --wait before the agent can take messages, so the
        first message is lost to startup or an unknown footer outlasts an explicit short wait."""
        for label, changes, ready in (
                ('unknown footer, process ready', {}, True),
                ('unknown footer, child still starting', {'startup_checked': False}, False),
                ('known refusal', {'reason': 'Native startup refused'}, False)):
            self.starting()
            with self.subTest(mode=label):
                write(self.directory / 'launches' / self.launch / 'request.json', {})
                agents.update_runtime(self.directory, self.launch, **dict(
                    state='running', control_endpoint=None, attachment={'readiness': 'process'},
                    **{'startup_checked': True, 'native_startup_observation': {'state': 'pending'}, **changes}))
                result = self.wait(.01)
                self.assertEqual(result['ready'], ready)
                if ready:
                    self.assertEqual(result['runtime']['native_startup_observation']['state'], 'unverified')
                    self.assertTrue(result['notices'])
                else:
                    self.assertEqual(agents.runtime(self.directory)['native_startup_observation']['state'], 'pending')

        self.starting()
        with self.subTest(mode='codex bridge, bound but never validated'):
            agents.bind_session(self.root, self.directory.name, self.native, self.launch)
            before = read(self.directory / 'runtime.json')
            result = self.wait()
            self.assertFalse(result['ok'])
            self.assertFalse(result['ready'])
            self.assertTrue(result['timed_out'])
            self.assertIn('launch was not stopped', result['error'])
            self.assertEqual(result['native_session_id'], self.native)
            self.assertEqual(read(self.directory / 'runtime.json'), before)

        self.starting()
        with self.subTest(mode='codex bridge, permission failure'):
            agents.update_runtime(self.directory, self.launch, error='Requested sandbox grant was not applied')
            result = self.wait()
            self.assertFalse(result['ready'])
            self.assertNotIn('timed_out', result)
            self.assertIn('sandbox grant', result['error'])

        self.starting()
        with self.subTest(mode='plain'):
            write(self.directory / 'launches' / self.launch / 'request.json', {})
            agents.update_runtime(self.directory, self.launch, state='running', pid=None, control_endpoint=None)
            self.assertTrue(self.wait()['timed_out'])
            agents.update_runtime(self.directory, self.launch, pid=123)
            self.assertTrue(self.wait()['ready'])

        self.starting()
        with self.subTest(mode='late plain fallback'):
            # A wait may already have read the original bridge launch request when
            # the runner selects plain after a relay preparation failure.
            agents.update_runtime(self.directory, self.launch, state='running', control_endpoint=None,
                                  native_mode={'policy': 'auto', 'mode': 'plain',
                                               'fallback_reason': 'relay unavailable'})
            self.assertTrue(self.wait()['ready'])

        self.starting()
        with self.subTest(mode='extension bridge'):
            spec = read(self.root / 'harnesses/claude/harness.json')
            spec['settings'] = {'inbox_bridge': True}
            write(self.root / 'harnesses/claude/harness.json', spec)
            self.adapter = harnesses.load(self.root, 'claude')
            write(self.directory / 'launches' / self.launch / 'request.json',
                  {'control_endpoint': {'transport': 'unix-lines'}})
            agents.bind_session(self.root, self.directory.name, self.native, self.launch)
            agents.update_runtime(self.directory, self.launch, state='running',
                                  control_endpoint={'transport': 'unix-lines', 'address': '/fake/inbox'})
            self.assertTrue(self.wait()['timed_out'])
            agents.update_runtime(self.directory, self.launch, native_bridge='claude-mod-v1')
            self.assertTrue(self.wait()['ready'])

    def test_wait_never_follows_a_replacement_and_reports_the_runners_failure(self):
        """wait never follows a replacement launch, reports the failure the runner publishes after the pane exits,
        and stays bounded when the runner died; pane exit races the last write, and losing the failed-outcome
        guard silently prints success and exits zero for a failed --wait (2adfa85)."""
        with self.subTest(launch='replaced'):
            write(self.directory / 'runtime.json', {**agents.runtime(self.directory), 'launch_id': 'replacement'})
            with self.assertRaisesRegex(ValueError, 'refusing to follow a replacement'):
                self.wait()

        self.starting()
        def exited(*args):
            # The pane snapshot can finish after the runner's final write,
            # while wait_ready still holds its earlier runtime snapshot.
            agents.update_runtime(self.directory, self.launch, state='failed',
                                  reason='Native startup failed: No such file or directory')
            return {'live': 'stopped'}
        agents.update_runtime(self.directory, self.launch, state='running')
        with self.subTest(launch='failed after its pane exited'), \
                patch.object(lifecycle, 'observe', side_effect=exited):
            result = self.wait()
            self.assertFalse(result['ready'])
            self.assertEqual(result['runtime']['state'], 'failed')
            self.assertEqual(result['error'], 'Native startup failed: No such file or directory')
            for tool, arguments in ((spawn, ['spawn', '--name', 'waiting']),
                                    (resume, ['resume', 'waiting'])):
                with self.subTest(command=arguments[0]), patch.object(tool, 'run', return_value=result), \
                        patch.object(sys, 'argv', ['mv2', '-u', str(self.root), *arguments, '--wait']):
                    output, errors = io.StringIO(), io.StringIO()
                    with redirect_stdout(output), redirect_stderr(errors), self.assertRaises(SystemExit) as exited:
                        cli.main()
                    self.assertEqual(exited.exception.code, 1)
                    self.assertIn(result['error'], errors.getvalue())
                    self.assertEqual(output.getvalue(), '')

        self.starting()
        with self.subTest(launch='runner died'):
            started = time.monotonic()
            with patch.object(lifecycle, 'observe', return_value={'live': 'dormant'}):
                result = self.wait(timeout=2)
            self.assertFalse(result['ready'])
            self.assertIn('ended before becoming ready', result['error'])
            self.assertLess(time.monotonic() - started, 1, 'A dead runner must not consume the full readiness timeout')
            # A replacement during the finalization grace is never followed.
            def replaced(*args):
                write(self.directory / 'runtime.json', {**agents.runtime(self.directory), 'launch_id': 'replacement'})
                return {'live': 'stopped'}
            with patch.object(lifecycle, 'observe', side_effect=replaced), \
                    self.assertRaisesRegex(ValueError, 'replacement'):
                self.wait()
