"""Durable sockets keep launch identity while temporary storage and cached tools change."""
import os
from pathlib import Path
from unittest.mock import patch

from isolation import track
from multiverse import harnesses
from multiverse.runtime import agent_identity, lifecycle, paths
from multiverse.store import read, uid, write
from multiverse.terminal import recovery
from multiverse.terminal.tmux import Tmux
from test_multiverse import BaseTest, FixtureTmux, TerminalBase, until


class RunDirectoryTest(BaseTest):
    def test_path_selection_and_saved_requests_do_not_follow_later_environment_changes(self):
        """An override must be read afresh for new launches while a saved request keeps exactly its old launch socket."""
        identifier = uid()
        env = dict(os.environ)
        env.pop('MV2_RUN_DIR', None)
        expected = Path(env['HOME']) / '.multiverse/run'
        self.assertEqual(paths.run_directory(env), expected.resolve())
        selected = self.work / 'private'
        with patch.dict(os.environ, {'MV2_RUN_DIR': str(selected)}):
            self.assertEqual(paths.run_directory(), selected)
            self.assertEqual(Tmux().prefix, ['tmux', '-S', str(selected / 'tmux.sock')])
            self.assertEqual(paths.prepare(), selected)
            self.assertEqual(selected.stat().st_mode & 0o777, 0o700)
            socket = paths.native_socket(identifier)
            self.assertEqual(socket, selected / 'native' / identifier / 'rpc.sock')
        second = self.work / 'second'
        with patch.dict(os.environ, {'MV2_RUN_DIR': str(second)}):
            self.assertEqual(paths.run_directory(), second)
            self.assertEqual(paths.launch_socket(identifier, {'MV2_RUN_DIR': str(selected)}), socket)
            self.assertEqual(paths.launch_socket(identifier, {}), Path('/tmp') / ('mv2-native-' + identifier) / 'rpc.sock')
        for bad in ('relative', '/tmp/../other', None, [], ''):
            with self.subTest(saved=bad), self.assertRaises(ValueError):
                paths.launch_socket(identifier, {'MV2_RUN_DIR': bad})
        with patch.dict(os.environ, {'MV2_RUN_DIR': 'relative'}), self.assertRaisesRegex(ValueError, 'MV2_RUN_DIR'):
            paths.run_directory()
        selected.chmod(0o755)
        with self.assertRaisesRegex(ValueError, '0700'):
            paths.prepare(selected)
        self.assertEqual(selected.stat().st_mode & 0o777, 0o755)
        blocked = self.work / 'blocked-run'
        blocked.write_text('keep this file')
        for candidate in (blocked, blocked / 'nested'):
            with self.subTest(blocked_directory=candidate):
                with self.assertRaisesRegex(ValueError, 'regular file.*Move it aside') as error:
                    paths.prepare(candidate)
                self.assertIn(str(blocked), str(error.exception))
                self.assertEqual(blocked.read_text(), 'keep this file')
        for platform, limit in (('darwin', 103), ('linux', 107)):
            with self.subTest(platform=platform), patch.object(paths.sys, 'platform', platform):
                suffix = '/native/' + identifier + '/rpc.sock'
                exact = Path('/' + 'x' * (limit - len(suffix) - 1))
                self.assertEqual(len(os.fsencode(paths.native_socket(identifier, directory=exact))), limit)
                with self.assertRaisesRegex(ValueError, 'MV2_RUN_DIR'):
                    paths.native_socket(identifier, directory=str(exact) + 'x')
        adapter = harnesses.BUILTINS['pi'].configured('pi', {})
        with patch.dict(os.environ, {'MV2_RUN_DIR': str(self.work / ('long-' + 'x' * 80))}), \
                patch.object(lifecycle, 'Tmux'), patch.object(harnesses, 'load', return_value=adapter), \
                patch.object(adapter, 'spawn', side_effect=AssertionError('native preparation reached')) as spawn:
            with self.assertRaisesRegex(ValueError, 'MV2_RUN_DIR'):
                lifecycle.start(self.root, name='too-long', harness='pi')
            spawn.assert_not_called()
        for source in ('explicit tmux', 'run directory', 'HOME default'):
            with self.subTest(overlong_socket=source):
                too_long = str(self.work / ('long-' + 'x' * 100))
                env = dict(os.environ)
                socket = None
                if source == 'explicit tmux':
                    socket = too_long + '/tmux.sock'
                    variable = 'MV2_TMUX_SOCKET'
                elif source == 'run directory':
                    env['MV2_RUN_DIR'] = too_long
                    variable = 'MV2_RUN_DIR'
                else:
                    env.pop('MV2_RUN_DIR', None)
                    env['HOME'] = too_long
                    variable = 'MV2_RUN_DIR'
                with patch.dict(os.environ, env, clear=True), \
                        patch.object(lifecycle, 'Tmux', side_effect=AssertionError('Terminal reached before path validation')), \
                        patch.object(harnesses, 'load', side_effect=AssertionError('Provider reached before path validation')):
                    with self.assertRaisesRegex(ValueError, variable):
                        lifecycle.start(self.root, name='invalid-path', harness='pi', socket_path=socket)
                self.assertFalse(Path(too_long).exists())


class DurableLaunchTest(TerminalBase):
    def test_a_legacy_server_stays_visible_and_resume_moves_only_after_it_stops(self):
        """An upgrade must find old panes before starting a replacement, and children must inherit the current run folder rather than cached tools."""
        legacy = FixtureTmux(legacy=True)
        self.backend = legacy
        self.socket = str(recovery.address(legacy))
        Path(self.socket).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        track(tmux_socket=self.socket)
        # End both fixture servers before TerminalBase waits for all runners
        # under this workspace; that wait intentionally covers every server.
        servers = [legacy]
        def cleanup():
            for backend in servers:
                try:
                    backend.run('kill-server')
                except (OSError, RuntimeError):
                    pass
        self.addCleanup(cleanup)
        first, directory = self.spawn()
        # Rewrite the legacy fixture only after the child identity is published;
        # the stand-in can write seen.json before the runner records its PID.
        until(lambda: (facts := read(directory / 'runtime.json')).get('runner_started')
              and facts.get('pid') and facts.get('pid_started'))
        facts = read(directory / 'runtime.json')
        facts.pop('run_directory')
        facts['tmux'].pop('server')
        write(directory / 'runtime.json', facts)
        launch = directory / 'launches' / facts['launch_id']
        request = read(launch / 'request.json')
        request['env'].pop('MV2_RUN_DIR')
        request['env']['MV2_CONTROL_SOCKET'] = str(paths.launch_socket(launch.name, {}))
        write(launch / 'request.json', request)
        recovery.registry(recovery.address(legacy)).unlink()
        selected = self.work / 'durable'
        servers.append(FixtureTmux(str(selected / 'tmux.sock')))
        with patch.dict(os.environ, {'MV2_RUN_DIR': str(selected)}):
            self.assertEqual(lifecycle.listing(self.root)[0]['live'], 'alive')
            with self.assertRaisesRegex(ValueError, 'running'):
                lifecycle.start(self.root, resume=directory.name)
            self.assertFalse((selected / 'tmux.sock').exists())
            lifecycle.kill(self.root, directory.name)
            resumed = lifecycle.start(self.root, resume=directory.name, wait=True, wait_timeout=15)
            self.backend = FixtureTmux(str(selected / 'tmux.sock'))
            track(tmux_socket=str(selected / 'tmux.sock'))
            self.assertTrue(resumed['ready'])
            current = read(directory / 'runtime.json')
            self.assertEqual(current['run_directory'], str(selected))
            self.assertEqual(current['tmux']['socket'], str(selected / 'tmux.sock'))
            saved = read(directory / 'launches' / current['launch_id'] / 'request.json')
            self.assertEqual(saved['env']['MV2_RUN_DIR'], str(selected))
            self.assertEqual(saved['env']['MV2_CONTROL_SOCKET'], str(paths.native_socket(current['launch_id'])))
            props = read(directory / 'properties.json')
            caller = {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': directory.name, 'MV2_AGENT_DIR': str(directory),
                      'MV2_LAUNCH_ID': current['launch_id'], 'MV2_RUN_DIR': str(self.work / 'stale-tools'),
                      'MV2_TMUX_SOCKET': str(self.work / 'stale.sock')}
            with patch.dict(os.environ, caller):
                child = lifecycle.start(self.root, name='child', harness='fake', wait=True, wait_timeout=15)
                self.assertEqual(child['runtime']['run_directory'], str(selected))
                self.assertEqual(child['runtime']['tmux']['socket'], current['tmux']['socket'])
                write(directory / 'runtime.json', {**current, 'run_directory': 'relative'})
                with self.assertRaisesRegex(ValueError, 'run directory'):
                    agent_identity.calling_paths()
                write(directory / 'runtime.json', current)
            self.assertEqual(read(directory / 'properties.json'), props)
