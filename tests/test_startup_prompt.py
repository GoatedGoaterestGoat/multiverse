"""A launch that stops at a prompt only the person can answer says so: waiting, never ready or failed."""
import contextlib
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import stat
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from multiverse.display import outcome
from multiverse.harnesses.base import ConfiguredAdapter
from multiverse.harnesses.claude import ClaudeAdapter
from multiverse.harnesses.types import LaunchRefused
from multiverse.runtime import lifecycle, native, refusal, runner
from multiverse.terminal import recovery, tmux
from multiverse.store import read, write
from multiverse.tools import list_agents, send, spawn
from multiverse.universe import agents, mailbox
from test_multiverse import BaseTest

SCREENS = {item['name']: item['text'] for item in
           json.loads((Path(__file__).parent / 'fixtures/claude/screens-2.1.314.json').read_text())}
TRUST = {'title': 'Accessing workspace', 'detail': '/private/tmp/mv2-pane-try'}


class Clock:
    """The observer's stop event as a fake clock that moves only when the observer waits."""
    elapsed = 0.

    def is_set(self):
        return self.elapsed >= 90

    def wait(self, seconds):
        self.elapsed += seconds


class StartupPromptTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.identity, self.launch = str(uuid4()), str(uuid4())
        self.directory = self.root / 'state' / 'agents' / self.identity
        write(self.directory / 'properties.json', {'id': self.identity, 'name': 'asks', 'harness': 'claude',
              'workspace': str(self.work), 'native_session_id': str(uuid4())})
        write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running', 'status': 'unknown',
              'pid': 1, 'startup_checked': True, 'native_versions': {'cli': '2.1.330'}})
        (self.directory / 'launches' / self.launch).mkdir(parents=True)
        write(self.directory / 'launches' / self.launch / 'request.json', {'observe_refusal': True})

    def test_task_requires_optional_positive_trust_and_can_be_retried_from_saved_input(self):
        """A trust refusal must precede pane creation and preserve the task without reserving its name."""
        adapter = ConfiguredAdapter('guarded', {'spawn': [sys.executable, '{task}'], 'resume': [sys.executable]})
        adapter.family = 'agy'
        adapter.workspace_trust = trust = Mock()
        backend = Mock()
        backend.create.return_value = {'socket': '/test', 'session': '$1', 'pane': '%1'}
        task = 'First line\nSecond line'
        common = {'name': 'workspace-task', 'harness': 'guarded', 'json': True}
        saved = []
        with patch.object(lifecycle.harnesses, 'load', return_value=adapter), \
                patch.object(lifecycle, 'Tmux', return_value=backend):
            for verdict in ('untrusted', 'unknown', True, 'TRUSTED'):
                with self.subTest(verdict=verdict):
                    trust.return_value = verdict
                    refusal = spawn.cli_run(self.root, task=task, **common)
                    self.assertFalse(refusal['ok'])
                    self.assertIn('Open the Antigravity CLI in that folder without a task and approve workspace trust',
                                  refusal['error'])
                    self.assertIn('rerun the same command', refusal['error'])
                    self.assertIn('You can delete this copy once the task starts', refusal['error'])
                    backend.create.assert_not_called()
                    self.assertNotIn('workspace-task', [read(path)['name'] for path in agents.record_paths(self.root)])
                    saved = sorted((self.root / 'state').glob('startup-task-*.md'))
                    self.assertTrue(all(path.read_text() == task for path in saved))
                    self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in saved))
            self.assertEqual(len(saved), 4)
            self.assertTrue(all(call.args == (str(self.work),) for call in trust.call_args_list))
            trust.return_value = 'trusted'
            result = spawn.run(self.root, name=common['name'], harness=common['harness'], task_file=saved[0])
            launch = agents.directory(self.root, result['id']) / 'launches' / result['runtime']['launch_id']
            self.assertTrue(read(launch / 'request.json')['task_trust'])
            self.assertEqual((launch / 'task.md').read_text(), task)
            trust.reset_mock()
            idle = spawn.run(self.root, name='workspace-idle', harness=common['harness'])
            launch = agents.directory(self.root, idle['id']) / 'launches' / idle['runtime']['launch_id']
            self.assertNotIn('task_trust', read(launch / 'request.json'))
            trust.assert_not_called()

    def test_adapter_refusal_preserves_the_task_before_pane_creation(self):
        """A native preparation refusal must leave its spawn name and saved input available for retry."""
        adapter = ConfiguredAdapter('guarded', {'spawn': [sys.executable, '{task}']})
        backend = Mock()
        backend.create.return_value = {'socket': '/test', 'session': '$1', 'pane': '%1'}
        common = {'name': 'refused-task', 'harness': 'guarded'}
        task = 'First line\nSecond line'
        with patch.object(lifecycle.harnesses, 'load', return_value=adapter), \
                patch.object(lifecycle, 'Tmux', return_value=backend):
            with patch.object(adapter, 'spawn', side_effect=LaunchRefused('Correct the harness settings')):
                with self.assertRaises(ValueError) as refused:
                    spawn.cli_run(self.root, task=task, **common)
                reason = str(refused.exception)
                self.assertIn('Correct the harness settings. Then rerun the same command.', reason)
                self.assertIn('--task-file', reason)
                with patch.object(lifecycle, '_save_refused_task', side_effect=OSError('retry file unavailable')):
                    with self.assertRaisesRegex(ValueError, 'Correct the harness settings.*retry file unavailable'):
                        spawn.cli_run(self.root, task=task, **common)
                idle = spawn.cli_run(self.root, json=True, **common)
                self.assertFalse(idle['ok'])
                self.assertNotIn('Saved input', idle['error'])
            with patch.object(adapter, 'spawn', side_effect=ValueError('Ordinary error')):
                refused = spawn.cli_run(self.root, task=task, json=True, **common)
                self.assertNotIn('Saved input', refused['error'])
            backend.create.assert_not_called()
            self.assertNotIn(common['name'], [read(path)['name'] for path in agents.record_paths(self.root)])
            [saved] = (self.root / 'state').glob('startup-task-*.md')
            self.assertEqual(saved.read_text(), task)
            self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
            self.assertEqual(spawn.run(self.root, task_file=saved, **common)['name'], common['name'])

    def test_wake_refusal_names_one_retry(self):
        """The delivery formatter already gives the retry command for the saved wake message."""
        adapter = ConfiguredAdapter('guarded', {'spawn': [sys.executable], 'resume': [sys.executable]})
        adapter.resume_prompt_args = ('{task}',)
        adapter.workspace_trust = Mock(return_value='untrusted')
        properties = self.directory / 'properties.json'
        write(properties, {**read(properties), 'harness': 'guarded', 'task': 'original task',
                           'universe_id': read(self.root / 'universe.json')['id']})
        write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'dormant'})
        with patch.object(lifecycle.harnesses, 'load', return_value=adapter), \
                patch.object(lifecycle, 'observe', return_value={'live': 'dormant'}), \
                patch.object(native, 'observe', return_value={'live': 'dormant'}):
            for reason, prepare in (
                ('Open guarded in that folder without a task and approve workspace trust', adapter.resume),
                ('Correct the harness settings', Mock(side_effect=LaunchRefused('Correct the harness settings'))),
            ):
                with self.subTest(reason=reason), patch.object(adapter, 'resume', prepare):
                    errors = []
                    result = send.run(self.root, agent='asks', text='saved wake', delivery='steer',
                                      _present_error=errors.append)
                    self.assertFalse(result['ok'])
                    [line] = errors
                    self.assertIn(reason, line)
                    self.assertEqual(line.count('mv2 deliver asks --retry'), 1)
        messages = mailbox.inbox(self.directory, all_messages=True)['messages']
        self.assertEqual([message['tracking']['submission_state'] for message in messages], ['failed', 'failed'])
        self.assertFalse(list((self.root / 'state').glob('startup-task-*.md')))

    def watch(self, frame, *, bridge=False):
        """Run the launch's screen observer on frames `frame(elapsed)` picks; what it saw, on each frame."""
        clock, seen = Clock(), []
        monitor = runner.RefusalMonitor(self.directory, self.launch, self.root, ClaudeAdapter.configured('claude', {}),
                                        'claude')
        monitor.stopped = clock
        backend = Mock()

        def command(name, *args):
            if name == 'capture-pane':
                facts = agents.runtime(self.directory)
                seen.append((clock.elapsed, facts.get('status'), facts.get('startup_prompt')))
                if bridge and not facts.get('native_bridge'):  # the plugin reports before the prompt shows
                    agents.update_runtime(self.directory, self.launch, native_bridge='claude-mod-v4', status='idle')
                return frame(clock.elapsed)
            return '1\t0\t0\t100'
        backend.run.side_effect = command
        with patch.object(runner.time, 'monotonic', side_effect=lambda: clock.elapsed), \
                patch('multiverse.terminal.tmux.owned', return_value=(backend, '%1')):
            monitor._run()
        return seen, clock

    def test_a_prompt_before_the_session_is_waiting_until_the_screen_moves_on(self):
        """Claude's plugin can't report its folder-trust prompt (Claude runs a plugin's calls only once a session has
        started), so only the launch screen can say the agent waits: missed, spawn says Started while it waits for
        good; kept after it's answered, the agent shows waiting while it works."""
        with self.subTest('answered after the observation window, as a person may'):
            seen, clock = self.watch(lambda at: SCREENS['trust dialog'] if at < 70 else SCREENS['idle'])
            asked = [entry for entry in seen if entry[2] is not None]
            self.assertTrue(asked and asked[-1][0] > refusal.STARTUP_WINDOW_SECONDS)  # still watched past the window
            self.assertTrue(all(status == 'waiting' and {k: shown[k] for k in TRUST} == TRUST
                                for at, status, shown in asked if at > 1))
            facts = agents.runtime(self.directory)
            self.assertEqual((facts.get('startup_prompt'), facts.get('status'), facts.get('startup_screen')),
                             (None, 'unknown', 'started'))
            self.assertLess(clock.elapsed, 75)  # it stopped once the input box showed
        with self.subTest("the plugin's own report is never overwritten"):
            write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running', 'status': 'unknown',
                  'pid': 1, 'startup_checked': True, 'native_versions': {'cli': '2.1.330'}})
            self.watch(lambda at: SCREENS['trust dialog'] if at < 5 else SCREENS['idle'], bridge=True)
            self.assertEqual(agents.runtime(self.directory).get('status'), 'idle')
        with self.subTest('answered after the window, the start still settles a pending observation'):
            write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running', 'status': 'unknown',
                  'pid': 1, 'startup_checked': True, 'native_versions': {'cli': '2.1.330'},
                  'attachment': {'readiness': 'process'}, 'native_startup_observation': {'state': 'pending'}})
            self.watch(lambda at: SCREENS['trust dialog'] if at < 70 else SCREENS['idle'])
            self.assertEqual(agents.runtime(self.directory)['native_startup_observation']['state'], 'started')
        with self.subTest("the plugin's first report retires the prompt, before the pane redraws"):
            write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running', 'status': 'unknown',
                  'pid': 1, 'startup_checked': True, 'native_versions': {'cli': '2.1.330'}})

            def reported(at):
                if at >= 5 and not agents.runtime(self.directory).get('native_bridge'):
                    agents.update_runtime(self.directory, self.launch, native_bridge='claude-mod-v4', status='idle')
                return SCREENS['trust dialog']
            seen, _ = self.watch(reported)
            self.assertTrue(any(at < 5 and shown is not None for at, _, shown in seen))
            facts = agents.runtime(self.directory)
            self.assertEqual((facts['status'], facts.get('startup_prompt')), ('idle', None))
            refusal.note_prompt(self.directory, self.launch, TRUST)  # a late frame can't put it back
            self.assertIsNone(agents.runtime(self.directory).get('startup_prompt'))
        with self.subTest('a --wait begun after the window, at the prompt, leaves the later start its settlement'):
            write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running', 'status': 'unknown',
                  'pid': 1, 'startup_checked': True, 'native_versions': {'cli': '2.1.330'},
                  'attachment': {'readiness': 'process'}, 'native_startup_observation': {'state': 'pending'}})
            asked = []

            def late(at):
                if at >= 60 and not asked:  # the launch is past its window when this wait starts
                    begun = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
                    agents.update_runtime(self.directory, self.launch, started_at=begun)
                    asked.append(lifecycle.wait_ready(self.root, self.directory, self.launch,
                                                      ClaudeAdapter.configured('claude', {}), 0))
                return SCREENS['trust dialog'] if at < 70 else SCREENS['idle']
            with patch.object(lifecycle, 'observe', return_value={'live': 'alive'}), \
                    patch.object(lifecycle, 'note_ready') as ready:
                self.watch(late)
            self.assertEqual(asked[0]['startup_prompt']['title'], TRUST['title'])
            self.assertEqual((agents.runtime(self.directory)['native_startup_observation']['state'], ready.call_count),
                             ('started', 1))
        with self.subTest('a dialog inside the session is not a startup prompt'):
            write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running', 'status': 'unknown',
                  'pid': 1, 'startup_checked': True, 'native_versions': {'cli': '2.1.330'}})
            seen, _ = self.watch(lambda at: SCREENS['permission dialog'])
            self.assertTrue(seen and all(shown is None for _, _, shown in seen))

    def test_spawn_resume_and_the_listing_say_it_waits_at_the_prompt(self):
        """What the person reads: a launch stopped at a startup prompt is neither ready nor failed, and every view
        names the prompt and how to answer it; mv2 never answers it, since these are trust questions."""
        adapter = ClaudeAdapter.configured('claude', {})
        agents.update_runtime(self.directory, self.launch, startup_prompt={**TRUST, 'at': 'now'}, status='waiting')
        line = ('It\'s waiting at Claude\'s prompt "Accessing workspace" (/private/tmp/mv2-pane-try): answer it in its '
                'pane with mv2 attach asks.')
        alive = patch.object(lifecycle, 'observe', return_value={'live': 'alive'})
        with self.subTest('--wait ends at once, waiting, not ready'), alive:
            found = lifecycle.wait_ready(self.root, self.directory, self.launch, adapter, 5)
            self.assertEqual((found['ok'], found['ready'], found['startup_prompt']['title']), (True, False, TRUST['title']))
            self.assertEqual(outcome(found, 'Started', 'did not start'), f'Started asks (Claude, plain). {line}')
        with self.subTest('the short wait without --wait'), alive:
            found = lifecycle.screen_wait(self.root, self.directory, self.launch, timeout=5)
            self.assertEqual(outcome(found, 'Resumed', 'did not resume'), f'Resumed asks (Claude, plain). {line}')
        with self.subTest('mv2 list names it under the row, while the agent is live'):
            row = {'live': 'alive', 'name': 'asks', 'harness': 'claude', 'runtime': agents.runtime(self.directory)}
            self.assertIn('  ' + line, list_agents.problems(row, False))
            # Killed at the prompt, it keeps startup_prompt in its runtime: never tell the person to attach to it.
            self.assertNotIn('  ' + line, list_agents.problems({**row, 'live': 'stopped'}, False))
        with self.subTest('a launch at the prompt is not ready, for startup delivery or the ready event'), alive:
            record = lifecycle.launch_result(self.root, self.directory)
            self.assertFalse(lifecycle.launch_ready(adapter, record, record['runtime'], self.directory, {}, 'alive',
                                                    include_delivery=False))
            found = lifecycle.wait_ready(self.root, self.directory, self.launch, adapter, 0, include_delivery=False,
                                         stop_at_prompt=False)
            self.assertEqual((found['ready'], found.get('timed_out')), (False, True))
        with self.subTest("startup delivery's own readiness check keeps waiting across the prompt"):
            delivery = runner.StartupDelivery(self.directory, self.launch, self.root)
            delivery.stopped = Mock()
            delivery.stopped.is_set.return_value = False
            asked = {**lifecycle.launch_result(self.root, self.directory), 'ok': False, 'ready': False, 'timed_out': True}
            ready = {**asked, 'ok': True, 'ready': True, 'timed_out': False}
            with patch.object(lifecycle, 'wait_ready', side_effect=(asked, ready)) as waited, \
                    patch.object(native, 'flush_startup') as flushed:
                delivery._run()
            self.assertTrue(all(call.kwargs.get('stop_at_prompt') is False for call in waited.call_args_list))
            flushed.assert_called_once_with(self.root, self.directory, self.launch, None)
        with self.subTest('a saved prompt never hides a launch that ended'), \
                patch.object(lifecycle, 'observe', return_value={'live': 'stopped'}):
            self.assertIs(lifecycle.screen_wait(self.root, self.directory, self.launch, timeout=1).get('ok'), False)
            self.assertIs(lifecycle.wait_ready(self.root, self.directory, self.launch, adapter, 1).get('ok'), False)
        with self.subTest('the short wait never follows a replacement launch'), alive:
            other = {**lifecycle.launch_result(self.root, self.directory)}
            other['runtime'] = {**other['runtime'], 'launch_id': 'replacement'}
            with patch.object(lifecycle, 'launch_result', return_value=other), \
                    self.assertRaisesRegex(ValueError, 'Launch changed'):
                lifecycle.screen_wait(self.root, self.directory, self.launch, timeout=1)
        with self.subTest('neither wait returns a prompt the plugin retired while liveness was checked'):
            for wait in (lifecycle.screen_wait, lambda *a: lifecycle.wait_ready(*a, adapter, 0)):
                agents.update_runtime(self.directory, self.launch, startup_prompt={**TRUST, 'at': 'now'},
                                      native_bridge=None, status='waiting')
                checked = []

                def retire(*_):
                    if not checked:
                        agents.update_runtime(self.directory, self.launch, native_bridge='claude-mod-v4',
                                              startup_prompt=None, startup_screen='started', status='idle')
                    checked.append(True)
                    return {'live': 'alive'}
                with patch.object(lifecycle, 'observe', side_effect=retire):
                    found = wait(self.root, self.directory, self.launch)
                self.assertIsNone(found.get('startup_prompt'))
            agents.update_runtime(self.directory, self.launch, native_bridge=None, startup_screen='prompt', status='waiting',
                                  startup_prompt={**TRUST, 'at': 'now'})
        with self.subTest('what ends a launch during the liveness check outranks its prompt, in both waits'):
            for name, end in (('a stop', lambda: agents.update_runtime(self.directory, self.launch, state='stopped')),
                              ('a refusal', lambda: refusal.record(self.directory, self.launch,
                                                                  {'message': 'refused at launch', 'source': 'test'}))):
                # A positive timeout: with none left, the short wait checks no pane and claims no prompt.
                for wait in (lambda *a: lifecycle.screen_wait(*a, timeout=1), lambda *a: lifecycle.wait_ready(*a, adapter, 0)):
                    with self.subTest(name):
                        write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running', 'pid': 1,
                              'startup_checked': True, 'status': 'waiting', 'startup_prompt': {**TRUST, 'at': 'now'}})
                        (self.directory / 'launches' / self.launch / 'native-refusal.json').unlink(missing_ok=True)

                        def ending(*_):
                            end()
                            return {'live': 'alive'}  # the check began before it ended
                        with patch.object(lifecycle, 'observe', side_effect=ending):
                            found = wait(self.root, self.directory, self.launch)
                        self.assertEqual((found.get('ok'), 'startup_prompt' in found), (False, False))
            write(self.directory / 'runtime.json', {'launch_id': self.launch, 'state': 'running', 'pid': 1,
                  'startup_checked': True, 'status': 'waiting', 'startup_prompt': {**TRUST, 'at': 'now'}})
        with self.subTest('a prompt recorded just before the window closes keeps the observation open'):
            agents.update_runtime(self.directory, self.launch, startup_prompt=None, attachment={'readiness': 'process'},
                                  native_startup_observation={'state': 'pending'})
            refusal.note_prompt(self.directory, self.launch, TRUST)  # it lands as the expiry is written
            self.assertFalse(refusal.settle_startup(self.directory, self.launch, 'unverified'))
            self.assertEqual(agents.runtime(self.directory)['native_startup_observation']['state'], 'pending')
            agents.update_runtime(self.directory, self.launch, attachment=None, native_startup_observation=None)
        with self.subTest("the short wait keeps to its time even when the pane's check hangs"):
            agents.update_runtime(self.directory, self.launch, startup_prompt={**TRUST, 'at': 'now'}, native_bridge=None,
                                  tmux={'socket': str(self.work / 'hung.sock'), 'pane': '%1'})
            queries = []

            def hangs(*_, **__):
                queries.append(1)
                time.sleep(4)  # a tmux server that doesn't answer
                return {}, None
            begun = time.monotonic()
            with patch.object(tmux.Tmux, 'snapshot', side_effect=hangs):
                found = lifecycle.screen_wait(self.root, self.directory, self.launch, timeout=2)  # several passes
            self.assertLess(time.monotonic() - begun, 3)
            self.assertNotIn('startup_prompt', found)  # unconfirmed, so not claimed
            self.assertEqual(len(queries), 1)  # every pass waited on the one query still running
            agents.update_runtime(self.directory, self.launch, tmux=None)
        with self.subTest("a later launch on the server never takes an earlier launch's check as its own"):
            socket = str(self.work / 'shared.sock')
            universe_id = lifecycle.read(self.root / 'universe.json')['id']
            write(self.directory / 'properties.json', {**lifecycle.read(self.directory / 'properties.json'),
                                                       'universe_id': universe_id})
            agents.update_runtime(self.directory, self.launch, startup_prompt={**TRUST, 'at': 'now'}, native_bridge=None,
                                  tmux={'socket': socket, 'pane': '%2'})
            asked, running = [], []

            def answer(_backend, *, facts, read_only):
                asked.append(facts['launch_id'])
                running.append(1)
                try:
                    if facts['launch_id'] == 'earlier':
                        time.sleep(.3)  # asked before this launch's pane existed, and slow to answer
                        return {}, None
                    return {'%2': {'@mv2_agent_id': self.identity, '@mv2_launch_id': self.launch, 'pane_dead': '0',
                                   '@mv2_universe_id': universe_id}}, None
                finally:
                    self.assertEqual(len(running), 1)  # one query at a time on the server
                    running.pop()
            with patch.object(tmux.Tmux, 'snapshot', autospec=True, side_effect=answer):
                lifecycle._read_only_snapshot(socket, {'launch_id': 'earlier', 'tmux': {'socket': socket, 'pane': '%1'}}, 0)
                found = lifecycle.screen_wait(self.root, self.directory, self.launch, timeout=2)
                lifecycle._snapshots[socket][0].join(2)
            self.assertEqual(found.get('startup_prompt', {}).get('title'), TRUST['title'], found.get('error'))
            self.assertEqual(asked, ['earlier', self.launch])
            agents.update_runtime(self.directory, self.launch, tmux=None)
        with self.subTest('a pane gone before any prompt ends the short wait as a launch that did not start'):
            agents.update_runtime(self.directory, self.launch, startup_prompt=None,
                                  tmux={'socket': str(self.work / 'gone.sock'), 'pane': '%1'})
            with patch.object(tmux.Tmux, 'snapshot', return_value=({}, None)):
                found = lifecycle.screen_wait(self.root, self.directory, self.launch, timeout=1)
            self.assertIs(found['ok'], False)
            self.assertIn('dormant', found['error'])
            agents.update_runtime(self.directory, self.launch, tmux=None)
        with self.subTest("the short wait's pane check never enters tmux recovery"):
            socket = str(self.work / 'unproved.sock')
            agents.update_runtime(self.directory, self.launch, startup_prompt={**TRUST, 'at': 'now'}, native_bridge=None,
                                  tmux={'socket': socket, 'pane': '%1'})
            locked = []
            with patch.object(recovery, 'healthy', return_value=None), \
                    patch.object(recovery, 'serialized', side_effect=lambda *a: locked.append(a) or contextlib.nullcontext()), \
                    patch.object(recovery, 'connected', return_value=None):
                found = lifecycle.screen_wait(self.root, self.directory, self.launch, timeout=.5)
                lifecycle._snapshots[socket][0].join(2)  # the check has finished, wherever it was
            self.assertEqual(locked, [])
            self.assertNotIn('startup_prompt', found)  # an unproved server: unknown, so no prompt is claimed
            agents.update_runtime(self.directory, self.launch, tmux=None)
        with self.subTest('a late last read leaves the pane check only the time actually left'):
            agents.update_runtime(self.directory, self.launch, startup_prompt={**TRUST, 'at': 'now'}, native_bridge=None)
            clock, given, read = [0.0], [], lifecycle._current

            def slow_read(*a):  # the last read of the record ends 0.2 s before the deadline
                if not given:
                    clock[0] = 9.8
                return read(*a)

            def check(result, facts, seconds):
                given.append(seconds)
                clock[0] += seconds
                return 'unknown'
            with patch.object(lifecycle, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda s: None)), \
                    patch.object(lifecycle, '_current', side_effect=slow_read), \
                    patch.object(lifecycle, '_observe_within', side_effect=check):
                lifecycle.screen_wait(self.root, self.directory, self.launch)
            self.assertLessEqual(clock[0], lifecycle.SCREEN_WAIT_SECONDS + 0.01)
        with self.subTest("a result's runtime and session come from one read"):
            agents.update_runtime(self.directory, self.launch, startup_prompt={**TRUST, 'at': 'now'}, native_bridge=None,
                                  status='waiting')

            def reports(*_):
                agents.update_runtime(self.directory, self.launch, native_bridge='claude-mod-v4', status='idle',
                                      startup_prompt=None, startup_screen='started')
                return {'live': 'alive'}
            with patch.object(lifecycle, 'observe', side_effect=reports):
                found = lifecycle.screen_wait(self.root, self.directory, self.launch, timeout=1)
            self.assertEqual((found['runtime']['native_bridge'], found['session']['bridge'],
                              found['runtime']['status'], found['session']['status']),
                             ('claude-mod-v4', 'claude-mod-v4', 'idle', 'idle'))
            agents.update_runtime(self.directory, self.launch, native_bridge=None, startup_screen='prompt', status='waiting',
                                  startup_prompt={**TRUST, 'at': 'now'})
        with self.subTest("clearing the prompt keeps a wait the harness reported itself"):
            agents.update_runtime(self.directory, self.launch, status='waiting', status_observed=True)
            refusal.note_prompt(self.directory, self.launch, None)
            self.assertEqual(agents.runtime(self.directory)['status'], 'waiting')
            agents.update_runtime(self.directory, self.launch, startup_prompt={**TRUST, 'at': 'now'})
        with self.subTest('the short wait ends as soon as the screen shows the session started'), alive:
            refusal.note_started(self.directory, self.launch)
            found = lifecycle.screen_wait(self.root, self.directory, self.launch, timeout=30)
            self.assertNotIn('startup_prompt', found)
            self.assertEqual(outcome(found, 'Started', 'did not start'), 'Started asks (Claude, plain).')
        with self.subTest('a launch that fails during it did not start'), alive:
            agents.update_runtime(self.directory, self.launch, state='failed', reason='Native process exited with code 1.')
            found = lifecycle.screen_wait(self.root, self.directory, self.launch, timeout=30)
            with self.assertRaisesRegex(ValueError, 'asks did not start: Native process exited with code 1'):
                outcome(found, 'Started', 'did not start')
