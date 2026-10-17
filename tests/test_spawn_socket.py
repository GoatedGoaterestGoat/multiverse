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
    def stable_parent(self):
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

    def test_stable_agent_child_uses_parent_runtime_socket_without_volatile_env(self):
        parent, directory, environment = self.stable_parent()
        original = Tmux.create
        def create(backend, *args):
            self.assertEqual(backend.prefix, ['tmux', '-S', self.socket],
                             'Refuse to create anything on the default server in this regression')
            return original(backend, *args)
        with patch.dict(os.environ, environment, clear=True), patch.object(Tmux, 'create', create):
            child = lifecycle.start(self.root, 'child', '', 'fake')
        self.assertEqual(child['runtime']['tmux']['socket'], self.socket)

    def test_legacy_caller_uses_current_record_and_refuses_a_stale_generation(self):
        parent, directory = self.spawn()
        environment = {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': parent['id'],
                       'MV2_AGENT_DIR': str(directory), 'MV2_LAUNCH_ID': parent['runtime']['launch_id'],
                       'MV2_TMUX_SOCKET': '/must-not-use-cached.sock'}
        self.assertEqual(agent_identity.calling_socket(environment), self.socket)
        with self.assertRaisesRegex(ValueError, 'Outdated launch'):
            agent_identity.calling_socket({**environment, 'MV2_LAUNCH_ID': 'old'})

    def test_bad_socket_or_agent_proof_never_falls_back_to_the_default_server(self):
        parent, directory, environment = self.stable_parent()
        facts = read(directory / 'runtime.json')
        for change in ({'MV2_IDENTITY_MODE': ''}, {'MV2_AGENT_DIR': str(self.work)},
                       {'CODEX_THREAD_ID': str(uuid4())}, {'CODEX_HOME': str(self.work / 'other')}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                agent_identity.calling_socket({**environment, **change})
        try:
            for terminal in (None, [], {'socket': None}, {'socket': ''}, {'socket': 7}, {'socket': 'bad\0socket'}):
                write(directory / 'runtime.json', {**facts, 'tmux': terminal})
                with self.subTest(terminal=terminal), self.assertRaisesRegex(ValueError, 'no recorded tmux'):
                    agent_identity.calling_socket(environment)
        finally:
            write(directory / 'runtime.json', facts)

    def test_explicit_and_recovery_socket_selection_do_not_read_caller_identity(self):
        self.assertIsNone(agent_identity.calling_socket({}))
        self.assertEqual(agent_identity.calling_socket({'MV2_TMUX_SOCKET': self.socket}), self.socket)
        with patch.object(agent_identity, 'calling_socket', side_effect=AssertionError('caller lookup')) as lookup:
            result = lifecycle.start(self.root, 'explicit', '', 'fake', socket_path=self.socket)
            self.assertEqual(result['runtime']['tmux']['socket'], self.socket)
            with patch.object(lifecycle, '_record', side_effect=ValueError('record phase reached')):
                with self.assertRaisesRegex(ValueError, 'record phase reached'):
                    lifecycle.start(self.root, resume='saved', _recovery={'terminal': {'socket': self.socket}})
            lookup.assert_not_called()
