import os
from pathlib import Path
import socket
import subprocess
import tempfile
import tomllib
import unittest

from native_safety import PROTECTED_HOMES, check_connect, check_spawn, install, isolated_home

install()


class NativeSafetyTest(unittest.TestCase):
    def test_default_daemon_commands_fail_before_process_creation(self):
        for home in PROTECTED_HOMES:
            for operation in ('start', 'stop', 'restart', 'update', 'version'):
                with self.subTest(home=home, operation=operation), self.assertRaisesRegex(AssertionError, 'default Codex daemon'):
                    subprocess.run(['codex', 'app-server', 'daemon', operation],
                                   env={**os.environ, 'CODEX_HOME': str(home)}, capture_output=True)
        with self.assertRaises(AssertionError):
            check_spawn(['codex', 'resume', 'saved-thread'], {})

    def test_default_socket_and_its_short_target_are_refused(self):
        for home in PROTECTED_HOMES:
            path = home / 'app-server-control/app-server-control.sock'
            for address in (str(path), str(path.resolve())):
                with socket.socket(socket.AF_UNIX) as client, self.assertRaisesRegex(AssertionError, 'default Codex daemon'):
                    client.connect(address)

    def test_isolated_native_home_and_read_only_cli_help_are_allowed(self):
        with tempfile.TemporaryDirectory() as path:
            with self.assertRaisesRegex(AssertionError, 'disable daemon automatic updates'):
                check_spawn(['codex', 'app-server', 'daemon', 'start'], {'CODEX_HOME': path})
            isolated_home(path)
            check_spawn(['codex', 'app-server', 'daemon', 'start'], {'CODEX_HOME': path})
        check_spawn(['codex', '--version'], {})
        check_spawn(['codex', '--help'], {})
        check_connect('/tmp/isolated-test/rpc.sock')

    def test_fixture_setup_cannot_change_default_home_settings(self):
        for home in PROTECTED_HOMES:
            with self.assertRaisesRegex(AssertionError, 'default Codex daemon'):
                isolated_home(home)

    def test_isolated_home_preserves_provider_config_after_top_level_update_setting(self):
        with tempfile.TemporaryDirectory() as path:
            home = isolated_home(path, config='model="offline"\n[model_providers.offline]\nname="Fixture"\n')
            config = tomllib.loads((home / 'config.toml').read_text())
            self.assertIs(config['check_for_update_on_startup'], False)
            self.assertEqual(config['model_providers']['offline']['name'], 'Fixture')
            check_spawn(['codex', 'resume', 'saved-thread'], {'CODEX_HOME': str(home)})

    def test_missing_or_overwritten_tui_update_setting_blocks_process_creation(self):
        with tempfile.TemporaryDirectory() as path:
            home = isolated_home(path)
            config = home / 'config.toml'
            for contents in (None, '', 'check_for_update_on_startup=true\n',
                             'check_for_update_on_startup="false"\n',
                             '[model_providers.offline]\ncheck_for_update_on_startup=false\n',
                             'invalid TOML'):
                with self.subTest(contents=contents):
                    if contents is None:
                        config.unlink()
                    else:
                        config.write_text(contents)
                    with self.assertRaisesRegex(AssertionError, 'disable TUI startup update checks'):
                        subprocess.run(['codex', 'resume', 'saved-thread'],
                                       env={**os.environ, 'CODEX_HOME': str(home)}, capture_output=True)
