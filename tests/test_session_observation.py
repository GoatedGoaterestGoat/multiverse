"""A harness's observation of its native session rebinds an agent only on exact evidence from its current launch."""
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import Mock
from uuid import uuid4

from multiverse import harnesses
from multiverse.harnesses.types import NativeResult, CommandSpec, ObservedBinding
from multiverse.runtime import attachments, lifecycle, native
from multiverse.store import read, write, lock
from multiverse.universe import agents
from multiverse.tools import send
from multiverse.universe.mailbox import inbox
from test_multiverse import BaseTest


class SessionObservationTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.identity, self.old = str(uuid4()), str(uuid4())
        self.directory = self.root / 'state' / 'agents' / self.identity
        self.properties = {'id': self.identity, 'name': 'worker', 'harness': 'pi', 'workspace': str(self.work),
                           'native_session_id': self.old, 'native_session_home': str(self.work),
                           'native_session_provider': 'fixture', 'universe_id': read(self.root / 'universe.json')['id'],
                           'task': ''}
        self.launch = str(uuid4())
        self.facts = {'launch_id': self.launch, 'state': 'running', 'pid': 123,
                      'tmux': {'socket': str(self.work / 'absent.sock'), 'session': '$1', 'pane': '%1'}}
        write(self.directory / 'properties.json', self.properties)
        write(self.directory / 'runtime.json', self.facts)

    def observing(self, target, **hooks):
        """An adapter whose process evidence shows target, and whose usage names the session it measured."""
        return SimpleNamespace(**{'observed_binding': lambda session: ObservedBinding(
            native_id=target, home=str(self.work), evidence='fixture-process-file'),
            'get_usage': lambda session: NativeResult('ok', value={'native_id': session.native_id}), **hooks})

    def test_an_exact_observation_rebinds_before_use(self):
        """An exact observation of a native /new or /clear rebinds before usage is measured or a message is targeted;
        a break goes unnoticed because the first message after the change goes to the old conversation."""
        target = str(uuid4())
        with patch.object(harnesses, 'load', return_value=self.observing(target)):
            result = native.usage_snapshot(self.root, self.properties, self.facts)
        self.assertEqual(result['value']['native_id'], target)
        self.assertEqual(read(self.directory / 'properties.json')['native_session_id'], target)
        self.assertIn('"reason": "detected"', (self.directory / 'sessions.jsonl').read_text())
        after = str(uuid4())
        with patch.object(harnesses, 'load', return_value=self.observing(after)), \
                patch.object(native, 'send_or_wake', return_value={'state': 'native_queued'}):
            result = send.run(self.root, agent=self.identity, text='after clear', sender={'name': 'user'})
        self.assertTrue(result['ok'])
        message, = inbox(self.directory, all_messages=True)['messages']
        self.assertEqual(message['target_session'], after)

    def test_rebind_needs_exact_current_launch_evidence(self):
        """No rebind and no measurement happen unless the evidence is exact, from the current launch, with a matching
        terminal and a free lock; fragile because observers read process files a reused process can supply, and a
        hijacked binding is invisible (02569a6, 97a84f8)."""
        for observed in (None, {'native_id': str(uuid4())}):
            with self.subTest(observed=observed):
                adapter = self.observing(None, observed_binding=lambda session: observed)
                with patch.object(harnesses, 'load', return_value=adapter):
                    result = native.usage_snapshot(self.root, self.properties, self.facts)
                self.assertEqual(result['state'], 'ok' if observed is None else 'error')
                self.assertEqual(read(self.directory / 'properties.json')['native_session_id'], self.old)
        with self.subTest('a replaced launch'):
            write(self.directory / 'runtime.json', {**self.facts, 'launch_id': 'new'})
            with patch.object(harnesses, 'load', return_value=self.observing(str(uuid4()))):
                result = native.usage_snapshot(self.root, self.properties, self.facts)
            self.assertEqual(result['state'], 'error')
            self.assertIn('Launch changed', result['reason'])
            write(self.directory / 'runtime.json', self.facts)
        with self.subTest('a launch busy with a change'):
            measured = Mock()
            adapter = self.observing(str(uuid4()), get_usage=measured)
            with lock(self.directory / '.launch.lock'), patch.object(harnesses, 'load', return_value=adapter):
                result = native.usage_snapshot(self.root, self.properties, self.facts)
            self.assertEqual(result['state'], 'error')
            self.assertIn('updating; retry', result['reason'])
            measured.assert_not_called()
        for error in (ValueError('damaged native evidence'), ValueError()):
            with self.subTest('a failed observation while listing', error=error):
                observed, measured = Mock(side_effect=error), Mock()
                adapter = SimpleNamespace(observed_binding=observed, get_usage=measured)
                with patch.object(harnesses, 'load', return_value=adapter), \
                        patch.object(lifecycle, 'observe', return_value={'live': 'alive'}):
                    [row] = lifecycle.listing(self.root)
                self.assertEqual(row['usage']['state'], 'error')
                if str(error):
                    self.assertEqual(row['usage']['reason'], str(error))
                observed.assert_called_once()
                measured.assert_not_called()
        with self.subTest('a send whose terminal does not match the launch'):
            token = 'Sat Oct 17 12:00:00 2026'
            self.facts.update(pid_started=token)
            write(self.directory / 'runtime.json', self.facts)
            observed = []
            def observer(session):
                observed.append(session.native_id)
                return ObservedBinding(native_id=str(uuid4()), home=str(self.work), evidence='fixture-process-file')
            absent = {'live': 'dormant', 'reason': 'Recorded pane is absent'}
            with patch.object(harnesses, 'load', return_value=SimpleNamespace(observed_binding=observer)), \
                    patch.object(native, 'observe', return_value=absent), \
                    patch.object(attachments, '_observation', return_value=(False, (token, 'S'))):
                send.run(self.root, agent=self.identity, text='must remain queued on the primary',
                         sender={'name': 'user'})
            self.assertEqual(observed, [], 'public send must detect runtime mismatch before native identity '
                                           'reconciliation')
        self.assertEqual(read(self.directory / 'properties.json')['native_session_id'], self.old)
        self.assertFalse((self.directory / 'sessions.jsonl').exists())

    def test_resume_rereads_binding_after_acquiring_launch_lock(self):
        """Resume uses the binding read under the launch lock; fragile because the read-before-lock window is tiny, and
        the wrong read resumes an old conversation silently."""
        target = str(uuid4())
        stale = dict(self.properties)
        agents.rebind_session(self.root, self.identity, self.old, target, 'clear', self.launch, home=str(self.work))
        seen = []
        def resume(session, context):
            seen.append(session.native_id)
            return CommandSpec(('/fake/program',))
        adapter = SimpleNamespace(name='pi', family='fixture', session_mode='assigned', default_model=None,
                                  bootstrap='', resume=resume)
        terminal = Mock()
        terminal.create.return_value = {'socket': '/fake', 'session': '$1', 'pane': '%1'}
        with patch.object(lifecycle, '_record', return_value=(self.directory, stale, 'pi', None)), \
                patch.object(harnesses, 'load', return_value=adapter), \
                patch.object(lifecycle.shutil, 'which', return_value='/fake/program'), \
                patch.object(lifecycle, 'observe', return_value={'live': 'dormant'}), \
                patch.object(lifecycle, 'Tmux', return_value=terminal):
            result = lifecycle.start(self.root, resume=self.identity)
        self.assertEqual(seen, [target])
        self.assertEqual(result['native_session_id'], target)
        self.assertEqual(read(self.directory / 'properties.json')['native_session_id'], target)
