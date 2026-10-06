"""Shared services survive terminal failure/exit and receive no agent identity."""
import json
from pathlib import Path
import socket
import sys
from unittest.mock import patch

from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse import harnesses
from multiverse.runtime import native
from multiverse.harnesses.types import CommandSpec, SharedServiceSpec
from multiverse.store import read
from test_multiverse import TerminalBase, until


class NativeServiceTest(TerminalBase):
    def test_service_cwd_preserves_workspace_relative_executable(self):
        address = self.work / 'shared.sock'
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(address))
        self.addCleanup(listener.close)
        info = {'socketPath': str(address), 'cliVersion': '1', 'appServerVersion': '1'}
        seen = self.work / 'daemon-cwd.json'
        executable = self.work / 'native-test'
        executable.write_text(f'#!{sys.executable}\nimport json,os,pathlib\n'
            f'pathlib.Path({str(seen)!r}).write_text(json.dumps(os.getcwd()))\n'
            f'print({json.dumps(info)!r})\n')
        executable.chmod(0o755)
        stable = self.work / 'persistent'
        stable.mkdir()
        plan = SharedServiceSpec(('./native-test',), 'socketPath', ('cliVersion', 'appServerVersion'), cwd=str(stable))
        endpoint = native.start_shared_service(plan, self.work, self.work / 'native.log', ())
        self.assertEqual(read(seen), str(stable))
        self.assertEqual(endpoint.address, str(address))

    def test_shared_service_survives_terminal_exit_and_scrubs_identity(self):
        address = self.work / 'shared.sock'
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(address))
        self.addCleanup(listener.close)
        info = {'socketPath': str(address), 'cliVersion': '1', 'appServerVersion': '1'}
        seen = self.work / 'daemon-env.json'
        starter = ('import json,os,pathlib; pathlib.Path(' + repr(str(seen)) + ').write_text('
                   'json.dumps({k:v for k,v in os.environ.items() if k.startswith("MV2_") or k in ("CODEX_THREAD_ID","CLAUDECODE")})); '
                   'print(' + repr(json.dumps(info)) + ')')
        def launch(adapter, context):
            return CommandSpec((sys.executable, '-c', 'print("DONE",flush=True)'),
                               shared_service=SharedServiceSpec((sys.executable, '-c', starter), 'socketPath',
                                                                ('cliVersion', 'appServerVersion')))
        with patch.object(harnesses.CodexAdapter, 'spawn', launch):
            result = lifecycle.start(self.root, 'exit', 'test', 'codex', socket_path=self.socket)
        directory = agents.directory(self.root, result['id'])
        facts = until(lambda: (r if (r := agents.runtime(directory)).get('state') == 'stopped' else None))
        self.assertEqual(read(seen), {})
        self.assertEqual(facts['control_endpoint']['address'], str(address))
        self.assertTrue(address.exists())
        self.assertNotIn('service_pid', facts)

    def test_shared_service_errors_never_launch_terminal_or_remove_socket(self):
        address = self.work / 'shared.sock'
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(address))
        self.addCleanup(listener.close)
        cases = [
            ('import sys; sys.stderr.write("native installation missing"); sys.exit(7)', 'native installation missing'),
            ('print("not json")', 'Expecting value'),
            ('print(\'{"socketPath":"relative","cliVersion":"1","appServerVersion":"1"}\')', 'absolute Unix socket'),
            ('print(' + repr(json.dumps({'socketPath': str(address), 'cliVersion': '2', 'appServerVersion': '1'})) + ')', 'version mismatch'),
        ]
        for index, (starter, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                marker = self.work / f'terminal-{index}'
                def launch(adapter, context):
                    return CommandSpec((sys.executable, '-c', f'open({str(marker)!r},"w").close()'),
                                       shared_service=SharedServiceSpec((sys.executable, '-c', starter), 'socketPath',
                                                                        ('cliVersion', 'appServerVersion')))
                with patch.object(harnesses.CodexAdapter, 'spawn', launch):
                    with self.assertRaisesRegex(RuntimeError, expected):
                        lifecycle.start(self.root, f'error-{index}', 'test', 'codex', socket_path=self.socket)
                directory = agents.directory(self.root, f'error-{index}')
                facts = until(lambda: (r if (r := agents.runtime(directory)).get('state') == 'failed' else None))
                self.assertIn(expected, facts['error'])
                self.assertFalse(marker.exists())
                self.assertTrue(address.exists())
