"""Shared services survive terminal failure/exit and receive no agent identity."""
import json
from pathlib import Path
import socket
import sys
from unittest.mock import patch

from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse import harnesses
from multiverse.harnesses.types import CommandSpec, SharedServiceSpec
from multiverse.store import read
from test_multiverse import TerminalBase, until


class NativeServiceTest(TerminalBase):
    def test_shared_service_survives_terminal_exit_scrubs_identity_and_keeps_its_socket(self):
        """The shared daemon starts with no MV2_* or CODEX_THREAD_ID, from an executable relative to the workspace but
        running in its own persistent directory, outlives the pane, and a failed start launches no terminal and never
        deletes the socket; a break goes unnoticed because a daemon carrying one agent's identity works but
        misattributes every agent on it, a changed directory finds a custom executable in the wrong folder, and a
        deleted socket breaks the others."""
        address = self.work / 'shared.sock'
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(address))
        self.addCleanup(listener.close)
        info = {'socketPath': str(address), 'cliVersion': '1', 'appServerVersion': '1'}
        seen = self.work / 'daemon-env.json'
        executable = self.work / 'native-test'  # relative to the workspace, as a custom native executable may be
        executable.write_text(
            f'#!{sys.executable}\nimport json,os,pathlib\n'
            f'environment = {{k: v for k, v in os.environ.items()\n'
            f'               if k.startswith("MV2_") or k in ("CODEX_THREAD_ID", "CLAUDECODE")}}\n'
            f'pathlib.Path({str(seen)!r}).write_text(json.dumps({{"environment": environment, "cwd": os.getcwd()}}))\n'
            f'print({json.dumps(info)!r})\n')
        executable.chmod(0o755)
        stable = self.work / 'persistent'
        stable.mkdir()
        def launch(adapter, context):
            return CommandSpec((sys.executable, '-c', 'print("DONE",flush=True)'),
                               shared_service=SharedServiceSpec(('./native-test',), 'socketPath',
                                                                ('cliVersion', 'appServerVersion'), cwd=str(stable)))
        with patch.object(harnesses.CodexAdapter, 'spawn', launch):
            result = lifecycle.start(self.root, 'exit', 'test', 'codex', socket_path=self.socket)
        directory = agents.directory(self.root, result['id'])
        facts = until(lambda: (r if (r := agents.runtime(directory)).get('state') == 'stopped' else None))
        self.assertEqual(read(seen), {'environment': {}, 'cwd': str(stable)})
        self.assertEqual(facts['control_endpoint']['address'], str(address))
        self.assertTrue(address.exists())
        self.assertNotIn('service_pid', facts)
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
