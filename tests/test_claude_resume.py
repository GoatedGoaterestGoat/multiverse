"""Exact Claude resume/empty recovery, using disposable storage and tmux only."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import pty
import select
import time
import sys
from unittest.mock import patch

from test_harnesses import HarnessLaunchTest
from multiverse import agents, harnesses
from multiverse.claude_terminal import Diagnostics
from multiverse.harnesses.claude import history
from multiverse.harnesses.types import SessionHandle, LaunchContext
from multiverse.store import read, write
from multiverse.tools import resume
from test_multiverse import BaseTest, until


class ClaudeResumeTest(HarnessLaunchTest):
    def launch(self, task='', legacy=False):
        self.fake_cli('claude')
        if legacy:
            path = self.root / 'harnesses/claude/harness.json'
            executable = read(path)['settings']['executable']
            spec = read(harnesses.PACKAGES / 'claude/defaults.json')
            spec['spawn'][0] = spec['resume'][0] = executable
            spec['resume'][1:1] = ['--permission-mode', 'default']
            write(path, spec)
        agent = agents.start(self.root, 'claude-test', task, 'claude', socket_path=self.socket)
        directory = agents.directory(self.root, agent['id'])
        until(lambda: (directory / 'seen.json').exists())
        return agent, directory

    def restart(self, agent, directory, empty=False):
        (directory / 'seen.json').unlink(missing_ok=True)
        with patch.dict(os.environ, {'MV2_TMUX_SOCKET': self.socket}):
            result = resume.run(self.root, agent=agent['id'], start_empty=empty)
        until(lambda: (directory / 'seen.json').exists())
        return result, read(directory / 'seen.json')

    def test_stop_before_interaction_reinitializes_same_uuid_for_both_schemas(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                agent, directory = self.launch(legacy=legacy)
                config = (self.root / 'harnesses/claude/harness.json').read_bytes()
                self.assertEqual(read(directory / 'seen.json')['tty'], [True, True, True])
                self.stop_agent(agent)
                restarted, seen = self.restart(agent, directory)
                self.assertIn('--session-id', seen['argv'])
                self.assertNotIn('--resume', seen['argv'])
                self.assertEqual(restarted['native_session_id'], agent['native_session_id'])
                self.assertNotEqual(restarted['runtime']['launch_id'], agent['runtime']['launch_id'])
                self.assertEqual((self.root / 'harnesses/claude/harness.json').read_bytes(), config)
                if legacy:
                    self.assertIn('--permission-mode', seen['argv'])
                self.stop_agent(restarted)

    def test_stop_before_ready_gate_can_restart_same_reserved_uuid(self):
        self.fake_cli('claude')
        touch = Path.touch
        def hold_ready(path, *args, **kwargs):
            if path.name != 'ready':
                return touch(path, *args, **kwargs)
        with patch.object(Path, 'touch', hold_ready):
            agent = agents.start(self.root, 'before-ready', '', 'claude', socket_path=self.socket)
        directory = agents.directory(self.root, agent['id'])
        self.assertFalse((directory / 'seen.json').exists())
        self.stop_agent(agent)
        restarted, seen = self.restart(agent, directory)
        self.assertIn('--session-id', seen['argv'])
        self.assertEqual(restarted['native_session_id'], agent['native_session_id'])
        self.stop_agent(restarted)

    def test_saved_history_resumes_with_pinned_home_storage_and_workspace(self):
        agent, directory = self.launch('persist this task')
        original = read(directory / 'properties.json')
        transcript = history.location(original['native_environment'], str(self.work), agent['native_session_id'])
        before = transcript.read_bytes()
        self.stop_agent(agent)
        other = self.work / 'different-home'
        with patch.dict(os.environ, {'HOME': str(other), 'CLAUDE_CONFIG_DIR': str(other / '.claude')}):
            restarted, seen = self.restart(agent, directory)
        self.assertIn('--resume', seen['argv'])
        self.assertEqual(seen['home'], str(self.home))
        self.assertEqual(seen['storage'], str(self.storage))
        self.assertEqual(seen['cwd'], str(self.work))
        self.assertEqual(transcript.read_bytes(), before)
        self.stop_agent(restarted)

    def test_missing_used_history_and_legacy_unknown_never_reset(self):
        agent, directory = self.launch('used')
        self.stop_agent(agent)
        props = read(directory / 'properties.json')
        transcript = history.location(props['native_environment'], str(self.work), agent['native_session_id'])
        transcript.unlink()
        before = read(directory / 'runtime.json')
        with self.assertRaisesRegex(ValueError, 'missing.*automatic reset is unsafe'):
            self.restart(agent, directory)
        self.assertEqual(read(directory / 'runtime.json'), before)
        props.pop('native_environment')
        write(directory / 'properties.json', props)
        with self.assertRaisesRegex(ValueError, 'storage identity was not recorded'):
            self.restart(agent, directory)
        self.assertEqual(read(directory / 'properties.json'), props)

    def test_explicit_empty_start_retains_history_and_changes_only_native_identity(self):
        agent, directory = self.launch('old task must not replay')
        self.stop_agent(agent)
        props = read(directory / 'properties.json')
        transcript = history.location(props['native_environment'], str(self.work), agent['native_session_id'])
        before = transcript.read_bytes()
        restarted, seen = self.restart(agent, directory, empty=True)
        self.assertEqual(restarted['id'], agent['id'])
        self.assertNotEqual(restarted['native_session_id'], agent['native_session_id'])
        self.assertEqual(restarted['native_session_history'][-1]['session_id'], agent['native_session_id'])
        self.assertIn('--session-id', seen['argv'])
        self.assertNotIn('old task must not replay', seen['argv'])
        self.assertEqual(transcript.read_bytes(), before)
        self.stop_agent(restarted)

    def test_input_marks_initialization_uncertain_before_forwarding(self):
        agent, directory = self.launch()
        self.backend.run('send-keys', '-t', agent['runtime']['tmux']['pane'], 'hello', 'Enter')
        until(lambda: not agents.runtime(directory)['claude_session']['never_initialized'])
        transcript = history.location(read(directory / 'properties.json')['native_environment'], str(self.work), agent['native_session_id'])
        until(transcript.exists)
        self.stop_agent(agent)
        transcript.unlink()
        with self.assertRaisesRegex(ValueError, 'automatic reset is unsafe'):
            self.restart(agent, directory)

    def test_provider_side_history_seen_then_removed_is_not_reinitialized(self):
        agent, directory = self.launch()
        props = read(directory / 'properties.json')
        transcript = history.location(props['native_environment'], str(self.work), agent['native_session_id'])
        transcript.parent.mkdir(parents=True)
        transcript.write_text('{}\n')
        until(lambda: not agents.runtime(directory)['claude_session']['never_initialized'])
        transcript.unlink()
        self.stop_agent(agent)
        with self.assertRaisesRegex(ValueError, 'automatic reset is unsafe'):
            self.restart(agent, directory)

    def test_fast_error_retained_after_pane_disappears_without_secret_output(self):
        self.fake_cli('claude')
        self.storage.mkdir(parents=True)
        (self.storage / 'fail').touch()
        agent = agents.start(self.root, 'fast-error', '', 'claude', socket_path=self.socket)
        directory = agents.directory(self.root, agent['id'])
        until(lambda: agents.runtime(directory).get('exit_code') == 1)
        until(lambda: agents.listing(self.root)[0]['live'] == 'stopped')
        row = agents.listing(self.root)[0]
        self.assertIn('No conversation found', row['reason'])
        self.assertIn('No conversation found', agents.terminal_action(self.root, agent['id'], 'inspect'))
        log = directory / 'launches' / agent['runtime']['launch_id'] / 'diagnostics.json'
        self.assertNotIn('SECRET_', log.read_text())
        self.assertLess(log.stat().st_size, 2048)
        self.assertNotIn(agent['runtime']['tmux']['pane'], self.backend.snapshot()[0])

    def test_claude_detach_keeps_provider_alive_and_exact_session(self):
        agent, directory = self.launch()
        pid = agents.runtime(directory)['pid']
        self.backend.run('set-option', '-g', 'prefix', 'C-b')
        self.backend.run('bind-key', 'd', 'detach-client')
        master, slave = pty.openpty()
        env = {key: value for key, value in os.environ.items() if key not in ('TMUX', 'TMUX_PANE')}
        env['TERM'] = 'xterm-256color'
        client = subprocess.Popen([*self.backend.prefix, 'attach-session', '-t', agent['runtime']['tmux']['session']],
                                  stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        try:
            output = b''
            deadline = time.monotonic() + 5
            while b'HARNESS READY' not in output and time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    output += os.read(master, 65536)
            self.assertIn(b'HARNESS READY', output)
            os.write(master, b'\x02d')
            client.wait(timeout=5)
            self.assertEqual(client.returncode, 0)
            self.assertEqual(agents.runtime(directory)['pid'], pid)
            self.assertEqual(read(directory / 'properties.json')['native_session_id'], agent['native_session_id'])
            self.assertEqual(agents.listing(self.root)[0]['live'], 'alive')
        finally:
            if client.poll() is None:
                client.kill()
                client.wait()
            os.close(master)
        self.stop_agent(agent)

    def test_replaced_storage_and_unfinished_runner_block_recovery(self):
        agent, directory = self.launch()
        self.stop_agent(agent)
        projects = self.storage / 'projects'
        projects.rename(self.storage / 'old-projects')
        projects.mkdir()
        with self.assertRaisesRegex(ValueError, 'automatic reset is unsafe'):
            self.restart(agent, directory)
        facts = agents.runtime(directory)
        facts['claude_session']['closed'] = False
        facts['runner_pid'] = os.getpid()
        write(directory / 'runtime.json', facts)
        for empty in (False, True):
            with self.assertRaisesRegex(ValueError, 'not finished stopping'):
                self.restart(agent, directory, empty=empty)

    def test_explicit_recovery_after_runner_crash_requires_dead_processes(self):
        agent, directory = self.launch()
        self.stop_agent(agent)
        facts = agents.runtime(directory)
        facts['claude_session']['closed'] = False
        write(directory / 'runtime.json', facts)
        with self.assertRaisesRegex(ValueError, 'not finished stopping'):
            self.restart(agent, directory)
        with patch('multiverse.agents.os.kill', side_effect=ProcessLookupError):
            restarted, _ = self.restart(agent, directory, empty=True)
        self.assertNotEqual(restarted['native_session_id'], agent['native_session_id'])
        self.stop_agent(restarted)

    def test_unknown_resume_fixture_fails_realistically(self):
        self.fake_cli('claude')
        result = subprocess.run([str(self.root / 'harnesses/claude/fake-cli'), '--resume', 'deadbeef-dead-beef-dead-beefdeadbeef'],
                                env=os.environ, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn('No conversation found', result.stdout)

    def test_inaccessible_discovery_and_stale_generation_block_reinitialization(self):
        agent, directory = self.launch()
        self.stop_agent(agent)
        original_scan = os.scandir
        def inaccessible(path):
            if Path(path) == self.storage / 'projects':
                raise PermissionError
            return original_scan(path)
        with patch('multiverse.harnesses.claude.history.os.scandir', side_effect=inaccessible):
            with self.assertRaisesRegex(ValueError, 'unknown or inaccessible'):
                self.restart(agent, directory)
        facts = agents.runtime(directory)
        facts['claude_session']['launch_id'] = 'stale'
        write(directory / 'runtime.json', facts)
        with self.assertRaisesRegex(ValueError, 'automatic reset is unsafe'):
            self.restart(agent, directory)


class ClaudeDiscoveryTest(BaseTest):
    def test_metadata_corruption_wrong_identity_and_other_workspace_are_unknown(self):
        env = {'HOME': str(self.work), 'CLAUDE_CONFIG_DIR': str(self.work / 'claude')}
        sid = '12345678-1234-1234-1234-123456789abc'
        path = history.location(env, str(self.work), sid)
        path.parent.mkdir(parents=True)
        for data in ('', '{bad', json.dumps({'type': 'queue-operation'}),
                     json.dumps({'type': 'user', 'sessionId': sid, 'cwd': '/other', 'message': 'x'})):
            path.write_text(data)
            self.assertEqual(history.discover(env, str(self.work), sid), 'unknown')
        path.unlink()
        foreign = history.location(env, '/other', sid)
        foreign.parent.mkdir()
        foreign.write_text('{}')
        self.assertEqual(history.discover(env, str(self.work), sid), 'unknown')

    def test_fifo_and_broken_symlink_are_unknown_without_blocking(self):
        env = {'HOME': str(self.work), 'CLAUDE_CONFIG_DIR': str(self.work / 'claude')}
        sid = '12345678-1234-1234-1234-123456789abc'
        path = history.location(env, str(self.work), sid)
        path.parent.mkdir(parents=True)
        os.mkfifo(path)
        self.assertEqual(history.discover(env, str(self.work), sid), 'unknown')
        path.unlink()
        path.symlink_to(self.work / 'missing')
        self.assertEqual(history.discover(env, str(self.work), sid), 'unknown')

    def test_diagnostics_are_bounded_allowlisted_and_handle_split_ansi_error(self):
        collector = Diagnostics('recorded-uuid')
        collector.feed(b'token=SECRET' * 10000)
        collector.feed(b'\x1b[31mNo conversation found with session ')
        collector.feed(b'ID: arbitrary-secret\x1b[0m')
        self.assertEqual(collector.messages, ['No conversation found with session ID: recorded-uuid'])
        self.assertLessEqual(len(collector.tail), 8192)
