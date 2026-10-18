"""Operation contract: an unknown provider works without edits to tools or UI."""
import sys
from unittest.mock import Mock, patch

from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse import harnesses
from multiverse.runtime import native
from multiverse.ui.backend import LocalBackend
from multiverse.harnesses.types import CAPABILITIES, Capability, CommandSpec, NativeResult, SessionHandle
from multiverse.tools import spawn
from test_multiverse import BaseTest


class FourthAdapter:
    """Implements only the public contract, without the bundled rendering base."""
    name = 'fourth'
    session_mode = 'assigned'
    default_model = None
    bootstrap = ''

    def __init__(self):
        self.calls = []
        self.failure = None

    def spawn(self, context):
        self.calls.append(('spawn', context))
        return CommandSpec((sys.executable, '-c', 'pass'))

    def resume(self, session, context):
        if not session.native_id:
            raise ValueError('Missing saved native session')
        self.calls.append(('resume', session, context))
        return CommandSpec((sys.executable, '-c', 'pass'))

    def stop(self, session, executor):
        self.calls.append(('stop', session))
        return NativeResult('ok')

    def capabilities(self, session, executor=None):
        return {key: Capability('supported') for key in CAPABILITIES}

    def send(self, session, message, executor):
        self.calls.append(('send', session, message, message.delivery))
        if self.failure:
            raise RuntimeError(self.failure)
        return NativeResult('native_queued', value={'message_id': message.id})

    def get_delivery(self, session, message_id, executor):
        self.calls.append(('get_delivery', session, message_id))
        return NativeResult('native_queued', value={'message_id': message_id, 'transcript_confirmed': True})

    def ask(self, session, question, executor):
        self.calls.append(('ask', session, question))
        return NativeResult('ok', value={'answer': question, 'native_session_id': session.native_id})

    def get_usage(self, session, executor=None):
        self.calls.append(('get_usage', session))
        return NativeResult('ok', value={'native_session_id': session.native_id, 'total_tokens': 0})


class OperationTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.adapter = FourthAdapter()
        self.enterContext(patch.object(native, 'owned'))
        self.enterContext(patch.object(native, 'observe', return_value={'live': 'alive'}))
        self.loader = self.enterContext(patch.object(harnesses, 'load', return_value=self.adapter))
        self.enterContext(patch.object(lifecycle, 'observe', return_value={'live': 'dormant'}))
        backend = Mock()
        backend.create.return_value = {'socket': '/fake', 'session': '$1', 'pane': '%1'}
        self.enterContext(patch.object(lifecycle, 'Tmux', return_value=backend))
        self.enterContext(patch.object(lifecycle.shutil, 'which', return_value='/fake/binary'))
        self.first = spawn.run(self.root, name='fourth-worker', harness='fourth')
        self.agent = self.first['id']
        self.directory = agents.directory(self.root, self.agent)

    def test_a_third_party_adapter_spawns_and_resumes_through_tools(self):
        """An adapter implementing only the public protocol spawns without a task, resumes with the same native id and a
        new launch, and a rejected resume writes nothing; fragile because every bundled adapter inherits the rendering
        base, so a tool calling a base-only method passes every other test."""
        properties = (self.directory / 'properties.json').read_bytes()
        runtime = (self.directory / 'runtime.json').read_bytes()
        launches = list((self.directory / 'launches').iterdir())
        with patch.object(self.adapter, 'resume', side_effect=ValueError('Invalid saved session')), \
                patch.object(lifecycle, 'Tmux') as terminal:
            with self.assertRaisesRegex(ValueError, 'Invalid saved session'):
                LocalBackend().resume(self.root, self.agent)
            terminal.assert_not_called()
        self.assertEqual((self.directory / 'properties.json').read_bytes(), properties)
        self.assertEqual((self.directory / 'runtime.json').read_bytes(), runtime)
        self.assertEqual(list((self.directory / 'launches').iterdir()), launches)
        first = self.first['session']
        self.assertEqual(first['native_id'], self.agent)
        self.assertEqual(self.first['task'], '')
        self.assertEqual(self.first['summary'], '')
        resumed = LocalBackend().resume(self.root, self.agent)['session']
        for key in ('agent_id', 'native_id', 'workspace', 'session_file'):
            self.assertEqual(resumed[key], first[key])
        self.assertNotEqual(resumed['launch_id'], first['launch_id'])
        self.assertEqual([call[0] for call in self.adapter.calls], ['spawn', 'resume'])
        target = self.adapter.calls[-1][1]
        self.assertIsInstance(target, SessionHandle)
        self.assertEqual(target.native_id, first['native_id'])
        self.assertEqual(target.workspace, first['workspace'])
        self.assertEqual(target.session_file, first['session_file'])
        self.assertEqual(target.launch_id, first['launch_id'])
