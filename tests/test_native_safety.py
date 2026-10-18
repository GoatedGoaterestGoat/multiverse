import os
from pathlib import Path
import socket
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from native_safety import PROTECTED_HOMES, check_connect, check_spawn, install, isolated_home, require_checked_codex

install()


class NativeSafetyTest(unittest.TestCase):
    def test_tests_cannot_touch_the_users_codex_home_or_daemon(self):
        """Spawn, connect and fixture setup against the user's Codex home, socket or daemon raise before acting;
        fragile because the guard patches subprocess and socket globally, so a refactor disables it silently and the
        first sign is a test restarting the user's daemon."""
        for home in PROTECTED_HOMES:
            for operation in ('start', 'stop', 'restart', 'update', 'version'):
                with self.subTest(home=home, operation=operation), self.assertRaisesRegex(AssertionError, 'default Codex daemon'):
                    subprocess.run(['codex', 'app-server', 'daemon', operation],
                                   env={**os.environ, 'CODEX_HOME': str(home)}, capture_output=True)
        with self.subTest('resume in the default home'), self.assertRaises(AssertionError):
            check_spawn(['codex', 'resume', 'saved-thread'], {})
        for home in PROTECTED_HOMES:
            path = home / 'app-server-control/app-server-control.sock'
            for address in (str(path), str(path.resolve())):
                with self.subTest(socket=address), socket.socket(socket.AF_UNIX) as client, \
                        self.assertRaisesRegex(AssertionError, 'default Codex daemon'):
                    client.connect(address)
            with self.subTest(fixture_setup=home), self.assertRaisesRegex(AssertionError, 'default Codex daemon'):
                isolated_home(home)

    def test_isolated_homes_are_allowed_only_with_update_checks_off(self):
        """An isolated Codex home is allowed only while its update checks are provably off; a break goes unnoticed
        until a test's Codex updates the user's installed Codex."""
        with tempfile.TemporaryDirectory() as path:
            with self.assertRaisesRegex(AssertionError, 'disable daemon automatic updates'):
                check_spawn(['codex', 'app-server', 'daemon', 'start'], {'CODEX_HOME': path})
            isolated_home(path)
            check_spawn(['codex', 'app-server', 'daemon', 'start'], {'CODEX_HOME': path})
        check_spawn(['codex', '--version'], {})
        check_spawn(['codex', '--help'], {})
        check_connect('/tmp/isolated-test/rpc.sock')
        # Unsupported installed CLIs must never reach native fixture setup.
        for version in (None, (0, 154, 0), (0, 162, 1), (0, 163, 0), (0, 164, 0)):
            with self.subTest(cli=version), patch('multiverse.harnesses.codex.plugins.codex_version',
                                                  return_value=version):
                if version is None or version < (0, 163, 0):
                    with self.assertRaisesRegex(unittest.SkipTest, 'require checked CLI'):
                        require_checked_codex(self)
                else:
                    require_checked_codex(self)
        with tempfile.TemporaryDirectory() as path:
            home = isolated_home(path, config='model="offline"\n[model_providers.offline]\nname="Fixture"\n')
            config = tomllib.loads((home / 'config.toml').read_text())
            self.assertIs(config['check_for_update_on_startup'], False)
            self.assertEqual(config['model_providers']['offline']['name'], 'Fixture')
            check_spawn(['codex', 'resume', 'saved-thread'], {'CODEX_HOME': str(home)})
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
