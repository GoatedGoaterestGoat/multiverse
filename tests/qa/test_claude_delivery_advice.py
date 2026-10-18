"""Relevant public CLI scenarios from qa-claude f3df071, pinned to this checkout."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
import sys
from isolation import track

class ClaudeLaunchOnly(unittest.TestCase):

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix='qacl-', dir='/tmp')).resolve()
        self.addCleanup(shutil.rmtree, self.scratch, True)
        self.work = self.scratch / 'work'
        self.work.mkdir()
        home = self.scratch / 'home'
        home.mkdir()
        self.env = {k: v for (k, v) in os.environ.items() if not k.startswith(('MV2_', 'CLAUDE', 'CODEX_')) and k not in ('TMUX', 'TMUX_PANE')}
        self.env.update(HOME=str(home), CLAUDE_CONFIG_DIR=str(home / '.claude'), CODEX_HOME=str(home / '.codex'), MV2_TMUX_SOCKET=str(self.scratch / 't.sock'))
        track(tmux_socket=self.env['MV2_TMUX_SOCKET'])
        self.mv2('init', str(self.work))
        self.addCleanup(subprocess.run, ['tmux', '-S', self.env['MV2_TMUX_SOCKET'], 'kill-server'], capture_output=True)
        fake = self.scratch / 'claude'
        fake.write_text('#!/bin/sh\n[ "$1" = --version ] && { echo "2.1.302 (Claude Code)"; exit 0; }\nexec sleep 600\n')
        fake.chmod(493)
        for (name, settings) in (('claude-plain', {'executable': str(fake), 'inbox_bridge': False}), ('claude-auto', {'executable': str(fake)})):
            directory = self.work / '.multiverse/harnesses' / name
            directory.mkdir(parents=True)
            (directory / 'harness.json').write_text(json.dumps({'schema': 2, 'adapter': 'claude', 'settings': settings}))

    def mv2(self, *args, code=0):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[2] / 'bin/mv2'), *args], cwd=self.work, env=self.env, capture_output=True, text=True, timeout=90)
        if code is not None:
            self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result

    def test_send_to_launch_only_agent_does_not_advise_kill_and_resume(self):
        """docs/cli/messaging.md, Read the result: the kill-and-resume advice is for "a running
        agent whose messaging broke"; a launch-only agent "can't receive steer or follow-up
        messages, so send it inbox-only messages instead". docs/use/harnesses.md: with
        `inbox_bridge` false the agent stays launch-only at every resume, so kill and resume
        can't help."""
        self.addCleanup(self.mv2, 'kill', 'worker', code=None)
        self.mv2('spawn', '--name', 'worker', '--harness', 'claude-plain', '--wait')
        for delivery in ('steer', 'follow-up'):
            result = self.mv2('send', 'worker', '--delivery', delivery, '--text', 'hello', code=None)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Not delivered to worker', result.stderr)
            self.assertNotIn('mv2 kill', result.stderr)
        inbox = self.mv2('inbox', '--agent', 'worker', '--all', '--json')
        self.assertEqual(len(json.loads(inbox.stdout)['messages']), 2)

    def test_retry_on_running_launch_only_agent_does_not_say_resume_it(self):
        """docs/cli/messaging.md: `Not delivered ... Saved; ...` tells you what to do next. After the
        advised kill and resume, the agent is running again, so "resume it" is not a next step."""
        self.addCleanup(self.mv2, 'kill', 'worker', code=None)
        self.mv2('spawn', '--name', 'worker', '--harness', 'claude-plain', '--wait')
        self.mv2('send', 'worker', '--text', 'hello', code=None)
        self.mv2('kill', 'worker')
        self.mv2('resume', 'worker', '--wait')
        result = self.mv2('deliver', 'worker', '--retry', code=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('resume it', result.stdout + result.stderr)

    def agent_dir(self, name):
        for properties in (self.work / '.multiverse/state/agents').glob('*/properties.json'):
            if json.loads(properties.read_text())['name'] == name:
                return properties.parent
        self.fail('no agent named ' + name)

    def test_send_with_wrong_type_inbox_state_does_not_crash(self):
        """docs/reference/files.md: `inbox-state.json` holds "delivery attempts and receipts per
        message". docs/cli/messaging.md: "Every message is saved to the recipient's inbox before mv2
        tries to deliver it", and failures print one line per recipient. A damaged file must not end
        `send` with a Python traceback."""
        self.addCleanup(self.mv2, 'kill', 'worker', code=None)
        self.mv2('spawn', '--name', 'worker', '--harness', 'claude-plain', '--wait')
        (self.agent_dir('worker') / 'inbox-state.json').write_text('[]')
        result = self.mv2('send', 'worker', '--text', 'hello', code=None)
        self.assertNotIn('Traceback', result.stderr)
        self.assertIn('worker', result.stderr)

    def test_send_that_failed_on_damaged_state_is_not_left_uncertain(self):
        """docs/cli/messaging.md, Check and retry deliveries: "--retry resends only definite failures.
        It never resends messages that were accepted or are uncertain." A message mv2 never submitted,
        because `inbox-state.json` was unreadable, must not stay "cannot yet tell" once the file is
        gone, or neither --check nor --retry can ever settle it."""
        self.addCleanup(self.mv2, 'kill', 'worker', code=None)
        self.mv2('spawn', '--name', 'worker', '--harness', 'claude-plain', '--wait')
        state = self.agent_dir('worker') / 'inbox-state.json'
        state.write_text('{"trunc\n')
        self.mv2('send', 'worker', '--text', 'hello', code=None)
        state.unlink()
        result = self.mv2('deliver', 'worker', '--check', code=None)
        self.assertNotIn('cannot yet tell', result.stdout + result.stderr)

    # qa-claude e75b3b1: same public failed-wake scenario.
    def test_wake_message_to_an_agent_that_fails_to_start_is_retryable(self):
        """docs/cli/messaging.md: a steer to a stopped agent resumes it with the message as its first
            prompt; "--retry resends only definite failures" and an uncertain message must not be resent.
            When the woken launch exits during startup, the message definitely didn't arrive, so it must
            not stay "cannot yet tell" forever where neither --check nor --retry can settle it."""
        failing = self.scratch / 'claude-fails'
        failing.write_text('#!/bin/sh\n[ "$1" = --version ] && { echo "2.1.302 (Claude Code)"; exit 0; }\necho "fake claude failing"; exit 3\n')
        failing.chmod(493)
        directory = self.work / '.multiverse/harnesses/claude-fails'
        directory.mkdir(parents=True)
        (directory / 'harness.json').write_text(json.dumps({'schema': 2, 'adapter': 'claude', 'settings': {'executable': str(failing)}}))
        self.addCleanup(self.mv2, 'kill', 'worker', code=None)
        self.mv2('spawn', '--name', 'worker', '--harness', 'claude-fails', '--wait', code=None)
        self.mv2('send', 'worker', '--text', 'hello', code=None)
        deadline = time.monotonic() + 30
        while 'exited with code 3' not in self.mv2('list', '--wide').stdout:
            self.assertLess(time.monotonic(), deadline, 'the woken launch never failed')
            time.sleep(0.5)
        result = self.mv2('deliver', 'worker', '--check', code=None)
        self.assertNotIn('cannot yet tell', result.stdout + result.stderr)
