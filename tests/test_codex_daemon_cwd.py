"""A native daemon must not inherit a disposable agent workspace as its cwd."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from multiverse import native, rpc
from multiverse.harnesses.codex import CodexAdapter
from multiverse.harnesses.codex.launch import launch_settings
from multiverse.harnesses.types import LaunchContext, RpcSpec
from native_safety import isolated_home
from test_multiverse import BaseTest


@unittest.skipUnless(shutil.which('codex'), 'Codex required')
class CodexDaemonCwdTest(BaseTest):
    def test_config_read_survives_removal_of_original_workspace(self):
        home = isolated_home(self.work / 'native-home')
        managed = home / 'packages/standalone/current/codex'
        managed.parent.mkdir(parents=True)
        managed.symlink_to(Path(shutil.which('codex')).resolve())
        workspace = self.work / 'throwaway-workspace'
        workspace.mkdir()
        env = {**os.environ, 'CODEX_HOME': str(home), 'CODEX_SQLITE_HOME': str(home)}

        def stop_owned_daemon():
            # This test owns a separate native home; never touch the user's daemon.
            result = subprocess.run(['codex', 'app-server', 'daemon', 'stop'],
                                    cwd=self.work, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(stop_owned_daemon)
        values = {'name': 'cwd-probe', 'workspace': str(workspace), 'agent_dir': str(self.root),
                  'bootstrap_json': json.dumps('probe'), 'task': '', 'session_id': '',
                  'control_socket': str(self.work / 'unused-relay.sock')}
        command = CodexAdapter.configured('codex', {'inbox_bridge': True}).spawn(
            LaunchContext('spawn', values, environment=env))
        with patch.dict(os.environ, env):
            endpoint = native.start_shared_service(command.shared_service, workspace,
                                                   self.work / 'startup.log', command.unset_env)
        self.assertFalse((home / 'app-server-daemon/daemon-updater.pid').exists(),
                         'Disposable test daemons must not start a background updater')
        before = rpc.execute(RpcSpec(endpoint.address, lambda: launch_settings(str(self.work))))
        self.assertEqual(before.state, 'ok', before.reason)
        workspace.rmdir()
        after = rpc.execute(RpcSpec(endpoint.address, lambda: launch_settings(str(self.work))))
        self.assertEqual(after.state, 'ok', after.reason)
        self.assertEqual(after.value, before.value)
