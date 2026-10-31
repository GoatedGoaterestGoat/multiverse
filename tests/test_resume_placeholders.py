"""Custom recipes receive this resume's prompt, never the saved original task."""
import os
from pathlib import Path
import sys
from contextlib import nullcontext
from unittest.mock import patch
from multiverse import harnesses
from multiverse.harnesses.types import Capability, NativeResult
from multiverse.runtime import lifecycle
from multiverse.terminal import recovery
from multiverse.store import read, uid, write
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
        write(folder / 'harness.json', {'schema': 1, 'session': 'assigned', 'spawn': command, 'resume': command})
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
        for case in ('legacy swept', 'legacy retry', 'legacy live', 'legacy recover', 'legacy peer recover',
                     'legacy changed tmp', 'legacy operator', 'default operator',
                     'custom present', 'custom live', 'custom swept', 'explicit resume', 'operator custom'):
            with self.subTest(socket_selection=case):
                run_root = self.work / 'run'
                legacy_root = self.work / 'old-tmp'
                old = (legacy_root / f'tmux-{os.getuid()}' / 'multiverse-v2' if case.startswith('legacy')
                       else run_root / 'tmux.sock' if case == 'default operator' else self.work / 'custom' / 'tmux.sock')
                old.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                self.socket, self.backend = str(old), type(self.backend)(str(old))
                environment = {key: value for key, value in os.environ.items() if not key.startswith('MV2_')}
                environment.update(MV2_RUN_DIR=str(run_root), TMUX_TMPDIR=str(legacy_root), MV2_TMUX_SOCKET=str(old))
                with patch.dict(os.environ, environment, clear=True), patch.object(harnesses, 'load', return_value=adapter):
                    first, _, _ = launched(name='socket-' + case.replace(' ', '-'), task='ORIGINAL', harness='recorder')
                    directory = self.root / 'state/agents' / first['id']
                    pid = self.backend.run('display-message', '-p', '#{pid}')
                    live = case in ('legacy live', 'legacy recover', 'legacy peer recover', 'custom live')
                    peer_facts = None
                    if live:
                        if case == 'legacy peer recover':
                            peer, _, _ = launched(name='main-era-peer', task='KEEPER', harness='recorder')
                            peer_dir = self.root / 'state/agents' / peer['id']
                            peer_facts = read(peer_dir / 'runtime.json')
                            peer_facts.pop('run_directory', None)
                            peer_facts['tmux'].pop('server', None)
                            peer_facts['tmux'].pop('server_identity', None)
                            write(peer_dir / 'runtime.json', peer_facts)
                        else:
                            self.backend.create('keeper', self.work, [sys.executable, '-c', 'import time; time.sleep(60)'])
                        lifecycle.kill(self.root, first['id'])
                    else:
                        self.kill_server()
                    if case not in ('legacy live', 'custom live', 'custom present', 'operator custom'):
                        old.unlink(missing_ok=True)
                        old.parent.rmdir()
                    # A main-era launch has no saved run_directory, but its
                    # request still exported the old server socket.
                    if case.startswith('legacy'):
                        facts = read(directory / 'runtime.json')
                        facts.pop('run_directory', None)
                        if peer_facts is not None:
                            facts['tmux'].pop('server', None)
                            facts['tmux'].pop('server_identity', None)
                            request_path = directory / 'launches' / facts['launch_id'] / 'request.json'
                            request = read(request_path)
                            request['env'].pop('MV2_RUN_DIR', None)
                            write(request_path, request)
                            recovery.registry(old.resolve()).unlink(missing_ok=True)
                            self.assertEqual(recovery.runner_server(peer_facts)['pid'], int(pid))
                        write(directory / 'runtime.json', facts)
                    for path in (output, file_output):
                        path.unlink(missing_ok=True)
                    os.environ.pop('MV2_TMUX_SOCKET')
                    if case == 'legacy changed tmp':
                        os.environ['TMUX_TMPDIR'] = str(self.work / 'new-tmp')
                    expected = str(old) if live or case in ('custom present', 'operator custom') else str(run_root / 'tmux.sock')
                    expected_run = run_root
                    if case == 'operator custom':
                        os.environ['MV2_TMUX_SOCKET'] = str(old)
                    elif case in ('legacy operator', 'default operator'):
                        expected = str(self.work / 'chosen.sock')
                        expected_run = self.work / 'next'
                        os.environ.update(MV2_TMUX_SOCKET=expected, MV2_RUN_DIR=str(expected_run))
                    elif live:
                        # A live/recoverable recorded server wins over the
                        # operator's different current placement.
                        os.environ.update(MV2_TMUX_SOCKET=str(self.work / 'other.sock'), MV2_RUN_DIR=str(self.work / 'next'))
                    advice = []
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
                                result = send.run(self.root, agent=first['id'], text='WAKE:' + case, _present=advice.append)
                        if case == 'custom swept':
                            self.assertFalse(result['ok'], result)
                            self.assertIn('recorded tmux socket folder', result['native_delivery']['reason'])
                            self.assertIn('MV2_TMUX_SOCKET', result['native_delivery']['reason'])
                            self.assertFalse(old.parent.exists())
                            self.assertEqual(type(self.backend)(str(run_root / 'tmux.sock')).snapshot(), ({}, None))
                            # Following the resume advice does not deliver the
                            # saved prompt: retry needs a bridge, while a plain
                            # agent can only receive manual inbox delivery.
                            os.environ['MV2_TMUX_SOCKET'] = expected
                            resumed = lifecycle.start(self.root, resume=first['id'], wait=True)
                            self.assertTrue(resumed['ready'], resumed)
                            plain = deliver.run(self.root, agent=first['id'], retry=True)
                            self.assertFalse(plain['ok'], plain)
                            self.assertIn('launch-only', plain['messages'][0]['reason'])
                            manual = send.run(self.root, agent=first['id'], text='MANUAL', delivery='inbox-only')
                            self.assertTrue(manual['saved'])
                            self.assertEqual(manual['state'], 'queued')
                            capabilities = {**adapter.capabilities(None), 'send_steer': Capability('supported')}
                            with patch.object(adapter, 'capabilities', return_value=capabilities), \
                                    patch.object(adapter, 'send', return_value=NativeResult('native_queued')) as submit:
                                bridged = deliver.run(self.root, agent=first['id'], retry=True)
                                self.assertTrue(bridged['ok'], bridged)
                                submit.assert_called_once()
                                self.assertIn('WAKE:' + case, submit.call_args.args[1].text)
                            self.assertIn('With a working bridge', advice[0])
                            self.assertIn('For a launch-only agent', advice[0])
                            self.assertIn('--delivery inbox-only', advice[0])
                        else:
                            self.assertTrue(result['ok'] if 'ok' in result else result['ready'], result)
                            until(lambda: output.exists() and file_output.exists())
                            facts = read(directory / 'runtime.json')
                            self.assertEqual(facts['tmux']['socket'], expected)
                            self.assertEqual(read(directory / 'launches' / facts['launch_id'] / 'request.json')['env']['MV2_TMUX_SOCKET'], expected)
                            # Main-era records have no run root to retain.
                            self.assertEqual(facts['run_directory'], str(self.work / 'next' if live and case.startswith('legacy') else expected_run))
                            if live:
                                self.assertEqual(self.backend.run('display-message', '-p', '#{pid}'), pid)
                            if case not in ('explicit resume', 'operator custom'):
                                self.assertIn('WAKE:' + case, output.read_text())
                                self.assertEqual(output.read_text(), file_output.read_text())
                    finally:
                        if peer_facts is not None:
                            # A mutation may skip the peer proof and leave the
                            # old server hidden. Recover through that same proof
                            # before cleanup, never through an unchecked PID.
                            recovery.observe(self.backend, facts=peer_facts)
                        # End both private candidates before the fixture waits
                        # for runners, even when a mutation chose the wrong one.
                        for candidate in {expected, str(old), str(run_root / 'tmux.sock'),
                                          str(lifecycle.paths.run_directory() / 'tmux.sock'),
                                          os.environ.get('MV2_TMUX_SOCKET')} - {None}:
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
                lifecycle._wake_socket(self.root, {}, self.work / 'run', self.socket)

        # Recordless wakes use an operator's current choice or a verified
        # same-tree sender's placement. No cross-tree or unprovable fallback.
        for caller in ('operator default', 'operator custom', 'operator legacy', 'same tree', 'cross tree', 'unprovable'):
            with self.subTest(recordless_wake=caller):
                environment = {key: value for key, value in os.environ.items() if not key.startswith('MV2_')}
                environment['MV2_RUN_DIR'] = str(self.work / 'recordless-run')
                environment['TMUX_TMPDIR'] = str(self.work / 'recordless-tmp')
                default = str(Path(environment['MV2_RUN_DIR']) / 'tmux.sock')
                selected = (str(Path(environment['TMUX_TMPDIR']) / f'tmux-{os.getuid()}' / 'multiverse-v2')
                            if caller == 'operator legacy' else str(self.work / 'operator' / 'tmux.sock'))
                Path(selected).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if caller != 'operator default':
                    environment['MV2_TMUX_SOCKET'] = selected
                expected = selected if caller in ('operator custom', 'operator legacy') else default
                expected_run = Path(environment['MV2_RUN_DIR'])
                if not caller.startswith('operator'):
                    sender_root = self.work / 'foreign' if caller == 'cross tree' else self.root / 'subverses/sender'
                    sender_universe = {'schema': 1, 'id': uid(), 'workspace': str(self.work)}
                    write(sender_root / 'universe.json', sender_universe)
                    sender_id, sender_launch = uid(), uid()
                    sender_dir = sender_root / 'state/agents' / sender_id
                    write(sender_dir / 'properties.json', {'id': sender_id, 'universe_id': sender_universe['id'],
                          'name': 'sender', 'harness': 'recorder', 'workspace': str(self.work)})
                    expected_run = self.work / 'caller-run'
                    write(sender_dir / 'runtime.json', {'launch_id': sender_launch, 'state': 'stopped',
                          'run_directory': str(expected_run), 'tmux': {'socket': selected}})
                    environment.update(MV2_AGENT_ID=sender_id, MV2_AGENT_DIR=str(sender_dir), MV2_UNIVERSE=str(sender_root),
                                       MV2_LAUNCH_ID='stale' if caller == 'unprovable' else sender_launch,
                                       MV2_TMUX_SOCKET=str(self.work / 'cached.sock'))
                    expected = selected
                write(directory / 'runtime.json', {})
                message = mailbox.append_message(directory, 'NO-PARENT', {'name': 'user'}, delivery='follow-up')
                with patch.dict(os.environ, environment, clear=True), patch.object(harnesses, 'load', return_value=adapter):
                    try:
                        if caller in ('cross tree', 'unprovable'):
                            with patch.object(lifecycle.Tmux, 'create', side_effect=AssertionError('Unproven wake created a server')):
                                with self.assertRaisesRegex(recovery.Unreachable, 'operator with MV2_TMUX_SOCKET'):
                                    lifecycle.start(self.root, resume=first['id'], resume_message_id=message['id'], wait=True)
                            self.assertEqual(read(directory / 'runtime.json'), {})
                            continue
                        resumed = lifecycle.start(self.root, resume=first['id'], resume_message_id=message['id'], wait=True)
                        self.assertTrue(resumed['ready'], resumed)
                        self.assertEqual(resumed['runtime']['tmux']['socket'], expected)
                        facts = resumed['runtime']
                        request = read(directory / 'launches' / facts['launch_id'] / 'request.json')
                        self.assertEqual(request['env']['MV2_TMUX_SOCKET'], expected)
                        self.assertEqual(facts['run_directory'], str(expected_run))
                        self.assertEqual(request['env']['MV2_RUN_DIR'], str(expected_run))
                        self.assertTrue(Path(request['env']['MV2_CONTROL_SOCKET']).is_relative_to(expected_run))
                    finally:
                        for candidate in {default, selected, str(expected_run / 'tmux.sock'),
                                          str(Path.home() / '.multiverse/run/tmux.sock'),
                                          environment.get('MV2_TMUX_SOCKET')} - {None}:
                            try:
                                type(self.backend)(candidate).run('kill-server')
                            except (OSError, RuntimeError):
                                pass
                        self.backend = type(self.backend)(expected)
                        self.kill_server()
