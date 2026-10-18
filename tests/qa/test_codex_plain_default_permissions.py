"""CODEX-03: launch-only default native permissions must reach Codex's UI.

Black-box CLI/tmux regression; real native controls use the same private home.
"""
import json
import os
import shlex
import shutil
import time
import unittest

from qa import test_codex_refusal_lifecycle as native_fixture


@unittest.skipUnless(os.environ.get('MV2_QA_REAL_CODEX') == '1', 'requires real Codex opt-in')
class PlainDefaultPermissions(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixture.PlainRefusalLifecycle()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.control_socket = str(self.fixture.scratch / 'c.sock')
        self.addCleanup(self.fixture.run_cli, 'tmux', '-S', self.control_socket, 'kill-server')

    def configure(self, trusted, auto=False):
        config = ('model = "gpt-6-luna"\nmodel_reasoning_effort = "low"\n'
                  'check_for_update_on_startup = false\n')
        if trusted:
            config += '[projects.' + json.dumps(str(self.fixture.work)) + ']\ntrust_level = "trusted"\n'
        (self.fixture.codex / 'config.toml').write_text(config)
        settings = {'args': ['--no-daemon']}
        if not auto:
            settings['inbox_bridge'] = False
        profile = self.fixture.work / '.multiverse/harnesses/plain/harness.json'
        profile.write_text(json.dumps({'schema': 2, 'adapter': 'codex', 'settings': settings}))

    def bare_native_control(self, trusted):
        native = shutil.which('codex', path=self.fixture.env['PATH'])
        self.assertTrue(native)
        wrapper = self.fixture.scratch / 'native-control.py'
        wrapper.write_text('import os\ne={k:v for k,v in os.environ.items() if not k.startswith("MV2_")}\n'
                           + 'os.execve(' + repr(native) + ', ' + repr([native, '--no-daemon', '--cd', str(self.fixture.work)]) + ', e)\n')
        command = shlex.join(['python3', str(wrapper)])
        started = self.fixture.run_cli('tmux', '-S', self.control_socket, 'new-session', '-d', '-s', 'bare',
                                      '-x', '120', '-y', '36', command)
        self.assertEqual(started.returncode, 0, started.stderr)
        start = time.monotonic()
        end = start + 15
        screen = ''
        while time.monotonic() < end:
            screen = self.fixture.run_cli('tmux', '-S', self.control_socket, 'capture-pane', '-p', '-t', 'bare').stdout
            if time.monotonic() - start >= 3 and ('trust' in screen.lower() or 'Ask Codex' in screen):
                break
            time.sleep(.2)
        label = 'trusted' if trusted else 'untrusted'
        (self.fixture.evidence / (self.fixture.scratch.name + '-bare-default-' + label + '.txt')).write_text(screen)
        alive = self.fixture.run_cli('tmux', '-S', self.control_socket, 'list-panes', '-t', 'bare', '-F', '#{pane_dead}')
        self.assertEqual(alive.stdout.strip(), '0', screen)
        self.assertNotIn('Error adding directories', screen)
        self.assertTrue('Ask Codex' in screen or 'trust' in screen.lower(), screen)
        self.fixture.run_cli('tmux', '-S', self.control_socket, 'kill-server')

    def record(self, label, result, sid=None):
        inspected = self.fixture.run_cli('mv2', 'inspect', 'plain')
        listed = self.fixture.run_cli('mv2', 'list', '--json')
        row = next(x for x in json.loads(listed.stdout) if x['name'] == 'plain')
        record = {'case': label, 'rc': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr,
                  'inspect': inspected.stdout, 'row': row, 'saved_sid_before_resume': sid}
        (self.fixture.evidence / (self.fixture.scratch.name + '-default-permissions-' + label + '.json')).write_text(
            json.dumps(record, indent=2))
        return row

    def check_launch(self, trusted, auto=False):
        self.configure(trusted, auto)
        self.bare_native_control(trusted)
        self.configure(trusted, auto)
        result = self.fixture.run_cli('mv2', 'spawn', '--name', 'plain', '--harness', 'plain',
                                      '--wait', '--wait-timeout', '120', timeout=150)
        row = self.record(('trusted' if trusted else 'untrusted') + ('-auto' if auto else '-explicit'), result)
        self.assertEqual(row['runtime']['native_mode']['mode'], 'plain')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_untrusted_plain_default_permissions_reaches_native_trust_prompt(self):
        """docs/use/harnesses.md: launch-only starts Codex normally; native trust stays native."""
        self.check_launch(False)

    def test_trusted_plain_default_permissions_starts(self):
        """docs/use/harnesses.md: launch-only does not require permission flags."""
        self.check_launch(True)

    def test_automatic_fallback_untrusted_default_permissions_reaches_native_prompt(self):
        """docs/use/harnesses.md: --no-daemon with auto policy falls back to a working plain launch."""
        self.check_launch(False, auto=True)

    def test_resuming_plain_with_default_permissions_in_trusted_and_untrusted_folder(self):
        """docs/cli/agents.md: resume the saved native session using current harness settings."""
        started = self.fixture.run_cli('mv2', 'spawn', '--name', 'plain', '--harness', 'plain',
            '--task', 'Run "$MV2_CLI" session-bind --session-id "$CODEX_THREAD_ID". Then reply BIND_READY only.',
            '--wait', '--wait-timeout', '45')
        self.assertEqual(started.returncode, 0, started.stderr)
        end = time.monotonic() + 35
        sid = None
        while time.monotonic() < end:
            _, props, _ = self.fixture.agent('plain')
            sid = props.get('native_session_id')
            if sid and '• BIND_READY' in self.fixture.screen('plain', 'bound-before-resume'):
                break
            time.sleep(.25)
        self.assertTrue(sid)
        for trusted in (True, False):
            with self.subTest(trusted=trusted):
                self.fixture.run_cli('mv2', 'kill', 'plain', '--attachment-only')
                self.configure(trusted)
                result = self.fixture.run_cli('mv2', 'resume', 'plain', '--wait', '--wait-timeout', '120', timeout=150)
                row = self.record('resume-' + ('trusted' if trusted else 'untrusted'), result, sid)
                self.assertEqual(row.get('native_session_id'), sid)
                self.assertEqual(result.returncode, 0, result.stderr)
