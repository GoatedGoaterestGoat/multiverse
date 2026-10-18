"""Operation contract: an unknown provider works without edits to tools or UI."""
from pathlib import Path
import sys
from unittest.mock import Mock, patch
from uuid import uuid4

from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse import harnesses
from multiverse.runtime import native
from multiverse.cli import parser
from multiverse.ui.backend import LocalBackend
from multiverse.harnesses.types import CAPABILITIES, Capability, CommandSpec, NativeResult, SessionHandle
from multiverse.universe.mailbox import inbox
from multiverse.store import read, write
from multiverse.tools import ask, deliver, get_usage, send, spawn
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

    def test_spawn_resume_return_exact_handles_without_requiring_a_task(self):
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

    def test_rejected_resume_preserves_saved_state_and_creates_no_launch(self):
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

    def test_binding_normalizes_uuid_without_an_adapter_validation_hook(self):
        bound = agents.bind_session(self.root, self.agent, self.agent.upper())
        self.assertEqual(bound['native_session_id'], self.agent)
        for invalid in ('not-a-uuid', None, 123):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, 'must be a UUID'):
                agents.bind_session(self.root, self.agent, invalid)
        self.assertEqual(read(self.directory / 'properties.json'), bound)

    def test_one_send_persists_then_routes_both_modes_and_literal_identity(self):
        for mode in ('steer', 'follow-up'):
            result = send.run(self.root, agent=self.agent, text='literal $(false)', delivery=mode,
                              sender={'agent_id': 'sender'})
            self.assertTrue(result['saved'])
            self.assertEqual(result['state'], 'native_queued')
            _, session, message, actual_mode = self.adapter.calls[-1]
            self.assertIsInstance(session, SessionHandle)
            self.assertEqual(session.native_id, self.agent)
            self.assertNotIn('id', result)
            self.assertEqual(message.id, inbox(self.directory, all_messages=True)['messages'][-1]['id'])
            self.assertFalse(hasattr(message, 'reply_to'))
            self.assertEqual(message.sender, {'agent_id': 'sender'})
            self.assertEqual(message.text, 'literal $(false)')
            self.assertEqual(actual_mode, mode)
        self.assertEqual(len(inbox(self.directory, all_messages=True)['messages']), 2)  # Acceptance preserves history.

    def test_queue_only_failure_and_retry_do_not_lose_or_duplicate_records(self):
        queued = send.run(self.root, agent=self.agent, text='later', queue_only=True)
        self.assertEqual(queued['state'], 'queued')
        self.assertEqual(len(self.adapter.calls), 1)  # only spawn
        self.adapter.failure = 'connection lost'
        failed = send.run(self.root, agent=self.agent, text='save despite failure')
        self.assertEqual(failed['state'], 'unknown')
        self.assertTrue(failed['saved'])
        self.assertFalse(failed['ok'])
        failed_id = inbox(self.directory, all_messages=True)['messages'][-1]['id']
        calls = len(self.adapter.calls)
        receipt = native.deliver(self.root, self.agent, failed_id)
        self.assertTrue(receipt['reused'])
        self.assertEqual(len(self.adapter.calls), calls)
        self.assertEqual(len(inbox(self.directory, all_messages=True)['messages']), 2)
        self.adapter.failure = None
        retried = native.deliver(self.root, self.agent, failed_id, retry=True)
        self.assertEqual(retried['state'], 'unknown')
        self.assertEqual(retried['message_id'], failed_id)
        self.assertEqual(len(inbox(self.directory, all_messages=True)['messages']), 2)

    def test_backend_delivery_confirms_once_and_tools_never_resubmit(self):
        message = send.run(self.root, agent=self.agent, text='Check my receipt', delivery='follow-up')
        backend = LocalBackend()
        checked = backend.deliver(self.root, self.agent, check=True)
        self.assertTrue(checked['ok'])
        self.assertEqual(len(checked['messages']), 1)
        self.assertIs(checked['messages'][0]['transcript_confirmed'], True)
        self.assertEqual(deliver.run(self.root, agent=self.agent, check=True), {'messages': [], 'ok': True})
        self.assertEqual(len([call for call in self.adapter.calls if call[0] == 'get_delivery']), 1)
        self.assertEqual(backend.deliver(self.root, self.agent, retry=True),
                         deliver.run(self.root, agent=self.agent, retry=True))
        self.assertEqual(len([call for call in self.adapter.calls if call[0] == 'send']), 1)

    def test_ask_usage_share_tools_without_main_conversation_mutation(self):
        properties = read(self.directory / 'properties.json')
        question = 'What about this edge case?'
        self.assertEqual(ask.run(self.root, agent=self.agent, question=question),
                         LocalBackend().ask(self.root, self.agent, question))
        self.assertEqual(get_usage.run(self.root, agent=self.agent), LocalBackend().get_usage(self.root, self.agent))
        self.assertEqual(read(self.directory / 'properties.json'), properties)
        self.assertEqual(inbox(self.directory)['messages'], [])
        self.assertFalse((self.directory / 'native-delivery.json').exists())
        # The base template leaves the compaction plugin (which adds a capability) off.
        self.assertEqual(set(LocalBackend().capabilities(self.root, self.agent)), set(CAPABILITIES))

    def test_cli_optional_prompt_and_explicit_queue_controls(self):
        args = parser().parse_args(['spawn', '--name', 'blank'])
        self.assertIsNone(args.task)
        self.assertEqual(parser().parse_args(['spawn', '--name', 'named', '--prompt', 'hello']).task, 'hello')
        args = parser().parse_args(['send', self.agent, '--text', 'next', '--delivery', 'follow-up'])
        self.assertFalse(args.queue_only)
        self.assertEqual(args.delivery, 'follow-up')
