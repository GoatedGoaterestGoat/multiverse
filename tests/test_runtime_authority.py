"""A damaged runtime identity cannot hide a running launch."""
from copy import deepcopy
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from isolation import isolate, isolated_environment, track
import home_safety
from multiverse import harnesses
from multiverse.runtime import lifecycle, missing_runtime, native
from multiverse.store import read, write, uid
from multiverse.terminal.tmux import Tmux
from multiverse.tools import doctor, send
from multiverse.universe import records, relocate, templates
from test_multiverse import BaseTest, TerminalBase, until


@unittest.skipUnless(shutil.which("tmux"), "tmux is required for terminal integration")
class DamagedRuntimeTest(unittest.TestCase):
    """One private tmux server and one pair of agents for the whole class: `worker`, in the lab subverse, is the agent
    whose runtime.json each test damages, and `other`, in the universe, must never be touched. Each test starts from
    a live worker, resuming it if an earlier test left it stopped, and leaves its runtime.json healthy."""

    @classmethod
    def setUpClass(cls):
        home_safety.install()
        temp = tempfile.TemporaryDirectory(prefix='mv2-test-', dir='/tmp')
        cls.addClassCleanup(temp.cleanup)
        cls.work = Path(temp.name).resolve()
        (cls.work / 'home').mkdir()
        # TMUX_TMPDIR: discovery also asks the default server, which is then this class's own (absent) one.
        cls.enterClassContext(patch.dict(os.environ, {**isolated_environment(cls.work / 'home'),
                                                      'TMUX_TMPDIR': str(cls.work)}, clear=True))
        cls.root = templates.create(cls.work / '.multiverse', 'test', workdir=cls.work)
        cls.lab = templates.create(cls.root / 'subverses/lab', 'lab', workdir=cls.work)
        cls.review = templates.create(cls.root / 'subverses/review', 'review', workdir=cls.work)
        cls.socket = str(cls.work / 'tmux.sock')
        cls.backend = Tmux(cls.socket)
        track(tmux_socket=cls.socket)
        cls.addClassCleanup(TerminalBase.kill_server, cls)
        argv = ['/bin/sh', '-c', 'exec sleep 600']  # a wake's prompt arguments only fill $0 and $1
        for scope in (cls.root, cls.lab, cls.review):
            write(scope / 'harnesses/fake/harness.json', {'schema': 1, 'spawn': argv, 'resume': argv})
        cls.other = lifecycle.start(cls.root, 'other', '', 'fake', socket_path=cls.socket)  # ready while worker starts
        worker = lifecycle.start(cls.lab, 'worker', '', 'fake', socket_path=cls.socket, wait=True, wait_timeout=15)
        if not worker['ready']:
            raise RuntimeError(f'the class agent did not start: {worker}')
        cls.worker_id = worker['id']
        cls.directory = cls.lab / 'state/agents' / cls.worker_id
        cls.path = cls.directory / 'runtime.json'

    def setUp(self):
        isolate(self)  # keeps the class's HOME

    def tagged(self, agent):
        panes, error = self.backend.snapshot()
        self.assertIsNone(error)
        return [pane for pane in panes.values() if pane.get('@mv2_agent_id') == agent]

    def live(self):
        """The worker's runtime facts, resumed first if an earlier test left it stopped."""
        if lifecycle.listing(self.lab, only=self.worker_id)[0]['live'] != 'alive':
            self.resume()
        facts = read(self.path)
        self.assertTrue(facts.get('pid_started'), facts)
        return facts

    def resume(self):
        resumed = lifecycle.start(self.lab, resume='worker', socket_path=self.socket, wait=True, wait_timeout=15)
        self.assertTrue(resumed['ready'], resumed)
        return resumed

    def test_a_live_agent_with_a_damaged_runtime_refuses_every_operation(self):
        """For each kind of runtime.json damage on a live agent (missing, null or invalid launch_id, state or tmux,
        bad pid or start token, a missing file, a valid-looking wrong pane or socket) resume refuses naming the file
        and how to recover, and for one of each kind wake, move and remove refuse too, keep its bytes and reconcile
        nothing; fragile because each operation has its own path to the shared check, and a missed one starts a
        second process for one conversation (abac381, 1eb2abc, 02569a6, LC-17)."""
        original = self.live()
        self.addCleanup(write, self.path, original)
        missing = object()

        def damaged(field, value):
            if field is None:
                return None  # no runtime.json at all
            facts = deepcopy(original)
            container, key = (facts['tmux'], field.split('.')[1]) if '.' in field else (facts, field)
            if value is missing:
                del container[key]
            else:
                container[key] = value
            return facts
        every = [('launch_id', None), ('launch_id', missing), ('launch_id', 'not-a-uuid'), ('tmux', missing),
                 ('tmux', {'pane': None}), ('tmux.pane', None), ('pid_started', 'invalid'), (None, None),
                 ('tmux.pane', '%999'), ('tmux.socket', str(self.work / 'absent.sock'))]
        cases = [('launch_id', value) for value in ('', [], 42)]
        cases += [('state', value) for value in (missing, None, [], 'invalid')]
        cases += [('tmux', value) for value in (None, [], {})]
        cases += [('tmux.socket', value) for value in (missing, '', [], 42, 'relative.sock')]
        cases += [(field, value) for field in ('tmux.pane', 'tmux.session')
                  for value in (missing, None, '', [], 42, 'wrong-format') if (field, value) != ('tmux.pane', None)]
        cases += [('pid', True), ('runner_pid', []), ('pid_started', []), ('exit_code', '0')]
        operations = [('remove agent', lambda: relocate.remove_agent(self.lab, 'worker')),
                      ('move agent', lambda: relocate.move_agent(self.lab, 'worker', self.review)),
                      ('remove subverse', lambda: relocate.remove_subverse(self.lab)),
                      ('move subverse', lambda: relocate.move_subverse(self.lab, self.review))]
        for field, value in every + cases:
            facts = damaged(field, value)
            if facts is None:
                self.path.unlink()
            else:
                write(self.path, facts)
            before = self.path.read_bytes() if self.path.exists() else None
            cause = 'inconsistent' if value in ('%999', str(self.work / 'absent.sock')) else 'running terminal'
            label = 'missing file' if field is None else f'{field}={"missing" if value is missing else value!r}'
            checks = [('resume', lambda: lifecycle.start(self.lab, resume='worker', socket_path=self.socket))]
            if (field, value) in every:
                checks += operations + [('wake', None)]
            for operation, call in checks:
                with self.subTest(damage=label, operation=operation), \
                        patch.object(native, 'session_snapshot', side_effect=AssertionError('no reconciliation')):
                    if call is None:
                        result = send.run(self.lab, agent='worker', text='must not create another launch')
                        self.assertFalse(result['ok'], result)
                        reason = result['native_delivery']['reason']
                    else:
                        with self.assertRaises(records.RecordError) as caught:
                            call()
                        self.assertEqual(caught.exception.file, self.path)
                        reason = str(caught.exception)
                    self.assertRegex(reason, 'runtime.json.*' + cause)
                    self.assertIn('mv2 kill', reason)
                    self.assertEqual(self.path.read_bytes() if self.path.exists() else None, before)
                    self.assertTrue(self.directory.is_dir())
                    self.assertFalse((self.review / 'state/agents' / self.worker_id).exists())
                    self.assertFalse((self.review / 'subverses/lab').exists())
                    self.assertFalse((self.root / 'trash').exists())
                    self.assertFalse((self.lab / 'trash').exists())
            with self.subTest(damage=label, operation='list'):
                row = lifecycle.listing(self.lab, only=self.worker_id)[0]
                self.assertEqual(row['damaged']['file'], f'state/agents/{self.worker_id}/runtime.json')
                self.assertEqual(row['damaged']['recover'], 'kill')
                self.assertEqual((row['name'], row['harness']), ('worker', 'fake'))
                if (field, value) in every:
                    self.assertTrue(any('worker' in problem and 'runtime.json' in problem
                                        for problem in doctor.run(self.lab)['problems']))
        self.assertEqual(len(self.tagged(self.worker_id)), 1)
        self.assertEqual(len(self.tagged(self.other['id'])), 1)

    def test_kill_recovers_only_the_damaged_agent(self):
        """kill on a live agent with a damaged or missing runtime closes exactly its pane by tags, leaves the other
        agent alive and the agent resumable, and refuses a pane tagged with another saved launch; fragile because
        recovery finds the pane by tags, not the record, and a wrong match kills another agent while a miss leaves an
        invisible process."""
        original = self.live()
        foreign = self.directory / 'launches' / uid()
        write(foreign / 'request.json', read(self.directory / 'launches' / original['launch_id'] / 'request.json'))
        pane = original['tmux']['pane']
        self.backend.run('set-option', '-p', '-t', pane, '@mv2_launch_id', foreign.name)
        try:
            before = self.path.read_bytes()
            with self.subTest(damage='pane tagged with another saved launch'), \
                    self.assertRaisesRegex(records.RecordError, 'different saved launch'):
                lifecycle.kill(self.lab, self.worker_id, attachment_only=True)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertIn(pane, self.backend.snapshot()[0])
        finally:
            self.backend.run('set-option', '-p', '-t', pane, '@mv2_launch_id', original['launch_id'])
            shutil.rmtree(foreign)
        for label in ('launch authority', 'missing file', 'wrong pane', 'wrong socket'):
            with self.subTest(damage=label):
                facts = self.live()
                if label == 'missing file':
                    self.path.unlink()
                    stopped = lifecycle.kill(self.lab, 'worker')
                    self.assertIn('runtime.json', stopped['native_cancellation']['reason'])
                else:
                    if label == 'launch authority':
                        write(self.path, {**facts, 'launch_id': None, 'tmux': []})
                    else:
                        field, value = (('pane', '%999') if label == 'wrong pane'
                                        else ('socket', str(self.work / 'absent.sock')))
                        write(self.path, {**facts, 'tmux': {**facts['tmux'], field: value}})
                    with patch.dict(os.environ, {'MV2_TMUX_SOCKET': self.socket}):
                        stopped = lifecycle.kill(self.lab, 'worker', attachment_only=True)
                    if label == 'launch authority':
                        self.assertIn('damaged launch authority', stopped['native_cancellation']['reason'])
                self.assertEqual(stopped['state'], 'dormant')
                self.assertEqual(lifecycle.listing(self.lab, only=self.worker_id)[0]['live'], 'dormant')
                self.assertEqual(self.tagged(self.worker_id), [])
                self.assertEqual(len(self.tagged(self.other['id'])), 1)
                self.assertEqual(lifecycle.listing(self.root, only=self.other['id'])[0]['live'], 'alive')
                self.resume()

    def test_resume_and_wake_recover_after_a_verified_stop(self):
        """After a verified stop, resume and wake of an agent whose runtime.json lost its launch_id or is missing
        start a new launch with a notice, while a pid that cannot be checked still refuses naming the file; fragile
        because the refuse-or-allow decision flips on process proof, and without these cases a fix that refuses
        everything, or allows everything, would pass."""
        for label in ('resume', 'wake', 'resume without runtime.json'):
            with self.subTest(recover=label):
                facts = self.live()
                if label == 'resume without runtime.json':
                    # A completed launch's real cleanup records prove its former processes gone.
                    cleanup = read(self.directory / 'launches' / facts['launch_id'] / 'cleanup.json')
                    self.assertEqual((cleanup['child_pid'], cleanup['child_started']),
                                     (facts['pid'], facts['pid_started']))
                lifecycle.kill(self.lab, 'worker')
                if label == 'resume without runtime.json':
                    self.path.unlink()
                else:
                    write(self.path, {**read(self.path), 'launch_id': None})
                if label == 'wake':
                    adapter = harnesses.load(self.lab, 'fake')
                    adapter.resume_prompt_args = ('{task}',)
                    with patch.object(harnesses, 'load', return_value=adapter), \
                            patch.dict(os.environ, {'MV2_TMUX_SOCKET': self.socket}):
                        result = send.run(self.lab, agent='worker', text='wake after verified stop')
                    self.assertTrue(result['ok'], result)
                    until(lambda: read(self.path).get('startup_checked') is True)
                else:
                    result = self.resume()
                notices = ' '.join(result.get('notices', []))
                self.assertNotEqual(read(self.path)['launch_id'], facts['launch_id'])
                if label == 'resume without runtime.json':
                    self.assertIn('runtime.json was missing', notices)
                else:
                    self.assertIn('damaged launch authority', notices)
                    self.assertNotIn('was missing', notices)
        with self.subTest(recover='a pid that cannot be checked'):
            self.live()
            lifecycle.kill(self.lab, 'worker', attachment_only=True)
            stopped = read(self.path)
            self.addCleanup(write, self.path, stopped)
            write(self.path, {**stopped, 'pid': 'abc'})
            before = self.path.read_bytes()
            for operation in (lambda: lifecycle.start(self.lab, resume='worker', socket_path=self.socket),
                              lambda: lifecycle.kill(self.lab, 'worker'),
                              lambda: relocate.remove_agent(self.lab, 'worker')):
                with self.assertRaises(records.RecordError) as caught:
                    operation()
                self.assertIn('remove', str(caught.exception))
                self.assertIn('runtime.json', str(caught.exception))
                self.assertNotIn('use mv2 kill', str(caught.exception))
                self.assertEqual(self.path.read_bytes(), before)


class RuntimeAuthorityProofTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory, self.properties, _, _ = lifecycle._record(self.root, 'proof', '', 'codex', None, None)
        write(self.directory / 'properties.json', self.properties)

    def test_damage_is_classified_per_field(self):
        """missing_runtime.damage() passes healthy and minimal-preparation records without discovery and flags a
        start token that does not parse; fragile because it is the single gate every operation trusts, and a parse
        slip lets a damaged record through silently."""
        healthy = {'launch_id': uid(), 'state': 'running',
                   'tmux': {'socket': '/private.sock', 'session': '$1', 'pane': '%2'}}
        preparing = {'launch_id': uid(), 'state': 'starting', 'preparing': True}
        with patch.object(missing_runtime, 'launches', side_effect=AssertionError('no history scan')), \
             patch.object(missing_runtime.Tmux, 'snapshot', side_effect=AssertionError('no tmux query')):
            self.assertIsNone(missing_runtime.damage(self.directory, healthy))
            self.assertIsNone(missing_runtime.damage(self.directory, preparing))
        self.assertIsNone(missing_runtime.damage(self.directory, {}))
        for key in ('pid_started', 'runner_started'):
            for value in ('', 'damaged', '2026-10-17', []):
                with self.subTest(key=key, value=value):
                    self.assertIsNotNone(missing_runtime.damage(self.directory, {**healthy, key: value}))
            self.assertIsNone(missing_runtime.damage(self.directory, {**healthy, key: None}))
            self.assertIsNone(missing_runtime.damage(self.directory, {**healthy, key: 'Sat Oct 17 12:00:00 2026'}))
