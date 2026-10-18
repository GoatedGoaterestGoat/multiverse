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
