"""The sandbox-to-runner binding handoff keeps the ordinary claim authority."""
import errno
import os
from unittest.mock import patch

from multiverse.runtime import binding_requests
from multiverse.store import read, uid, write
from multiverse.tools import session_bind
from multiverse.universe import agents, sessions
from test_multiverse import BaseTest


class BindingRequestsTest(BaseTest):
    def test_runner_claims_are_exact_and_never_recreate_removed_records(self):
        """A sandbox callback cannot bypass session ownership or generation checks, or revive removed records, through its writable request file."""
        foreign = uid()
        peer = self.root / 'state' / 'agents' / uid()
        write(peer / 'properties.json', {'id': peer.name, 'name': 'holder', 'harness': 'codex',
              'native_session_provider': 'codex', 'native_session_home': str(self.work),
              'native_session_id': foreign})
        for case in ('success', 'foreign session', 'wrong launch', 'wrong agent', 'bridge', 'child exited',
                     'replaced', 'ending', 'removed', 'other permission', 'operator'):
            with self.subTest(case=case):
                directory = self.root / 'state' / 'agents' / uid()
                launch = directory / 'launches' / uid()
                launch.mkdir(parents=True)
                (directory / '.launch.lock').touch()
                props = {'id': directory.name, 'name': case.replace(' ', '-'), 'harness': 'codex',
                         'workspace': str(self.work), 'universe_id': read(self.root / 'universe.json')['id'],
                         'native_session_provider': 'codex', 'native_session_home': str(self.work)}
                facts = {'launch_id': launch.name, 'state': 'running', 'pid': os.getpid(),
                         'tmux': {'socket': str(self.work / 'absent.sock'), 'session': '$1', 'pane': '%1'}}
                write(directory / 'properties.json', props)
                write(directory / 'runtime.json', facts)
                target = foreign if case == 'foreign session' else uid()
                request = {'id': uid(), 'agent_id': directory.name, 'launch_id': launch.name,
                           'native_session_id': target}
                if case == 'wrong launch':
                    request['launch_id'] = uid()
                if case == 'wrong agent':
                    request['agent_id'] = peer.name
                if case == 'bridge':
                    write(directory / 'runtime.json', {**facts, 'control_endpoint': {'address': 'bridge'}})
                if case == 'replaced':
                    write(directory / 'runtime.json', {**facts, 'launch_id': uid()})
                if case == 'ending':
                    write(directory / 'runtime.json', {**facts, 'ending_at': 'now'})
                worker = binding_requests.BindingRequests(self.root, launch, lambda _: case != 'child exited')
                write(launch / 'bind-request.json', request)
                if case == 'removed':
                    trash = self.work / uid()
                    directory.rename(trash)
                    with self.assertRaises(FileNotFoundError):
                        worker.check()
                    self.assertFalse(directory.exists())
                    self.assertEqual(read(trash / 'properties.json'), props)
                    continue
                if case in ('other permission', 'operator'):
                    environment = {} if case == 'operator' else {
                        'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': directory.name,
                        'MV2_AGENT_DIR': str(directory), 'MV2_LAUNCH_ID': launch.name}
                    denied_path = (self.root / 'state/.sessions.lock' if case == 'operator'
                                   else directory / 'properties.json')
                    with patch.dict(os.environ, environment, clear=True), patch.object(
                            sessions, 'namespace', side_effect=PermissionError(errno.EPERM, 'denied', str(denied_path))), \
                            patch.object(binding_requests, 'request_binding') as fallback, self.assertRaises(PermissionError):
                        session_bind.run(self.root, agent=directory.name, session_id=target)
                    fallback.assert_not_called()
                    self.assertEqual(read(directory / 'properties.json'), props)
                    continue
                worker.check()
                response = read(launch / 'bind-response.json')
                self.assertEqual(response['id'], request['id'])
                self.assertEqual(response['ok'], case == 'success')
                if case == 'success':
                    self.assertEqual(read(directory / 'properties.json')['native_session_id'], target)
                    before = (directory / 'properties.json').read_bytes()
                    worker.check()  # The same request never writes twice.
                    self.assertEqual((directory / 'properties.json').read_bytes(), before)
                else:
                    self.assertTrue(response['error'])
                    self.assertEqual(read(directory / 'properties.json'), props)
