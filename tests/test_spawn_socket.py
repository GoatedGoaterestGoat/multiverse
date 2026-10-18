"""An agent's child uses its recorded server without cached shell socket values."""
import os
from dataclasses import asdict
from unittest.mock import patch
from uuid import uuid4

from multiverse.harnesses.types import AgentIdentitySpec, NativeEndpoint, NativeServiceHandle, ServiceIdentity
from multiverse.runtime import agent_identity, lifecycle
from multiverse.store import read, write
from multiverse.terminal.tmux import Tmux
from test_multiverse import TerminalBase


class SpawnSocketTest(TerminalBase):
    def stable_parent(self, parent=None, directory=None):
        if parent is None:
            parent, directory = self.spawn()
        properties = read(directory / 'properties.json')
        native = str(uuid4())
        service = NativeServiceHandle(ServiceIdentity('codex', {'CODEX_HOME': str(self.work)}),
                                      NativeEndpoint('unix-websocket', str(self.work / 'unused.sock')))
        spec = AgentIdentitySpec('codex-agent-v1', 'CODEX_THREAD_ID', ('CODEX_HOME', 'CODEX_SQLITE_HOME'))
        properties.update(native_session_id=native, native_service_identity=asdict(service.identity))
        properties['agent_identity'] = agent_identity.record(spec, properties, service)
        write(directory / 'properties.json', properties)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(('MV2_', 'CODEX_')) and key not in ('TMUX', 'TMUX_PANE')}
        environment.update({'MV2_IDENTITY_MODE': spec.mode, 'MV2_UNIVERSE': str(self.root),
                       'MV2_AGENT_ID': parent['id'], 'MV2_AGENT_DIR': str(directory),
                       'CODEX_THREAD_ID': native, 'CODEX_HOME': str(self.work)})
        return parent, directory, environment

    def test_child_agents_use_the_parents_recorded_tmux_server(self):
        """A spawn from inside an agent uses the parent's recorded tmux socket in both identity modes, never a cached
        MV2_TMUX_SOCKET, and refuses a stale generation; a break goes unnoticed because agents run fine on the user's
        default server, just in the wrong place (bec1f85)."""
        parent, directory = self.spawn()
        with self.subTest('a legacy caller uses the current record'):
            environment = {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': parent['id'],
                           'MV2_AGENT_DIR': str(directory), 'MV2_LAUNCH_ID': parent['runtime']['launch_id'],
                           'MV2_TMUX_SOCKET': '/must-not-use-cached.sock'}
            self.assertEqual(agent_identity.calling_socket(environment), self.socket)
            with self.assertRaisesRegex(ValueError, 'Outdated launch'):
                agent_identity.calling_socket({**environment, 'MV2_LAUNCH_ID': 'old'})
        with self.subTest("a stable agent's child is created on the parent's server"):
            _, _, environment = self.stable_parent(parent, directory)
            original = Tmux.create
            def create(backend, *args):
                self.assertEqual(backend.prefix, ['tmux', '-S', self.socket],
                                 'Refuse to create anything on the default server in this regression')
                return original(backend, *args)
            with patch.dict(os.environ, environment, clear=True), patch.object(Tmux, 'create', create):
                child = lifecycle.start(self.root, 'child', '', 'fake')
            self.assertEqual(child['runtime']['tmux']['socket'], self.socket)

    def test_bad_socket_or_agent_proof_never_falls_back_to_the_default_server(self):
        """Wrong proof fields, missing/torn runtime or a damaged tmux record raise instead of falling back to the
        default server; fragile because falling back to the default is the natural failure of or-None code
        (bec1f85)."""
        parent, directory, environment = self.stable_parent()
        facts = read(directory / 'runtime.json')
        for change in ({'MV2_IDENTITY_MODE': ''}, {'MV2_AGENT_DIR': str(self.work)},
                       {'CODEX_THREAD_ID': str(uuid4())}, {'CODEX_HOME': str(self.work / 'other')}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                agent_identity.calling_socket({**environment, **change})
        try:
            for mode in ('stable', 'legacy'):
                caller = dict(environment)
                if mode == 'legacy':
                    caller.pop('MV2_IDENTITY_MODE')
                    caller['MV2_LAUNCH_ID'] = facts['launch_id']
                for contents in (None, '{torn'):
                    with self.subTest(mode=mode, runtime=contents):
                        path = directory / 'runtime.json'
                        if contents is None:
                            path.unlink(missing_ok=True)
                        else:
                            path.write_text(contents)
                        with self.assertRaisesRegex(ValueError, str(path)):
                            agent_identity.calling_socket(caller)
            for terminal in (None, [], {'socket': None}, {'socket': ''}, {'socket': 7}, {'socket': 'bad\0socket'}):
                write(directory / 'runtime.json', {**facts, 'tmux': terminal})
                with self.subTest(terminal=terminal), self.assertRaisesRegex(ValueError, 'no recorded tmux'):
                    agent_identity.calling_socket(environment)
        finally:
            write(directory / 'runtime.json', facts)
