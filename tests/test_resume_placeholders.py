"""Custom recipes receive this resume's prompt, never the saved original task."""
import os
from pathlib import Path
import sys
from contextlib import nullcontext
from unittest.mock import patch
from multiverse import harnesses
from multiverse.runtime import lifecycle
from multiverse.terminal import recovery
from multiverse.store import read, write
from multiverse.tools import deliver, send
from multiverse.universe import mailbox
from test_multiverse import TerminalBase, until


class ResumePlaceholderTest(TerminalBase):
    def test_wake_values_and_task_file_both_contain_only_wake_prompt(self):
        """On resume a custom recipe's {task} and {task_file} carry only the wake prompt, or nothing without one, while
        properties keep the original task; a break goes unnoticed because a resumed agent redoes its whole task, which
        looks like work (797bfec, qa Q3)."""
        folder = self.root / 'harnesses' / 'recorder'
        folder.mkdir()
        output = self.work / 'seen-task'
        file_output = self.work / 'seen-file'
        # Each launch writes both values whole, then renames them into place, so a file that exists is complete.
        command = ['/bin/sh', '-c', 'printf "%s" "$1" > "$3.new"; cat "$2" > "$4.new"; mv "$3.new" "$3"; '
                   'mv "$4.new" "$4"; exec sleep 600', 'sh', '{task}', '{task_file}', str(output), str(file_output)]
        write(folder / 'harness.json', {'schema': 1, 'spawn': command, 'resume': command})
        adapter = harnesses.load(self.root, 'recorder')
        # This declared adapter consumes its verified interactive prompt through
        # its recipe; unlike a built-in it needs no additional positional args.
        adapter.resume_prompt_args = ()

        def launched(**options):
            for path in (output, file_output):
                path.unlink(missing_ok=True)
            with patch.object(harnesses, 'load', return_value=adapter):
                result = lifecycle.start(self.root, socket_path=self.socket, wait=True, **options)
            self.assertTrue(result['ready'], result)
            until(lambda: output.exists() and file_output.exists())
            return result, output.read_text(), file_output.read_text()

        first, task, task_file = launched(name='recorded', task='ORIGINAL', harness='recorder')
        self.assertEqual((task, task_file), ('ORIGINAL', 'ORIGINAL'))
        for prompt, expected in ((None, ''), ('ONLY-WAKE', 'ONLY-WAKE')):
            with self.subTest(resume_prompt=prompt):
                lifecycle.kill(self.root, first['id'])
                resumed, task, task_file = launched(resume=first['id'], resume_prompt=prompt)
                self.assertEqual((task, task_file), (expected, expected))
                self.assertEqual(resumed['task'], 'ORIGINAL')
                self.assertEqual(read(self.root / 'state' / 'agents' / first['id'] / 'properties.json')['task'],
                                 'ORIGINAL')

        # A wake must survive a swept legacy default without silently moving a
        # custom, isolated server onto the operator's default server.
        self.kill_server()
        for case in ('legacy swept', 'legacy retry', 'legacy live', 'legacy recover',
                     'custom present', 'custom swept', 'explicit resume', 'operator custom'):
            with self.subTest(socket_selection=case):
                run_root = self.work / 'run'
                legacy_root = self.work / 'old-tmp'
                old = (legacy_root / f'tmux-{os.getuid()}' / 'multiverse-v2' if case.startswith('legacy')
                       else self.work / 'custom' / 'tmux.sock')
                old.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                self.socket, self.backend = str(old), type(self.backend)(str(old))
                environment = {key: value for key, value in os.environ.items() if not key.startswith('MV2_')}
                environment.update(MV2_RUN_DIR=str(run_root), TMUX_TMPDIR=str(legacy_root), MV2_TMUX_SOCKET=str(old))
                with patch.dict(os.environ, environment, clear=True), patch.object(harnesses, 'load', return_value=adapter):
                    first, _, _ = launched(name='socket-' + case.replace(' ', '-'), task='ORIGINAL', harness='recorder')
                    directory = self.root / 'state/agents' / first['id']
                    pid = self.backend.run('display-message', '-p', '#{pid}')
                    live = case in ('legacy live', 'legacy recover')
                    if live:
                        self.backend.create('keeper', self.work, [sys.executable, '-c', 'import time; time.sleep(60)'])
                        lifecycle.kill(self.root, first['id'])
                    else:
                        self.kill_server()
                    if case not in ('legacy live', 'custom present', 'operator custom'):
                        old.unlink(missing_ok=True)
                        old.parent.rmdir()
                    # A main-era launch has no saved run_directory, but its
                    # request still exported the old server socket.
                    if case.startswith('legacy'):
                        facts = read(directory / 'runtime.json')
                        facts.pop('run_directory', None)
                        write(directory / 'runtime.json', facts)
                    for path in (output, file_output):
                        path.unlink(missing_ok=True)
                    os.environ.pop('MV2_TMUX_SOCKET')
                    expected = str(old) if live or case in ('custom present', 'operator custom') else str(run_root / 'tmux.sock')
                    if case == 'operator custom':
                        os.environ['MV2_TMUX_SOCKET'] = str(old)
                    try:
                        if case in ('explicit resume', 'operator custom'):
                            result = lifecycle.start(self.root, resume=first['id'], wait=True)
                        elif case == 'legacy retry':
                            with patch.object(lifecycle, 'start', side_effect=ValueError('retry fixture')):
                                failed = send.run(self.root, agent=first['id'], text='WAKE:' + case)
                            self.assertFalse(failed['ok'])
                            result = deliver.run(self.root, agent=first['id'], retry=True)
                        else:
                            guard = (patch.object(lifecycle.Tmux, 'create', side_effect=AssertionError('A refused wake created a server'))
                                     if case == 'custom swept' else nullcontext())
                            with guard:
                                result = send.run(self.root, agent=first['id'], text='WAKE:' + case)
                        if case == 'custom swept':
                            self.assertFalse(result['ok'], result)
                            self.assertIn('recorded tmux socket folder', result['native_delivery']['reason'])
                            self.assertIn('MV2_TMUX_SOCKET', result['native_delivery']['reason'])
                            self.assertFalse(old.parent.exists())
                            self.assertEqual(type(self.backend)(str(run_root / 'tmux.sock')).snapshot(), ({}, None))
                        else:
                            self.assertTrue(result['ok'] if 'ok' in result else result['ready'], result)
                            until(lambda: output.exists() and file_output.exists())
                            facts = read(directory / 'runtime.json')
                            self.assertEqual(facts['tmux']['socket'], expected)
                            self.assertEqual(read(directory / 'launches' / facts['launch_id'] / 'request.json')['env']['MV2_TMUX_SOCKET'], expected)
                            self.assertEqual(facts['run_directory'], str(run_root))
                            if live:
                                self.assertEqual(self.backend.run('display-message', '-p', '#{pid}'), pid)
                            if case not in ('explicit resume', 'operator custom'):
                                self.assertIn('WAKE:' + case, output.read_text())
                                self.assertEqual(output.read_text(), file_output.read_text())
                    finally:
                        # End both private candidates before the fixture waits
                        # for runners, even when a mutation chose the wrong one.
                        for candidate in {expected, str(old), str(run_root / 'tmux.sock')}:
                            try:
                                type(self.backend)(candidate).run('kill-server')
                            except (OSError, RuntimeError):
                                pass
                        self.backend = type(self.backend)(str(old))
                        self.kill_server()

        with self.subTest('an unknown recorded server cannot select another socket'), \
                patch.object(recovery, 'observe', side_effect=recovery.Unreachable('Recorded server is unknown')), \
                patch.object(lifecycle.Tmux, 'create', side_effect=AssertionError('Created an unverified replacement')):
            with self.assertRaisesRegex(recovery.Unreachable, 'Recorded server is unknown'):
                lifecycle._wake_socket({}, self.work / 'run', self.socket)

        with self.subTest('a wake without a recorded terminal never places a child of the sender'):
            write(directory / 'runtime.json', {})
            message = mailbox.append_message(directory, 'NO-PARENT', {'name': 'user'}, delivery='follow-up')
            with patch.object(harnesses, 'load', return_value=adapter), \
                    patch.object(lifecycle.agent_identity, 'calling_paths', side_effect=AssertionError('Wake used sender placement')):
                resumed = lifecycle.start(self.root, resume=first['id'], resume_message_id=message['id'], wait=True)
            self.backend = type(self.backend)(resumed['runtime']['tmux']['socket'])
            self.assertTrue(resumed['ready'], resumed)
            self.assertEqual(resumed['runtime']['tmux']['socket'], str(lifecycle.paths.run_directory() / 'tmux.sock'))
            self.kill_server()
