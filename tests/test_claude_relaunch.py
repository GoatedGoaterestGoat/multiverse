"""Claude's own record of a launch's process: the binding it shows and delivery after /restart.

Claude keeps ~/.claude/sessions/<pid>.json for each interactive process: its sessionId,
procStart (the start in UTC) and the socket it listens on. /restart runs the new
version in the same process without mv2's --messaging-socket-path, so Claude then
listens on its own socket; /clear moves it to a new sessionId.
"""
import hashlib
import json
import os
from pathlib import Path
import socket
import time
from unittest.mock import Mock, patch
from uuid import uuid4

from multiverse import harnesses
from multiverse.runtime import lifecycle, process
from multiverse.store import write
from multiverse.harnesses.types import Message, NativeEndpoint, NativeResult, ObservedBinding, SessionHandle
from multiverse.runtime.process import process_started

from test_multiverse import BaseTest

TOKEN = '0123456789abcdef0123456789abcdef'
# Claude's own files: a sessions file from macOS (Claude Code 2.1.302, the user's paths replaced), the same shape
# with the procStart a real Linux sessions file holds, and a /proc/<pid>/stat line in Linux's format (proc(5))
# whose field 22, the start in clock ticks since boot, is that procStart.
FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'claude'
LINUX_START = '1334113325'
RELAUNCHED = "Claude relaunched itself (/restart) without mv2's inbox; restart the agent"


class ClaudeRelaunchTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.config = self.work / 'claude-config'
        (self.config / 'sessions').mkdir(parents=True)
        self.enterContext(patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.config)}))
        self.enterContext(patch('multiverse.harnesses.claude.claude_version', return_value=(2, 1, 302)))
        self.adapter = harnesses.load(self.root, 'claude')
        self.native = str(uuid4())
        self.ours = str(self.work / 'mv2.sock')        # the launch's own socket, gone after /restart
        self.theirs = str(self.work / 'relaunch.sock')  # where Claude listens after /restart
        self.sockets = []
        self.started = process_started(os.getpid())  # this process's start, as the runner records pid_started

    def tearDown(self):
        for sock in self.sockets:
            sock.close()
        super().tearDown()

    def listening(self, path):
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(path)
        self.sockets.append(sock)

    def key(self, path, token=TOKEN, pid=None):
        digest = hashlib.sha256(os.path.abspath(path).encode()).hexdigest()
        (self.config / 'sessions' / f'{pid or os.getpid()}.{digest}.key').write_text(json.dumps({'peerToken': token}))

    def record(self, socket_path=None, session_id=None, proc_start=None, pid=None):
        (self.config / 'sessions' / f'{pid or os.getpid()}.json').write_text(json.dumps({
            'pid': pid or os.getpid(), 'sessionId': session_id or self.native, 'kind': 'interactive',
            'procStart': proc_start or process_started(pid or os.getpid()), 'messagingSocketPath': socket_path or self.ours}))

    def session(self, **changes):
        values = dict(native_id=self.native, state='running', endpoint=NativeEndpoint('unix-lines', self.ours),
                      bridge='claude-mod-v1', home=str(self.config), pid=os.getpid(),
                      pid_started=self.started, session_file=str(self.work / 'agent' / 'session.jsonl'))
        values.update(changes)
        return SessionHandle('agent', 'claude', str(self.work), **values)

    def executor(self):
        executor = Mock()
        executor.write_lines.return_value = NativeResult('ok', 'written')
        return executor

    def message(self):
        return Message(id=str(uuid4()), sender={'name': 'lead'}, text='hi', delivery='steer', framed='Message from lead:\nhi')

    def test_session_home_is_the_launch_environments_storage(self):
        self.assertEqual(self.adapter.session_home({'CLAUDE_CONFIG_DIR': str(self.config)}), os.path.realpath(self.config))
        self.assertEqual(self.adapter.session_home({'HOME': str(self.work)}), os.path.realpath(self.work / '.claude'))

    def test_the_observed_binding_is_the_exact_process_or_nothing(self):
        self.record()
        self.assertEqual(self.adapter.observed_binding(self.session()),
                         ObservedBinding(native_id=self.native, home=str(self.config), evidence='claude-process-file'))
        cleared = str(uuid4())
        self.record(session_id=cleared)  # /clear moved the same process to a new session
        self.assertEqual(self.adapter.observed_binding(self.session()).native_id, cleared)
        self.record(proc_start='Thu Jan  1 00:00:00 1970')  # the file is for another process on this pid
        self.assertIsNone(self.adapter.observed_binding(self.session()))
        self.record()
        # The pid now belongs to a later process than the one this launch started: file and pid agree, but not with us.
        self.assertIsNone(self.adapter.observed_binding(self.session(pid_started='Thu Jan 1 00:00:00 1970')))
        self.record(session_id='not a uuid')
        self.assertIsNone(self.adapter.observed_binding(self.session()))
        self.record()
        self.assertIsNone(self.adapter.observed_binding(self.session(pid=None)))
        (self.config / 'sessions' / f'{os.getpid()}.json').unlink()
        self.assertIsNone(self.adapter.observed_binding(self.session()))

    def test_delivery_follows_claude_to_its_own_socket_after_restart(self):
        self.listening(self.theirs)
        self.key(self.theirs)
        self.record(socket_path=self.theirs)
        executor = self.executor()
        result = self.adapter.send(self.session(), self.message(), executor)
        self.assertEqual(result.state, 'native_queued')
        target, lines = executor.write_lines.call_args.args
        self.assertEqual(target.endpoint, NativeEndpoint('unix-lines', self.theirs))
        self.assertEqual(json.loads(lines[0]), {'type': 'auth', 'token': TOKEN})
        self.assertEqual(json.loads(lines[1])['session_id'], self.native)
        self.assertIsNone(self.adapter.connection_problem(self.session()))  # healed: nothing to report

    def test_a_relaunch_that_cannot_be_healed_says_so(self):
        self.record(socket_path=self.theirs)  # Claude moved, but published no key there (or nothing listens)
        for state_of_theirs in ('no key', 'no socket'):
            with self.subTest(state_of_theirs):
                if state_of_theirs == 'no socket':
                    self.key(self.theirs)
                executor = self.executor()
                result = self.adapter.send(self.session(), self.message(), executor)
                self.assertEqual((result.state, result.reason), ('unavailable', RELAUNCHED))
                executor.write_lines.assert_not_called()
                self.assertEqual(self.adapter.ask(self.session(), 'Still there?', executor).reason, RELAUNCHED)
                self.assertEqual(self.adapter.connection_problem(self.session()), RELAUNCHED)

    def test_the_launch_socket_is_used_while_it_is_there_and_other_processes_are_ignored(self):
        self.key(self.ours)
        self.record()
        executor = self.executor()
        self.assertEqual(self.adapter.send(self.session(), self.message(), executor).state, 'native_queued')
        self.assertEqual(executor.write_lines.call_args.args[0].endpoint, NativeEndpoint('unix-lines', self.ours))
        self.assertIsNone(self.adapter.connection_problem(self.session()))
        (self.config / 'sessions' / f'{os.getpid()}.{hashlib.sha256(self.ours.encode()).hexdigest()}.key').unlink()
        # Another process's record (a reused pid), or another session's: never a relaunch of this launch.
        for record in (dict(socket_path=self.theirs, proc_start='Thu Jan  1 00:00:00 1970'),
                       dict(socket_path=self.theirs, session_id=str(uuid4()))):
            with self.subTest(**record):
                self.listening(self.theirs) if not os.path.exists(self.theirs) else None
                self.key(self.theirs)
                self.record(**record)
                result = self.adapter.send(self.session(), self.message(), self.executor())
                self.assertEqual(result.state, 'unavailable')
                self.assertNotEqual(result.reason, RELAUNCHED)
                self.assertIsNone(self.adapter.connection_problem(self.session()))

    def test_a_plain_or_stopped_launch_has_no_connection_problem(self):
        self.record(socket_path=self.theirs)
        self.assertIsNone(self.adapter.connection_problem(self.session(state='stopped')))
        self.assertIsNone(self.adapter.connection_problem(self.session(endpoint=None, bridge=None)))

    def test_a_reused_pid_is_never_healed_onto(self):
        self.listening(self.theirs)
        self.key(self.theirs)
        self.record(socket_path=self.theirs)  # a later Claude on our pid, listening elsewhere
        session = self.session(pid_started='Thu Jan 1 00:00:00 1970')
        executor = self.executor()
        result = self.adapter.send(session, self.message(), executor)
        self.assertEqual(result.state, 'unavailable')
        self.assertNotEqual(result.reason, RELAUNCHED)
        executor.write_lines.assert_not_called()
        self.assertIsNone(self.adapter.connection_problem(session))
        self.assertIsNone(self.adapter.observed_binding(session))

    def test_a_launch_recorded_before_its_start_is_told_to_restart_but_never_healed(self):
        self.listening(self.theirs)
        self.key(self.theirs)
        self.record(socket_path=self.theirs)
        session = self.session(pid_started=None)
        executor = self.executor()
        result = self.adapter.send(session, self.message(), executor)
        self.assertEqual((result.state, result.reason), ('unavailable', RELAUNCHED))
        executor.write_lines.assert_not_called()
        self.assertEqual(self.adapter.connection_problem(session), RELAUNCHED)
        self.assertIsNone(self.adapter.observed_binding(session))
        self.record()  # its file names mv2's own socket: nothing relaunched, only no key yet
        self.assertIsNone(self.adapter.connection_problem(session))
        self.assertNotEqual(self.adapter.send(session, self.message(), self.executor()).reason, RELAUNCHED)


    def claude_file(self, fixture, **changes):
        """Claude's sessions file for this process, from a real one: its pid, session and socket made this launch's."""
        record = json.loads((FIXTURES / fixture).read_text())
        record.update({'pid': os.getpid(), 'sessionId': self.native, 'messagingSocketPath': self.ours, **changes})
        (self.config / 'sessions' / f'{os.getpid()}.json').write_text(json.dumps(record))

    def on_linux(self, *, state='S', ticks=LINUX_START, name='claude', line=None):
        """Run the adapter as on Linux, with this process's /proc/<pid>/stat from the Linux fixture."""
        fields = (FIXTURES / 'proc-stat-linux.txt').read_text().split(' ')
        fields[0], fields[1], fields[2], fields[21] = str(os.getpid()), f'({name})', state, ticks
        proc = self.work / 'proc'
        (proc / str(os.getpid())).mkdir(parents=True, exist_ok=True)
        (proc / str(os.getpid()) / 'stat').write_text(' '.join(fields) if line is None else line)
        self.enterContext(patch('multiverse.harnesses.claude.LINUX', True))
        self.enterContext(patch('multiverse.harnesses.claude.PROC', proc))

    def counted(self):
        """Every ps the observer runs (multiverse.runtime.process), counted."""
        return self.enterContext(patch.object(process, '_query', wraps=process._query))

    def test_a_real_macos_process_file_identifies_its_process(self):
        self.claude_file('sessions-darwin.json', procStart=process_started(os.getpid()))
        self.assertEqual(self.adapter.observed_binding(self.session()).native_id, self.native)
        self.claude_file('sessions-darwin.json')  # the start the real file holds: another process
        self.assertIsNone(self.adapter.observed_binding(self.session()))

    def test_on_linux_the_start_is_compared_in_claudes_own_clock_ticks(self):
        self.on_linux()
        self.claude_file('sessions-linux.json')
        self.assertEqual(self.adapter.observed_binding(self.session()),
                         ObservedBinding(native_id=self.native, home=str(self.config), evidence='claude-process-file'))
        # The /restart heal and the advice work there too.
        self.listening(self.theirs)
        self.key(self.theirs)
        self.claude_file('sessions-linux.json', messagingSocketPath=self.theirs)
        executor = self.executor()
        self.assertEqual(self.adapter.send(self.session(), self.message(), executor).state, 'native_queued')
        self.assertEqual(executor.write_lines.call_args.args[0].endpoint, NativeEndpoint('unix-lines', self.theirs))
        self.assertEqual(self.adapter.connection_problem(self.session(pid_started=None)), RELAUNCHED)

    def test_on_linux_another_process_a_reused_pid_a_zombie_or_a_damaged_stat_is_never_this_process(self):
        self.claude_file('sessions-linux.json')
        cases = {'another start in /proc': dict(ticks='1334113326'), 'a zombie': dict(state='Z'),
                 'an empty stat': dict(line=''), 'a cut-short stat': dict(line=f'{os.getpid()} (claude) S 1 2 3'),
                 'a start that is not a number': dict(ticks='12x4')}
        for case, stat in cases.items():
            with self.subTest(case):
                self.on_linux(**stat)
                self.assertIsNone(self.adapter.observed_binding(self.session()))
        self.on_linux()
        self.assertIsNone(self.adapter.observed_binding(self.session(pid_started='Thu Jan 1 00:00:00 1970')))  # reused
        # A command name holding ") " and spaces: the fields are read after its last parenthesis, as Claude does.
        self.on_linux(name='claude) S 1 2 (x')
        self.assertEqual(self.adapter.observed_binding(self.session()).native_id, self.native)

    def test_a_damaged_process_file_or_a_zombie_on_macos_is_never_this_process(self):
        path = self.config / 'sessions' / f'{os.getpid()}.json'
        for damaged in ('{', '[]', json.dumps({'sessionId': self.native}), json.dumps({'procStart': 1234, 'sessionId': self.native})):
            with self.subTest(damaged=damaged):
                path.write_text(damaged)
                self.assertIsNone(self.adapter.observed_binding(self.session()))
                self.assertIsNone(self.adapter.connection_problem(self.session()))
        self.claude_file('sessions-darwin.json', procStart=self.started)
        with patch('multiverse.harnesses.claude.process_snapshot', return_value=(self.started, 'Z')):
            self.assertIsNone(self.adapter.observed_binding(self.session()))

    def test_a_stopped_launch_or_one_without_a_process_file_runs_no_process(self):
        query = self.counted()
        self.claude_file('sessions-darwin.json', procStart=process_started(os.getpid()))
        query.reset_mock()
        for session in (self.session(state='stopped'), self.session(state='failed'), self.session(pid=None)):
            self.assertIsNone(self.adapter.observed_binding(session))
            self.assertIsNone(self.adapter.connection_problem(session))
        (self.config / 'sessions' / f'{os.getpid()}.json').unlink()
        self.assertIsNone(self.adapter.observed_binding(self.session()))
        self.assertEqual(query.call_count, 0)
        self.claude_file('sessions-darwin.json', procStart=process_started(os.getpid()))
        query.reset_mock()
        self.assertIsNotNone(self.adapter.observed_binding(self.session()))
        self.assertEqual(query.call_count, 1)  # one ps for a running launch on macOS

    def test_a_listing_of_stopped_claude_agents_runs_no_process(self):
        for number in range(10):
            agent = self.root / 'state/agents' / str(uuid4())
            write(agent / 'properties.json', {'id': agent.name, 'name': f'stopped-{number}', 'harness': 'claude',
                                              'workspace': str(self.work), 'native_session_id': str(uuid4()),
                                              'native_session_home': str(self.config)})
            write(agent / 'runtime.json', {'launch_id': str(uuid4()), 'state': 'stopped', 'pid': os.getpid(),
                                           'pid_started': process_started(os.getpid())})
        self.claude_file('sessions-darwin.json', procStart=process_started(os.getpid()))  # a live Claude on that pid
        query = self.counted()
        query.reset_mock()
        started = time.monotonic()
        rows = lifecycle.listing(self.root)
        elapsed = time.monotonic() - started
        self.assertEqual(len([row for row in rows if row['name'].startswith('stopped-')]), 10)
        self.assertEqual(query.call_count, 0)
        self.assertLess(elapsed, 1.0)

    def test_each_check_of_a_live_healed_agent_queries_its_process_once(self):
        # After /restart is healed the launch's own key is gone, so a listing asks the adapter for the binding
        # and for a connection problem. Each check queries the process once; nothing is cached between checks,
        # so a pid reused in between is still caught.
        agent = self.root / 'state/agents' / str(uuid4())
        write(agent / 'properties.json', {'id': agent.name, 'name': 'healed', 'harness': 'claude',
                                          'workspace': str(self.work), 'native_session_id': self.native,
                                          'native_session_home': os.path.realpath(self.config)})
        write(agent / 'runtime.json', {'launch_id': str(uuid4()), 'state': 'running', 'pid': os.getpid(),
                                       'pid_started': self.started, 'native_bridge': 'claude-mod-v1',
                                       'control_endpoint': {'transport': 'unix-lines', 'address': self.ours}})
        self.listening(self.theirs)
        self.key(self.theirs)
        self.claude_file('sessions-darwin.json', procStart=self.started, messagingSocketPath=self.theirs)
        query = self.counted()
        query.reset_mock()
        self.assertIsNotNone(self.adapter.observed_binding(self.session()))
        self.assertEqual(query.call_count, 1)
        self.assertIsNone(self.adapter.connection_problem(self.session()))  # healed: nothing to report
        self.assertEqual(query.call_count, 2)
        query.reset_mock()
        with patch('multiverse.runtime.lifecycle.observe', return_value={'live': 'alive'}):
            [row] = lifecycle.listing(self.root)
        self.assertIsNone(row['connection_error'])
        # Listing reconciles once, then checks the healed connection separately.
        # No process identity is cached across those checks.
        self.assertEqual(query.call_count, 2)
