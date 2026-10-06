"""Agent-local status rendering and real isolated tmux wrappers; no native daemon."""
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shlex
import struct
import subprocess
import sys
import termios
import time
import unittest
from uuid import uuid4

from multiverse import agents, universe
from multiverse.universe import templates
from multiverse.terminal import statusline
from multiverse.store import append_message, inbox, read, write
from test_multiverse import BaseTest, TerminalBase


class StatusRendererTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.agent, self.launch = str(uuid4()), str(uuid4())
        self.directory = self.root / 'state/agents' / self.agent
        self.properties = {'id': self.agent, 'universe_id': read(self.root / 'universe.json')['id'],
                           'name': 'worker', 'harness': 'codex', 'phase': 'working', 'summary': 'Read the patch',
                           'native_session_id': str(uuid4())}
        self.runtime = {'launch_id': self.launch, 'state': 'running', 'status': 'waiting',
                        'usage': {'source': 'codex-app-server', 'native_session_id': self.properties['native_session_id'],
                                  'context': {'tokens': 82000, 'window': 200000, 'percent': 37}}}
        self.save()

    def save(self):
        write(self.directory / 'properties.json', self.properties)
        write(self.directory / 'runtime.json', self.runtime)

    def render(self, family='codex'):
        return statusline.render(self.root, self.agent, self.launch, family)

    def configure(self, **tmux):
        write(self.root / 'statusline.json', {'schema': 1, 'tmux': tmux})

    def test_template_and_code_defaults_match_including_key_order(self):
        for name in ('base', 'development', 'testing'):
            data = read(templates.TEMPLATES / name / 'statusline.json')['tmux']
            self.assertEqual(data, statusline.DEFAULTS)
            self.assertEqual(list(data['items']), list(statusline.DEFAULTS['items']))
        self.assertNotIn('ctx', self.render())
        self.assertEqual(self.render(), 'worker · codex · status waiting · phase working')
        self.properties['harness'] = 'claude'
        self.save()
        append_message(self.directory, 'pending', {})
        self.assertEqual(self.render('claude'), 'worker · claude')
        self.properties['harness'] = 'pi'
        self.save()
        self.assertEqual(self.render('pi'), 'worker · pi · phase working · inbox 1')
        self.assertEqual(self.render(''), self.render('pi'))

    def test_partial_bad_and_unknown_configuration_is_safe(self):
        expected = statusline.config({}, 'codex')
        for data in (None, [], {'schema': 9}, {'schema': True}, {'tmux': None}, {'tmux': []},
                     {'tmux': {'items': []}}, {'tmux': {'items': {'phase': 'no', 'unknown': True}}},
                     {'tmux': {'harnesses': {'codex': []}}}):
            with self.subTest(data=data):
                self.assertEqual(statusline.config(data, 'codex'), expected)
        for value, interval in ((True, 2), ('3', 2), (None, 2), (float('nan'), 2), (float('inf'), 2),
                                (-7, 1), (0, 1), (1.5, 2), (999, 60), (10**400, 60)):
            self.assertEqual(statusline.config({'tmux': {'refresh_seconds': value}})[1], interval)
        for invalid in ('{bad json', '[' * 2000 + ']' * 2000):
            (self.root / 'statusline.json').write_text(invalid)
            self.assertEqual(self.render(), 'worker · codex · status waiting · phase working')

    def test_live_order_and_harness_overrides_preserve_shared_phase_summary(self):
        self.configure(items={'inbox': True, 'summary': True, 'agent': False, 'harness': False},
                       harnesses={'codex': {'items': {'status': False}}})
        append_message(self.directory, 'pending', {})
        self.assertEqual(self.render(), 'inbox 1 · phase working: Read the patch')
        self.configure(items={'context': True, 'phase': False, 'summary': False, 'inbox': False},
                       harnesses={'codex': {'items': {'status': False}}})
        self.assertEqual(self.render(), 'ctx 82k/200k · worker · codex')

    def test_subverse_identity_and_control_and_format_escaping(self):
        child = templates.create(self.root / 'subverses/review', 'review', workdir=self.work)
        self.root = child
        self.directory = child / 'state/agents' / self.agent
        self.properties.update(universe_id=read(child / 'universe.json')['id'],
                               name='\x1b[31mworker\x1b[0m\n#{pane_id}',
                               summary='\x1b]0;bad-title\x07A summary #[fg=red] #(touch BAD)\u202e followed by more words ' * 2)
        self.save()
        self.configure(items={'summary': True})
        rendered = self.render()
        self.assertIn('worker ##{pane_id} (review)', rendered)
        self.assertIn('##[fg=red] ##(touch BAD)', rendered)
        self.assertNotIn('\x1b', rendered)
        self.assertNotIn('\u202e', rendered)
        self.assertNotIn('bad-title', rendered)
        self.assertEqual(statusline.clean('hello ' + 'world ' * 20, 20), 'hello world world…')

    def test_inbox_matches_peek_without_consuming_native_or_partial_records(self):
        append_message(self.directory, 'already read', {})
        inbox(self.directory)
        cursor = (self.directory / 'inbox.cursor').read_bytes()
        append_message(self.directory, 'steer', {}, 'steer')
        append_message(self.directory, 'follow-up', {}, 'follow-up')
        append_message(self.directory, 'manual', {})
        with (self.directory / 'inbox.jsonl').open('ab') as out:
            out.write(b'{"id":"legacy","text":"old format"}\n{"partial":')
        self.assertEqual(statusline.unread(self.directory), len(inbox(self.directory, peek=True)['messages']))
        self.assertEqual(statusline.unread(self.directory), 2)
        self.assertEqual((self.directory / 'inbox.cursor').read_bytes(), cursor)
        self.assertIn('inbox 2', self.render())

    def test_inbox_limits_and_invalid_cursor_remain_unknown(self):
        path = self.directory / 'inbox.jsonl'
        path.write_text('{"text":"legacy"}\n' * 60)
        self.assertEqual(statusline.unread(self.directory), 50)
        self.assertIn('inbox 50+', self.render())
        path.write_text(json.dumps({'schema': 2, 'delivery': 'steer', 'text': 'x' * statusline.READ_LIMIT}) + '\n')
        self.assertIsNone(statusline.unread(self.directory))
        self.assertIn('inbox ?', self.render())
        (self.directory / 'inbox.cursor').write_text(str(path.stat().st_size))
        self.assertEqual(statusline.unread(self.directory), 0)
        for cursor in ('garbage', '-1', '1', str(path.stat().st_size + 1)):
            (self.directory / 'inbox.cursor').write_text(cursor)
            self.assertIsNone(statusline.unread(self.directory))

    def test_native_values_must_belong_to_this_launch_and_session(self):
        self.configure(items={'context': True})
        self.assertIn('ctx 82k/200k', self.render())
        self.runtime['status'] = 'working'
        self.save()
        self.assertNotIn('status working', self.render())
        self.assertIn('phase working', self.render())
        self.runtime['usage']['native_session_id'] = 'another-thread'
        self.save()
        self.assertNotIn('ctx', self.render())
        self.runtime['native_relay'] = {'state': 'lost'}
        self.save()
        self.assertIn('status disconnected', self.render())
        self.runtime['exit_code'] = 0
        self.save()
        self.assertNotIn('status ', self.render())
        self.assertNotIn('ctx ', self.render())
        self.runtime['state'] = 'stopped'
        self.save()
        self.assertNotIn('status ', self.render())
        self.runtime['launch_id'] = 'replacement'
        self.save()
        self.assertEqual(self.render(), '')
        self.assertEqual(statusline.render(self.root, '../escape', self.launch), '')

    def test_entry_point_has_no_cli_harness_or_tui_imports_and_does_not_write(self):
        paths = {path: path.read_bytes() for path in self.directory.iterdir() if path.is_file()}
        code = ('import runpy,sys; sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name="__main__"); '
                'assert not any(n.startswith(("multiverse.agents","multiverse.cli","textual","websockets")) for n in sys.modules)')
        result = subprocess.run([sys.executable, '-S', '-c', code, str(Path(statusline.__file__).resolve()),
                                 str(self.root), self.agent, self.launch, 'codex'],
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), self.render())
        self.assertEqual(paths, {path: path.read_bytes() for path in self.directory.iterdir() if path.is_file()})


class StatusWrapperTest(TerminalBase):
    def attach(self, session):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 20, 130, 0, 0))
        env = {**os.environ, 'TERM': 'xterm-256color'}
        env.pop('TMUX', None)
        process = subprocess.Popen([*self.backend.prefix, 'attach-session', '-t', session],
                                   stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            os.close(master)
        self.addCleanup(cleanup)
        return master, process

    def output_until(self, master, text, timeout=6):
        data = b''
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if select.select([master], [], [], .1)[0]:
                data += os.read(master, 65536)
                if text.encode() in data:
                    return data
        self.fail(f'Status text {text!r} missing from PTY: {data!r}')

    def test_spawn_resume_options_do_not_change_globals_or_unrelated_sessions(self):
        other = self.backend.create('other', self.work, ['sleep', '60'])
        for name, value in (('status-left', 'GLOBAL LEFT'), ('status-right', 'GLOBAL RIGHT'), ('status-interval', '15')):
            self.backend.run('set-option', '-g', name, value)
        self.backend.run('set-option', '-wg', 'automatic-rename', 'on')
        self.backend.run('set-option', '-wg', 'allow-rename', 'on')
        global_format = self.backend.run('show-options', '-gv', 'status-format[0]')
        for resume in (None, 'saved'):
            if resume:
                agents.terminal_action(self.root, agent_id, 'stop')
                write(self.root / 'statusline.json', {'tmux': {'refresh_seconds': 5}})
                result = agents.start(self.root, resume=agent_id, socket_path=self.socket, wait=True, wait_timeout=5)
            else:
                result = agents.start(self.root, 'reader', harness='fake', socket_path=self.socket, wait=True, wait_timeout=5)
                agent_id = result['id']
            self.assertTrue(result['ready'], result)
            terminal = result['runtime']['tmux']
            self.assertEqual(self.backend.run('display-message', '-p', '-t', terminal['pane'], '#W'), 'reader')
            for name in ('automatic-rename', 'allow-rename'):
                self.assertEqual(self.backend.run('show-options', '-wAv', '-t', terminal['pane'], name), 'off')
                self.assertEqual(self.backend.run('show-options', '-wAv', '-t', other['pane'], name), 'on')
                self.assertEqual(self.backend.run('show-options', '-wgv', name), 'on')
            self.assertEqual(self.backend.run('show-options', '-Av', '-t', terminal['session'], 'status-interval'), '5' if resume else '2')
            self.assertEqual(self.backend.run('show-options', '-Av', '-t', terminal['session'], 'status-left'), '')
            self.assertEqual(self.backend.run('show-options', '-Av', '-t', terminal['session'], 'status-right'), '')
            self.assertEqual(self.backend.run('show-options', '-Av', '-t', other['session'], 'status-left'), 'GLOBAL LEFT')
            self.assertEqual(self.backend.run('show-options', '-gv', 'status-right'), 'GLOBAL RIGHT')
            self.assertEqual(self.backend.run('show-options', '-gv', 'status-format[0]'), global_format)
            self.assertEqual(self.backend.run('show-options', '-pAv', '-t', terminal['pane'], 'remain-on-exit'), 'on')

    def test_custom_codex_family_is_known_before_native_start(self):
        worker = self.work / 'stand-in'
        worker.write_text(f'#!{sys.executable}\nimport os,subprocess,time\nfrom pathlib import Path\n'
                          'command=subprocess.check_output(["tmux","-S",os.environ["MV2_TMUX_SOCKET"],'
                          '"show-options","-Av","-t",os.environ["TMUX_PANE"],"@mv2_status_command"],text=True)\n'
                          'Path(os.environ["MV2_AGENT_DIR"],"status-at-start").write_text(command)\n'
                          'while True: time.sleep(1)\n')
        worker.chmod(0o700)
        write(self.root / 'harnesses/custom/harness.json', {'schema': 2, 'adapter': 'codex',
              'settings': {'executable': str(worker), 'inbox_bridge': False}})
        result = agents.start(self.root, 'custom', harness='custom', socket_path=self.socket, wait=True, wait_timeout=5)
        self.assertTrue(result['ready'], result)
        command = (agents.directory(self.root, result['id']) / 'status-at-start').read_text()
        self.assertEqual(shlex.split(command)[-1], 'codex')

    def test_attached_status_updates_from_files_and_escapes_agent_text(self):
        result = agents.start(self.root, 'display', harness='fake', socket_path=self.socket, wait=True, wait_timeout=5)
        directory = agents.directory(self.root, result['id'])
        master, _ = self.attach(result['runtime']['tmux']['session'])
        self.output_until(master, 'display · fake · phase starting')
        agents.report(self.root, result['id'], 'blocked', 'Waiting')
        append_message(directory, 'hello', {})
        self.output_until(master, 'inbox 1')
        # Configuration is read on each refresh, not just when a wrapper starts.
        write(self.root / 'statusline.json', {'tmux': {'items': {'summary': True, 'phase': False, 'inbox': False}}})
        agents.report(self.root, result['id'], 'done', 'LITERAL #[fg=red] #(touch BAD) #{pane_id}\x1b[31m')
        output = self.output_until(master, 'LITERAL #[fg=red] #(touch BAD) #{pane_id}')
        self.assertNotIn(b'\x1b[31m', output)
        self.assertFalse((self.work / 'BAD').exists())
        self.assertEqual(len(inbox(directory, peek=True)['messages']), 1)

    def test_shell_and_tmux_characters_in_root_paths_are_literal(self):
        root = templates.create(self.work / "odd ' ) #(touch BAD) #{pane_id}" / '.multiverse', 'odd', workdir=self.work)
        # Use the existing fixture's stand-in harness without creating another native process type.
        import shutil
        shutil.copytree(self.root / 'harnesses/fake', root / 'harnesses/fake')
        result = agents.start(root, 'quoted', harness='fake', socket_path=self.socket, wait=True, wait_timeout=5)
        master, _ = self.attach(result['runtime']['tmux']['session'])
        self.output_until(master, 'quoted · fake · phase starting')
        self.assertFalse((self.work / 'BAD').exists())


if __name__ == '__main__':
    unittest.main()
