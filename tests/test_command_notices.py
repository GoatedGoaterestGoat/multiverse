"""Only the selected launch candidate contributes presentation notices."""
from dataclasses import replace
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

from multiverse.runtime import lifecycle, runner, rpc
from multiverse.store import read, write
from multiverse.universe import agents
from multiverse.tools import inspect, send
from test_native_backends import NativeBackendTest


class CommandNoticeTest(NativeBackendTest):
    def test_startup_retry_is_once_after_exit_and_before_any_readiness_or_delivery(self):
        """A retry cannot repeat accepted work or mistake native errors for runner notices; shared-pane output and generation races otherwise lose retries or duplicate turns."""
        launch, proxy = self.fallback_fixture(False, False)
        original = read(launch / 'request.json')
        original['fallback']['startup_retry'] = {'argv': ['plain', '--retry'], 'notices': ['Retry selected.']}
        write(launch / 'request.json', original)
        from multiverse.harnesses.codex import CodexAdapter
        diagnostic = json.loads((Path(__file__).parent / 'fixtures/codex/add-dir-readonly.json').read_text())[0]['text']
        matcher = CodexAdapter.configured('codex', {'inbox_bridge': False}).startup_retryable
        saved = 'Saved native storage retained.'
        for label, notices, capture, starts in (
                ('only native output', [], diagnostic, 2),
                ('retained home notice', [saved], saved + '\n' + diagnostic, 2),
                ('multiple printed notices', [saved, 'Plugin note.'], saved + '\nPlugin note.\n' + diagnostic, 2),
                ('historical request workspace', [], diagnostic, 2),
                ('wrapped notice', [saved], 'Saved native sto\nrage retained.\n' + diagnostic, 2),
                ('native screen cleared the notice', [saved], diagnostic, 2),
                ('unprinted discarded notice', [], 'Discarded bridge note.\n' + diagnostic, 1),
                ('changed notice', [saved], 'Saved different storage retained.\n' + diagnostic, 1),
                ('unrelated prefix', [saved], 'Other failure\n' + saved + '\n' + diagnostic, 1),
                ('unrelated suffix', [saved], saved + '\n' + diagnostic + '\nOther failure', 1),
                ('known prelude at end is still native output', [saved], saved + '\n' + diagnostic + '\n' + saved, 1),
                ('known notice joined to native text', [saved], saved + diagnostic, 1)):
            with self.subTest(output=label):
                request = {**original, 'notices': ['Discarded bridge note.'], 'resume_notices': [],
                           'fallback': {**original['fallback'], 'notices': notices}}
                if label == 'historical request workspace':
                    request.pop('workspace', None)
                write(launch / 'request.json', request)
                write(self.directory / 'runtime.json', self.facts)
                write(self.directory / 'properties.json', self.properties)
                first, second = Mock(pid=123), Mock(pid=124)
                self.backend.run.return_value = capture
                for child in (first, second):
                    child.poll.return_value = child.wait.return_value = 1
                calls = []
                def start(argv, **kwargs):
                    calls.append(argv)
                    if len(calls) == 2:
                        self.assertEqual(read(launch / 'request.json')['notices'], ['Retry selected.'])
                        self.assertEqual(agents.runtime(self.directory)['launch_notices'], ['Retry selected.'])
                    return first if len(calls) == 1 else second
                def check(snapshot):
                    self.assertEqual(snapshot['workspace'], self.properties['workspace'])
                    return matcher(snapshot)
                with patch.object(runner.sys, 'argv', ['runner', str(launch)]), \
                        patch.object(rpc, 'LaunchProxy', return_value=proxy), \
                        patch.object(runner, 'process_started', return_value=None), \
                        patch.object(runner.subprocess, 'Popen', side_effect=start), \
                        patch.object(self.adapter, 'startup_retryable', side_effect=check, create=True), \
                        patch('multiverse.terminal.tmux.owned', return_value=(self.backend, '%1')), \
                        patch.object(runner, 'launch_diagnostic', return_value={}):
                    self.assertEqual(runner.main(), 1)
                self.assertEqual(calls, [['plain'], ['plain', '--retry']][:starts])
                if starts == 2:
                    self.assertIsNone(read(launch / 'request.json')['startup_retry'])
                    self.assertEqual(agents.runtime(self.directory)['startup_retry']['exit_code'], 1)

        with self.subTest('adapter load failure cannot leave an observer pending or start its child'):
            from multiverse import harnesses
            plain = {**read(launch / 'request.json'), 'observe_refusal': True, 'startup_retry': None}
            write(launch / 'request.json', plain)
            with patch.object(runner.sys, 'argv', ['runner', str(launch)]), \
                    patch.object(harnesses, 'load', side_effect=ValueError('damaged harness')), \
                    patch.object(runner, 'process_started', return_value=None), \
                    patch.object(runner.subprocess, 'Popen') as child, \
                    patch.object(runner, 'launch_diagnostic', return_value={}):
                self.assertEqual(runner.main(), 1)
            child.assert_not_called()
            self.assertNotEqual((agents.runtime(self.directory).get('native_startup_observation') or {}).get('state'),
                                'pending')

        request = {**read(launch / 'request.json'), 'startup_retry': original['fallback']['startup_retry']}
        baseline = {**agents.runtime(self.directory), 'state': 'running', 'pid': 123,
                    'startup_checked': False, 'startup_retry': None, 'exit_code': None,
                    'native_relay': None, 'startup_delivery': None}
        for observed, elapsed, exited in (('pending', 3, True), ('started', 3, False), ('pending', 55, False)):
            with self.subTest(screen=observed, elapsed=elapsed):
                write(self.directory / 'runtime.json', {**baseline,
                    'native_startup_observation': {'state': observed}})
                child = Mock(pid=123)
                child.poll.return_value = None
                child.wait.side_effect = [subprocess.TimeoutExpired('plain', .1), 1]
                with patch.object(runner.time, 'monotonic', side_effect=[0, elapsed]):
                    self.assertEqual(runner.wait_startup_candidate(self.directory, launch.name, child, 0), not exited)
                self.assertEqual(child.wait.call_count, 2 if exited else 1)
                if exited:
                    self.assertTrue(agents.runtime(self.directory)['startup_checked'])
        with self.subTest('slow native setup can fail before its pending screen ever starts'):
            write(launch / 'request.json', request)
            write(self.directory / 'runtime.json', {**baseline, 'startup_checked': True,
                'native_startup_observation': {'state': 'pending'}})
            child = Mock(return_value=second)
            with patch.object(self.adapter, 'startup_retryable', return_value=True, create=True), \
                    patch('multiverse.terminal.tmux.owned', return_value=(self.backend, '%1')):
                self.assertIsNotNone(runner.retry_startup(launch, request, self.adapter, first,
                    runner.STARTUP_SECONDS + 1, child, native_id=self.properties.get('native_session_id')))
            child.assert_called_once()
        with self.subTest('unsupported screen keeps the ordinary short retry window'):
            write(launch / 'request.json', request)
            write(self.directory / 'runtime.json', {**baseline,
                'native_startup_observation': {'state': 'unsupported'}})
            child = Mock(return_value=second)
            with patch.object(self.adapter, 'startup_retryable', return_value=True, create=True), \
                    patch('multiverse.terminal.tmux.owned', return_value=(self.backend, '%1')):
                self.assertIsNotNone(runner.retry_startup(launch, request, self.adapter, first, .1,
                    child, native_id=self.properties.get('native_session_id')))
            child.assert_called_once()
        with self.subTest('a wait settling the screen during diagnostic capture wins over retry'):
            write(launch / 'request.json', request)
            write(self.directory / 'runtime.json', {**baseline, 'startup_checked': True,
                'native_startup_observation': {'state': 'pending'}})
            def settles(_):
                agents.update_runtime(self.directory, launch.name,
                                      native_startup_observation={'state': 'unverified'})
                return True
            with patch.object(self.adapter, 'startup_retryable', side_effect=settles, create=True), \
                    patch('multiverse.terminal.tmux.owned', return_value=(self.backend, '%1')):
                child = Mock()
                self.assertIsNone(runner.retry_startup(launch, request, self.adapter, first, 3, child,
                                                      native_id=self.properties.get('native_session_id')))
                child.assert_not_called()
        for label, changes, code, elapsed, matched in (
                ('still live', {}, None, 0, True), ('clean exit', {}, 0, 0, True),
                ('after startup window', {}, 1, runner.STARTUP_SECONDS + 1, True),
                ('ready', {'startup_checked': True}, 1, 0, True),
                ('ending', {'ending_at': 'now'}, 1, 0, True),
                ('replacement', {'launch_id': 'replacement'}, 1, 0, True),
                ('different child', {'pid': 999}, 1, 0, True),
                ('already retried', {'startup_retry': {'exit_code': 1}}, 1, 0, True),
                ('submitted', {'startup_delivery': 'ready'}, 1, 0, True),
                ('bound', {'native_relay': {'state': 'ready'}}, 1, 0, True),
                ('unrelated failure', {}, 1, 0, False)):
            with self.subTest(label=label):
                write(launch / 'request.json', request)
                write(self.directory / 'runtime.json', {**baseline, **changes})
                first.poll.return_value = code
                before = (self.directory / 'runtime.json').read_bytes()
                with patch.object(self.adapter, 'startup_retryable', return_value=matched, create=True), \
                        patch('multiverse.terminal.tmux.owned', return_value=(self.backend, '%1')):
                    child = Mock(side_effect=AssertionError('refused retry must not start a child'))
                    self.assertIsNone(runner.retry_startup(launch, request, self.adapter, first, elapsed, child,
                                                          native_id=self.properties.get('native_session_id')))
                self.assertEqual((self.directory / 'runtime.json').read_bytes(), before)
        with self.subTest('a native binding during the first attempt rules out a retry'):
            write(self.directory / 'runtime.json', baseline)
            write(self.directory / 'properties.json', {**self.properties, 'native_session_id': 'new binding'})
            with patch.object(self.adapter, 'startup_retryable', return_value=True, create=True):
                self.assertIsNone(runner.retry_startup(launch, request, self.adapter, first, 0,
                    Mock(side_effect=AssertionError('a bound conversation must not start twice')),
                    native_id=self.properties.get('native_session_id')))

    def test_selected_command_notices_reach_every_launch_result_once(self):
        """The selected command's notices reach spawn, resume and message-wake results once, in text and JSON, a
        discarded fallback's notes never do, and a bad optional note never changes the launch; kept because notices
        once went missing from some results (c6153d5)."""
        original = self.adapter.command
        with self.subTest('a late fallback replaces the discarded notes before its child starts'):
            launch, proxy = self.fallback_fixture(False, False)
            request = read(launch / 'request.json')
            request.update(notices=['Saved home kept.', 'Bridge only.'], resume_notices=['Saved home kept.'])
            request['fallback']['notices'] = ['Plain only.\nSafe text.']
            write(launch / 'request.json', request)
            process = Mock(pid=123)
            process.poll.return_value = process.wait.return_value = 0
            selected = ['Saved home kept.', 'Plain only. Safe text.']
            def start(*args, **kwargs):
                self.assertEqual(agents.runtime(self.directory)['launch_notices'], selected)
                self.assertEqual(read(launch / 'request.json')['notices'], selected)
                return process
            with patch.object(runner.sys, 'argv', ['runner', str(launch)]), \
                    patch.object(rpc, 'LaunchProxy', return_value=proxy), \
                    patch.object(runner, 'process_started', return_value=None), \
                    patch.object(runner.subprocess, 'Popen', side_effect=start), \
                    patch('multiverse.terminal.tmux.owned', return_value=(self.backend, '%1')), \
                    patch.object(runner, 'launch_diagnostic', return_value={}):
                self.assertEqual(runner.main(), 0)
            self.assertEqual(lifecycle.launch_result(self.root, self.directory)['notices'], selected)
            self.assertEqual(inspect.cli_run(self.root, agent=self.result['id'], json=True)['notices'], selected)
        with self.subTest('spawn and resume keep the selected command and storage notes once'):
            def command(context):
                notices = ('Selected command note.\nOne line.', 'Selected command note.\nOne line.')
                return replace(original(context), notices=notices)
            with patch.object(self.adapter, 'command', side_effect=command):
                spawned = lifecycle.start(self.root, name='noted-spawn', harness='service-test')
                with patch.object(self.adapter, 'resume_notice', return_value='Saved home kept.', create=True):
                    resumed = lifecycle.start(self.root, resume=self.result['id'], socket_path='/fake')
            self.assertEqual(spawned['notices'], ['Selected command note. One line.'])
            self.assertEqual(resumed['notices'], ['Saved home kept.', 'Selected command note. One line.'])
            directory = agents.directory(self.root, spawned['id'])
            request = read(directory / 'launches' / spawned['runtime']['launch_id'] / 'request.json')
            self.assertEqual(request['notices'], spawned['notices'])
            self.assertEqual(spawned['runtime']['launch_notices'], spawned['notices'])
            self.assertEqual(lifecycle.launch_result(self.root, directory)['notices'], spawned['notices'])
        with self.subTest("a discarded fallback's notes are never shown for a selected bridge"):
            def command(context):
                plain = replace(original(context), notices=('Plain only.',))
                return replace(original(context), notices=('Bridge only.',), fallback=plain)
            with patch.object(self.adapter, 'command', side_effect=command):
                result = lifecycle.start(self.root, name='bridge-noted', harness='service-test')
            self.assertEqual(result['notices'], ['Bridge only.'])
            directory = agents.directory(self.root, result['id'])
            request = read(directory / 'launches' / result['runtime']['launch_id'] / 'request.json')
            self.assertEqual(request['fallback']['notices'], ['Plain only.'])
            self.assertEqual(request['notices'], ['Bridge only.'])
        with self.subTest('a message wake shows the selected notice in text and JSON'):
            def command(context):
                return replace(original(context), notices=('Selected wake permissions note.',))
            with patch.object(self.adapter, 'command', side_effect=command), \
                    patch('multiverse.runtime.native.observe', return_value={'live': 'dormant'}):
                out = io.StringIO()
                with redirect_stdout(out):
                    send.cli_run(self.root, agent=self.result['id'], text='wake')
                self.assertIn('Selected wake permissions note.', out.getvalue().splitlines())
                result = send.cli_run(self.root, agent=self.result['id'], text='wake again', json=True)
            self.assertEqual(result['notices'], ['Selected wake permissions note.'])
        for value in (None, 5, 'not a tuple', {'bad': 'mapping'}, (None, 5, '')):
            with self.subTest(bad_notes=value), patch.object(self.adapter, 'command',
                    side_effect=lambda context: replace(original(context), notices=value)):
                result = lifecycle.start(self.root, resume=self.result['id'], socket_path='/fake')
                self.assertEqual(result.get('notices', []), [])
                self.assertEqual(result['native_session_id'], self.properties['native_session_id'])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(CommandNoticeTest(name) for name in CommandNoticeTest.__dict__ if name.startswith('test_'))
