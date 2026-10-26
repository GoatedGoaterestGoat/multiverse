"""Native ownership refusal propagation, using only disposable Codex servers."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch

from websockets.sync.client import unix_connect

from native_safety import isolated_home
from native_processes import PrivateProcessGroup
from multiverse import harnesses
from multiverse.runtime import lifecycle, native
from multiverse.store import write
from multiverse.tools import inspect
from multiverse.universe import agents
from multiverse.universe.mailbox import append_message, message_record
from test_multiverse import TerminalBase, until


@unittest.skipUnless(shutil.which('codex') and shutil.which('tmux'), 'Codex and tmux required')
class NativeRefusalTest(TerminalBase):
    def test_separate_writer_refuses_plain_and_bridge_until_new_resume(self):
        """On live Codex the real active-writer and open-in-another-app refusals reach resume --wait, list, inspect,
        delivery and wait_ready, attachment-only kill spares the other writer, and only a fresh resume clears them;
        fragile because it depends on Codex's exact refusal strings and screen, and a miss leaves mv2 saying ready while
        messages go nowhere."""
        if subprocess.check_output(['codex', '--version'], text=True).strip() != 'codex-cli 0.164.0':
            self.skipTest('native ownership widget evidence pinned to 0.164.0')
        home = isolated_home(self.work / 'native-home', config=
            'model="fixture"\nmodel_provider="fixture"\n'
            '[model_providers.fixture]\nname="Private fixture"\n'
            'base_url="http://127.0.0.1:9/v1"\nwire_api="responses"\nrequires_openai_auth=false\n'
            f'[projects.{json.dumps(str(self.work))}]\ntrust_level="trusted"\n')
        managed = home / 'packages/standalone/current/codex'
        managed.parent.mkdir(parents=True)
        managed.symlink_to(Path(shutil.which('codex')).resolve())
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith('MV2_') and key not in
                       ('CODEX_HOME', 'CODEX_SQLITE_HOME', 'CODEX_THREAD_ID', 'TMUX', 'TMUX_PANE')}
        environment['CODEX_HOME'] = str(home)
        owner_socket = self.work / 'owner.sock'
        owner = subprocess.Popen(['codex', 'app-server', '--listen', 'unix://' + str(owner_socket)],
            cwd=self.work, env=environment, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        owner_group = PrivateProcessGroup(owner.pid)
        owner_closed = False
        def stop_owner():
            nonlocal owner_closed
            if owner_closed:
                return  # Explicit writer release is followed by unittest cleanup.
            owner_group.close(reap=owner.poll)
            owner.wait(timeout=5)
            owner_closed = True
            owner.stdin.close()
        self.addCleanup(stop_owner)
        daemon_group = None
        def stop_private_daemon():
            try:
                subprocess.run(['codex', 'app-server', 'daemon', 'stop'], env=environment,
                               capture_output=True, timeout=30, check=True)
            finally:
                if daemon_group is not None:
                    daemon_group.close()
        self.addCleanup(stop_private_daemon)
        # Start only this home's daemon explicitly so cleanup knows its exact
        # group before mv2 reuses it. Native stop alone leaves clone helpers alive.
        started = subprocess.run(['codex', 'app-server', 'daemon', 'start'], env=environment,
                                 capture_output=True, text=True, timeout=30, check=True)
        daemon_group = PrivateProcessGroup(json.loads(started.stdout)['pid'])
        until(owner_socket.exists)
        client = self.enterContext(unix_connect(str(owner_socket)))
        identifier = 0
        def request(method, params):
            nonlocal identifier
            identifier += 1
            client.send(json.dumps({'id': identifier, 'method': method, 'params': params}))
            while True:
                value = json.loads(client.recv(timeout=10))
                if value.get('id') == identifier:
                    self.assertNotIn('error', value, value)
                    return value['result']
        request('initialize', {'clientInfo': {'name': 'mv2-refusal-fixture', 'version': '1'},
                              'capabilities': {'experimentalApi': True}})
        client.send(json.dumps({'method': 'initialized', 'params': {}}))
        thread = request('thread/start', {'cwd': str(self.work), 'ephemeral': False})['thread']['id']
        request('thread/name/set', {'threadId': thread, 'name': 'private refusal fixture'})
        until(lambda: any(home.glob('sessions/**/rollout-*' + thread + '.jsonl')))
        directory, properties, _, _ = lifecycle._record(self.root, 'refused', '', 'codex', None, None)
        # This externally created conversation represents a current launch, whose
        # storage snapshot proves absence as well as the selected home.
        properties.update(workspace=str(self.work), native_session_id=thread,
                          native_environment={'CODEX_HOME': str(home)},
                          native_storage_environment={'CODEX_HOME': str(home), 'CODEX_SQLITE_HOME': None,
                                                      'CLAUDE_CONFIG_DIR': environment.get('CLAUDE_CONFIG_DIR')})
        write(directory / 'properties.json', properties)
        configuration = self.root / 'harnesses/codex/harness.json'
        with patch.dict(os.environ, environment, clear=True):
            for bridge in (True, False):
                write(configuration, {'schema': 2, 'adapter': 'codex', 'settings': {
                    'inbox_bridge': bridge, 'resume_args': [] if bridge else ['--no-daemon']}})
                launched = lifecycle.start(self.root, resume=properties['id'], socket_path=self.socket,
                                           wait=not bridge, wait_timeout=20)
                if not bridge:
                    # Ask --wait immediately, before the test has observed a
                    # refusal. Waiting for its record first masked this race.
                    self.assertFalse(launched['ready'], launched)
                    self.assertIn('This conversation is open in another app', launched['error'])
                try:
                    until(lambda: agents.runtime(directory).get('native_refusal'), seconds=15)
                except AssertionError:
                    facts = agents.runtime(directory)
                    print('Refusal fixture runtime:', facts)
                    print('Refusal fixture screen:', self.backend.run('capture-pane', '-p', '-t', facts['tmux']['pane']))
                    print('Refusal fixture cursor:', self.backend.run('display-message', '-p', '-t', facts['tmux']['pane'], '#{cursor_flag}'))
                    raise
                facts = agents.runtime(directory)
                message = facts['native_refusal']['message']
                self.assertIn('active writer' if bridge else 'open in another app', message)
                self.assertEqual(lifecycle.listing(self.root)[0]['launch_error'], message)
                self.assertEqual(inspect.cli_run(self.root, agent=properties['id'], json=True)['error'], message)
                pending = append_message(directory, 'must not reach the writer', {},
                                         delivery='steer', target_session=thread)
                delivered = native.deliver(self.root, properties['id'], pending['id'])
                self.assertFalse(delivered['ok'])
                self.assertEqual(delivered['reason'], message)
                self.assertEqual(message_record(directory, pending['id'])[1]['submission_state'], 'failed')
                waited = lifecycle.wait_ready(self.root, directory, facts['launch_id'],
                    harnesses.load(self.root, 'codex'), 2)
                self.assertFalse(waited['ready'])
                self.assertEqual(waited['error'], message)
                # A new resume asks native again; closing this frontend does not
                # interrupt or unsubscribe the writer owned by another client.
                if bridge:
                    with patch.object(native, 'stop', side_effect=AssertionError('must not cancel native work')):
                        lifecycle.kill(self.root, properties['id'], attachment_only=True)
                self.assertEqual(request('thread/read', {'threadId': thread, 'includeTurns': False})['thread']['id'], thread)
                self.assertEqual(agents.runtime(directory)['reason'], message)
            # The user's other private test server closes its own conversation.
            # R can now succeed in the plain native frontend. That cannot clear
            # mv2's error: only a fresh launch can validate its attachment again.
            client.close()
            stop_owner()
            pane = agents.runtime(directory)['tmux']['pane']
            self.backend.run('send-keys', '-t', pane, 'r')
            def native_retried():
                screen = self.backend.run('capture-pane', '-p', '-t', pane)
                return '›' in screen and 'Close it there and press R' not in screen
            until(native_retried, seconds=15)
            self.assertEqual(agents.runtime(directory)['reason'], message)
            with patch.object(native, 'stop', side_effect=AssertionError('must not cancel native work')):
                lifecycle.kill(self.root, properties['id'], attachment_only=True)
            # A fresh mv2 bridge resume now validates and clears the old error.
            write(configuration, {'schema': 2, 'adapter': 'codex', 'settings': {'inbox_bridge': True}})
            result = lifecycle.start(self.root, resume=properties['id'], socket_path=self.socket,
                                     wait=True, wait_timeout=20)
            self.assertTrue(result['ready'], result)
            self.assertNotIn('native_refusal', result['runtime'])
            lifecycle.kill(self.root, properties['id'], attachment_only=True)
