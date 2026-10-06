"""Codex wrapper mouse support, including real TUI wheel events without model access."""
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import select
import shutil
import struct
import subprocess
import sys
import termios
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from multiverse.universe import agents
from multiverse.runtime import lifecycle
from multiverse.store import read, write
from native_safety import isolated_home
from test_multiverse import TerminalBase, until


class CodexMouseSetupTest(TerminalBase):
    def test_custom_codex_profile_enables_only_its_session_on_spawn_and_resume(self):
        other = self.backend.create('other', self.work, ['sleep', '60'])
        self.backend.run('set-option', '-g', 'mouse', 'off')
        self.backend.run('bind-key', '-T', 'root', 'WheelUpPane', 'display-message', 'custom-wheel')
        binding = self.backend.run('list-keys', '-T', 'root', 'WheelUpPane')
        worker = self.work / 'stand-in'
        worker.write_text(f'#!{sys.executable}\nimport os,subprocess,time\nfrom pathlib import Path\n'
                          'mouse = subprocess.check_output(["tmux", "-S", os.environ["MV2_TMUX_SOCKET"], '
                          '"show-options", "-Av", "-t", os.environ["TMUX_PANE"], "mouse"], text=True)\n'
                          'Path(os.environ["MV2_AGENT_DIR"], "launches", os.environ["MV2_LAUNCH_ID"], '
                          '"mouse-at-start").write_text(mouse)\nprint("READY", flush=True)\n'
                          'while True: time.sleep(1)\n')
        worker.chmod(0o700)
        write(self.root / 'harnesses/custom-codex/harness.json',
              {'schema': 2, 'adapter': 'codex',
               'settings': {'executable': str(worker), 'inbox_bridge': False}})
        started = lifecycle.start(self.root, 'mouse', harness='custom-codex', socket_path=self.socket,
                               wait=True, wait_timeout=5)
        self.assertTrue(started['ready'], started)
        directory = agents.directory(self.root, started['id'])
        self.assertEqual((directory / 'launches' / started['runtime']['launch_id'] / 'mouse-at-start').read_text(), 'on\n')
        session_id = str(uuid4())
        agents.bind_session(self.root, started['id'], session_id)
        self.assertEqual(self.backend.run('show-options', '-Av', '-t', started['runtime']['tmux']['session'], 'mouse'), 'on')
        self.assertEqual(self.backend.run('show-options', '-Av', '-t', other['session'], 'mouse'), 'off')
        self.assertEqual(self.backend.run('show-options', '-gv', 'mouse'), 'off')
        self.assertEqual(self.backend.run('list-keys', '-T', 'root', 'WheelUpPane'), binding)
        # Recreate the server: this must be launch setup, not a one-time server setting.
        self.kill_server()
        until(lambda: lifecycle.listing(self.root)[0]['live'] == 'dormant')
        other = self.backend.create('new-other', self.work, ['sleep', '60'])
        self.backend.run('set-option', '-g', 'mouse', 'off')
        resumed = lifecycle.start(self.root, resume=started['id'], socket_path=self.socket,
                               wait=True, wait_timeout=5)
        self.assertTrue(resumed['ready'], resumed)
        self.assertEqual((directory / 'launches' / resumed['runtime']['launch_id'] / 'mouse-at-start').read_text(), 'on\n')
        self.assertEqual(resumed['native_session_id'], session_id)
        self.assertEqual(self.backend.run('show-options', '-Av', '-t', resumed['runtime']['tmux']['session'], 'mouse'), 'on')
        self.assertEqual(self.backend.run('show-options', '-Av', '-t', other['session'], 'mouse'), 'off')
        self.assertEqual(self.backend.run('show-options', '-gv', 'mouse'), 'off')

    def test_legacy_profile_named_codex_keeps_its_existing_mouse_behavior(self):
        self.backend.create('anchor', self.work, ['sleep', '60'])
        self.backend.run('set-option', '-g', 'mouse', 'off')
        write(self.root / 'harnesses/codex/harness.json', read(self.root / 'harnesses/fake/harness.json'))
        shutil.copy(self.root / 'harnesses/fake/worker.py', self.root / 'harnesses/codex/worker.py')
        started = lifecycle.start(self.root, 'legacy', harness='codex', socket_path=self.socket,
                               wait=True, wait_timeout=5)
        self.assertTrue(started['ready'], started)
        self.assertEqual(self.backend.run('show-options', '-Av', '-t', started['runtime']['tmux']['session'], 'mouse'), 'off')


@unittest.skipUnless(shutil.which('codex') and shutil.which('tmux'), 'Codex and tmux required')
class CodexWheelTest(TerminalBase):
    def attach(self, pane, environment):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 100, 0, 0))
        env = {**environment, 'TERM': 'xterm-256color'}
        env.pop('TMUX', None)
        env.pop('TMUX_PANE', None)
        process = subprocess.Popen([*self.backend.prefix, 'attach-session', '-t', pane],
                                   stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        stop = threading.Event()

        def drain():
            while not stop.is_set():
                if select.select([master], [], [], .1)[0]:
                    try:
                        os.read(master, 65536)
                    except OSError:
                        return

        thread = threading.Thread(target=drain, daemon=True)
        thread.start()

        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            stop.set()
            thread.join(timeout=2)
            os.close(master)

        self.addCleanup(cleanup)
        return master

    def exercise_wheel(self, bridge):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"data":[]}')

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
                title = 'title' in request.get('text', {}).get('format', {}).get('schema', {}).get('properties', {})
                text = '{"title":"Wheel test"}' if title else '\n'.join(f'WHEEL_LINE_{i:03d}' for i in range(90))
                item = {'type': 'message', 'role': 'assistant', 'id': 'msg-wheel',
                        'content': [{'type': 'output_text', 'text': text}]}
                events = [{'type': 'response.created', 'response': {'id': 'resp-wheel'}},
                          {'type': 'response.output_item.done', 'item': item},
                          {'type': 'response.completed', 'response': {'id': 'resp-wheel',
                           'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}}}]
                payload = ''.join(f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n' for event in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        home = isolated_home(self.work / 'native-home', config=
            'model="gpt-6-sol"\nmodel_provider="offline"\n[model_providers.offline]\n'
            'name="Offline"\nwire_api="responses"\nrequires_openai_auth=false\n'
            f'base_url="http://127.0.0.1:{server.server_port}/v1"\n'
            f'[projects.{json.dumps(str(self.work))}]\ntrust_level="trusted"\n')
        environment = {**os.environ, 'CODEX_HOME': str(home), 'CODEX_SQLITE_HOME': str(home),
                       'NO_PROXY': '127.0.0.1,localhost', 'HTTP_PROXY': '', 'HTTPS_PROXY': '',
                       'ALL_PROXY': '', 'http_proxy': '', 'https_proxy': '', 'all_proxy': ''}
        if bridge:
            managed = home / 'packages/standalone/current/codex'
            managed.parent.mkdir(parents=True)
            managed.symlink_to(Path(shutil.which('codex')).resolve())
            self.addCleanup(lambda: subprocess.run(['codex', 'app-server', 'daemon', 'stop'],
                                                  env=environment, capture_output=True, timeout=20))
        path = self.root / 'harnesses/codex/harness.json'
        spec = read(path)
        spec['settings'] = {'inbox_bridge': bridge,
                            'args': ['--sandbox', 'workspace-write', '--ask-for-approval', 'never',
                                     '-c', 'model_provider="offline"']
                                    + ([] if bridge else ['--no-daemon'])}
        write(path, spec)
        with patch.dict(os.environ, environment):
            # Plain runners inherit the environment of their tmux server;
            # create it inside the isolated native environment too.
            self.backend.create('unrelated', self.work, ['sleep', '60'])
            self.backend.run('set-option', '-g', 'mouse', 'off')
            started = lifecycle.start(self.root, 'wheel', 'HISTORY_SENTINEL', 'codex', socket_path=self.socket,
                                   wait=True, wait_timeout=30)
        self.assertTrue(started['ready'], started)
        pane = started['runtime']['tmux']['pane']
        master = self.attach(pane, environment)
        capture = lambda: self.backend.run('capture-pane', '-p', '-t', pane)
        try:
            until(lambda: 'WHEEL_LINE_089' in capture(), seconds=30)
        except AssertionError:
            self.fail('Codex did not render the local response:\n' + capture())

        def check_wheel(terminal, target):
            self.assertEqual(self.backend.run('display-message', '-p', '-t', target, '#{alternate_on} #{mouse_any_flag}'), '1 1')
            os.write(terminal, b'\x1b[<64;10;10M' * 3)
            until(lambda: 'Back to bottom' in self.backend.run('capture-pane', '-p', '-t', target))
            self.assertEqual(self.backend.run('display-message', '-p', '-t', target, '#{pane_in_mode}'), '0')
            self.assertIn('Ask Codex to do anything', self.backend.run('capture-pane', '-p', '-t', target))
            self.assertEqual(self.backend.run('show-options', '-gv', 'mouse'), 'off')

        check_wheel(master, pane)
        if bridge:
            native_id = started['native_session_id']
            lifecycle.terminal_action(self.root, started['id'], 'stop')
            with patch.dict(os.environ, environment):
                resumed = lifecycle.start(self.root, resume=started['id'], socket_path=self.socket,
                                       wait=True, wait_timeout=30)
            self.assertTrue(resumed['ready'], resumed)
            self.assertEqual(resumed['native_session_id'], native_id)
            pane = resumed['runtime']['tmux']['pane']
            master = self.attach(pane, environment)
            until(lambda: 'WHEEL_LINE_089' in capture(), seconds=30)
            check_wheel(master, pane)

    def test_plain_tui_wheel_scrolls_transcript_without_composer_history_or_copy_mode(self):
        self.exercise_wheel(False)

    def test_bridge_tui_wheel_scrolls_transcript_on_spawn_and_resume(self):
        self.exercise_wheel(True)
