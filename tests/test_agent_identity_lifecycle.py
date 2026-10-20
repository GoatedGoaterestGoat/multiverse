"""An independent shared-service provider declares the same authority contract."""
from dataclasses import asdict, replace
from pathlib import Path
import os
import sys
import shutil
import unittest
from unittest.mock import Mock, patch

from multiverse.harnesses.types import AgentIdentitySpec, NativeServiceHandle, NativeEndpoint, ServiceIdentity
from multiverse.runtime import agent_identity, lifecycle, runner, rpc
from multiverse.store import read, write
from multiverse.universe import agents
from test_native_backends import NativeBackendTest


class IdentityLifecycleTest(NativeBackendTest):
    def stable_spawn(self):
        spec = AgentIdentitySpec('service-agent-v1', 'SERVICE_THREAD', ('TEST_NATIVE_HOME',))
        self.adapter.agent_identity_policy = lambda: spec
        original = self.adapter.command
        def command(context):
            result = original(context)
            service = replace(result.shared_service, identity=ServiceIdentity('service-test', {'TEST_NATIVE_HOME': str(self.adapter.home)}))
            return replace(result, shared_service=service, agent_identity=spec)
        self.adapter.command = command
        result = lifecycle.start(self.root, name='stable-service-worker', harness='service-test')
        self.addCleanup(shutil.rmtree, Path('/tmp') / ('mv2-native-' + result['runtime']['launch_id']), True)
        return result, agents.directory(self.root, result['id'])

    def test_declared_provider_gets_stable_context_but_runner_keeps_private_generation(self):
        """Resume refreshes MV2_CLI, MV2_PYTHON and PATH from the current install, also from a damaged saved
        environment, keeps the stable identity and never restores a removed checkout; fragile because it matters
        only after an upgrade or move, and a break points agents' mv2 at a deleted interpreter (02da069)."""
        result, directory = self.stable_spawn()
        first = read(directory / 'properties.json')
        self.assertEqual(first['agent_identity']['spec']['mode'], 'service-agent-v1')
        self.assertNotIn('MV2_LAUNCH_ID', first['agent_environment'])
        initial, final = [call[-1] for call in self.adapter.calls if call[0] == 'spawn'][-2:]
        self.assertEqual(initial.agent_environment, final.agent_environment)
        self.assertNotIn('MV2_TMUX_SOCKET', final.agent_environment)
        before = dict(first['agent_environment'])
        refreshed = {**before, 'MV2_CLI': '/another/installation/bin/mv2', 'MV2_PYTHON': sys.executable,
                     'PATH': '/another/installation/bin' + os.pathsep + os.environ.get('PATH', '')}
        removed = {'MV2_CLI': '/removed/checkout/bin/mv2', 'MV2_PYTHON': '/removed/venv/bin/python',
                   'PATH': '/removed/bin'}
        # A removed checkout/interpreter must not be restored into the new request.
        for saved in ({**before, **removed}, None, [], {'MV2_CLI': 7, 'MV2_PYTHON': None, 'PATH': []}):
            with self.subTest(saved=saved):
                write(directory / 'properties.json', {**first, 'agent_environment': saved})
                with patch.object(lifecycle, '_cli_path', return_value='/another/installation/bin/mv2'):
                    resumed = lifecycle.start(self.root, resume=result['id'])
                self.addCleanup(shutil.rmtree, Path('/tmp') / ('mv2-native-' + resumed['runtime']['launch_id']), True)
                after = read(directory / 'properties.json')
                self.assertEqual(after['agent_environment'], refreshed)
                self.assertEqual(after['agent_identity'], first['agent_identity'])
                request = read(directory / 'launches' / resumed['runtime']['launch_id'] / 'request.json')
                self.assertEqual(request['env']['MV2_CLI'], '/another/installation/bin/mv2')
                self.assertEqual(request['agent_environment'], after['agent_environment'])
                self.assertNotEqual(request['env']['MV2_LAUNCH_ID'], result['runtime']['launch_id'])
        # Executable locations are requested tool configuration, never actor proof.
        proof = {**refreshed, 'SERVICE_THREAD': first['native_session_id'], 'TEST_NATIVE_HOME': str(self.adapter.home)}
        self.assertIsNotNone(agent_identity.actor(proof))
        self.assertEqual(agent_identity.actor({**proof, **removed}), agent_identity.actor(proof))
        self.assertEqual(agent_identity.environment(request['env'], None, refreshed), {})

    def test_late_plain_fallback_removes_the_provisional_policy(self):
        """A runner falling back to plain drops the provisional stable-identity policy; fragile because the late
        fallback is rarely exercised, and a break makes all of that agent's callbacks fail identity could not be
        verified."""
        result, directory = self.stable_spawn()
        launch = directory / 'launches' / result['runtime']['launch_id']
        request = read(launch / 'request.json')
        from multiverse.harnesses.types import CommandSpec
        request['fallback'] = asdict(CommandSpec(('plain',)))
        write(launch / 'request.json', request)
        (launch / 'ready').touch()
        proxy = Mock()
        proxy.protocol.bound = False
        proxy.start.side_effect = RuntimeError('unavailable relay')
        process = Mock(pid=123)
        process.poll.return_value = process.wait.return_value = 0
        with patch.object(runner.sys, 'argv', ['runner', str(launch)]), \
             patch.object(runner, 'process_started', return_value=None), \
             patch.object(rpc, 'LaunchProxy', return_value=proxy), \
             patch.object(runner.subprocess, 'Popen', return_value=process), \
             patch('multiverse.terminal.tmux.owned', return_value=(self.backend, '%1')), \
             patch.object(runner, 'launch_diagnostic', return_value={}):
            self.assertEqual(runner.main(), 0)
        props = read(directory / 'properties.json')
        self.assertNotIn('agent_identity', props)
        self.assertNotIn('agent_environment', props)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(IdentityLifecycleTest(name) for name in IdentityLifecycleTest.__dict__ if name.startswith('test_'))
