"""Claude's part of `mv2 slash`: the command frames, the command table and the result files, with no Claude session."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse import harnesses
from multiverse.harnesses.claude import BRIDGE, MOD, command_table, slash_entry
from multiverse.harnesses.types import LaunchContext, Message, NativeEndpoint, NativeResult, SessionHandle, SlashRequest
from multiverse.store import read, write
from test_multiverse import BaseTest

TOKEN = '0123456789abcdef0123456789abcdef'
DEFAULTS = Path(__file__).resolve().parents[1] / 'src/multiverse/harnesses/claude/defaults.json'


def mod_tag():
    """The mod's own tag regex: Python's frames must be what it reads, since each side keeps its copy."""
    found = re.search(r'^const TAG = /(.+)/$', (MOD / 'hooks/index.ts').read_text(), re.M)
    return re.compile(found.group(1).replace('\\/', '/'))


class ClaudeSlashTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.socket = str(self.work / 'inbox.sock')
        self.config = self.work / 'claude-config'
        self.agent_dir = self.work / 'agent'
        self.agent_dir.mkdir()
        self.enterContext(patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(self.config)}))
        self.enterContext(patch('multiverse.harnesses.claude.claude_version', return_value=(2, 1, 316)))
        path = self.root / 'harnesses/claude/harness.json'
        config = read(path)
        config['settings']['inbox_bridge'] = True
        write(path, config)
        self.adapter = harnesses.load(self.root, 'claude')
        digest = hashlib.sha256(os.path.abspath(self.socket).encode()).hexdigest()
        (self.config / 'sessions').mkdir(parents=True)
        (self.config / 'sessions' / f'4242.{digest}.key').write_text(json.dumps({'peerToken': TOKEN}))

    def session(self, bridge=BRIDGE):
        return SessionHandle('agent', 'claude', str(self.work), native_id=str(uuid4()), state='running',
                             endpoint=NativeEndpoint('unix-lines', self.socket), bridge=bridge,
                             session_file=str(self.agent_dir / 'session.jsonl'))

    def result_file(self, run_id, record):
        (self.agent_dir / 'claude-commands').mkdir(exist_ok=True)
        path = self.agent_dir / 'claude-commands' / f'{run_id}.json'
        path.write_text(record if isinstance(record, str) else json.dumps(record) + '\n')
        return path

    def test_a_command_goes_only_to_a_mod_that_runs_it_tagged_as_the_mod_reads_it(self):
        """A mod older than claude-mod-v3 can't parse a command tag and shows the model the raw frame, with no result,
        so only a v3 launch is sent one; v2 still gets send times on messages. Each side keeps its own tag copy."""
        executor = Mock()
        executor.write_lines.return_value = NativeResult('ok')
        request = SlashRequest(id=str(uuid4()), command='compact', args='keep the plan')
        reply = self.adapter.run_slash(self.session(), request, executor)
        self.assertEqual((reply.state, reply.value), ('unknown', {'state': 'queued'}))
        (auth, frame), = [call.args[1] for call in executor.write_lines.call_args_list]
        self.assertEqual(json.loads(auth), {'type': 'auth', 'token': TOKEN})
        frame = json.loads(frame)
        found = mod_tag().search(frame['message']['content'])
        self.assertEqual(found.groups()[:3], ('command', request.id, None))
        self.assertEqual(json.loads(found.group(4)), {'command': 'compact', 'args': 'keep the plan'})
        self.assertEqual(frame['msg_id'], request.id)
        for older in ('claude-mod-v2', 'claude-mod-v1'):
            with self.subTest(bridge=older):
                executor.reset_mock()
                session = self.session(older)
                for reply in (self.adapter.run_slash(session, request, executor), self.adapter.slash_commands(session, executor)):
                    self.assertEqual(reply.state, 'unavailable')
                self.assertIn('predates mv2 slash', reply.reason)
                executor.write_lines.assert_not_called()
                self.assertEqual(self.adapter.capabilities(session)['slash'].state, 'unavailable')
        # The advice names the agent, as the person types it, once its record says the name.
        (self.agent_dir / 'properties.json').write_text(json.dumps({'name': 'a1'}))
        self.assertIn('(mv2 kill a1, then mv2 resume a1)', self.adapter.run_slash(self.session('claude-mod-v2'), request, executor).reason)
        self.assertEqual(self.adapter.capabilities(self.session())['slash'].state, 'supported')
        sent = '2026-11-01T10:00:00+00:00'
        for bridge, tagged in (('claude-mod-v1', False), ('claude-mod-v2', True), (BRIDGE, True)):
            with self.subTest(send_time=bridge):
                executor.reset_mock()
                self.adapter.send(self.session(bridge), Message(id=str(uuid4()), text='Hi', delivery='steer', sent_at=sent), executor)
                content = json.loads(executor.write_lines.call_args.args[1][1])['message']['content']
                self.assertEqual(f' sent={sent}' in content, tagged)

    def test_the_table_lets_through_only_what_its_version_check_classified(self):
        """A built-in the table doesn't know may open a screen in the agent's terminal or save the person's settings
        (/model does), so it is refused until a version check classifies it; one that saves settings is marked so core
        runs it only for a person (decision H1); plugin, user and MCP commands run."""
        version, table = command_table()
        cases = [
            ({'name': 'compact', 'source': 'builtin'}, False, None, False),
            # Found live on 2.1.316: Claude runs /recap only for the person, /context writes its output for the
            # model alone (mv2 gets nothing, the agent's context grows), and /rename renames the session mv2 names.
            ({'name': 'recap', 'source': 'builtin'}, False, 'only when the person', False),
            ({'name': 'context', 'source': 'builtin'}, False, 'for the model', False),
            ({'name': 'rename', 'source': 'builtin'}, False, 'mv2 manages', False),
            ({'name': 'clear', 'source': 'builtin'}, False, None, False),
            ({'name': 'code-review', 'source': 'builtin'}, False, None, False),
            ({'name': 'model', 'source': 'builtin'}, True, None, True),
            ({'name': 'effort', 'source': 'builtin'}, True, None, True),
            ({'name': 'fast', 'source': 'builtin'}, False, None, True),
            ({'name': 'agents', 'source': 'builtin'}, False, 'interactive screen', False),
            # A form the table leaves out is unclassified, not a screen: plugin opens one bare and isn't classified with
            # arguments.
            ({'name': 'plugin', 'source': 'builtin'}, False, f"with arguments, it isn't classified for Claude Code {version}", False),
            ({'name': 'exit', 'source': 'builtin'}, False, 'mv2 manages', False),
            ({'name': 'radio', 'source': 'builtin'}, False, f'Not classified for Claude Code {version}', False),
            ({'name': 'review', 'source': 'user'}, False, None, False),
            ({'name': 'probe:ping', 'source': 'plugin', 'plugin': 'probe'}, False, None, False),
            ({'name': 'docs', 'source': 'mcp'}, False, None, False),
        ]
        for item, needs_args, refused, settings in cases:
            with self.subTest(command=item['name']):
                entry = slash_entry(item, version, table)
                self.assertEqual((entry.needs_args, entry.changes_settings), (needs_args, settings))
                if refused is None:
                    self.assertIsNone(entry.refused)
                else:
                    self.assertIn(refused, entry.refused or '')
        executor = Mock()
        for command, args, why in (('clear', 'everything', f"isn't classified for Claude Code {version}"),
                                   ('rename', 'x', 'mv2 manages'), ('reload-plugins', 'now', "isn't classified"),
                                   ('agents', '', 'opens an interactive screen')):
            with self.subTest(form=f'{command} {args}'):
                reply = self.adapter.run_slash(self.session(), SlashRequest(id=str(uuid4()), command=command, args=args), executor)
                self.assertEqual((reply.state, reply.value['state']), ('ok', 'refused'))
                self.assertIn(why, reply.value['reason'])
        executor.write_lines.assert_not_called()
        self.assertEqual(version, read(DEFAULTS)['checked_cli_version'])  # re-checked with each version bump

    def test_a_result_is_read_only_from_this_runs_own_file(self):
        """Core polls this after the launch ended and on --result: a missing file must say absent (so core can call
        the run lost once the launch is gone) and a damaged or foreign one must never pass for a result."""
        run_id = str(uuid4())
        request = SlashRequest(id=run_id, command='compact')
        session = self.session()
        reply = self.adapter.slash_result(session, request)
        self.assertEqual((reply.state, reply.value), ('unknown', {'absent': True}))
        for label, record in (('not JSON', '{"id": '), ('another run', {'id': str(uuid4()), 'state': 'ran', 'text': 'x', 'prompted': False}),
                              ('unknown state', {'id': run_id, 'state': 'maybe'}), ('bad output', {'id': run_id, 'state': 'ran', 'text': 5, 'prompted': False}),
                              ('no prompted flag', {'id': run_id, 'state': 'ran', 'text': None})):
            with self.subTest(label):
                self.result_file(run_id, record)
                reply = self.adapter.slash_result(session, request)
                self.assertEqual((reply.state, reply.value), ('unknown', None))
        ran = {'id': run_id, 'state': 'ran', 'text': 'Compacted', 'prompted': False, 'model': 'claude-haiku-5-5',
               'session_id': session.native_id, 'at': 1}
        path = self.result_file(run_id, ran)
        reply = self.adapter.slash_result(session, request)
        self.assertEqual((reply.state, reply.value), ('ok', {'state': 'ran', 'output': 'Compacted', 'prompted': False,
                         'model': 'claude-haiku-5-5', 'session_id': session.native_id, 'saved_default': False}))
        self.assertTrue(path.exists())  # kept: --result may read it again
        for state in ('failed', 'refused'):
            with self.subTest(state=state):
                self.result_file(run_id, {'id': run_id, 'state': state, 'reason': 'why', 'at': 1})
                self.assertEqual(self.adapter.slash_result(session, request).value, {'state': state, 'reason': 'why'})
        # /effort: the mod's readback is the only effort reported, and a settings form says it saved the default.
        effort_run = SlashRequest(id=str(uuid4()), command='effort', args='low')
        self.result_file(effort_run.id, {'id': effort_run.id, 'state': 'ran', 'text': None, 'prompted': False,
                                         'model': 'claude-haiku-5-5', 'effort': 'low', 'session_id': session.native_id})
        self.assertEqual({k: v for k, v in self.adapter.slash_result(session, effort_run).value.items() if k in ('effort', 'saved_default')},
                         {'effort': 'low', 'saved_default': True})
        # A level the session's model doesn't take is saved by Claude all the same, and confirms nothing.
        for model, level in (('claude-haiku-4-5', 'low'), ('claude-haiku-5-5', 'ultra'), (None, 'low')):
            with self.subTest(model=model, level=level):
                run = SlashRequest(id=str(uuid4()), command='effort', args=level)
                self.result_file(run.id, {'id': run.id, 'state': 'ran', 'text': None, 'prompted': False,
                                          'model': model, 'effort': level, 'session_id': session.native_id})
                self.assertNotIn('effort', self.adapter.slash_result(session, run).value)
        os.mkfifo(path.with_name(f'{uuid4()}.json'))
        fifo = SlashRequest(id=path.with_name(sorted(p.name for p in path.parent.iterdir() if p.is_fifo())[0]).stem, command='compact')
        self.assertEqual(self.adapter.slash_result(session, fifo).state, 'unknown')  # never blocks

    def test_the_list_comes_from_the_mod_and_is_described_by_the_table(self):
        """The list is what core refuses against: it must be Claude's own list for this session, each built-in
        described by the table, the mod's file consumed, and a name mv2 can't address left out (one such name made
        core refuse the whole list, and with it every run, on a live 2.1.316 session with an MCP server)."""
        def listed(target, lines):
            frame = json.loads(lines[1])
            found = mod_tag().search(frame['message']['content'])
            self.assertEqual(found.group(1), 'commands')
            self.result_file(frame['msg_id'], {'id': frame['msg_id'], 'state': 'listed', 'commands': [
                {'name': 'compact', 'description': 'Free up context', 'source': 'builtin', 'plugin': None},
                {'name': 'radio', 'description': 'Listen', 'source': 'builtin', 'plugin': None},
                {'name': 'review', 'description': 'Review', 'source': 'user', 'plugin': None},
                # Claude lists an MCP prompt under a display name ('… (MCP)') no mv2 command line can name.
                {'name': 'plugin:github:github:AssignCodingAgent (MCP)', 'description': 'Assign', 'source': 'mcp', 'plugin': 'github'}]})
            return NativeResult('ok')
        executor = Mock()
        executor.write_lines.side_effect = listed
        reply = self.adapter.slash_commands(self.session(), executor)
        self.assertEqual(reply.state, 'ok', reply.reason)
        self.assertEqual([(c['name'], c['needs_args'], c['refused'] is None) for c in reply.value],
                         [('compact', False, True), ('radio', False, False), ('review', False, True)])
        self.assertEqual(list((self.agent_dir / 'claude-commands').iterdir()), [])


    def test_launch_effort_is_claudes_session_flag(self):
        """--effort on spawn and resume is the only per-agent way to set effort (/effort saves the person's default);
        core refuses it for an adapter that doesn't declare it, so a missing flag would silently launch at another."""
        self.assertIs(self.adapter.supports_launch_effort, True)
        values = dict(name='worker', agent_id=str(uuid4()), session_id=str(uuid4()), bootstrap='Hello', bootstrap_json='"Hello"',
                      task='task', agent_dir=str(self.agent_dir), harness_dir='/h', session_file='', bootstrap_file='',
                      task_file='', workspace=str(self.work), model='', control_socket=self.socket)
        for effort in ('high', None):
            with self.subTest(effort=effort):
                spawned = list(self.adapter.spawn(LaunchContext('spawn', values, effort=effort)).argv)
                session = SessionHandle(values['agent_id'], 'claude', str(self.work), native_id=values['session_id'])
                transcript = self.config / 'projects' / 'p' / f"{values['session_id']}.jsonl"
                transcript.parent.mkdir(parents=True, exist_ok=True)
                transcript.write_text('{}\n')
                resumed = list(self.adapter.resume(session, LaunchContext('resume', values, effort=effort)).argv)
                for argv in (spawned, resumed):
                    if effort is None:
                        self.assertNotIn('--effort', argv)
                    else:
                        at = argv.index('--effort')
                        self.assertEqual(argv[at + 1], 'high')
                        self.assertLess(at, argv.index('--') if '--' in argv else len(argv))


if __name__ == '__main__':
    unittest.main()
