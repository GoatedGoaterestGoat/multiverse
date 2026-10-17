"""Claude's own record of a launch's process: the binding it shows and delivery after /restart.

Claude keeps ~/.claude/sessions/<pid>.json for each interactive process: its sessionId,
procStart (the start in UTC) and the socket it listens on. /restart runs the new
version in the same process without mv2's --messaging-socket-path, so Claude then
listens on its own socket; /clear moves it to a new sessionId.
"""
import hashlib
import json
import os
import socket
from unittest.mock import Mock, patch
from uuid import uuid4

from multiverse import harnesses
from multiverse.harnesses.types import Message, NativeEndpoint, NativeResult, ObservedBinding, SessionHandle
from multiverse.runtime.process import process_started

from test_multiverse import BaseTest

TOKEN = '0123456789abcdef0123456789abcdef'
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
                      pid_started=process_started(os.getpid()), session_file=str(self.work / 'agent' / 'session.jsonl'))
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
