import os
from pathlib import Path
import socket
import subprocess
import unittest

from native_safety import PROTECTED_HOMES, check_connect, check_spawn, install

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
        check_spawn(['codex', 'app-server', 'daemon', 'start'], {'CODEX_HOME': '/tmp/disposable-native-home'})
        check_spawn(['codex', '--version'], {})
        check_spawn(['codex', '--help'], {})
        check_connect('/tmp/isolated-test/rpc.sock')
