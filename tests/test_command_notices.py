"""Only the selected launch candidate contributes presentation notices."""
from dataclasses import replace
from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import Mock, patch

from multiverse.runtime import lifecycle, runner, rpc
from multiverse.store import read, write
from multiverse.universe import agents
from multiverse.tools import inspect, send
from test_native_backends import NativeBackendTest


class CommandNoticeTest(NativeBackendTest):
    def test_spawn_and_resume_results_keep_selected_command_and_storage_notes_once(self):
        original = self.adapter.command
        def command(context):
            return replace(original(context), notices=('Selected command note.\nOne line.', 'Selected command note.\nOne line.'))
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

    def test_discarded_fallback_notes_are_never_shown_for_a_selected_bridge(self):
        original = self.adapter.command
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

    def test_late_fallback_replaces_discarded_notes_before_child_start(self):
        launch, proxy = self.fallback_fixture(False, False)
        request = read(launch / 'request.json')
        request.update(notices=['Saved home kept.', 'Bridge only.'], resume_notices=['Saved home kept.'])
        request['fallback']['notices'] = ['Plain only.\nSafe text.']
        write(launch / 'request.json', request)
        process = Mock(pid=123)
        process.poll.return_value = process.wait.return_value = 0
        def start(*args, **kwargs):
            self.assertEqual(agents.runtime(self.directory)['launch_notices'], ['Saved home kept.', 'Plain only. Safe text.'])
            self.assertEqual(read(launch / 'request.json')['notices'], ['Saved home kept.', 'Plain only. Safe text.'])
            return process
        with patch.object(runner.sys, 'argv', ['runner', str(launch)]), \
                patch.object(rpc, 'LaunchProxy', return_value=proxy), \
                patch.object(runner, 'process_started', return_value=None), \
                patch.object(runner.subprocess, 'Popen', side_effect=start), \
                patch('multiverse.terminal.tmux.owned', return_value=(self.backend, '%1')), \
                patch.object(runner, 'launch_diagnostic', return_value={}):
            self.assertEqual(runner.main(), 0)
        self.assertEqual(lifecycle.launch_result(self.root, self.directory)['notices'], ['Saved home kept.', 'Plain only. Safe text.'])
        self.assertEqual(inspect.cli_run(self.root, agent=self.result['id'], json=True)['notices'],
                         ['Saved home kept.', 'Plain only. Safe text.'])

    def test_message_wake_shows_selected_command_notice_in_human_and_json_output(self):
        original = self.adapter.command
        def command(context):
            return replace(original(context), notices=('Selected wake permissions note.',))
        with patch.object(self.adapter, 'command', side_effect=command), \
                patch('multiverse.runtime.native.observe', return_value={'live': 'dormant'}):
            out = io.StringIO()
            with redirect_stdout(out):
                send.cli_run(self.root, agent=self.result['id'], text='wake')
            self.assertEqual(out.getvalue().splitlines()[-1], 'Selected wake permissions note.')
            result = send.cli_run(self.root, agent=self.result['id'], text='wake again', json=True)
        self.assertEqual(result['notices'], ['Selected wake permissions note.'])

    def test_bad_optional_notes_never_change_the_launch(self):
        original = self.adapter.command
        for value in (None, 5, 'not a tuple', {'bad': 'mapping'}, (None, 5, '')):
            with self.subTest(value=value), patch.object(self.adapter, 'command',
                    side_effect=lambda context: replace(original(context), notices=value)):
                result = lifecycle.start(self.root, resume=self.result['id'], socket_path='/fake')
                self.assertEqual(result.get('notices', []), [])
                self.assertEqual(result['native_session_id'], self.properties['native_session_id'])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(CommandNoticeTest(name) for name in CommandNoticeTest.__dict__ if name.startswith('test_'))
