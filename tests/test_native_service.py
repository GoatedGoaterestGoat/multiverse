"""A native service failure stops only the terminal owned by its core launch."""
from pathlib import Path
import sys
from unittest.mock import patch

from multiverse import agents, harnesses
from multiverse.harnesses.types import CommandSpec, NativeEndpoint, ServiceSpec
from test_multiverse import TerminalBase, until


class NativeServiceTest(TerminalBase):
    def test_native_service_failure_is_diagnosable_and_cleans_private_socket(self):
        def launch(adapter, context):
            endpoint = context.values['control_socket']
            service = ('import socket,sys,time; s=socket.socket(socket.AF_UNIX); '
                       's.bind(sys.argv[1]); s.listen(); time.sleep(1); sys.exit(7)')
            terminal = ('import time; print("OWNED TERMINAL",flush=True); time.sleep(30)')
            return CommandSpec((sys.executable, '-c', terminal), control_endpoint=NativeEndpoint('unix-websocket', endpoint),
                               service=ServiceSpec((sys.executable, '-c', service, endpoint), endpoint))
        with patch.object(harnesses.CodexAdapter, 'spawn', launch):
            result = agents.start(self.root, 'failure', 'test', 'codex', socket_path=self.socket)
        directory = agents.directory(self.root, result['id'])
        endpoint = Path(result['runtime']['control_endpoint']['address'])
        facts = until(lambda: (r if (r := agents.runtime(directory)).get('state') == 'failed' else None))
        self.assertIn('Native service exited 7', facts['error'])
        self.assertIn('native-service.log', facts['error'])
        until(lambda: not endpoint.parent.exists())
        until(lambda: agents.listing(self.root)[0]['live'] == 'stopped')
