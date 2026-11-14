"""Hot-path shortcuts must save waits without lending an optimistic read to authority."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from multiverse import plugins, store
from multiverse.plugins import loader, harness as plugin_harness
from multiverse.runtime import attachments, recovery, storage_adoption
from multiverse.universe import focus, mailbox
from multiverse.tools import listen
from multiverse.harnesses.types import NativeResult
from test_multiverse import BaseTest
from test_network_records import missing_once


class RetryHotPathsTest(BaseTest):
    @contextmanager
    def clock(self):
        elapsed, sleeps = [0.0], []
        def sleep(delay):
            elapsed[0] += delay
            sleeps.append(delay)
        with patch.object(store, 'time', SimpleNamespace(monotonic=lambda: elapsed[0], sleep=sleep)):
            yield sleeps

    def test_new_connection_ids_need_no_previous_record_but_close_reads_retry(self):
        """The first publication of a fresh RPC UUID must not wait for absent history, while a later close must retain its original metadata through a replacement gap."""
        launch = self.work / store.uid()
        (launch / 'roles').mkdir(parents=True)
        record = attachments.connection_logger(launch, started='fixture')
        value = {'id': store.uid(), 'role': 'control RPC', 'endpoint': '/fixture.sock', 'state': 'opening'}
        with self.clock() as sleeps:
            record(value)
            self.assertEqual(sleeps, [])
        path = launch / 'roles' / ('connection-' + value['id'] + '.json')
        at = store.read(path)['at']
        with self.clock() as sleeps, missing_once(path, probe='read_text', failures=2):
            record({**value, 'state': 'closed'})
            self.assertEqual(sleeps, [store.MISSING_DELAY] * 2)
        self.assertEqual((store.read(path)['at'], store.read(path)['state']), (at, 'closed'))

    def test_cleanup_poll_waits_without_repeating_absence_windows_but_checks_before_return(self):
        """A cleanup poll must not wait twice, and a missed cleanup hint must never admit a replacement or a false timeout."""
        directory = self.work / 'agent'
        launch = directory / 'launches' / store.uid()
        launch.mkdir(parents=True)
        path = launch / 'cleanup.json'
        facts = {'launch_id': launch.name, 'runner_pid': 123}
        with self.clock() as sleeps, patch.object(attachments.agents, 'runtime', return_value=facts), \
                patch.object(attachments, '_observation', return_value=(False, None)), \
                patch.object(attachments, 'process_command', return_value='fixture'), \
                patch.object(attachments, 'time', SimpleNamespace(monotonic=lambda: 0,
                    sleep=Mock(side_effect=[None, None, StopIteration('three polls')]))):
            with self.assertRaises(StopIteration):
                attachments.wait(directory, launch.name, 1)
            self.assertEqual(sleeps, [])
        # No process and no journal is a valid older launch, but only after the normal absence window.
        facts.pop('runner_pid')
        with self.clock() as sleeps, patch.object(attachments.agents, 'runtime', return_value=facts):
            attachments.wait(directory, launch.name, 0)
            self.assertEqual(sleeps, [store.MISSING_DELAY] * (store.MISSING_ATTEMPTS - 1))
        # The optimistic read misses an active record; the final read must recover it and refuse.
        store.write(path, {'schema': 1, 'launch_id': launch.name, 'state': 'active', 'pid': 123})
        with self.clock() as sleeps, missing_once(path, probe='read_text', failures=2), \
                patch.object(attachments.agents, 'runtime', return_value=facts), \
                patch.object(attachments, '_observation', return_value=(False, None)), \
                patch.object(attachments, 'process_command', return_value='fixture'):
            with self.assertRaisesRegex(RuntimeError, 'Attachment cleanup is not complete'):
                attachments.wait(directory, launch.name, 0)
            self.assertEqual(sleeps, [store.MISSING_DELAY])
        # Conversely a missed complete record must not become a spurious timeout from old runner metadata.
        store.write(path, {'schema': 1, 'launch_id': launch.name, 'state': 'complete'})
        facts['runner_pid'] = 123
        with self.clock(), missing_once(path, probe='read_text', failures=2), \
                patch.object(attachments.agents, 'runtime', return_value=facts), \
                patch.object(attachments, '_observation', return_value=(False, None)), \
                patch.object(attachments, 'process_command', return_value='fixture'):
            attachments.wait(directory, launch.name, 0)
        roles = launch / 'roles'
        roles.mkdir()
        for _ in range(100):
            store.write(roles / (store.uid() + '.json'),
                        {'schema': 1, 'launch_id': launch.name, 'state': 'closed'})
        for timeout in (0, 1):
            with self.subTest(timeout=timeout), patch.object(attachments.agents, 'runtime', return_value=facts), \
                    patch.object(attachments, '_read_record', wraps=attachments._read_record) as read:
                attachments.wait(directory, launch.name, timeout)
                self.assertEqual(sum(call.args[0].parent == roles for call in read.call_args_list), 100)
        # A tentative success still needs the full role scan: an unverifiable live role must refuse.
        store.write(roles / (store.uid() + '.json'),
                    {'schema': 1, 'launch_id': launch.name, 'state': 'active', 'pid': 123})
        with patch.object(attachments.agents, 'runtime', return_value=facts), \
                patch.object(attachments, '_observation', return_value=(False, None)), \
                patch.object(attachments, 'process_command', return_value='fixture'):
            with self.assertRaisesRegex(RuntimeError, 'Attachment cleanup is not complete'):
                attachments.wait(directory, launch.name, 0)

    def test_focus_poll_is_optimistic_but_the_authority_read_still_retries(self):
        """Quiet polling must not sleep on unused focus records or turn a missed hint into permission to print through a focus."""
        directory = self.work / 'agent'
        directory.mkdir()
        (directory / '.launch.lock').touch()
        facts = {'launch_id': 'fixture'}
        with self.clock() as sleeps, patch.object(listen, '_current', return_value=facts), \
                patch.object(listen, 'sender_actor'), patch.object(listen, '_processes'), \
                patch.object(mailbox, 'inbox', return_value={'messages': []}), \
                patch.object(listen, 'time', SimpleNamespace(monotonic=lambda: 0,
                    sleep=Mock(side_effect=StopIteration('one quiet poll')))):
            with self.assertRaises(StopIteration):
                listen._wait(directory, 'fixture', None, None, Mock(), Mock())
            self.assertEqual(sleeps, [store.MISSING_DELAY] * (store.MISSING_ATTEMPTS - 1))
        with self.clock() as sleeps:
            for _ in range(20):
                self.assertIsNone(focus.hint(directory))
            self.assertEqual(sleeps, [])
        active = {'id': store.uid(), 'since': store.now(), 'by': 'user', 'until': None}
        store.write(directory / focus.FILE, active)
        with self.clock() as sleeps, missing_once(directory / focus.FILE, failures=2):
            self.assertIsNone(focus.hint(directory))
            self.assertEqual(sleeps, [])
            with focus.locked(directory):
                self.assertEqual(focus.current(directory), active)
            self.assertEqual(sleeps, [store.MISSING_DELAY])

    def test_no_bootstrap_extension_needs_no_plugin_switch_read(self):
        """A launch with no bootstrap contributor must not load plugin switches, while declaring the extension restores the ordinary loader gate."""
        with patch.object(loader, 'manifest_records', return_value=[SimpleNamespace(extends=())]), \
                patch.object(loader, 'active', side_effect=AssertionError('Unrelated plugins need no bootstrap load')):
            self.assertEqual(plugins.bootstrap_lines(self.root), [])
        loaded = SimpleNamespace(registrations=SimpleNamespace(bootstrap=['fixture line']))
        with patch.object(loader, 'manifest_records', return_value=[SimpleNamespace(extends=('bootstrap',))]), \
                patch.object(loader, 'active', return_value=[loaded]) as active:
            self.assertEqual(plugins.bootstrap_lines(self.root), ['fixture line'])
            active.assert_called_once_with(self.root)

    def test_retained_launch_requests_share_absence_rounds_without_following_links(self):
        """Failed launch folders must not each add an absence window, but a recovered request or a symlink must still change storage admission."""
        directory = self.work / 'agent'
        launches = [directory / 'launches' / store.uid() for _ in range(20)]
        for launch in launches:
            launch.mkdir(parents=True)
        with self.clock() as sleeps:
            self.assertEqual(storage_adoption._launch_requests(directory), [])
            self.assertEqual(sleeps, [store.MISSING_DELAY] * (store.MISSING_ATTEMPTS - 1))
        request = launches[0] / 'request.json'
        value = {'env': {'HOME': str(self.work)}}
        store.write(request, value)
        with self.clock(), missing_once(request, probe='lstat', failures=2):
            self.assertEqual(storage_adoption._launch_requests(directory), [(request, value)])
        request.unlink()
        with self.clock():
            self.assertEqual(storage_adoption._launch_requests(directory), [])
        request.symlink_to('never-created-target')
        with self.assertRaisesRegex(ValueError, 'not a regular request'):
            storage_adoption._launch_requests(directory)

    def test_unfit_extensions_do_not_read_grants_but_fitting_ones_still_do(self):
        """An extension that cannot run needs no grant read, but a fitting extension must never bypass the current access ceiling."""
        info = plugin_harness.LaunchInfo(self.root, 'fixture', 'fixture', 'spawn', 'claude', 'claude',
                                        version=(2, 1, 322), bridge=False)
        with patch.object(loader, 'access_level', side_effect=AssertionError('No fitting extension')):
            plan = plugin_harness.launch_plan(info)
        self.assertIsNone(plan.error)
        self.assertEqual(plan.claude_plugin_dirs, [])
        self.assertIn('needs mv2', plan.plugins['typeahead']['reason'])
        from dataclasses import replace
        with patch.object(loader, 'access_level', return_value=None) as level:
            plan = plugin_harness.launch_plan(replace(info, bridge=True))
        self.assertEqual(level.call_count, 1)
        self.assertEqual(plan.claude_plugin_dirs, [])
        self.assertIn('access', plan.plugins['typeahead']['reason'])

    def test_monitor_publishes_only_after_one_fresh_post_rpc_delivery_read(self):
        """Removing a redundant monitor preflight must not publish idle proof when input arrives before or during its read-only RPC."""
        for arrival in ('none', 'before', 'during'):
            with self.subTest(arrival=arrival):
                directory = self.root / 'state/agents' / store.uid()
                native_id, launch_id = store.uid(), store.uid()
                properties = {'id': directory.name, 'name': 'monitor', 'harness': 'fixture',
                              'workspace': str(self.work), 'native_session_id': native_id}
                facts = {'launch_id': launch_id, 'state': 'running', 'status': 'idle', 'startup_checked': True,
                         'native_relay': {'state': 'ready'}}
                store.write(directory / 'properties.json', properties)
                store.write(directory / 'runtime.json', facts)
                (directory / '.launch.lock').touch()
                def append():
                    mailbox.append_message(directory, 'fixture input', {}, delivery='follow-up')
                if arrival == 'before':
                    append()
                def check(*args):
                    if arrival == 'during':
                        append()
                    return NativeResult('ok')
                monitor = recovery.RecoveryMonitor(directory, launch_id, self.root)
                monitor.stopped = Mock()
                monitor.stopped.wait.side_effect = [False, False, True]
                with patch.object(recovery, 'recovery_unavailable', return_value=None), \
                        patch.object(recovery, 'check', side_effect=check) as observed, \
                        patch.object(recovery, 'in_flight', wraps=recovery.in_flight) as checked:
                    monitor._run()
                self.assertEqual(observed.call_count, 1)
                self.assertEqual(checked.call_count, 1)
                self.assertEqual(store.read(directory / 'runtime.json')['recovery_snapshot']['safe'], arrival == 'none')
