"""Antigravity CLI adapter, local bridge, hooks/statusLine and real tmux handoff contracts."""
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multiverse import harnesses
from multiverse.harnesses import agy
from multiverse.harnesses.agy import AgyAdapter, BRIDGE
from multiverse.harnesses.agy import bridge as agy_bridge
from multiverse.harnesses.agy import storage as agy_storage
from multiverse.harnesses.base import PARENT_AGY
from multiverse.harnesses.types import CommandSpec, LaunchContext, Message, NativeEndpoint, SessionHandle, ShellCallbackResult
from multiverse.runtime import launch_files, lifecycle, native, runner
from multiverse.store import now, read, write
from multiverse.tools import _messaging
from multiverse.universe import agents
from native_cli import forbidden_python, mv2_launcher
from test_multiverse import BaseTest, TerminalBase, until


class _LoopbackListener:
    """Minimal loopback TCP listener for endpoint pinning tests (no HTTP or ConversationService endpoints)."""

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(('127.0.0.1', 0))
        self._sock.listen(16)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self.address = f'127.0.0.1:{self.port}'
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=5)


class AgyAdapterUnitTest(BaseTest):
    def _values(self, **overrides):
        launch_dir = self.work / 'agent' / 'launches' / str(uuid4())
        values = {
            'name': 'builder',
            'agent_id': str(uuid4()),
            'session_id': str(uuid4()),
            'bootstrap': 'Universe bootstrap instructions.',
            'bootstrap_json': '"Universe bootstrap instructions."',
            'task': '--help\n$(false)',
            'agent_dir': str(self.work / 'agent'),
            'harness_dir': str(self.root / 'harnesses' / 'agy'),
            'launch_dir': str(launch_dir),
            'session_file': str(self.work / 'agent' / 'session.jsonl'),
            'bootstrap_file': str(launch_dir / 'bootstrap.md'),
            'task_file': str(launch_dir / 'task.md'),
            'workspace': str(self.work),
            'model': 'gemini-3.8-flash-low',
            'control_socket': str(self.work / 'rpc.sock'),
        }
        values.update(overrides)
        return values

    def _create_saved_conversation(self, native_id, *, brain_only=False):
        home = agy_storage.agy_home()
        if brain_only:
            _, transcript = agy_storage.transcript_paths(native_id, home)
            transcript.parent.mkdir(parents=True, exist_ok=True)
            transcript.write_text(json.dumps({'type': 'USER_INPUT', 'content': 'hello'}) + '\n')
        else:
            db = home / 'conversations' / f'{native_id}.db'
            db.parent.mkdir(parents=True, exist_ok=True)
            db.write_bytes(b'SQLite format 3\x00')
        return home

    def test_spawn_resume_settings_merge_and_missing_conversation_refusal(self):
        """Agy spawn/resume place --prompt-interactive last, write AGENTS.md/hooks/settings launch_files, merge user
        --settings instead of clobbering it, scrub PARENT_AGY, and refuse resume when no saved conversation exists;
        fragile because agy --conversation <missing> silently starts a fresh session and last --settings wins."""
        user_settings = self.work / 'user-agy-settings.json'
        user_settings.write_text(json.dumps({'theme': 'ocean', 'sandbox': {'enabled': True}}))
        adapter = AgyAdapter.configured('agy', {'args': ['--settings', str(user_settings)]})
        repo_ws = self.work / 'repo-workspace'
        repo_ws.mkdir()
        values = self._values(workspace=str(repo_ws))

        with self.subTest('bridged spawn merges user settings, adds minimal sandbox allow rules and keeps prompt last'):
            user_settings.write_text(json.dumps({
                'theme': 'ocean',
                'sandbox': {'enabled': True},
                'permissions': {
                    'allow': [
                        'read_file(/custom)',
                        'read_file(~/.aws)',
                        'read_file(~//.aws)',
                        'write_file(~/scratch/out/)',
                        'command(cat ~/.aws/config)',
                        'read_file(~/Library/Application Support/custom-app)',
                        'read_file(~/.gemini/GEMINI.md)',
                        'read_file(~/.gemini/config/mcp_config.json)',
                        'read_file(~/.gemini/antigravity-cli/brain/session-1)',
                        'read_file(~/.ssh)',
                        'read_file(~//.ssh)',
                        'read_file(~/.gnupg)',
                        'read_file(~/.gemini/antigravity-browser-profile)',
                        'read_file(~/.gemini/antigravity-browser-profile/Default/Cookies)',
                        'read_file(~/.gemini/jetski-standalone-oauth-token)',
                        'read_file(~/.gemini/antigravity-cli/antigravity-oauth-token)',
                        'read_file(~/.gemini/antigravity-cli/jetski-cli-oauth-token)',
                        'read_file(~/.gemini/antigravity-cli/mcp_oauth_tokens.json)',
                        'read_file(~/.gemini/antigravity-cli/cache/acp-auth-state.json)',
                        'read_file(~/Library/Application Support/antigravity/plugin-secrets.json)',
                        'read_file(~/.config/antigravity/plugin-secrets.json)',
                        'read_file(~/.gemini/antigravity-cli)',
                        'read_file(~/Library/Keychains)',
                        'read_file(~/.multiverse/plugin-secrets)',
                        'read_file(/)',
                        'read_file(*)',
                        'read_file(~otheruser/.aws)',
                        'read_file(./notes.md)',
                        'read_file(src/**)',
                        'read_file($HOME/.aws)',
                        'read_file(~/.aws/*)',
                        'read_file(/custom/*.txt)',
                        'read_file(',
                        'read_file()',
                        'read_file',
                        'not a rule',
                    ],
                    'ask': ['write_file(~/notes)', 'write_file(~/docs/*)', 'write_file(./rel-ask)', 'command(git push)'],
                    'deny': [
                        'write_file(/etc)',
                        'read_file(~/.aws/prod)',
                        'read_file(~/private/**)',
                        'read_file(~/finance/*.csv)',
                        'write_file(build/**)',
                        'write_file(.env)',
                        'read_file(~otheruser/.ssh)',
                        'command(rm -rf ~/*)',
                    ],
                },
            }))
            env = {
                'MV2_UNIVERSE': str(self.root),
                'MV2_RUN_DIR': str(self.work / 'mv2-run'),
                'MV2_SOURCE': str(self.work / 'mv2-src'),
                'MV2_CLI': str(self.work / 'mv2-bin' / 'mv2'),
                'HOME': str(self.work / 'home'),
            }
            spawn_home = (self.work / 'home').resolve()
            cmd = adapter.spawn(LaunchContext('spawn', values, 'gemini-3.8-flash-low', environment=env))
            self.assertEqual(cmd.argv[-1], f"--prompt-interactive={values['task']}")
            self.assertEqual(cmd.argv.count('--settings'), 1)
            settings_path = cmd.argv[cmd.argv.index('--settings') + 1]
            self.assertEqual(settings_path, str(Path(values['launch_dir']) / 'settings.json'))
            self.assertIn('--add-dir', cmd.argv)
            self.assertIn(values['launch_dir'], cmd.argv)
            self.assertEqual(cmd.launch_files['AGENTS.md'], values['bootstrap'])
            self.assertEqual(cmd.cleanup_files, (agy_bridge.LS_FILE,))
            self.assertTrue(cmd.shell_callback)
            merged = json.loads(cmd.launch_files['settings.json'])
            self.assertEqual(merged['theme'], 'ocean')
            self.assertEqual(merged['sandbox'], {'enabled': True})
            self.assertEqual(
                merged['permissions']['ask'],
                [
                    f'write_file({spawn_home / "notes"})',
                    f'write_file({spawn_home / "docs" / "*"})',
                    'write_file(./rel-ask)',
                    'command(git push)',
                ],
            )
            self.assertEqual(
                merged['permissions']['deny'],
                [
                    'write_file(/etc)',
                    f'read_file({spawn_home / ".aws" / "prod"})',
                    f'read_file({spawn_home / "private" / "**"})',
                    f'read_file({spawn_home / "finance" / "*.csv"})',
                    'write_file(build/**)',
                    'write_file(.env)',
                    'read_file(~otheruser/.ssh)',
                    'command(rm -rf ~/*)',
                ],
            )
            for allowed in (
                'read_file(/custom)',
                f'read_file({spawn_home / ".aws"})',
                f'write_file({spawn_home / "scratch" / "out"})',
                'command(cat ~/.aws/config)',
                f'read_file({spawn_home / "Library" / "Application Support" / "custom-app"})',
                f'read_file({spawn_home / ".gemini" / "GEMINI.md"})',
                f'read_file({spawn_home / ".gemini" / "config" / "mcp_config.json"})',
                f'read_file({spawn_home / ".gemini" / "antigravity-cli" / "brain" / "session-1"})',
            ):
                self.assertIn(allowed, merged['permissions']['allow'])
            self.assertEqual(merged['permissions']['allow'].count(f'read_file({spawn_home / ".aws"})'), 1)
            for unexpanded in (
                'read_file(~/.aws)',
                'read_file(~//.aws)',
                'write_file(~/scratch/out/)',
                'read_file(~/Library/Application Support/custom-app)',
                'read_file(~/.gemini/GEMINI.md)',
                'read_file(~/.gemini/config/mcp_config.json)',
                'read_file(~/.gemini/antigravity-cli/brain/session-1)',
            ):
                self.assertNotIn(unexpanded, merged['permissions']['allow'])
            for bad_rule in (
                'read_file(~otheruser/.aws)', 'read_file(./notes.md)', 'read_file(src/**)',
                'read_file($HOME/.aws)', 'read_file(~/.aws/*)', f'read_file({spawn_home / ".aws" / "*"})',
                'read_file(/custom/*.txt)', 'read_file(', 'read_file()', 'read_file', 'not a rule',
            ):
                self.assertNotIn(bad_rule, merged['permissions']['allow'])
            invalid_notices = [n for n in cmd.notices if 'Dropped unsupported permissions.allow' in n]
            self.assertEqual(len(invalid_notices), 1)
            for expected_named in (
                'read_file(~otheruser/.aws)', 'read_file(./notes.md)', 'read_file(src/**)',
                'read_file($HOME/.aws)', 'read_file(~/.aws/*)', 'read_file(/custom/*.txt)',
                'read_file(', 'read_file()', 'read_file', 'not a rule',
            ):
                self.assertIn(expected_named, invalid_notices[0])
            for kept_restrictive in ('write_file(./rel-ask)', 'read_file(~otheruser/.ssh)', 'write_file(build/**)', 'write_file(.env)'):
                self.assertNotIn(kept_restrictive, invalid_notices[0])
            # Every file rule emitted into launch permissions.allow is absolute and non-globbed.
            for emitted in merged['permissions']['allow']:
                if emitted.startswith(('read_file(', 'write_file(')):
                    inner = emitted[emitted.index('(') + 1:-1]
                    self.assertTrue(os.path.isabs(inner), f'allow: {emitted!r}')
                    self.assertFalse(any(ch in inner for ch in ('*', '?', '[')), f'allow: {emitted!r}')
            # Derived rules also expand leading ~/ before running the wide derived exclusion checks.
            derived_merged = agy._merge_permissions(
                {}, None,
                [
                    'read_file(~/safe-derived)',
                    'read_file(~/.aws)',
                    'read_file(~otheruser/plug)',
                    'read_file(./rel-derived)',
                    'read_file(/abs/glob/*)',
                ],
                home_dir=spawn_home,
            )
            self.assertEqual(derived_merged, {'allow': [f'read_file({spawn_home / "safe-derived"})']})
            for forbidden in ('read_file(~/.ssh)', 'read_file(~//.ssh)', f'read_file({spawn_home / ".ssh"})',
                              'read_file(~/.gnupg)', f'read_file({spawn_home / ".gnupg"})',
                              'read_file(~/.gemini/antigravity-browser-profile)',
                              'read_file(~/.gemini/antigravity-browser-profile/Default/Cookies)',
                              'read_file(~/.gemini/jetski-standalone-oauth-token)',
                              'read_file(~/.gemini/antigravity-cli/antigravity-oauth-token)',
                              'read_file(~/.gemini/antigravity-cli/jetski-cli-oauth-token)',
                              'read_file(~/.gemini/antigravity-cli/mcp_oauth_tokens.json)',
                              'read_file(~/.gemini/antigravity-cli/cache/acp-auth-state.json)',
                              'read_file(~/Library/Application Support/antigravity/plugin-secrets.json)',
                              'read_file(~/.config/antigravity/plugin-secrets.json)',
                              'read_file(~/.gemini/antigravity-cli)',
                              'read_file(~/Library/Keychains)', 'read_file(~/.multiverse/plugin-secrets)',
                              'read_file(/)', 'read_file(*)'):
                self.assertNotIn(forbidden, merged['permissions']['allow'])
            self.assertTrue(any(
                'read_file(~/.ssh)' in n
                and 'antigravity-browser-profile' in n
                and 'antigravity-oauth-token' in n
                and 'mcp_oauth_tokens.json' in n
                and 'plugin-secrets.json' in n
                for n in cmd.notices
            ))
            self.assertIn(f'read_file({self.root.resolve()})', merged['permissions']['allow'])
            self.assertIn(f'write_file({self.root.resolve() / "state"})', merged['permissions']['allow'])
            self.assertIn(f'write_file({(self.work / "mv2-run").resolve()})', merged['permissions']['allow'])
            self.assertIn(
                f'write_file({(self.work / "home").resolve() / ".multiverse" / "tmux-servers"})',
                merged['permissions']['allow'],
            )
            self.assertIn(f'read_file({(self.work / "mv2-src").resolve()})', merged['permissions']['allow'])
            self.assertIn(f'read_file({(self.work / "mv2-bin" / "mv2").resolve()})', merged['permissions']['allow'])
            self.assertIn(
                f'read_file({(self.work / "home").resolve() / ".multiverse" / "plugins"})',
                merged['permissions']['allow'],
            )
            self.assertIn(
                f'read_file({(self.work / "home").resolve() / ".multiverse" / "trusted-plugins.json"})',
                merged['permissions']['allow'],
            )
            self.assertIn(
                f'read_file({(self.work / "home").resolve() / ".multiverse" / "plugin-cache"})',
                merged['permissions']['allow'],
            )
            self.assertNotIn(f'read_file({(self.work / "home").resolve() / ".multiverse"})', merged['permissions']['allow'])
            self.assertNotIn(f'write_file({(self.work / "home").resolve()})', merged['permissions']['allow'])
            self.assertNotIn(f'write_file({self.root.resolve()})', merged['permissions']['allow'])
            self.assertTrue(merged['statusLine']['stack_with_default'])
            hooks = json.loads(cmd.launch_files['.agents/hooks.json'])
            self.assertEqual(set(hooks['mv2-bridge']), {'SessionStart', 'PreInvocation', 'Stop'})
            expected_hook_launch = Path(values['launch_dir']).name
            for event_name, entries in hooks['mv2-bridge'].items():
                self.assertIn(f'--launch-id {expected_hook_launch}', entries[0]['command'], event_name)
            for key in PARENT_AGY:
                self.assertIn(key, cmd.unset_env)
            self.assertEqual(cmd.control_endpoint, NativeEndpoint('unix-json', values['control_socket']))
            self.assertEqual(cmd.native_mode['mode'], 'bridge')

        with self.subTest('sandbox rules cover uv-style symlinked venv Python and verified plugin dirs and resist agent tampering'):
            home_dir = self.work / 'uv-home'
            real_py_dir = home_dir / '.local' / 'share' / 'uv' / 'python' / 'cpython-3.11.15-macos-aarch64-none'
            (real_py_dir / 'bin').mkdir(parents=True)
            real_py = real_py_dir / 'bin' / 'python3.11'
            real_py.write_text('#!/bin/sh\n')
            sym_py_dir = home_dir / '.local' / 'share' / 'uv' / 'python' / 'cpython-3.11-macos-aarch64-none'
            sym_py_dir.symlink_to(real_py_dir, target_is_directory=True)
            venv_dir = self.work / 'uv-venv'
            (venv_dir / 'bin').mkdir(parents=True)
            (venv_dir / 'pyvenv.cfg').write_text(f'home = {sym_py_dir / "bin"}\nimplementation = CPython\n')
            venv_py = venv_dir / 'bin' / 'python'
            venv_py.symlink_to(real_py)

            path_plugins_root = self.work / 'ext-plugins'
            path_plugin = path_plugins_root / 'path-plug'
            non_plugin_sibling = path_plugins_root / 'not-a-plugin'
            rec_plugin = self.work / 'recorded-plugins' / 'qa-agy'
            unpinned_plugin = self.work / 'recorded-plugins' / 'unpinned-plug'
            disabled_plugin = self.work / 'recorded-plugins' / 'disabled-plug'
            off_default_plugin = self.work / 'recorded-plugins' / 'off-default-plug'
            ssh_dir = home_dir / '.ssh'
            aws_dir = home_dir / '.aws'
            app_support_dir = home_dir / 'Library' / 'Application Support'
            gemini_dir = home_dir / '.gemini' / 'antigravity-cli'
            gemini_non_signin = home_dir / '.gemini' / 'custom-plug'
            keychain_dir = home_dir / 'Library' / 'Keychains'
            secrets_dir = home_dir / '.multiverse' / 'plugin-secrets'
            for folder in (path_plugin, non_plugin_sibling, rec_plugin, unpinned_plugin, disabled_plugin,
                           off_default_plugin, ssh_dir, aws_dir, app_support_dir, gemini_dir,
                           gemini_non_signin, keychain_dir, secrets_dir):
                folder.mkdir(parents=True)
            for folder, mname in (
                (path_plugin, 'path-plug'), (rec_plugin, 'qa-agy'), (unpinned_plugin, 'unpinned-plug'),
                (disabled_plugin, 'disabled-plug'), (off_default_plugin, 'off-default-plug'),
                (ssh_dir, 'forged-ssh'), (aws_dir, 'forged-aws'), (app_support_dir, 'forged-appsup'),
                (gemini_dir, 'forged-gemini'), (gemini_non_signin, 'forged-gemini-sub'),
                (keychain_dir, 'forged-keychain'), (secrets_dir, 'forged-secrets'),
            ):
                (folder / 'plugin.json').write_text(json.dumps({'schema': 1, 'name': mname, 'default': 'off'}) + '\n')
            # Symlinked plugin pointing at ~/.ssh must never pass either via resolved or symlink spelling.
            sym_ssh = path_plugins_root / 'sym-ssh'
            sym_ssh.symlink_to(ssh_dir, target_is_directory=True)
            # Pin only rec_plugin, disabled_plugin and off_default_plugin in person-owned trusted-plugins.json.
            valid_fp = 'sha256:' + ('a' * 64)
            write(home_dir / '.multiverse' / 'trusted-plugins.json', {
                'schema': 1,
                'plugins': {
                    str(rec_plugin.resolve()): {'fingerprint': valid_fp},
                    str(disabled_plugin.resolve()): {'fingerprint': valid_fp},
                    str(off_default_plugin.resolve()): {'fingerprint': valid_fp},
                    # Even if trusted-plugins.json somehow named a wide-list or secret folder, derived rules drop it.
                    str(aws_dir.resolve()): {'fingerprint': valid_fp},
                    str(secrets_dir.resolve()): {'fingerprint': valid_fp},
                },
            })
            plugins_cfg_path = self.root / 'plugins.json'
            orig_plugins_cfg = plugins_cfg_path.read_text() if plugins_cfg_path.exists() else None
            write(plugins_cfg_path, {
                'schema': 1,
                'enabled': {
                    'qa-agy': True,
                    'disabled-plug': False,
                    'unpinned-plug': True,
                    'forged-aws': True,
                    'forged-secrets': True,
                    'aliased-disabled': True,
                },
            })
            checkout_root = self.work / 'checkout-repo'
            (checkout_root / 'src' / 'multiverse').mkdir(parents=True)
            (checkout_root / 'plugins').mkdir(parents=True)
            reg_path = self.root / 'state' / 'plugins' / 'registry.json'
            write(reg_path, {
                'schema': 1,
                'plugins': {
                    'qa-agy': {'events': ['turn.started'], 'source': 'path', 'directory': str(rec_plugin)},
                    'unpinned-plug': {'events': [], 'source': 'path', 'directory': str(unpinned_plugin)},
                    'disabled-plug': {'events': [], 'source': 'path', 'directory': str(disabled_plugin)},
                    # Agent aliases disabled_plugin under an enabled fake key -> manifest name mismatch blocks it.
                    'aliased-disabled': {'events': [], 'source': 'path', 'directory': str(disabled_plugin)},
                    # Pinned off-by-default plugin not enabled in plugins.json -> blocked.
                    'off-default-plug': {'events': [], 'source': 'path', 'directory': str(off_default_plugin)},
                    'forged-ssh': {'events': [], 'source': 'path', 'directory': str(ssh_dir)},
                    'forged-aws': {'events': [], 'source': 'path', 'directory': str(aws_dir)},
                    'forged-appsup': {'events': [], 'source': 'path', 'directory': str(app_support_dir)},
                    'forged-gemini': {'events': [], 'source': 'path', 'directory': str(gemini_dir)},
                    'forged-gemini-sub': {'events': [], 'source': 'path', 'directory': str(gemini_non_signin)},
                    'forged-keychain': {'events': [], 'source': 'path', 'directory': str(keychain_dir)},
                    'forged-secrets': {'events': [], 'source': 'path', 'directory': str(secrets_dir)},
                },
            })
            try:
                in_state_agent = self.root / 'state' / 'agents' / values['agent_id']
                in_state_launch = str(uuid4())
                env_ext = {
                    'HOME': str(home_dir),
                    'MV2_UNIVERSE': str(self.root),
                    'MV2_AGENT_DIR': str(in_state_agent),
                    'MV2_LAUNCH_ID': in_state_launch,
                    'MV2_SOURCE': str(checkout_root / 'src'),
                    'MV2_PYTHON': str(venv_py),
                    'MV2_PLUGIN_PATH': os.pathsep.join((
                        str(path_plugins_root), str(secrets_dir), str(ssh_dir), str(aws_dir),
                        str(app_support_dir), str(gemini_dir), str(gemini_non_signin),
                        str(keychain_dir), str(home_dir), '/',
                    )),
                }
                derived_adapter = AgyAdapter.configured('agy', {})
                cmd_ext = derived_adapter.spawn(LaunchContext('spawn', values, 'gemini-3.8-flash-low', environment=env_ext))
                allow_ext = json.loads(cmd_ext.launch_files['settings.json'])['permissions']['allow']
                self.assertIn(f'write_file({(in_state_agent / "launches" / in_state_launch).resolve()})', allow_ext)
                # Outside-state MV2_AGENT_DIR must never receive a write_file allow rule.
                bad_env = {**env_ext, 'MV2_AGENT_DIR': str(self.work / 'outside-agent')}
                bad_allow = json.loads(
                    derived_adapter.spawn(LaunchContext('spawn', values, 'gemini-3.8-flash-low', environment=bad_env))
                    .launch_files['settings.json']
                )['permissions']['allow']
                self.assertFalse(any('outside-agent' in rule for rule in bad_allow))
                self.assertIn(f'read_file({venv_dir.resolve()})', allow_ext)
                self.assertIn(f'read_file({real_py_dir.resolve()})', allow_ext)
                self.assertIn(f'read_file({os.path.normpath(str(sym_py_dir))})', allow_ext)
                # Folder-of-plugins MV2_PLUGIN_PATH grants only safe subfolders holding a regular plugin.json.
                self.assertIn(f'read_file({path_plugin.resolve()})', allow_ext)
                self.assertNotIn(f'read_file({path_plugins_root.resolve()})', allow_ext)
                self.assertNotIn(f'read_file({non_plugin_sibling.resolve()})', allow_ext)
                self.assertNotIn(f'read_file({os.path.normpath(str(sym_ssh))})', allow_ext)
                # Only pinned + enabled + matching-name registry plugins pass; unpinned, disabled, aliased, off-default,
                # and secret entries never pass.
                self.assertIn(f'read_file({rec_plugin.resolve()})', allow_ext)
                self.assertNotIn(f'read_file({unpinned_plugin.resolve()})', allow_ext)
                self.assertNotIn(f'read_file({disabled_plugin.resolve()})', allow_ext)
                self.assertNotIn(f'read_file({off_default_plugin.resolve()})', allow_ext)
                self.assertIn(f'read_file({(checkout_root / "plugins").resolve()})', allow_ext)
                # Derived rules never widen into the wide sensitive list (even when pinned or in MV2_PLUGIN_PATH).
                for secret_path in (ssh_dir, aws_dir, app_support_dir, gemini_dir, gemini_non_signin,
                                    keychain_dir, secrets_dir):
                    self.assertNotIn(f'read_file({secret_path.resolve()})', allow_ext)
                # Never allow broad roots like /, $HOME, or ~/.local even if named in MV2_PLUGIN_PATH.
                self.assertNotIn('read_file(/)', allow_ext)
                self.assertNotIn(f'read_file({home_dir.resolve()})', allow_ext)
                self.assertNotIn(f'read_file({(home_dir / ".local").resolve()})', allow_ext)
                self.assertNotIn(f'read_file({(home_dir / ".multiverse").resolve()})', allow_ext)

                # Tamper test 1 (plugins.json): damaged plugins.json grants zero registry plugins.
                plugins_cfg_path.write_text('{broken')
                broken_allow = json.loads(
                    derived_adapter.spawn(LaunchContext('spawn', values, 'gemini-3.8-flash-low', environment=env_ext))
                    .launch_files['settings.json']
                )['permissions']['allow']
                self.assertNotIn(f'read_file({rec_plugin.resolve()})', broken_allow)
                write(plugins_cfg_path, {'schema': 1, 'enabled': {'qa-agy': True, 'disabled-plug': False}})

                # Tamper test 2 (pyvenv.cfg): agent edits writable pyvenv.cfg to point home at ~/.ssh, Keychains,
                # an unrelated dir, a forged dir whose bin/ symlinks to real Python bin, or a symlinked pyvenv.cfg.
                unrelated_dir = self.work / 'unrelated-target' / 'bin'
                unrelated_dir.mkdir(parents=True)
                forged_parent = self.work / 'forged-py-parent'
                forged_parent.mkdir()
                (forged_parent / 'bin').symlink_to(real_py_dir / 'bin', target_is_directory=True)
                for tampered_home in (ssh_dir, aws_dir, app_support_dir, keychain_dir, unrelated_dir, forged_parent / 'bin'):
                    (venv_dir / 'pyvenv.cfg').write_text(f'home = {tampered_home}\n')
                    t_cmd = derived_adapter.spawn(LaunchContext('spawn', values, 'gemini-3.8-flash-low', environment=env_ext))
                    t_allow = json.loads(t_cmd.launch_files['settings.json'])['permissions']['allow']
                    self.assertNotIn(f'read_file({tampered_home.resolve()})', t_allow)
                    self.assertNotIn(f'read_file({tampered_home.parent.resolve()})', t_allow)
                    self.assertNotIn(f'read_file({os.path.normpath(str(tampered_home))})', t_allow)
                    self.assertNotIn(f'read_file({os.path.normpath(str(tampered_home.parent))})', t_allow)
                # Symlinked pyvenv.cfg is ignored.
                cfg_target = self.work / 'real-cfg.txt'
                cfg_target.write_text(f'home = {sym_py_dir / "bin"}\n')
                (venv_dir / 'pyvenv.cfg').unlink()
                (venv_dir / 'pyvenv.cfg').symlink_to(cfg_target)
                sym_cfg_allow = json.loads(
                    derived_adapter.spawn(LaunchContext('spawn', values, 'gemini-3.8-flash-low', environment=env_ext))
                    .launch_files['settings.json']
                )['permissions']['allow']
                self.assertNotIn(f'read_file({os.path.normpath(str(sym_py_dir))})', sym_cfg_allow)
                (venv_dir / 'pyvenv.cfg').unlink()
                (venv_dir / 'pyvenv.cfg').write_text(f'home = {sym_py_dir / "bin"}\nimplementation = CPython\n')

                # Tamper test 3 (symlinked checkout/plugins and universe/state): replacing either with a symlink to
                # an outside dir must never grant read_file or write_file on the symlink target.
                sym_target = self.work / 'private-outside'
                sym_target.mkdir()
                (checkout_root / 'plugins').rmdir()
                (checkout_root / 'plugins').symlink_to(sym_target, target_is_directory=True)
                sym_co_allow = json.loads(
                    derived_adapter.spawn(LaunchContext('spawn', values, 'gemini-3.8-flash-low', environment=env_ext))
                    .launch_files['settings.json']
                )['permissions']['allow']
                self.assertFalse(any(str(sym_target.resolve()) in rule for rule in sym_co_allow))
                (checkout_root / 'plugins').unlink()
                (checkout_root / 'plugins').mkdir()
                fake_uni = self.work / 'sym-uni'
                fake_uni.mkdir()
                (fake_uni / 'state').symlink_to(sym_target, target_is_directory=True)
                sym_state_allow = json.loads(
                    derived_adapter.spawn(LaunchContext(
                        'spawn', values, 'gemini-3.8-flash-low',
                        environment={**env_ext, 'MV2_UNIVERSE': str(fake_uni),
                                     'MV2_AGENT_DIR': str(fake_uni / 'state' / 'agents' / values['agent_id'])},
                    )).launch_files['settings.json']
                )['permissions']['allow']
                self.assertFalse(any(str(sym_target.resolve()) in rule for rule in sym_state_allow))

                # Tamper test 4 (harness --settings and personal agy settings): files inside state/ or run_dir are
                # refused; person-written rules (even inside the workspace) are honored in full—including ~/.aws,
                # ~/Library/Application Support, non-sign-in ~/.gemini paths, and sandbox/yolo settings—except the
                # five protected credential paths, which drop with a launch notice naming the dropped rules.
                state_settings = self.root / 'state' / 'evil-settings.json'
                state_settings.write_text(json.dumps({'permissions': {'allow': [f'read_file({ssh_dir})']}}))
                run_dir_path = self.work / 'tamper-run'
                run_dir_path.mkdir()
                run_settings = run_dir_path / 'evil-run-settings.json'
                run_settings.write_text(json.dumps({'permissions': {'allow': ['read_file(/custom)']}}))
                try:
                    for bad_settings in (state_settings, run_settings):
                        evil_adapter = AgyAdapter.configured('agy', {'args': ['--settings', str(bad_settings)]})
                        with self.assertRaisesRegex(ValueError, 'sandbox-writable state'):
                            evil_adapter.spawn(LaunchContext(
                                'spawn', values, 'gemini-3.8-flash-low',
                                environment={**env_ext, 'MV2_RUN_DIR': str(run_dir_path)},
                            ))
                finally:
                    state_settings.unlink(missing_ok=True)
                    run_settings.unlink(missing_ok=True)
                ws_settings_file = repo_ws / 'ws-harness-settings.json'
                xdg_dir = self.work / 'custom-xdg'
                ws_settings_file.write_text(json.dumps({
                    'theme': 'forest',
                    'sandbox': {'enabled': False},
                    'yolo': True,
                    'approvalMode': 'yolo',
                    'permissions': {
                        'allow': [
                            'read_file(/ws-custom)',
                            'read_file(~/.aws)',
                            'read_file(~/Library/Application Support/my-app)',
                            'read_file(~/.gemini/GEMINI.md)',
                            'read_file(~/.gemini/antigravity-cli/brain/s1)',
                            f'read_file({xdg_dir / "other-app"})',
                            f'read_file({ssh_dir})',
                            'read_file(~/.gnupg)',
                            'read_file(~/Library/Keychains)',
                            'read_file(~/.gemini/antigravity-browser-profile)',
                            'read_file(~/.gemini/antigravity-browser-profile/Default/Login Data)',
                            'read_file(~/.gemini/antigravity-cli/antigravity-oauth-token)',
                            'read_file(~/.gemini/antigravity-cli/mcp_oauth_tokens.json)',
                            'read_file(~/.gemini/antigravity-cli/cache/acp-auth-state.json)',
                            'read_file(~/Library/Application Support/antigravity/plugin-secrets.json)',
                            'read_file(~/.config/antigravity/plugin-secrets.json)',
                            f'read_file({xdg_dir / "antigravity" / "plugin-secrets.json"})',
                            'read_file(~/.gemini/antigravity-cli/antigravity-*)',
                            'read_file(~/foo/*/../../.ssh)',
                            'read_file(~/.multiverse/plugin-secrets)',
                            'read_file(/)',
                            'read_file(*)',
                        ],
                        'deny': ['write_file(/etc)'],
                        'defaultMode': 'yolo',
                    },
                }))
                ws_file_adapter = AgyAdapter.configured('agy', {'args': ['--settings', str(ws_settings_file)]})
                ws_file_cmd = ws_file_adapter.spawn(LaunchContext(
                    'spawn', values, 'gemini-3.8-flash-low',
                    environment={**env_ext, 'XDG_CONFIG_HOME': str(xdg_dir)},
                ))
                ws_file_merged = json.loads(ws_file_cmd.launch_files['settings.json'])
                self.assertEqual(ws_file_merged['theme'], 'forest')
                self.assertEqual(ws_file_merged['sandbox'], {'enabled': False})
                self.assertIs(ws_file_merged['yolo'], True)
                self.assertEqual(ws_file_merged['approvalMode'], 'yolo')
                self.assertEqual(ws_file_merged['permissions']['deny'], ['write_file(/etc)'])
                self.assertEqual(ws_file_merged['permissions']['defaultMode'], 'yolo')
                uv_home = home_dir.resolve()
                for kept_rule in (
                    'read_file(/ws-custom)',
                    f'read_file({uv_home / ".aws"})',
                    f'read_file({uv_home / "Library" / "Application Support" / "my-app"})',
                    f'read_file({uv_home / ".gemini" / "GEMINI.md"})',
                    f'read_file({uv_home / ".gemini" / "antigravity-cli" / "brain" / "s1"})',
                    f'read_file({xdg_dir / "other-app"})',
                ):
                    self.assertIn(kept_rule, ws_file_merged['permissions']['allow'])
                self.assertNotIn('read_file(~/.aws)', ws_file_merged['permissions']['allow'])
                for dropped_rule in (
                    f'read_file({ssh_dir})',
                    'read_file(~/.gnupg)',
                    f'read_file({uv_home / ".gnupg"})',
                    'read_file(~/Library/Keychains)',
                    'read_file(~/.gemini/antigravity-browser-profile)',
                    'read_file(~/.gemini/antigravity-browser-profile/Default/Login Data)',
                    'read_file(~/.gemini/antigravity-cli/antigravity-oauth-token)',
                    'read_file(~/.gemini/antigravity-cli/mcp_oauth_tokens.json)',
                    'read_file(~/.gemini/antigravity-cli/cache/acp-auth-state.json)',
                    'read_file(~/Library/Application Support/antigravity/plugin-secrets.json)',
                    'read_file(~/.config/antigravity/plugin-secrets.json)',
                    f'read_file({xdg_dir / "antigravity" / "plugin-secrets.json"})',
                    'read_file(~/.gemini/antigravity-cli/antigravity-*)',
                    'read_file(~/foo/*/../../.ssh)',
                    'read_file(~/.multiverse/plugin-secrets)',
                    'read_file(/)',
                    'read_file(*)',
                ):
                    self.assertNotIn(dropped_rule, ws_file_merged['permissions']['allow'])
                drop_notices = [n for n in ws_file_cmd.notices if 'Dropped excluded permissions.allow' in n]
                self.assertEqual(len(drop_notices), 1)
                self.assertIn(f'read_file({ssh_dir})', drop_notices[0])
                self.assertIn('antigravity-browser-profile', drop_notices[0])
                self.assertIn('antigravity-oauth-token', drop_notices[0])
                self.assertNotIn('~/.aws', drop_notices[0])
                self.assertNotIn('sandbox', drop_notices[0])
                # Universe inside workspace (standard <workspace>/.multiverse): inline --settings applies fully
                # with no notice when no rule touches the five protected credential paths.
                inline_cfg = AgyAdapter.configured('agy', {'args': [
                    '--settings',
                    '{{"theme": "moss", "sandbox": false, "permissions": {{"allow": ["read_file(/inline-custom)", "read_file(~/.aws)"], "deny": ["write_file(/etc)"]}}}}',
                ]})
                ws_uni_values = self._values(workspace=str(self.work))
                ws_uni_cmd = inline_cfg.spawn(LaunchContext('spawn', ws_uni_values, 'gemini-3.8-flash-low', environment=env_ext))
                ws_uni_merged = json.loads(ws_uni_cmd.launch_files['settings.json'])
                self.assertEqual(ws_uni_merged['theme'], 'moss')
                self.assertIs(ws_uni_merged['sandbox'], False)
                self.assertEqual(ws_uni_merged['permissions']['deny'], ['write_file(/etc)'])
                self.assertIn('read_file(/inline-custom)', ws_uni_merged['permissions']['allow'])
                self.assertIn(f'read_file({uv_home / ".aws"})', ws_uni_merged['permissions']['allow'])
                self.assertNotIn('read_file(~/.aws)', ws_uni_merged['permissions']['allow'])
                self.assertFalse(any('Dropped excluded permissions.allow' in n for n in ws_uni_cmd.notices))
                # Personal ~/.gemini/antigravity-cli/settings.json uses the same five-path exclusion list and notice,
                # expands leading ~/ in file rules, and deduplicates against matching absolute harness rules.
                personal_agy = home_dir / '.gemini' / 'antigravity-cli' / 'settings.json'
                personal_agy.write_text(json.dumps({
                    'permissions': {
                        'allow': [
                            'read_file(~/.aws)',
                            'read_file(~/.gemini/GEMINI.md)',
                            'read_file(~/.ssh)',
                            'read_file(~/.gemini/antigravity-browser-profile/Default/Cookies)',
                            'read_file(~/.gemini/antigravity-cli/antigravity-oauth-token)',
                        ],
                        'ask': ['write_file(~/notes)'],
                        'deny': ['read_file(~/.ssh)', 'command(cat ~/.ssh/id_rsa)'],
                    },
                }))
                try:
                    p_adapter = AgyAdapter.configured('agy', {'args': [
                        '--settings',
                        f'{{"permissions": {{"allow": ["read_file({uv_home / ".aws"})"]}}}}'.replace('{', '{{').replace('}', '}}'),
                    ]})
                    p_cmd = p_adapter.spawn(LaunchContext(
                        'spawn', values, 'gemini-3.8-flash-low', environment=env_ext,
                    ))
                finally:
                    personal_agy.unlink(missing_ok=True)
                p_perms = json.loads(p_cmd.launch_files['settings.json'])['permissions']
                self.assertIn(f'read_file({uv_home / ".aws"})', p_perms['allow'])
                self.assertEqual(p_perms['allow'].count(f'read_file({uv_home / ".aws"})'), 1)
                self.assertIn(f'read_file({uv_home / ".gemini" / "GEMINI.md"})', p_perms['allow'])
                self.assertNotIn('read_file(~/.aws)', p_perms['allow'])
                self.assertNotIn('read_file(~/.ssh)', p_perms['allow'])
                self.assertNotIn(f'read_file({uv_home / ".ssh"})', p_perms['allow'])
                self.assertNotIn('read_file(~/.gemini/antigravity-browser-profile/Default/Cookies)', p_perms['allow'])
                self.assertNotIn('read_file(~/.gemini/antigravity-cli/antigravity-oauth-token)', p_perms['allow'])
                self.assertEqual(p_perms['ask'], [f'write_file({uv_home / "notes"})'])
                self.assertEqual(p_perms['deny'], [f'read_file({uv_home / ".ssh"})', 'command(cat ~/.ssh/id_rsa)'])
                self.assertTrue(any(
                    'Dropped excluded permissions.allow' in n
                    and 'read_file(~/.ssh)' in n
                    and 'antigravity-browser-profile/Default/Cookies' in n
                    and 'antigravity-oauth-token' in n
                    and '~/.aws' not in n
                    for n in p_cmd.notices
                ))

                # Tamper test 5 (resume state smuggling): properties.json native_environment (HOME, MV2_PLUGIN_PATH)
                # and runtime.json run_directory (MV2_RUN_DIR) cannot redirect personal settings or widen allow rules.
                smuggled_home = self.work / 'smuggled-home'
                (smuggled_home / '.gemini' / 'antigravity-cli').mkdir(parents=True)
                (smuggled_home / '.gemini' / 'antigravity-cli' / 'settings.json').write_text(
                    json.dumps({'permissions': {'allow': ['read_file(/smuggled-secret)']}})
                )
                smuggled_plug = self.work / 'smuggled-plug'
                smuggled_plug.mkdir()
                (smuggled_plug / 'plugin.json').write_text('{"schema": 1}\n')
                tampered_run = self.work / 'tampered-run-0700'
                tampered_run.mkdir(mode=0o700)
                host_run = self.work / 'legit-host-run'
                host_run.mkdir(mode=0o700)
                conv_res = str(uuid4())
                real_home = self._create_saved_conversation(conv_res)
                res_sess = SessionHandle(values['agent_id'], 'agy', values['workspace'],
                                         native_id=conv_res, home=os.path.realpath(real_home))
                for tampered_run_dir in (ssh_dir, tampered_run):
                    smuggled_env = {
                        'HOME': str(smuggled_home),
                        'MV2_UNIVERSE': str(self.root),
                        'MV2_PLUGIN_PATH': str(smuggled_plug),
                        'MV2_RUN_DIR': str(tampered_run_dir),
                    }
                    with patch.dict(os.environ, {'HOME': str(self.work), 'MV2_RUN_DIR': str(host_run)}, clear=False):
                        os.environ.pop('MV2_PLUGIN_PATH', None)
                        res_cmd = adapter.resume(res_sess, LaunchContext('resume', values, environment=smuggled_env))
                    res_allow = json.loads(res_cmd.launch_files['settings.json'])['permissions']['allow']
                    self.assertNotIn('read_file(/smuggled-secret)', res_allow)
                    self.assertNotIn(f'read_file({smuggled_plug.resolve()})', res_allow)
                    self.assertNotIn(f'write_file({tampered_run_dir.resolve()})', res_allow)
                    self.assertTrue(any('Skipped sandbox write access' in n and 'MV2_RUN_DIR' in n for n in res_cmd.notices))
                # Verified host MV2_RUN_DIR on resume IS still granted write_file without any skip notice.
                with patch.dict(os.environ, {'HOME': str(self.work), 'MV2_RUN_DIR': str(host_run)}, clear=False):
                    os.environ.pop('MV2_PLUGIN_PATH', None)
                    ok_res = adapter.resume(res_sess, LaunchContext(
                        'resume', values, environment={'MV2_UNIVERSE': str(self.root), 'MV2_RUN_DIR': str(host_run)},
                    ))
                self.assertIn(
                    f'write_file({host_run.resolve()})',
                    json.loads(ok_res.launch_files['settings.json'])['permissions']['allow'],
                )
                self.assertFalse(any('Skipped sandbox write access' in n for n in ok_res.notices))
            finally:
                reg_path.unlink(missing_ok=True)
                if orig_plugins_cfg is None:
                    plugins_cfg_path.unlink(missing_ok=True)
                else:
                    plugins_cfg_path.write_text(orig_plugins_cfg)

        with self.subTest('inline --settings JSON merges and missing --settings file is refused'):
            inline_adapter = AgyAdapter.configured('agy', {'args': ['--settings', '{{"theme": "pine"}}']})
            inline_cmd = inline_adapter.spawn(LaunchContext('spawn', values, 'gemini-3.8-flash-low'))
            self.assertEqual(json.loads(inline_cmd.launch_files['settings.json'])['theme'], 'pine')
            missing_adapter = AgyAdapter.configured('agy', {'args': ['--settings', str(self.work / 'no-such.json')]})
            with self.assertRaisesRegex(ValueError, 'does not exist'):
                missing_adapter.spawn(LaunchContext('spawn', values, 'gemini-3.8-flash-low'))

        with self.subTest('plain spawn omits hooks/settings and keeps AGENTS.md'):
            plain = AgyAdapter.configured('agy', {'inbox_bridge': False})
            cmd = plain.spawn(LaunchContext('spawn', {**values, 'task': ''}, 'gemini-3.8-flash-low'))
            self.assertFalse(any(arg.startswith('--prompt-interactive=') for arg in cmd.argv))
            self.assertNotIn('--settings', cmd.argv)
            self.assertEqual(set(cmd.launch_files), {'AGENTS.md'})
            self.assertEqual(cmd.cleanup_files, ())
            self.assertFalse(cmd.shell_callback)
            self.assertIsNone(cmd.control_endpoint)
            self.assertEqual(cmd.native_mode['mode'], 'plain')

        with self.subTest('resume requires conversations/<id>.db and refuses surviving brain transcript alone'):
            session = SessionHandle(values['agent_id'], 'agy', values['workspace'],
                                    native_id=values['session_id'], session_file=values['session_file'])
            home = agy_storage.agy_home()
            with self.assertRaisesRegex(ValueError, 'No saved Antigravity conversation'):
                adapter.resume(session, LaunchContext('resume', values))
            # An empty brain dir, non-SQLite .db file, or surviving brain/ transcript without .db must NOT satisfy resume.
            agy_storage.brain_dir(values['session_id'], home).mkdir(parents=True)
            bad_db = agy_storage.conversation_db(values['session_id'], home)
            bad_db.parent.mkdir(parents=True, exist_ok=True)
            bad_db.write_bytes(b'not-sqlite')
            self.assertIs(agy_storage.conversation_exists(values['session_id'], home), False)
            with self.assertRaisesRegex(ValueError, 'No saved Antigravity conversation'):
                adapter.resume(session, LaunchContext('resume', values))
            bad_db.unlink()
            self._create_saved_conversation(values['session_id'], brain_only=True)
            self.assertIs(agy_storage.conversation_exists(values['session_id'], home), False)
            ev_brain_only = adapter.session_storage_evidence(session, str(home))
            self.assertEqual(ev_brain_only.state, 'ok')
            self.assertEqual(ev_brain_only.value, {'present': False, 'store_read': True})
            with self.assertRaisesRegex(ValueError, 'No saved Antigravity conversation'):
                adapter.resume(session, LaunchContext('resume', values))
            # Only a valid SQLite conversations/<id>.db satisfies storage evidence and resume.
            self._create_saved_conversation(values['session_id'], brain_only=False)
            self.assertIs(agy_storage.conversation_exists(values['session_id'], home), True)
            self.assertEqual(adapter.session_storage_evidence(session, str(home)).value, {'present': True})
            # Symlinked conversations/ folder must fail closed as unknown (never claim store_read=True, present=False).
            linked_home = self.work / 'linked-agy-home'
            linked_home.mkdir()
            (linked_home / 'conversations').symlink_to(home / 'conversations', target_is_directory=True)
            self.assertIsNone(agy_storage.conversation_exists(values['session_id'], linked_home))
            self.assertEqual(adapter.session_storage_evidence(session, str(linked_home)).state, 'unknown')
            resumed = adapter.resume(session, LaunchContext('resume', values, resume_prompt='--help'))
            self.assertEqual(resumed.argv[-1], '--prompt-interactive=--help')
            self.assertEqual(resumed.argv[resumed.argv.index('--conversation') + 1], values['session_id'])
            # Promptless resume notice must not mention --task (mv2 resume has no --task flag).
            resumed_promptless = adapter.resume(session, LaunchContext('resume', values))
            self.assertTrue(any('first mv2 command' in n for n in resumed_promptless.notices))
            self.assertFalse(any('--task' in n for n in resumed_promptless.notices))
            spawn_promptless = adapter.spawn(LaunchContext('spawn', {**values, 'task': ''}, 'gemini-3.8-flash-low'))
            self.assertTrue(any('--task' in n for n in spawn_promptless.notices))

        with self.subTest('launch_files rejects traversal, reserved core names, links and preserves 0600 mode'):
            launch = self.work / 'bad-launch'
            launch.mkdir()
            for bad_path in ('../escape.md', '/tmp/abs.md', 'sub/../../escape.md',
                             'request.json', 'bootstrap.md', 'roles/item', '.mv2-tmp', 'a\\b'):
                with self.assertRaises(ValueError, msg=bad_path):
                    launch_files.write_files(launch, {bad_path: 'nope'})
            with self.assertRaises(ValueError):
                launch_files.write_files(launch, {'dir': 'x', 'dir/file': 'y'})
            outside = self.work / 'outside.txt'
            outside.write_text('untouched')
            (launch / 'leaf-link').symlink_to(outside)
            with self.assertRaises(ValueError):
                launch_files.write_files(launch, {'leaf-link': 'pwned'})
            other_dir = self.work / 'other-dir'
            other_dir.mkdir()
            (launch / 'dir-link').symlink_to(other_dir, target_is_directory=True)
            with self.assertRaises(OSError):
                launch_files.write_files(launch, {'dir-link/file': 'pwned'})
            os.link(outside, launch / 'hard-link')
            with self.assertRaises(ValueError):
                launch_files.write_files(launch, {'hard-link': 'pwned'})
            self.assertEqual(outside.read_text(), 'untouched')
            existing = launch / 'existing.txt'
            existing.write_text('old')
            existing.chmod(0o644)
            launch_files.write_files(launch, {'existing.txt': 'private'})
            self.assertEqual(existing.read_text(), 'private')
            self.assertEqual(stat.S_IMODE(existing.stat().st_mode), 0o600)
            self.assertEqual(launch_files.unlink_files(launch, ('existing.txt',)), [])
            self.assertFalse(existing.exists())
            errors = launch_files.unlink_files(launch, ('leaf-link',))
            self.assertTrue(errors)
            self.assertTrue((launch / 'leaf-link').is_symlink())

    def test_statusline_and_hooks_bind_rebind_and_pop_followups(self):
        """statusLine binds/rebinds sessions and pushes status+usage while Stop hook pops follow-ups FIFO and respects
        USER_CANCELED, fullyIdle=false, and conversation changes; breaks go unnoticed because agy ignores hook errors."""
        agent_id = str(uuid4())
        launch_id = str(uuid4())
        conv1 = str(uuid4())
        conv2 = str(uuid4())
        agent_dir = self.root / 'state' / 'agents' / agent_id
        (agent_dir / 'launches' / launch_id).mkdir(parents=True)
        write(agent_dir / 'launches' / launch_id / 'request.json', {'expected_native_id': None})
        write(agent_dir / 'properties.json', {
            'id': agent_id, 'name': 'worker', 'harness': 'agy', 'workspace': str(self.work),
            'phase': 'working', 'universe_id': read(self.root / 'universe.json')['id'],
            'native_session_provider': 'agy',
            'native_session_home': os.path.realpath(agy_storage.agy_home()),
        })
        write(agent_dir / 'runtime.json', {
            'launch_id': launch_id, 'state': 'running', 'status': 'unknown',
            'native_mode': {'policy': 'auto', 'mode': 'bridge', 'fallback_reason': None},
        })
        env = {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': agent_id, 'MV2_LAUNCH_ID': launch_id}

        line = agy_bridge.handle_statusline({
            'conversation_id': conv1,
            'agent_state': 'tool_use',
            'context_window': {
                'context_window_size': 1048576,
                'used_percentage': 12.5,
                'current_usage': {'input_tokens': 1000, 'cache_read_input_tokens': 9000, 'cache_creation_input_tokens': 0},
            },
            'cost': {'total_usd': 0.123},
            'quota': {'gemini-weekly': {'remaining_fraction': 0.8, 'reset_time': '2026-12-01T00:00:00Z'}},
        }, env=env)
        self.assertIn('mv2:worker', line)
        self.assertIn('phase working', line)
        self.assertEqual(read(agent_dir / 'properties.json')['native_session_id'], conv1)
        facts = read(agent_dir / 'runtime.json')
        self.assertEqual(facts['status'], 'working')
        self.assertEqual(facts['usage']['context'], {'window': 1048576, 'percent': 12.5, 'tokens': 10000})
        self.assertEqual(facts['usage']['cost_usd'], 0.123)
        self.assertEqual(facts['usage']['rate_limits'][0]['percent_used'], 20.0)

        # Tool confirmation flips status to waiting.
        agy_bridge.handle_statusline({'conversation_id': conv1, 'agent_state': 'working', 'tool_confirmation_pending': True}, env=env)
        self.assertEqual(read(agent_dir / 'runtime.json')['status'], 'waiting')

        # Enqueue two follow-ups for conv1; USER_CANCELED and fullyIdle=False pause queued entries so an
        # immediate idle statusLine does NOT auto-flush them until the next turn (PreInvocation) or normal Stop.
        m1, m2 = str(uuid4()), str(uuid4())
        agy_bridge.write_receipt(agent_dir, m1, launch_id=launch_id, native_id=conv1, route='stop-hook',
                                 submitted_at=now(), text_sha256=agy_storage.message_hash('first'))
        agy_bridge.enqueue_followup(agent_dir, launch_id, m1, conv1, 'first')
        agy_bridge.enqueue_followup(agent_dir, launch_id, m2, conv1, 'second')
        self.assertEqual(agy_bridge.handle_hook('Stop', {'conversationId': conv1, 'terminationReason': 'USER_CANCELED', 'fullyIdle': True}, env=env), {})
        self.assertEqual(agy_bridge.followup_status(agent_dir, launch_id, m1), ('queued', 'cancel'))
        with patch.object(agy_bridge, '_spawn_idle_flush') as spawn_flush:
            agy_bridge.handle_statusline({'conversation_id': conv1, 'agent_state': 'idle'}, env=env)
        spawn_flush.assert_not_called()
        # get_delivery reports paused=True after USER_CANCELED.
        cancel_adapter = AgyAdapter.configured('agy', {})
        cancel_session = SessionHandle.from_records(read(agent_dir / 'properties.json'), read(agent_dir / 'runtime.json'), agent_dir)
        cancel_delivery = cancel_adapter.get_delivery(cancel_session, m1)
        self.assertEqual(cancel_delivery.state, 'native_queued')
        self.assertTrue(cancel_delivery.value.get('paused'))
        # PreInvocation starts a new turn, sets status='working', and unpauses queued entries.
        agy_bridge.handle_hook('PreInvocation', {'conversationId': conv1}, env=env)
        self.assertEqual(read(agent_dir / 'runtime.json')['status'], 'working')
        self.assertEqual(agy_bridge.followup_status(agent_dir, launch_id, m1), ('queued', None))
        # A subsequent idle statusLine with unchanged usage must still flip runtime status back from working to idle.
        agy_bridge.handle_statusline({'conversation_id': conv1, 'agent_state': 'idle'}, env=env)
        self.assertEqual(read(agent_dir / 'runtime.json')['status'], 'idle')
        # fullyIdle=False also pauses queued entries against idle statusLine flush, and a normal Stop still pops them.
        agy_bridge.handle_hook('PreInvocation', {'conversationId': conv1}, env=env)
        self.assertEqual(agy_bridge.handle_hook('Stop', {'conversationId': conv1, 'terminationReason': 'model_stop', 'fullyIdle': False}, env=env), {})
        self.assertEqual(agy_bridge.followup_status(agent_dir, launch_id, m1), ('queued', 'subagent'))
        with patch.object(agy_bridge, '_spawn_idle_flush') as spawn_flush:
            agy_bridge.handle_statusline({'conversation_id': conv1, 'agent_state': 'idle'}, env=env)
        spawn_flush.assert_not_called()
        self.assertEqual(agy_bridge.handle_hook('Stop', {'conversationId': conv1, 'terminationReason': 'model_stop', 'fullyIdle': True}, env=env),
                         {'decision': 'continue', 'reason': 'first'})
        self.assertIsNotNone(agy_bridge.read_receipt(agent_dir, m1)['delivered_at'])
        self.assertEqual(agy_bridge.followup_status(agent_dir, launch_id, m2), ('queued', None))

        # /clear moves to conv2: statusLine rebinds and Stop hook for conv2 must NOT pop conv1's remaining follow-up.
        agy_bridge.handle_statusline({'conversation_id': conv2, 'agent_state': 'idle'}, env=env)
        self.assertEqual(read(agent_dir / 'properties.json')['native_session_id'], conv2)
        self.assertEqual(read(agent_dir / 'runtime.json')['status'], 'idle')
        self.assertEqual(agy_bridge.handle_hook('Stop', {'conversationId': conv2, 'terminationReason': 'model_stop', 'fullyIdle': True}, env=env), {})
        self.assertTrue(agy_bridge.has_queued_followup(agent_dir, launch_id, m2))

    def test_bridge_registration_send_delivery_usage_and_screen_detection(self):
        """Shell handoff validates loopback addresses, registers the bridge without ConversationService, routes
        steer/idle vs busy follow-up (including idle-transition recheck), confirms receipts in transcript.jsonl,
        and merges usage; a break goes unnoticed because shell handoff fails silently by design."""
        for bad in ('192.168.1.5:8080', 'http://127.0.0.1:8080', '127.0.0.1:8080/path', 'evil.com:80', 'localhost:0', ''):
            with self.assertRaises(ValueError, msg=bad):
                agy_bridge.validate_ls_address(bad)
        for good in ('127.0.0.1:4242', 'localhost:9999', '[::1]:1234'):
            self.assertEqual(agy_bridge.validate_ls_address(good), good)

        server = _LoopbackListener()
        self.addCleanup(server.close)
        agent_id, launch_id, conv_id = str(uuid4()), str(uuid4()), str(uuid4())
        agent_dir = self.root / 'state' / 'agents' / agent_id
        (agent_dir / 'launches' / launch_id).mkdir(parents=True)
        write(agent_dir / 'launches' / launch_id / 'request.json', {
            'expected_native_id': None,
            'env': {
                'MV2_UNIVERSE': str(self.root),
                'MV2_AGENT_ID': agent_id,
                'MV2_LAUNCH_ID': launch_id,
                'MV2_SHELL_CALLBACK': '1',
            },
            'cleanup_files': [agy_bridge.LS_FILE],
            'shell_callback': True,
        })
        home = self._create_saved_conversation(conv_id)
        write(agent_dir / 'properties.json', {
            'id': agent_id, 'name': 'alpha', 'harness': 'agy', 'workspace': str(self.work),
            'universe_id': read(self.root / 'universe.json')['id'],
            'native_session_provider': 'agy', 'native_session_home': os.path.realpath(home),
        })
        write(agent_dir / 'runtime.json', {
            'launch_id': launch_id, 'state': 'running', 'status': 'idle',
            'started_at': now(),
            'control_endpoint': {'transport': 'unix-json', 'address': str(self.work / 'rpc.sock')},
            'native_mode': {'policy': 'auto', 'mode': 'bridge', 'fallback_reason': None},
        })

        registered = native.shell_callback(self.root, agent_dir, launch_id, environment={
            'MV2_UNIVERSE': str(self.root),
            'MV2_AGENT_ID': agent_id,
            'MV2_LAUNCH_ID': launch_id,
            'MV2_SHELL_CALLBACK': '1',
            'ANTIGRAVITY_LS_ADDRESS': server.address,
            'ANTIGRAVITY_CSRF_TOKEN': 'tok-abc-123',
            'ANTIGRAVITY_CONVERSATION_ID': conv_id,
        })
        self.assertTrue(registered)
        ls_file = agy_bridge.ls_path(agent_dir, launch_id)
        self.assertEqual(stat.S_IMODE(ls_file.stat().st_mode), 0o600)
        self.assertEqual(read(agent_dir / 'properties.json')['native_session_id'], conv_id)
        self.assertEqual(read(agent_dir / 'runtime.json')['native_bridge'], BRIDGE)
        cb_env = {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': agent_id, 'MV2_LAUNCH_ID': launch_id}
        agy_bridge.handle_statusline({'conversation_id': conv_id, 'agent_state': 'idle'}, env=cb_env)

        adapter = AgyAdapter.configured('agy', {})
        session = SessionHandle.from_records(read(agent_dir / 'properties.json'), read(agent_dir / 'runtime.json'), agent_dir)
        self.assertTrue(adapter.attachment_ready(session))
        # Antigravity CLI has no non-interactive conversation rename API (ConversationService removed in 1.5.16).
        self.assertEqual(adapter.capabilities(session)['session_name'].state, 'unavailable')
        self.assertEqual(adapter.capabilities(SessionHandle('', 'agy', str(self.work)))['session_name'].state, 'unavailable')

        executor = Mock()
        executor.command.return_value = subprocess.CompletedProcess(
            args=['agy'], returncode=0,
            stdout=json.dumps({'response': {'sendMessage': {'recipientId': conv_id}}}), stderr='',
        )

        # Steer submits via executor.command with token only in env, never in argv (zero HTTP/ConversationService calls).
        msg_steer = str(uuid4())
        sent = adapter.send(session, Message(msg_steer, 'check logs', delivery='steer', framed='Message from user:\ncheck logs'), executor)
        self.assertEqual(sent.state, 'native_queued')
        called_spec = executor.command.call_args.args[0]
        self.assertEqual(called_spec.argv[-2:], (conv_id, 'Message from user:\ncheck logs'))
        self.assertNotIn('tok-abc-123', ' '.join(called_spec.argv))
        self.assertEqual(called_spec.environment['ANTIGRAVITY_CSRF_TOKEN'], 'tok-abc-123')
        self.assertNotIn('check logs', agy_bridge.receipt_path(agent_dir, msg_steer).read_text())

        # An immediate follow-up right after an idle agentapi send queues for Stop until the turn begins and finishes.
        executor.command.reset_mock()
        msg_after_idle_steer = str(uuid4())
        queued_immediate = adapter.send(
            session, Message(msg_after_idle_steer, 'wait for steer turn', delivery='follow-up'), executor,
        )
        self.assertEqual(queued_immediate.state, 'native_queued')
        self.assertEqual(queued_immediate.value['route'], 'stop-hook')
        executor.command.assert_not_called()
        agy_bridge.remove_followup(agent_dir, launch_id, msg_after_idle_steer, reset_idle_flush=True)

        # Before transcript row appears, delivery is unconfirmed; after SYSTEM_MESSAGE appears, confirmed.
        self.assertEqual(adapter.get_delivery(session, msg_steer).state, 'unknown')
        transcript = home / 'brain' / conv_id / '.system_generated' / 'logs' / 'transcript.jsonl'
        transcript.parent.mkdir(parents=True, exist_ok=True)
        wrapped_steer = (
            'The following is a <SYSTEM_MESSAGE> not actually sent by the user. '
            'It is provided by the system as important information to pay attention to.\n\n'
            '<SYSTEM_MESSAGE>\n'
            '[Message] timestamp=2026-11-22T13:35:46Z sender=system priority=MESSAGE_PRIORITY_NORMAL '
            'content=Message from user:\ncheck logs\n'
            '</SYSTEM_MESSAGE>'
        )
        transcript.write_text(
            json.dumps({'type': 'SYSTEM_MESSAGE', 'created_at': now(), 'content': wrapped_steer}) + '\n'
            + json.dumps({'type': 'PLANNER_RESPONSE', 'model': 'gemini-3.8-flash-low', 'input_tokens': 500,
                          'cache_read_tokens': 2500, 'output_tokens': 300, 'created_at': now()}) + '\n'
        )
        confirmed = adapter.get_delivery(session, msg_steer)
        self.assertEqual(confirmed.state, 'native_queued')
        self.assertTrue(confirmed.value['transcript_confirmed'])

        # Busy follow-up queues for Stop hook; while running get_delivery shows queued, after stopped shows definite failure.
        agy_bridge.handle_statusline({'conversation_id': conv_id, 'agent_state': 'working'}, env=cb_env)
        msg_busy = str(uuid4())
        executor.command.reset_mock()
        queued = adapter.send(session, Message(msg_busy, 'after turn', delivery='follow-up', framed='Message from user:\nafter turn'), executor)
        self.assertEqual(queued.state, 'native_queued')
        self.assertEqual(queued.value['route'], 'stop-hook')
        executor.command.assert_not_called()
        still_running = adapter.get_delivery(session, msg_busy)
        self.assertEqual(still_running.state, 'native_queued')
        self.assertTrue(still_running.value['queued'])
        ended_check = adapter.get_delivery(SessionHandle(**{**session.__dict__, 'state': 'stopped'}), msg_busy)
        self.assertEqual(ended_check.state, 'error')
        self.assertTrue(ended_check.value['definite_failure'])

        # Stop-hook delivery written with Antigravity's "Stop hook blocked termination:" wrapper confirms cleanly.
        wrapped_stop = (
            'The following is a <SYSTEM_MESSAGE> not actually sent by the user. '
            'It is provided by the system as important information to pay attention to.\n\n'
            '<SYSTEM_MESSAGE>\n'
            'Stop hook blocked termination: Message from user:\nafter turn\n'
            '</SYSTEM_MESSAGE>'
        )
        with transcript.open('a') as handle:
            handle.write(json.dumps({'type': 'SYSTEM_MESSAGE', 'created_at': now(), 'content': wrapped_stop}) + '\n')
        agy_bridge.remove_followup(agent_dir, launch_id, msg_busy, reset_idle_flush=True)
        stop_confirmed = adapter.get_delivery(session, msg_busy)
        self.assertEqual(stop_confirmed.state, 'native_queued')
        self.assertTrue(stop_confirmed.value['transcript_confirmed'])

        # Idle-transition race: runtime status is working when send starts, flips idle as enqueue completes -> claims and sends via agentapi.
        real_enqueue = agy_bridge.enqueue_followup
        def flip_idle_on_enqueue(*args, **kwargs):
            real_enqueue(*args, **kwargs)
            write(agent_dir / 'runtime.json', {**read(agent_dir / 'runtime.json'), 'status': 'idle'})
        msg_race = str(uuid4())
        with patch.object(agy_bridge, 'enqueue_followup', side_effect=flip_idle_on_enqueue):
            raced = adapter.send(session, Message(msg_race, 'raced msg', delivery='follow-up', framed='Message from user:\nraced msg'), executor)
        self.assertEqual(raced.state, 'native_queued')
        self.assertEqual(raced.value['route'], 'agentapi')
        executor.command.assert_called_once()
        self.assertFalse(agy_bridge.has_queued_followup(agent_dir, launch_id, msg_race))

        # get_usage sums uncached + cached into input_tokens and merges runtime statusLine usage.
        write(agent_dir / 'runtime.json', {
            **read(agent_dir / 'runtime.json'),
            'usage': {'context': {'window': 1048576, 'percent': 1.2, 'tokens': 3000}, 'cost_usd': 0.05, 'rate_limits': [], 'reported_at': now()},
        })
        usage = adapter.get_usage(session)
        self.assertEqual(usage.state, 'ok')
        self.assertEqual(usage.value['input_tokens'], 3000)
        self.assertEqual(usage.value['cached_input_tokens'], 2500)
        self.assertEqual(usage.value['output_tokens'], 300)
        self.assertEqual(usage.value['total_tokens'], 3300)
        self.assertEqual(usage.value['cost_usd'], 0.05)
        self.assertEqual(usage.value['model'], 'gemini-3.8-flash')
        self.assertEqual(usage.value['effort'], 'low')

        # Storage evidence & startup screen detection.
        self.assertEqual(adapter.session_storage_evidence(session, str(home)).value, {'present': True})
        self.assertEqual(
            adapter.launch_prompt({'text': 'Accessing workspace: /tmp/proj\nDo you trust the contents of this project?\n> Yes'}),
            {'title': 'Trust this workspace', 'detail': '/tmp/proj'},
        )
        self.assertEqual(
            adapter.launch_prompt({'text': 'Accessing workspace:\n\n/tmp/proj2\n\nDo you trust the contents of this project?\n> Yes, I trust this folder'}),
            {'title': 'Trust this workspace', 'detail': '/tmp/proj2'},
        )
        self.assertFalse(adapter.launch_started({'text': 'Do you trust the contents of this project?\n? for shortcuts'}))
        # Scrollback mentioning trust/ready far above must not false-positive active bottom detection.
        old_scrollback = '\n'.join([
            'Do you trust the contents of this project?',
            '? for shortcuts',
            *[f'ordinary conversation line {i}' for i in range(25)],
        ])
        self.assertIsNone(adapter.launch_prompt({'text': old_scrollback}))
        self.assertFalse(adapter.launch_started({'text': old_scrollback}))
        self.assertTrue(adapter.launch_started({'text': old_scrollback + '\n? for shortcuts'}))

    def test_security_fences_queue_durability_session_ordering_and_strict_receipts(self):
        """Unreachable/non-loopback localhost endpoints, broad/symlinked handoffs, stopped/retired shell callbacks,
        damaged or failed-receipt follow-up queues, delayed pre-clear/resume-mismatch callbacks, undated/wrong-route/
        duplicate transcript confirmations and unacknowledged agentapi exits must all fail closed; subtle regressions
        silently leak tokens, lose messages, rebind backwards or falsely confirm delivery."""
        # 1. Closed loopback port fails cleanly before any receipt or subprocess runs.
        closed = _LoopbackListener()
        closed_addr = closed.address
        closed.close()
        with self.assertRaisesRegex(RuntimeError, 'unreachable'):
            agy_bridge.pin_loopback_endpoint(closed_addr, timeout=1.0)

        # 2. Poisoned non-loopback localhost DNS resolution is refused before connecting.
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('203.0.113.10', 4444))]):
            with self.assertRaisesRegex(RuntimeError, 'non-loopback'):
                agy_bridge.pin_loopback_endpoint('localhost:4444', timeout=1.0)

        # 3. Symlinked or world-readable agy-ls.json is rejected by read_ls_info.
        agent_id, launch_id, conv1, conv2 = str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4())
        agent_dir = self.root / 'state' / 'agents' / agent_id
        launch_dir = agent_dir / 'launches' / launch_id
        launch_dir.mkdir(parents=True)
        home = self._create_saved_conversation(conv1)
        write(agent_dir / 'properties.json', {
            'id': agent_id, 'name': 'guard', 'harness': 'agy', 'workspace': str(self.work),
            'universe_id': read(self.root / 'universe.json')['id'],
            'native_session_provider': 'agy', 'native_session_id': conv1,
            'native_session_home': os.path.realpath(home),
        })
        write(agent_dir / 'runtime.json', {
            'launch_id': launch_id, 'state': 'running', 'status': 'working',
            'control_endpoint': {'transport': 'unix-json', 'address': str(self.work / 'rpc.sock')},
            'native_mode': {'policy': 'auto', 'mode': 'bridge', 'fallback_reason': None},
            'native_bridge': BRIDGE,
        })
        write(launch_dir / 'request.json', {
            'argv': ['agy'],
            'env': {
                'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': agent_id,
                'MV2_LAUNCH_ID': launch_id, 'MV2_SHELL_CALLBACK': '1',
            },
            'unset_env': [], 'cleanup_files': [agy_bridge.LS_FILE], 'shell_callback': True,
            'expected_native_id': conv1,
        })
        ls_file = agy_bridge.write_ls_info(agent_dir, launch_id, '127.0.0.1:5555', 'tok-123', conv1)
        ls_file.chmod(0o644)
        self.assertIsNone(agy_bridge.read_ls_info(agent_dir, launch_id))
        ls_file.unlink()
        outside_ls = self.work / 'external-ls.json'
        outside_ls.write_text(json.dumps({
            'schema': 1, 'launch_id': launch_id, 'ls_address': '127.0.0.1:5555', 'csrf_token': 'tok-123',
        }))
        outside_ls.chmod(0o600)
        ls_file.symlink_to(outside_ls)
        self.assertIsNone(agy_bridge.read_ls_info(agent_dir, launch_id))
        ls_file.unlink()

        # 4. Core shell_callback fences: refuses without MV2_SHELL_CALLBACK, refuses mismatched caller identity,
        # refuses undeclared/revoked files, refuses stopped/cleanup_retired launches (without claiming a session),
        # refuses symlinked launches parent, refuses rebinding different native_id, rejects lone surrogates upfront,
        # and clears env in runner.
        env_with_creds = {
            'MV2_UNIVERSE': str(self.root),
            'MV2_AGENT_ID': agent_id,
            'MV2_LAUNCH_ID': launch_id,
            'MV2_SHELL_CALLBACK': '1',
            'ANTIGRAVITY_LS_ADDRESS': '127.0.0.1:5555',
            'ANTIGRAVITY_CSRF_TOKEN': 'tok-123',
            'ANTIGRAVITY_CONVERSATION_ID': conv2,
        }
        self.assertFalse(native.shell_callback(self.root, agent_dir, launch_id, {**env_with_creds, 'MV2_SHELL_CALLBACK': '0'}))
        self.assertFalse(native.shell_callback(self.root, agent_dir, launch_id, {**env_with_creds, 'MV2_AGENT_ID': str(uuid4()), 'ANTIGRAVITY_CONVERSATION_ID': conv1}))
        self.assertFalse(native.shell_callback(self.root, agent_dir, launch_id, {**env_with_creds, 'MV2_UNIVERSE': str(self.work), 'ANTIGRAVITY_CONVERSATION_ID': conv1}))
        self.assertFalse(native.shell_callback(self.root, agent_dir, launch_id, {**env_with_creds, 'MV2_LAUNCH_ID': str(uuid4()), 'ANTIGRAVITY_CONVERSATION_ID': conv1}))
        self.assertFalse(native.shell_callback(self.root, agent_dir, launch_id, env_with_creds))  # conv2 != bound conv1
        self.assertEqual(read(agent_dir / 'properties.json')['native_session_id'], conv1)
        with patch.object(AgyAdapter, 'shell_callback', return_value=ShellCallbackResult(launch_files={'extra.json': '{}'})):
            with self.assertRaisesRegex(ValueError, 'not declared in cleanup_files'):
                native.shell_callback(self.root, agent_dir, launch_id, {**env_with_creds, 'ANTIGRAVITY_CONVERSATION_ID': conv1})
        # Revoking request opt-in or stopping/retiring while the hook runs must neither write files nor claim a session.
        saved_props = read(agent_dir / 'properties.json')
        write(agent_dir / 'properties.json', {**saved_props, 'native_session_id': None})
        def stop_during_hook(*_):
            write(agent_dir / 'runtime.json', {**read(agent_dir / 'runtime.json'), 'state': 'stopped', 'cleanup_retired': True})
            return ShellCallbackResult(native_id=conv2, bridge=BRIDGE, launch_files={agy_bridge.LS_FILE: '{}'})
        with patch.object(AgyAdapter, 'shell_callback', side_effect=stop_during_hook):
            self.assertFalse(native.shell_callback(self.root, agent_dir, launch_id, {**env_with_creds, 'ANTIGRAVITY_CONVERSATION_ID': conv2}))
        self.assertIsNone(read(agent_dir / 'properties.json').get('native_session_id'))
        self.assertFalse(ls_file.exists())
        write(agent_dir / 'properties.json', saved_props)
        write(agent_dir / 'runtime.json', {**read(agent_dir / 'runtime.json'), 'state': 'running', 'cleanup_retired': False})
        saved_req = read(launch_dir / 'request.json')
        def revoke_during_hook(*_):
            write(launch_dir / 'request.json', {**saved_req, 'shell_callback': False, 'cleanup_files': []})
            return ShellCallbackResult(bridge=BRIDGE, launch_files={agy_bridge.LS_FILE: '{}'})
        with patch.object(AgyAdapter, 'shell_callback', side_effect=revoke_during_hook):
            self.assertFalse(native.shell_callback(self.root, agent_dir, launch_id, {**env_with_creds, 'ANTIGRAVITY_CONVERSATION_ID': conv1}))
        self.assertFalse(ls_file.exists())
        write(launch_dir / 'request.json', saved_req)
        # Lone surrogates must be rejected before writing any earlier entry in the batch.
        with self.assertRaisesRegex(ValueError, 'UTF-8'):
            launch_files.write_files(launch_dir, {'first.txt': 'ok', 'bad.txt': '\ud800'}, agent_dir=agent_dir)
        self.assertFalse((launch_dir / 'first.txt').exists())
        with self.assertRaisesRegex(ValueError, 'UTF-8'):
            lifecycle._validate_launch_files(CommandSpec(('agy',), launch_files={'bad.txt': '\ud800'}))
        # Fallback payload bytes stay outside request.json (only names retained), and runner fallbacks must match primary.
        fb = CommandSpec(('agy',), launch_files={'private.json': 'secret'}, cleanup_files=('private.json',))
        req = lifecycle._request(['agy'], {}, CommandSpec(('agy',), fallback=fb), None, None)
        self.assertNotIn('launch_files', req['fallback'])
        self.assertEqual(req['fallback']['launch_file_names'], ['private.json'])
        from multiverse.harnesses.types import RpcProxySpec
        with self.assertRaisesRegex(ValueError, 'must match a primary launch_files entry'):
            lifecycle._validate_launch_files(
                CommandSpec(('agy',), rpc_proxy=RpcProxySpec('/tmp/s', '/tmp/u', {}), fallback=fb)
            )
        # Initial launch-file publication is inside lifecycle.start's cleanup try scope.
        with self.subTest('initial launch_files failure unlinks earlier declared cleanup_files'):
            real_write = launch_files.write_files
            def fail_after_first(path, files, **kwargs):
                real_write(path, {'declared.txt': 'secret'}, **kwargs)
                raise OSError('simulated disk full on second launch file')
            failing_adapter = AgyAdapter.configured('agy', {'executable': sys.executable})
            failing_cmd = CommandSpec(
                (sys.executable, '-c', 'pass'),
                launch_files={'declared.txt': 'secret', 'second.txt': 'more'},
                cleanup_files=('declared.txt',),
            )
            with patch.object(lifecycle.harnesses, 'load', return_value=failing_adapter), \
                    patch.object(failing_adapter, 'spawn', return_value=failing_cmd), \
                    patch.object(lifecycle, '_write_launch_files', side_effect=fail_after_first), \
                    patch.object(lifecycle, 'Tmux'):
                with self.assertRaisesRegex(OSError, 'simulated disk full'):
                    lifecycle.start(self.root, name='partial-write', harness='agy')
            self.assertEqual(list((self.root / 'state' / 'agents').rglob('declared.txt')), [])
        # Replacing launches/ with a symlink blocks anchored writes, request reads, and cleanup.
        elsewhere = self.work / 'linked-launches'
        (agent_dir / 'launches').rename(elsewhere)
        (agent_dir / 'launches').symlink_to(elsewhere, target_is_directory=True)
        try:
            with self.assertRaises((OSError, ValueError)):
                launch_files.write_files(launch_dir, {'escaped.txt': 'nope'})
            with self.assertRaises((OSError, ValueError)):
                launch_files.read_request(agent_dir, launch_id)
            self.assertFalse((elsewhere / launch_id / 'escaped.txt').exists())
        finally:
            (agent_dir / 'launches').unlink()
            elsewhere.rename(agent_dir / 'launches')
        write(agent_dir / 'runtime.json', {**read(agent_dir / 'runtime.json'), 'cleanup_retired': True})
        self.assertFalse(native.shell_callback(self.root, agent_dir, launch_id, {**env_with_creds, 'ANTIGRAVITY_CONVERSATION_ID': conv1}))
        self.assertFalse(ls_file.exists())
        write(agent_dir / 'runtime.json', {**read(agent_dir / 'runtime.json'), 'cleanup_retired': False})
        with patch.dict(os.environ, {'MV2_SHELL_CALLBACK': '1'}):
            stripped = runner.launch_environment({'env': {'MV2_CLI': '/usr/bin/mv2'}, 'unset_env': [], 'shell_callback': False})
            self.assertNotIn('MV2_SHELL_CALLBACK', stripped)
        # native.execute strips MV2_* injected via command.environment.
        res = native.execute(CommandSpec((sys.executable, '-c', 'import os; print(os.environ.get("MV2_TEST", ""))'),
                                         environment={'MV2_TEST': 'leaked'}), self.work)
        self.assertEqual(res.stdout.strip(), '')

        # 5. Resume mismatch fence: first callback with S2 != expected S1 records binding_conflict instead of rebinding.
        cb_env = {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': agent_id, 'MV2_LAUNCH_ID': launch_id, 'HOME': str(home.parent.parent)}
        agy_bridge.handle_statusline({'conversation_id': conv2, 'agent_state': 'idle'}, env=cb_env)
        self.assertEqual(read(agent_dir / 'properties.json')['native_session_id'], conv1)
        self.assertIn('refusing automatic rebind', read(agent_dir / 'runtime.json').get('binding_conflict', ''))
        # Once expected conv1 IS observed, a later /clear to conv2 succeeds, and a delayed conv1 callback is ignored.
        write(agent_dir / 'runtime.json', {**read(agent_dir / 'runtime.json'), 'binding_conflict': None})
        agy_bridge.handle_statusline({'conversation_id': conv1, 'agent_state': 'working'}, env=cb_env)
        agy_bridge.handle_statusline({'conversation_id': conv2, 'agent_state': 'idle'}, env=cb_env)
        self.assertEqual(read(agent_dir / 'properties.json')['native_session_id'], conv2)
        self.assertEqual(read(agent_dir / 'runtime.json')['status'], 'idle')
        agy_bridge.handle_statusline({'conversation_id': conv1, 'agent_state': 'tool_use'}, env=cb_env)
        self.assertEqual(read(agent_dir / 'properties.json')['native_session_id'], conv2)
        self.assertEqual(read(agent_dir / 'runtime.json')['status'], 'idle')

        # 6. Follow-up queue: damaged file preserved, oversized queue rejected, receipt write error preserves entry.
        qpath = launch_dir / agy_bridge.FOLLOWUPS_FILE
        qpath.write_text('{"entries": [')
        qpath.chmod(0o600)
        with self.assertRaises(ValueError):
            agy_bridge.enqueue_followup(agent_dir, launch_id, str(uuid4()), conv2, 'next')
        self.assertEqual(qpath.read_text(), '{"entries": [')
        qpath.unlink()
        with self.assertRaisesRegex(ValueError, 'byte limit'):
            agy_bridge.enqueue_followup(agent_dir, launch_id, str(uuid4()), conv2, 'x' * (agy_bridge.MAX_QUEUE_BYTES + 10))
        self.assertFalse(qpath.exists())
        m_keep = str(uuid4())
        agy_bridge.enqueue_followup(agent_dir, launch_id, m_keep, conv2, 'keep me')
        with patch.object(agy_bridge, 'mark_receipt_delivered', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                agy_bridge.handle_hook('Stop', {'conversationId': conv2, 'fullyIdle': True}, env=cb_env)
        self.assertTrue(agy_bridge.has_queued_followup(agent_dir, launch_id, m_keep))
        # After successful Stop handoff, a second Stop hook does not replay the same entry.
        self.assertEqual(agy_bridge.handle_hook('Stop', {'conversationId': conv2, 'fullyIdle': True}, env=cb_env),
                         {'decision': 'continue', 'reason': 'keep me'})
        self.assertEqual(agy_bridge.handle_hook('Stop', {'conversationId': conv2, 'fullyIdle': True}, env=cb_env), {})

        # 7. agentapi exit 0 without recipientId returns unknown (not native_queued); exit 1 removes its pre-written receipt.
        server = _LoopbackListener()
        self.addCleanup(server.close)
        agy_bridge.write_ls_info(agent_dir, launch_id, server.address, 'tok-ok', conv2)
        adapter = AgyAdapter.configured('agy', {})
        session = SessionHandle.from_records(read(agent_dir / 'properties.json'), read(agent_dir / 'runtime.json'), agent_dir)
        executor = Mock()
        executor.command.return_value = subprocess.CompletedProcess(args=['agy'], returncode=0, stdout='{}', stderr='')
        m_unack = str(uuid4())
        uncertain = adapter.send(session, Message(m_unack, 'maybe', delivery='steer', framed='Message from user:\nmaybe'), executor)
        self.assertEqual(uncertain.state, 'unknown')
        self.assertFalse(agy_bridge.read_receipt(agent_dir, m_unack)['acknowledged'])
        # Ambiguous non-zero exit (e.g., DeadlineExceeded) keeps the receipt and returns unknown;
        # a proved pre-connect failure returns definite error and removes the pre-written receipt.
        executor.command.return_value = subprocess.CompletedProcess(
            args=['agy'], returncode=1, stdout='',
            stderr='rpc error: code = DeadlineExceeded desc = context deadline exceeded',
        )
        m_deadline = str(uuid4())
        deadline_res = adapter.send(session, Message(m_deadline, 'deadline body', delivery='steer', framed='Message from user:\ndeadline body'), executor)
        self.assertEqual(deadline_res.state, 'unknown')
        self.assertIsNotNone(agy_bridge.read_receipt(agent_dir, m_deadline))
        self.assertFalse(agy_bridge.read_receipt(agent_dir, m_deadline)['acknowledged'])
        agy_bridge.remove_receipt(agent_dir, m_deadline)
        executor.command.return_value = subprocess.CompletedProcess(
            args=['agy'], returncode=1,
            stdout=json.dumps({'error': 'rpc error: code = Unavailable desc = transport: Error while dialing: connect: connection refused'}),
            stderr='',
        )
        m_err = str(uuid4())
        failed_send = adapter.send(session, Message(m_err, 'same body', delivery='steer', framed='Message from user:\nsame body'), executor)
        self.assertEqual(failed_send.state, 'error')
        self.assertIsNone(agy_bridge.read_receipt(agent_dir, m_err))
        # agentapi CommandSpec strips sender ANTIGRAVITY_* identity markers via unset_env=PARENT_AGY.
        self.assertEqual(executor.command.call_args.args[0].unset_env, PARENT_AGY)

        # 8. Strict transcript confirmation: route-specific envelopes, undated rows, and ambiguous cohort protection.
        conv3 = str(uuid4())
        self._create_saved_conversation(conv3)
        transcript = home / 'brain' / conv3 / '.system_generated' / 'logs' / 'transcript.jsonl'
        transcript.parent.mkdir(parents=True, exist_ok=True)
        dup_text = 'Message from user:\nsame body'
        dup_hash = agy_storage.message_hash(dup_text)
        t0 = '2026-11-22T12:00:00Z'
        t2 = '2026-11-22T12:00:02Z'
        r1, r2 = sorted((str(uuid4()), str(uuid4())))
        agy_bridge.write_receipt(agent_dir, r1, launch_id=launch_id, native_id=conv3, route='agentapi',
                                 submitted_at=t0, text_sha256=dup_hash, acknowledged=False)
        agy_bridge.write_receipt(agent_dir, r2, launch_id=launch_id, native_id=conv3, route='agentapi',
                                 submitted_at=t2, text_sha256=dup_hash, delivered_at=t2, acknowledged=True)
        # USER_INPUT row, undated SYSTEM_MESSAGE row, and wrong-route Stop row must NOT confirm an agentapi receipt.
        transcript.write_text(
            json.dumps({'type': 'USER_INPUT', 'created_at': '2026-11-22T12:00:03Z', 'content': f'<USER_REQUEST>\n{dup_text}\n</USER_REQUEST>'}) + '\n'
            + json.dumps({'type': 'SYSTEM_MESSAGE', 'content': f'<SYSTEM_MESSAGE>\n[Message] sender=system content={dup_text}\n</SYSTEM_MESSAGE>'}) + '\n'
            + json.dumps({'type': 'SYSTEM_MESSAGE', 'created_at': '2026-11-22T12:00:03Z', 'content': f'<SYSTEM_MESSAGE>\nStop hook blocked termination: {dup_text}\n</SYSTEM_MESSAGE>'}) + '\n'
        )
        self.assertEqual(adapter.get_delivery(session, r1).state, 'unknown')
        # A single row after both submissions is ambiguous: neither the earlier unacknowledged nor the later attempt confirms.
        with transcript.open('a') as handle:
            handle.write(json.dumps({
                'type': 'SYSTEM_MESSAGE', 'created_at': '2026-11-22T12:00:03Z',
                'content': f'<SYSTEM_MESSAGE>\n[Message] sender=system content={dup_text}\n</SYSTEM_MESSAGE>',
            }) + '\n')
        ordered_ids = [r1, r2]
        self.assertIsNot((adapter.get_delivery(session, r1).value or {}).get('transcript_confirmed'), True)
        self.assertIsNot((adapter.get_delivery(session, r2).value or {}).get('transcript_confirmed'), True)
        # Once a second matching row lands, both attempts are accounted for and confirm.
        with transcript.open('a') as handle:
            handle.write(json.dumps({
                'type': 'SYSTEM_MESSAGE', 'created_at': '2026-11-22T12:00:04Z',
                'content': f'<SYSTEM_MESSAGE>\n[Message] sender=system content={dup_text}\n</SYSTEM_MESSAGE>',
            }) + '\n')
        self.assertTrue(adapter.get_delivery(session, r1).value['transcript_confirmed'])
        self.assertTrue(adapter.get_delivery(session, r2).value['transcript_confirmed'])
        # A temporally isolated earlier row (strictly before a later attempt) still confirms only the earlier attempt.
        iso_text = 'Message from user:\nisolated body'
        iso_hash = agy_storage.message_hash(iso_text)
        iso1, iso2 = str(uuid4()), str(uuid4())
        agy_bridge.write_receipt(agent_dir, iso1, launch_id=launch_id, native_id=conv3, route='agentapi',
                                 submitted_at='2026-11-22T12:10:00Z', text_sha256=iso_hash)
        agy_bridge.write_receipt(agent_dir, iso2, launch_id=launch_id, native_id=conv3, route='agentapi',
                                 submitted_at='2026-11-22T12:10:10Z', text_sha256=iso_hash)
        with transcript.open('a') as handle:
            handle.write(json.dumps({
                'type': 'SYSTEM_MESSAGE', 'created_at': '2026-11-22T12:10:01Z',
                'content': f'<SYSTEM_MESSAGE>\n[Message] sender=system content={iso_text}\n</SYSTEM_MESSAGE>',
            }) + '\n')
        self.assertTrue(adapter.get_delivery(session, iso1).value['transcript_confirmed'])
        self.assertEqual(adapter.get_delivery(session, iso2).state, 'unknown')

        # 8b. Reviewer security/durability regressions:
        # (a) Pre-submission agentapi write_receipt error rolls agentapi_submitting back to queued without calling command.
        m_roll = str(uuid4())
        rollback_exec = Mock()
        real_write_receipt = agy_bridge.write_receipt
        real_enqueue = agy_bridge.enqueue_followup
        write(agent_dir / 'runtime.json', {**read(agent_dir / 'runtime.json'), 'status': 'working'})
        def fail_agentapi_receipt(*args, **kwargs):
            if kwargs.get('route') == 'agentapi':
                raise OSError('transient disk refusal')
            return real_write_receipt(*args, **kwargs)
        def flip_idle_for_rollback(*args, **kwargs):
            real_enqueue(*args, **kwargs)
            write(agent_dir / 'runtime.json', {**read(agent_dir / 'runtime.json'), 'status': 'idle'})
        with patch.object(agy_bridge, 'write_receipt', side_effect=fail_agentapi_receipt), \
                patch.object(agy_bridge, 'enqueue_followup', side_effect=flip_idle_for_rollback):
            rolled = adapter.send(session, Message(m_roll, 'rollback me', delivery='follow-up'), rollback_exec)
        rollback_exec.command.assert_not_called()
        self.assertEqual(rolled.state, 'native_queued')
        self.assertEqual(agy_bridge.followup_state(agent_dir, launch_id, m_roll), 'queued')
        agy_bridge.remove_followup(agent_dir, launch_id, m_roll, reset_idle_flush=True)
        # Pre-submission receipt write failure on both busy follow-up and steer returns definite error (nothing queued/sent).
        for delivery_mode, status_val in (('follow-up', 'working'), ('steer', 'idle')):
            m_ro = str(uuid4())
            ro_exec = Mock()
            write(agent_dir / 'runtime.json', {**read(agent_dir / 'runtime.json'), 'status': status_val})
            with patch.object(agy_bridge, 'write_receipt', side_effect=PermissionError(13, 'Permission denied', '.write-x')):
                ro_res = adapter.send(session, Message(m_ro, 'ro check', delivery=delivery_mode), ro_exec)
            self.assertEqual(ro_res.state, 'error', delivery_mode)
            self.assertIn('Cannot persist Antigravity delivery receipt before send', ro_res.reason)
            ro_exec.command.assert_not_called()
            self.assertIsNone(agy_bridge.followup_state(agent_dir, launch_id, m_ro))

        # (b) localhost LS address pins the verified numeric loopback endpoint for agentapi too.
        port = server.port
        agy_bridge.write_ls_info(agent_dir, launch_id, f'localhost:{port}', 'tok-ok', conv2)
        pin_exec = Mock()
        pin_exec.command.return_value = subprocess.CompletedProcess(
            args=['agy'], returncode=0,
            stdout=json.dumps({'response': {'sendMessage': {'recipientId': conv2}}}), stderr='',
        )
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', port))]):
            pinned_res = adapter.send(session, Message(str(uuid4()), 'pin check', delivery='steer'), pin_exec)
        self.assertEqual(pinned_res.state, 'native_queued')
        self.assertEqual(pin_exec.command.call_args.args[0].environment['ANTIGRAVITY_LS_ADDRESS'], f'127.0.0.1:{port}')
        agy_bridge.write_ls_info(agent_dir, launch_id, server.address, 'tok-ok', conv2)

        # (c) Broad-mode agy-followups.json error omits raw agent/launch UUID path.
        qpath.write_text(json.dumps({'schema': 1, 'launch_id': launch_id, 'entries': []}))
        qpath.chmod(0o644)
        with self.assertRaises(ValueError) as bad_mode_err:
            agy_bridge.enqueue_followup(agent_dir, launch_id, str(uuid4()), conv2, 'nope')
        self.assertNotIn(agent_id, str(bad_mode_err.exception))
        self.assertNotIn(launch_id, str(bad_mode_err.exception))
        qpath.unlink()

        # (d) Damaged/missing request.json refuses foreign rebind; resume expected_native_id runs before unbound branch;
        # unresolved binding_conflict suppresses Stop-hook, send, and shell_callback delivery.
        saved_req_text = (launch_dir / 'request.json').read_text()
        (launch_dir / 'request.json').write_text('{"expected_native_id":')
        foreign_conv = str(uuid4())
        agy_bridge.handle_hook('SessionStart', {'conversationId': foreign_conv}, env=cb_env)
        self.assertEqual(read(agent_dir / 'properties.json')['native_session_id'], conv2)
        self.assertTrue(read(agent_dir / 'runtime.json').get('binding_conflict'))
        m_sup = str(uuid4())
        agy_bridge.enqueue_followup(agent_dir, launch_id, m_sup, conv2, 'blocked by conflict')
        self.assertEqual(agy_bridge.handle_hook('Stop', {'conversationId': conv2, 'fullyIdle': True}, env=cb_env), {})
        self.assertEqual(adapter.send(session, Message(str(uuid4()), 'blocked', delivery='steer'), Mock()).state, 'unavailable')
        self.assertIsNone(adapter.shell_callback(session, {**env_with_creds, 'ANTIGRAVITY_CONVERSATION_ID': conv2}))
        agy_bridge.remove_followup(agent_dir, launch_id, m_sup)
        (launch_dir / 'request.json').write_text(saved_req_text)
        write(agent_dir / 'runtime.json', {**read(agent_dir / 'runtime.json'), 'binding_conflict': None})
        # Resume expected_native_id is enforced even if native_session_id is currently unbound (None).
        saved_state_path = launch_dir / agy_bridge.SESSION_STATE_FILE
        saved_state_text = saved_state_path.read_text() if saved_state_path.exists() else None
        saved_state_path.unlink(missing_ok=True)
        saved_props_cur = read(agent_dir / 'properties.json')
        write(agent_dir / 'properties.json', {**saved_props_cur, 'native_session_id': None})
        agy_bridge.handle_hook('SessionStart', {'conversationId': foreign_conv}, env=cb_env)
        self.assertIsNone(read(agent_dir / 'properties.json').get('native_session_id'))
        conflict_text = read(agent_dir / 'runtime.json').get('binding_conflict', '')
        self.assertIn(conv1, conflict_text)
        # Human-facing messaging redaction must read cleanly (no "conversation the session instead of resumed session the session").
        self.assertEqual(_messaging.failure_reason(conflict_text), 'the saved conversation does not match the running session')
        write(agent_dir / 'properties.json', saved_props_cur)
        write(agent_dir / 'runtime.json', {**read(agent_dir / 'runtime.json'), 'binding_conflict': None})
        if saved_state_text is not None:
            saved_state_path.write_text(saved_state_text)
            saved_state_path.chmod(0o600)

        # (e) /clear agy-ls.json sync cannot recreate agy-ls.json if cleanup retired runtime.json during read_ls_info.
        real_read_ls = agy_bridge.read_ls_info
        saved_runtime_before_clear = read(agent_dir / 'runtime.json')
        def retire_during_read(directory, lid):
            existing = real_read_ls(directory, lid)
            agy_bridge.clear_ls_info(launch_dir)
            write(agent_dir / 'runtime.json', {**read(agent_dir / 'runtime.json'), 'state': 'stopped', 'cleanup_retired': True})
            return existing
        conv_race = str(uuid4())
        self._create_saved_conversation(conv_race)
        with patch.object(agy_bridge, 'read_ls_info', side_effect=retire_during_read):
            agy_bridge.handle_statusline({'conversation_id': conv_race, 'agent_state': 'idle'}, env=cb_env)
        self.assertFalse((launch_dir / agy_bridge.LS_FILE).exists())
        write(agent_dir / 'runtime.json', saved_runtime_before_clear)
        agy_bridge.write_ls_info(agent_dir, launch_id, server.address, 'tok-ok', conv_race)

        # (f) Round 1 review regressions:
        # 1. >64 KiB request.json (e.g. 70 KB task/wake prompt) still validates launch proof.
        saved_req_raw = (launch_dir / 'request.json').read_text()
        launch_files.write_request(agent_dir, launch_id, {
            'expected_native_id': None, 'argv': ['agy', '--prompt-interactive=' + 'x' * 70_000], 'env': {},
        })
        self.assertEqual(agy_bridge._launch_expected_id(agent_dir, launch_id), ('fresh', None))
        (launch_dir / 'request.json').write_text(saved_req_raw)

        # 2. Handed-off follow-up entries drop full text so large delivered prompts never exhaust MAX_QUEUE_BYTES.
        for _ in range(4):
            big_id = str(uuid4())
            agy_bridge.enqueue_followup(agent_dir, launch_id, big_id, conv_race, 'z' * 60_000)
            ans = agy_bridge.handle_hook('Stop', {'conversationId': conv_race, 'fullyIdle': True}, env=cb_env)
            self.assertEqual(ans.get('decision'), 'continue')
        agy_bridge.enqueue_followup(agent_dir, launch_id, str(uuid4()), conv_race, 'still fits ' * 2000)
        qpath.unlink()

        # 3. Stop hook rechecks runtime.json under the queue lock (ending_at stops handoff).
        m_end = str(uuid4())
        agy_bridge.enqueue_followup(agent_dir, launch_id, m_end, conv_race, 'ending race')
        write(agent_dir / 'runtime.json', {**read(agent_dir / 'runtime.json'), 'ending_at': now()})
        self.assertEqual(agy_bridge.handle_hook('Stop', {'conversationId': conv_race, 'fullyIdle': True}, env=cb_env), {})
        self.assertEqual(agy_bridge.followup_state(agent_dir, launch_id, m_end), 'queued')
        write(agent_dir / 'runtime.json', saved_runtime_before_clear)
        qpath.unlink()

        # 4. Idle statusLine flushes AT MOST ONE stranded follow-up per idle period (second waits for next active->idle).
        flush_log = self.work / 'flush-agentapi.jsonl'
        fake_flush_agy = self.work / 'fake-flush-agy'
        fake_flush_agy.write_text(
            f'#!{sys.executable}\nimport json, os, sys\nfrom pathlib import Path\n'
            f'with Path({str(flush_log)!r}).open("a") as f:\n'
            '    f.write(json.dumps({"argv": sys.argv[1:], '
            '"leaked": [k for k in ("ANTIGRAVITY_CONVERSATION_ID", "ANTIGRAVITY_TRAJECTORY_ID") if k in os.environ], '
            '"addr": os.environ.get("ANTIGRAVITY_LS_ADDRESS")}) + "\\n")\n'
            'print(json.dumps({"response": {"sendMessage": {"recipientId": sys.argv[3]}}}))\n'
        )
        fake_flush_agy.chmod(0o755)
        launch_files.write_request(agent_dir, launch_id, {
            **json.loads(saved_req_raw), 'argv': [str(fake_flush_agy), '--conversation', conv1],
        })
        m_stranded_1, m_stranded_2 = str(uuid4()), str(uuid4())
        for mid, body in ((m_stranded_1, 'stranded first'), (m_stranded_2, 'stranded second')):
            agy_bridge.write_receipt(agent_dir, mid, launch_id=launch_id, native_id=conv_race, route='stop-hook',
                                     submitted_at=now(), text_sha256=agy_storage.message_hash(body))
            agy_bridge.enqueue_followup(agent_dir, launch_id, mid, conv_race, body)
        def _wait_followup(mid: str, expected: str, timeout: float = 5.0) -> None:
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if agy_bridge.followup_state(agent_dir, launch_id, mid) == expected:
                    return
                time.sleep(0.02)
            self.fail(f'Follow-up {mid} stayed {agy_bridge.followup_state(agent_dir, launch_id, mid)!r} instead of {expected!r}')

        with patch.dict(os.environ, {'ANTIGRAVITY_CONVERSATION_ID': str(uuid4()), 'ANTIGRAVITY_TRAJECTORY_ID': 'sender-t'}):
            # Paused entries (after Esc / USER_CANCELED) must NOT be flushed by idle statusLine.
            agy_bridge.handle_hook('Stop', {'conversationId': conv_race, 'terminationReason': 'USER_CANCELED', 'fullyIdle': True}, env=cb_env)
            agy_bridge.handle_statusline({'conversation_id': conv_race, 'agent_state': 'idle'}, env=cb_env)
            time.sleep(0.15)
            self.assertEqual(agy_bridge.followup_state(agent_dir, launch_id, m_stranded_1), 'queued')
            self.assertFalse(flush_log.exists())
            # A late idle statusLine after PreInvocation (before Stop) must NOT flush queued follow-ups mid-turn.
            agy_bridge.handle_hook('PreInvocation', {'conversationId': conv_race}, env=cb_env)
            agy_bridge.handle_statusline({'conversation_id': conv_race, 'agent_state': 'idle'}, env=cb_env)
            time.sleep(0.15)
            self.assertEqual(agy_bridge.followup_state(agent_dir, launch_id, m_stranded_1), 'queued')
            self.assertFalse(flush_log.exists())
            # Once Stop finishes (clearing turn_active), idle statusLine flushes AT MOST ONE stranded post-Stop follow-up.
            qpath.unlink()
            agy_bridge.handle_hook('PreInvocation', {'conversationId': conv_race}, env=cb_env)
            agy_bridge.handle_hook('Stop', {'conversationId': conv_race, 'fullyIdle': True}, env=cb_env)
            for mid, body in ((m_stranded_1, 'stranded first'), (m_stranded_2, 'stranded second')):
                agy_bridge.write_receipt(agent_dir, mid, launch_id=launch_id, native_id=conv_race, route='stop-hook',
                                         submitted_at=now(), text_sha256=agy_storage.message_hash(body))
                agy_bridge.enqueue_followup(agent_dir, launch_id, mid, conv_race, body)
            agy_bridge.handle_statusline({'conversation_id': conv_race, 'agent_state': 'idle'}, env=cb_env)
            _wait_followup(m_stranded_1, 'handed_off')
            # Second consecutive idle statusLine (even with status cache deleted) must NOT flush the second entry.
            (launch_dir / agy_bridge.STATUS_CACHE_FILE).unlink(missing_ok=True)
            agy_bridge.handle_statusline({'conversation_id': conv_race, 'agent_state': 'idle'}, env=cb_env)
            time.sleep(0.15)
        self.assertEqual(agy_bridge.followup_state(agent_dir, launch_id, m_stranded_1), 'handed_off')
        self.assertEqual(agy_bridge.followup_state(agent_dir, launch_id, m_stranded_2), 'queued')
        flushed_rows = [json.loads(line) for line in flush_log.read_text().splitlines() if line.strip()]
        self.assertEqual(len(flushed_rows), 1)
        self.assertEqual(flushed_rows[0]['argv'], ['agentapi', 'send-message', conv_race, 'stranded first'])
        self.assertEqual(flushed_rows[0]['leaked'], [])
        self.assertEqual(agy_bridge.read_receipt(agent_dir, m_stranded_1)['route'], 'agentapi')
        self.assertTrue(agy_bridge.read_receipt(agent_dir, m_stranded_1)['acknowledged'])
        # Transitioning through working -> idle clears the idle_flushed gate and flushes the second queued entry.
        agy_bridge.handle_statusline({'conversation_id': conv_race, 'agent_state': 'working'}, env=cb_env)
        agy_bridge.handle_statusline({'conversation_id': conv_race, 'agent_state': 'idle'}, env=cb_env)
        _wait_followup(m_stranded_2, 'handed_off')
        flushed_rows = [json.loads(line) for line in flush_log.read_text().splitlines() if line.strip()]
        self.assertEqual(len(flushed_rows), 2)
        self.assertEqual(flushed_rows[1]['argv'], ['agentapi', 'send-message', conv_race, 'stranded second'])

        # 4b. Fast turn finishing BEFORE agentapi returns, ambiguous DeadlineExceeded exit, and expired idle_flushed_at
        # must never strand later follow-ups on an idle agent.
        qpath.unlink(missing_ok=True)
        write(agent_dir / 'runtime.json', {**read(agent_dir / 'runtime.json'), 'status': 'idle'})
        race_session = SessionHandle.from_records(read(agent_dir / 'properties.json'), read(agent_dir / 'runtime.json'), agent_dir)
        def fast_turn_exec(spec, timeout=15):
            agy_bridge.handle_hook('PreInvocation', {'conversationId': conv_race}, env=cb_env)
            agy_bridge.handle_hook('Stop', {'conversationId': conv_race, 'fullyIdle': True}, env=cb_env)
            agy_bridge.handle_statusline({'conversation_id': conv_race, 'agent_state': 'idle'}, env=cb_env)
            return subprocess.CompletedProcess(
                spec.argv, 0, json.dumps({'response': {'sendMessage': {'recipientId': conv_race}}}), '',
            )
        fast_exec = Mock()
        fast_exec.command.side_effect = fast_turn_exec
        fast_res = adapter.send(race_session, Message(str(uuid4()), 'fast steer', delivery='steer'), fast_exec)
        self.assertEqual(fast_res.state, 'native_queued')
        # Subsequent follow-up on the now-idle agent submits immediately instead of stranding in queue.
        next_exec = Mock()
        next_exec.command.return_value = subprocess.CompletedProcess(
            ['agy'], 0, json.dumps({'response': {'sendMessage': {'recipientId': conv_race}}}), '',
        )
        m_after_fast = str(uuid4())
        after_fast = adapter.send(race_session, Message(m_after_fast, 'after fast steer', delivery='follow-up'), next_exec)
        self.assertEqual(after_fast.value['route'], 'agentapi')
        self.assertIsNone(agy_bridge.followup_state(agent_dir, launch_id, m_after_fast))
        # Ambiguous DeadlineExceeded exit on idle send must also roll back idle_flushed so later follow-ups do not strand.
        qpath.unlink(missing_ok=True)
        ambig_exec = Mock()
        ambig_exec.command.return_value = subprocess.CompletedProcess(
            ['agy'], 1, '', 'rpc error: code = DeadlineExceeded desc = context deadline exceeded',
        )
        self.assertEqual(adapter.send(race_session, Message(str(uuid4()), 'ambig', delivery='follow-up'), ambig_exec).state, 'unknown')
        m_after_ambig = str(uuid4())
        after_ambig = adapter.send(race_session, Message(m_after_ambig, 'after ambig', delivery='follow-up'), next_exec)
        self.assertEqual(after_ambig.value['route'], 'agentapi')
        # An expired idle_flushed_at marker (> grace period) self-clears on idle statusLine and flushes queued follow-ups.
        m_expired = str(uuid4())
        agy_bridge.write_receipt(agent_dir, m_expired, launch_id=launch_id, native_id=conv_race, route='stop-hook',
                                 submitted_at=now(), text_sha256=agy_storage.message_hash('expired flush'))
        agy_bridge.enqueue_followup(agent_dir, launch_id, m_expired, conv_race, 'expired flush')
        expired_doc = read(qpath)
        expired_doc['idle_flushed'] = True
        expired_doc['idle_flushed_at'] = '2020-01-01T00:00:00+00:00'
        expired_doc['turn_active'] = False
        agy_bridge._write_private_json(qpath, expired_doc)
        agy_bridge.handle_statusline({'conversation_id': conv_race, 'agent_state': 'idle'}, env=cb_env)
        _wait_followup(m_expired, 'handed_off')
        (launch_dir / 'request.json').write_text(saved_req_raw)
        qpath.unlink()

        # 5. Same-launch queued follow-up peer does not block an already-delivered identical follow-up from confirming;
        # stat-verified receipt index keeps 5,000-receipt cohort lookups fast while still rejecting tampered receipts.
        sh_text = 'Message from user:\nsame-launch followup'
        sh_hash = agy_storage.message_hash(sh_text)
        sh_done, sh_wait = str(uuid4()), str(uuid4())
        agy_bridge.write_receipt(agent_dir, sh_done, launch_id=launch_id, native_id=conv3, route='stop-hook',
                                 submitted_at='2026-11-22T12:20:00Z', text_sha256=sh_hash,
                                 delivered_at='2026-11-22T12:20:01Z', acknowledged=True)
        agy_bridge.write_receipt(agent_dir, sh_wait, launch_id=launch_id, native_id=conv3, route='stop-hook',
                                 submitted_at='2026-11-22T12:20:02Z', text_sha256=sh_hash, acknowledged=True)
        agy_bridge.enqueue_followup(agent_dir, launch_id, sh_wait, conv3, sh_text)
        with transcript.open('a') as handle:
            handle.write(json.dumps({
                'type': 'SYSTEM_MESSAGE', 'created_at': '2026-11-22T12:20:01Z',
                'content': f'<SYSTEM_MESSAGE>\nStop hook blocked termination: {sh_text}\n</SYSTEM_MESSAGE>',
            }) + '\n')
        self.assertTrue(adapter.get_delivery(session, sh_done).value['transcript_confirmed'])
        qpath.unlink()

        receipts_dir = agent_dir / agy_bridge.RECEIPTS_DIR
        extra_files = []
        for idx in range(5000):
            other = str(uuid4())
            path = receipts_dir / f'{other}.json'
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as handle:
                json.dump({'schema': 1, 'id': other, 'launch_id': launch_id, 'native_id': conv3,
                           'route': 'agentapi', 'submitted_at': '2026-11-22T11:00:00Z',
                           'text_sha256': agy_storage.message_hash(f'bulk {idx}'),
                           'delivered_at': '2026-11-22T11:00:00Z', 'acknowledged': True}, handle)
            extra_files.append(path)
        try:
            # Prime index (and verify persistence to .index.json), then time repeated cohort lookups.
            self.assertTrue(adapter.get_delivery(session, r1).value['transcript_confirmed'])
            self.assertTrue((receipts_dir / agy_bridge._RECEIPT_INDEX_FILE).exists())
            agy_bridge._RECEIPT_CACHE.clear()  # Force reload from disk .index.json as in a separate mv2 CLI process.
            t0 = time.monotonic()
            for _ in range(10):
                self.assertTrue(adapter.get_delivery(session, r1).value['transcript_confirmed'])
            per_call = (time.monotonic() - t0) / 10
            self.assertLess(per_call, 0.25)
            # Stat verification still immediately fails closed when a cached receipt is chmodded broad or corrupted.
            extra_files[0].chmod(0o644)
            self.assertIsNone(agy_bridge.receipt_cohort(
                agent_dir, r1, native_id=conv3, route='agentapi',
                text_sha256=agy_storage.message_hash('Message from user:\nhello'),
            ))
            extra_files[0].chmod(0o600)
            extra_files[0].write_text('{corrupt')
            self.assertIsNone(agy_bridge.receipt_cohort(
                agent_dir, r1, native_id=conv3, route='agentapi',
                text_sha256=agy_storage.message_hash('Message from user:\nhello'),
            ))
        finally:
            for path in extra_files:
                path.unlink(missing_ok=True)
            (receipts_dir / agy_bridge._RECEIPT_INDEX_FILE).unlink(missing_ok=True)
            agy_bridge._RECEIPT_CACHE.clear()

        # 5b. Anchored agentapi pre-connect classification ignores echoed message text inside server errors.
        self.assertFalse(agy_bridge.is_pre_connect_agentapi_error(
            json.dumps({'error': 'rpc error: code = DeadlineExceeded desc = connect: connection refused'}),
            '',
        ))
        self.assertTrue(agy_bridge.is_pre_connect_agentapi_error(
            json.dumps({'error': 'Error while dialing: dial tcp 127.0.0.1:9: connect: connection refused'}),
            '',
        ))
        self.assertTrue(agy_bridge.is_pre_connect_agentapi_error(
            json.dumps({'error': 'rpc error: code = Unauthenticated desc = invalid token'}),
            '',
        ))

        # 6. Personal ~/.gemini/antigravity-cli/settings.json supports JSONC (comments + trailing commas);
        # corrupt personal settings fall back to plain in auto mode and fail closed in required mode;
        # launch_files reserves native-refusal.json/.refusal.lock; _screen_ready ignores trailing blank pane rows.
        personal_home = self.work / 'personal-home'
        personal_settings = personal_home / '.gemini' / 'antigravity-cli' / 'settings.json'
        personal_settings.parent.mkdir(parents=True)
        personal_settings.write_text(
            '// personal JSONC comment with "https://example.com"\n'
            '{\n'
            '  /* block comment */\n'
            '  "colorScheme": "dark",\n'
            '  "permissions": {\n'
            '    "allow": ["command(git status)",],\n'
            '    "ask": ["command(git push)",],\n'
            '    "deny": ["read_file(/etc/shadow)", "write_file(.env)",],\n'
            '  },\n'
            '}\n'
        )
        merged_cmd = AgyAdapter.configured('agy', {}).spawn(LaunchContext(
            'spawn', self._values(task='hi'),
            environment={'HOME': str(personal_home), 'MV2_UNIVERSE': str(self.root)},
        ))
        self.assertEqual(merged_cmd.native_mode['mode'], 'bridge')
        merged_perms = json.loads(merged_cmd.launch_files['settings.json'])['permissions']
        self.assertEqual(merged_perms['deny'], ['read_file(/etc/shadow)', 'write_file(.env)'])
        self.assertEqual(merged_perms['ask'], ['command(git push)'])
        self.assertIn('command(git status)', merged_perms['allow'])
        self.assertIn(f'read_file({self.root.resolve()})', merged_perms['allow'])
        personal_settings.write_text('{broken')
        team_settings = self.work / 'team-settings.json'
        team_settings.write_text(json.dumps({'permissions': {'deny': ['write_file(prod.env)']}}))
        auto_bad = AgyAdapter.configured('agy', {'args': ['--settings', str(team_settings)]}).spawn(LaunchContext(
            'spawn', self._values(task='hi'), environment={'HOME': str(personal_home)},
        ))
        self.assertEqual(auto_bad.native_mode['mode'], 'plain')
        self.assertIn('personal settings', auto_bad.native_mode['fallback_reason'])
        # Plain fallback must preserve the harness's own --settings flag (including any deny rules).
        self.assertIn('--settings', auto_bad.argv)
        self.assertIn(str(team_settings), auto_bad.argv)
        self.assertNotIn('settings.json', auto_bad.launch_files)
        with self.assertRaisesRegex(ValueError, 'personal settings'):
            AgyAdapter.configured('agy', {'inbox_bridge': True}).spawn(LaunchContext(
                'spawn', self._values(task='hi'), environment={'HOME': str(personal_home)},
            ))

        for reserved in ('native-refusal.json', '.refusal.lock'):
            with self.assertRaisesRegex(ValueError, 'collides'):
                launch_files.validate_path(reserved)

        from multiverse.harnesses.agy import _screen_ready
        padded_active = (
            '────────────────────────────────────────\n'
            '> Auto mode: edits & safe commands auto-approved\n'
            '────────────────────────────────────────\n'
            'esc to cancel   auto · Gemini 4 Neon · medium\n'
            + '\n' * 18
        )
        self.assertTrue(_screen_ready(padded_active))

        # 9. Older agy (< 1.5.0) falls back to plain on auto and refuses on inbox_bridge=True; --settings rejects dirs/FIFOs.
        old_agy = self.work / 'agy-149'
        old_agy.write_text('#!/bin/sh\necho "agy 1.4.9"\n')
        old_agy.chmod(0o755)
        auto_old = AgyAdapter.configured('agy', {'executable': str(old_agy)})
        fallback_cmd = auto_old.spawn(LaunchContext('spawn', self._values(task='hi'), environment={'HOME': str(self.work)}))
        self.assertEqual(fallback_cmd.native_mode['mode'], 'plain')
        self.assertIn('1.4.9', fallback_cmd.native_mode['fallback_reason'])
        self.assertFalse(fallback_cmd.shell_callback)
        self.assertNotIn('--settings', fallback_cmd.argv)
        req_old = AgyAdapter.configured('agy', {'executable': str(old_agy), 'inbox_bridge': True})
        with self.assertRaisesRegex(ValueError, 'older than 1.5.0'):
            req_old.spawn(LaunchContext('spawn', self._values(task='hi'), environment={'HOME': str(self.work)}))

        fifo = self.work / 'settings.fifo'
        os.mkfifo(fifo)
        for bad_target in (self.work, fifo):
            bad_adapter = AgyAdapter.configured('agy', {'args': ['--settings', str(bad_target)]})
            with self.assertRaisesRegex(ValueError, 'not a regular file'):
                bad_adapter.spawn(LaunchContext('spawn', self._values(task='hi'), environment={'HOME': str(self.work)}))

        # 10. Pre-/clear queued follow-up fails immediately as definite_failure, and accepted_message_evidence reconciles across resume.
        conv4 = str(uuid4())
        self._create_saved_conversation(conv4)
        agy_bridge.handle_statusline({'conversation_id': conv3, 'agent_state': 'working'}, env=cb_env)
        q_id = str(uuid4())
        agy_bridge.write_receipt(agent_dir, q_id, launch_id=launch_id, native_id=conv3, route='stop-hook',
                                 submitted_at=now(), text_sha256=agy_storage.message_hash('after clear'))
        agy_bridge.enqueue_followup(agent_dir, launch_id, q_id, conv3, 'after clear')
        agy_bridge.handle_statusline({'conversation_id': conv4, 'agent_state': 'idle'}, env=cb_env)
        switched = adapter.get_delivery(replace(session, native_id=conv3), q_id)
        self.assertEqual(switched.state, 'error')
        self.assertTrue(switched.value['definite_failure'])
        self.assertIn('switched', switched.reason)
        # Even after the launch later stops, a pre-/clear queued follow-up keeps its conversation-switched diagnosis.
        switched_stopped = adapter.get_delivery(replace(session, native_id=conv3, state='stopped'), q_id)
        self.assertEqual(switched_stopped.state, 'error')
        self.assertIn('switched', switched_stopped.reason)
        # Retrying that same follow-up ID on the new conversation replaces the stale pre-/clear queue entry.
        agy_bridge.enqueue_followup(agent_dir, launch_id, q_id, conv4, 'after clear')
        self.assertEqual(
            agy_bridge.handle_hook('Stop', {'conversationId': conv4, 'fullyIdle': True}, env=cb_env),
            {'decision': 'continue', 'reason': 'after clear'},
        )
        # Retrying a failed stop-hook follow-up (lost at kill or stranded by /clear) on a stopped agent wakes directly
        # with an interactive prompt (never _queue_wake / promptless resume).
        from multiverse.tools import deliver as deliver_tool
        from multiverse.universe.mailbox import append_message, update_message
        for case, target_conv in (('lost-at-kill', conv4), ('stranded-by-clear', conv3)):
            msg = append_message(agent_dir, f'retry {case}', {'name': 'user'}, 'follow-up', target_session=target_conv)
            agy_bridge.write_receipt(agent_dir, msg['id'], launch_id=launch_id, native_id=target_conv, route='stop-hook',
                                     submitted_at=now(), text_sha256=agy_storage.message_hash(f'retry {case}'))
            agy_bridge.enqueue_followup(agent_dir, launch_id, msg['id'], target_conv, f'retry {case}')
            update_message(agent_dir, msg['id'], submission_state='accepted', delivery_method='native', attempts=[{
                'state': 'native_queued', 'mode': 'follow-up', 'message_id': msg['id'],
                'native_session_id': target_conv, 'launch_id': launch_id, 'at': now(),
            }])
            write(agent_dir / 'runtime.json', {
                **read(agent_dir / 'runtime.json'),
                'state': 'dormant',
                'tmux': {'socket': str(self.work / 'tmux.sock'), 'pane': '%1', 'session': '$1'},
            })
            checked = deliver_tool.run(self.root, agent=agent_id, check=True)
            self.assertFalse(checked['ok'], case)
            captured = {}
            def fake_start(root, **kwargs):
                captured.update(kwargs)
                return {'runtime': {'launch_id': str(uuid4())}, 'notices': []}
            with patch.object(native, 'observe', return_value={'live': 'dormant'}), \
                    patch.object(lifecycle, 'start', side_effect=fake_start):
                retried = deliver_tool.run(self.root, agent=agent_id, retry=True)
            self.assertTrue(retried['ok'], (case, retried))
            self.assertEqual(captured.get('resume_message_id'), msg['id'], case)
            self.assertFalse(captured.get('_queue_wake', False), (case, captured))
            # Dismiss so the second subcase checks only its own message.
            update_message(agent_dir, msg['id'], dismissed_at=now())
        write(agent_dir / 'runtime.json', {**read(agent_dir / 'runtime.json'), 'state': 'running'})

        # accepted_message_evidence confirms present rows, reports False only on a readable transcript without the row,
        # and stays unknown when no transcript exists.
        old_session = replace(session, native_id=conv3, launch_id=launch_id)
        ev_present = adapter.accepted_message_evidence(old_session, ordered_ids[0], dup_text, t0, os.path.realpath(home))
        self.assertEqual((ev_present.state, ev_present.value['delivered']), ('ok', True))
        missing_id = str(uuid4())
        agy_bridge.write_receipt(agent_dir, missing_id, launch_id=launch_id, native_id=conv3, route='agentapi',
                                 submitted_at=now(), text_sha256=agy_storage.message_hash('never arrived'))
        ev_absent = adapter.accepted_message_evidence(old_session, missing_id, 'never arrived', now(), os.path.realpath(home))
        self.assertEqual((ev_absent.state, ev_absent.value['delivered']), ('ok', False))
        no_log_id = str(uuid4())
        agy_bridge.write_receipt(agent_dir, no_log_id, launch_id=launch_id, native_id=conv4, route='agentapi',
                                 submitted_at=now(), text_sha256=agy_storage.message_hash('no transcript yet'))
        ev_unknown = adapter.accepted_message_evidence(
            replace(session, native_id=conv4, launch_id=launch_id), no_log_id, 'no transcript yet', now(), os.path.realpath(home),
        )
        self.assertEqual(ev_unknown.state, 'unknown')

    def test_delivery_state_recovery_across_missed_stop_crashed_hook_stale_claim_kill_and_resume(self):
        """Missed Stop hooks, crashed hooks, stale agentapi_submitting claims, mid-turn kills and resumes on a new
        launch must never strand follow-ups: every queued follow-up is either delivered once or reported failed and
        retryable; fragile because sticky launch flags silently deadlock every subsequent follow-up."""
        server = _LoopbackListener()
        self.addCleanup(server.close)
        agent_id, launch1, launch2, conv_id = str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4())
        home = self._create_saved_conversation(conv_id)
        agent_dir = self.root / 'state' / 'agents' / agent_id
        for lid in (launch1, launch2):
            (agent_dir / 'launches' / lid).mkdir(parents=True)
        flush_log = self.work / 'class-fix-flushes.jsonl'
        fake_agy = self.work / 'fake-agy-class'
        fake_agy.write_text(
            f'#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n'
            f'with Path({str(flush_log)!r}).open("a") as f:\n'
            '    f.write(json.dumps({"conv": sys.argv[3], "text": sys.argv[4]}) + "\\n")\n'
            'print(json.dumps({"response": {"sendMessage": {"recipientId": sys.argv[3]}}}))\n'
        )
        fake_agy.chmod(0o755)
        launch_files.write_request(agent_dir, launch1, {'expected_native_id': None, 'argv': [str(fake_agy)]})
        write(agent_dir / 'properties.json', {
            'id': agent_id, 'name': 'resilient', 'harness': 'agy', 'workspace': str(self.work),
            'universe_id': read(self.root / 'universe.json')['id'],
            'native_session_provider': 'agy', 'native_session_id': conv_id,
            'native_session_home': os.path.realpath(home),
        })
        write(agent_dir / 'runtime.json', {
            'launch_id': launch1, 'state': 'running', 'status': 'working', 'started_at': now(),
            'control_endpoint': {'transport': 'unix-json', 'address': str(self.work / 'rpc.sock')},
            'native_mode': {'policy': 'auto', 'mode': 'bridge', 'fallback_reason': None},
            'native_bridge': BRIDGE,
        })
        agy_bridge.write_ls_info(agent_dir, launch1, server.address, 'tok-class', conv_id)
        env1 = {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': agent_id, 'MV2_LAUNCH_ID': launch1}
        adapter = AgyAdapter.configured('agy', {'executable': str(fake_agy)})
        qpath1 = agent_dir / 'launches' / launch1 / agy_bridge.FOLLOWUPS_FILE

        def _wait_state(lid: str, mid: str, expected: str, timeout: float = 5.0) -> None:
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if agy_bridge.followup_state(agent_dir, lid, mid) == expected:
                    return
                time.sleep(0.02)
            self.fail(f'{mid} stayed {agy_bridge.followup_state(agent_dir, lid, mid)!r} != {expected!r}')

        def _delivered_texts() -> list[str]:
            if not flush_log.exists():
                return []
            return [json.loads(line)['text'] for line in flush_log.read_text().splitlines() if line.strip()]

        # 1. Esc without Stop hook (agy 1.5.16 skips Stop on Esc during tool calls and text generation):
        #    PreInvocation sets turn_active; 1st late idle statusLine does NOT flush or pause mid-turn,
        #    and the 2nd consecutive idle statusLine pauses the queued follow-up with reason='cancel' and
        #    records runtime.json['queue_state']. Further idle statusLine renders / keystrokes never release it;
        #    a new terminal turn (PreInvocation -> Stop) resumes and delivers it once.
        agy_bridge.handle_hook('PreInvocation', {'conversationId': conv_id}, env=env1)
        m_no_stop = str(uuid4())
        agy_bridge.write_receipt(agent_dir, m_no_stop, launch_id=launch1, native_id=conv_id, route='stop-hook',
                                 submitted_at=now(), text_sha256=agy_storage.message_hash('esc without stop'))
        agy_bridge.enqueue_followup(agent_dir, launch1, m_no_stop, conv_id, 'esc without stop')
        agy_bridge.handle_statusline({'conversation_id': conv_id, 'agent_state': 'idle'}, env=env1)
        time.sleep(0.1)
        self.assertEqual(agy_bridge.followup_status(agent_dir, launch1, m_no_stop), ('queued', None))
        # 2nd idle statusLine confirms missing-Stop return to idle -> pause as cancel, never flush.
        for _ in range(4):
            agy_bridge.handle_statusline({'conversation_id': conv_id, 'agent_state': 'idle'}, env=env1)
        time.sleep(0.1)
        self.assertEqual(agy_bridge.followup_status(agent_dir, launch1, m_no_stop), ('queued', 'cancel'))
        self.assertEqual(_delivered_texts().count('esc without stop'), 0)
        qs_after_esc = read(agent_dir / 'runtime.json').get('queue_state')
        self.assertEqual(qs_after_esc, {
            'native_session_id': conv_id, 'launch_id': launch1,
            'state': 'paused', 'reason': 'cancel', 'paused_count': 1,
            'paused_message_ids': [m_no_stop],
        })
        # get_delivery reports paused after turn cancellation.
        live_after_esc = SessionHandle.from_records(read(agent_dir / 'properties.json'), read(agent_dir / 'runtime.json'), agent_dir)
        esc_check = adapter.get_delivery(live_after_esc, m_no_stop)
        self.assertEqual((esc_check.state, esc_check.value.get('paused'), esc_check.value.get('paused_reason')),
                         ('native_queued', True, 'cancel'))
        # A new prompt (PreInvocation) resumes the paused queue, and Stop delivers it once.
        agy_bridge.handle_hook('PreInvocation', {'conversationId': conv_id}, env=env1)
        self.assertIsNone(read(agent_dir / 'runtime.json').get('queue_state'))
        self.assertEqual(
            agy_bridge.handle_hook('Stop', {'conversationId': conv_id, 'fullyIdle': True}, env=env1),
            {'decision': 'continue', 'reason': 'esc without stop'},
        )
        agy_bridge.handle_hook('Stop', {'conversationId': conv_id, 'fullyIdle': True}, env=env1)

        # 1b. Single idle statusLine + elapsed grace (no further statusLine renders): a new follow-up arrival
        #     reaps turn_active via live runtime.json (pausing then resuming for the new idle send), queues behind
        #     the FIFO head, and wakes flush-idle for the head.
        agy_bridge.handle_hook('PreInvocation', {'conversationId': conv_id}, env=env1)
        m_silent_head, m_silent_tail = str(uuid4()), str(uuid4())
        agy_bridge.write_receipt(agent_dir, m_silent_head, launch_id=launch1, native_id=conv_id, route='stop-hook',
                                 submitted_at=now(), text_sha256=agy_storage.message_hash('silent head'))
        agy_bridge.enqueue_followup(agent_dir, launch1, m_silent_head, conv_id, 'silent head')
        agy_bridge.handle_statusline({'conversation_id': conv_id, 'agent_state': 'idle'}, env=env1)
        doc = read(qpath1)
        doc['turn_active_at'] = '2020-01-01T00:00:00+00:00'
        agy_bridge._write_private_json(qpath1, doc)
        write(agent_dir / 'runtime.json', {
            **read(agent_dir / 'runtime.json'),
            'status': 'idle',
            'status_changed_at': '2020-01-01T00:00:01+00:00',
            'observed_at': '2020-01-01T00:00:01+00:00',
        })
        live_session1 = SessionHandle.from_records(read(agent_dir / 'properties.json'), read(agent_dir / 'runtime.json'), agent_dir)
        tail_res = adapter.send(live_session1, Message(m_silent_tail, 'silent tail', delivery='follow-up'), Mock())
        self.assertEqual((tail_res.state, tail_res.value['route']), ('native_queued', 'stop-hook'))
        _wait_state(launch1, m_silent_head, 'handed_off')
        self.assertEqual(_delivered_texts().count('silent head'), 1)
        self.assertEqual(agy_bridge.followup_state(agent_dir, launch1, m_silent_tail), 'queued')
        # Normal Stop delivers the queued tail once.
        self.assertEqual(
            agy_bridge.handle_hook('Stop', {'conversationId': conv_id, 'fullyIdle': True}, env=env1),
            {'decision': 'continue', 'reason': 'silent tail'},
        )
        agy_bridge.handle_hook('Stop', {'conversationId': conv_id, 'fullyIdle': True}, env=env1)

        # 2. Subagent/background pause (Stop fullyIdle=False) + missing/crashed final Stop hook:
        #    idle statusLine renders while background work runs must NOT flush the paused follow-up; once the
        #    wakeup turn starts (PreInvocation) and its final Stop does not run, unbroken idle pauses it as
        #    cancel rather than auto-flushing, and a new idle follow-up resumes the FIFO head cleanly.
        agy_bridge.handle_hook('PreInvocation', {'conversationId': conv_id}, env=env1)
        m_crash_hook = str(uuid4())
        agy_bridge.write_receipt(agent_dir, m_crash_hook, launch_id=launch1, native_id=conv_id, route='stop-hook',
                                 submitted_at=now(), text_sha256=agy_storage.message_hash('after hook crash'))
        agy_bridge.enqueue_followup(agent_dir, launch1, m_crash_hook, conv_id, 'after hook crash')
        agy_bridge.handle_hook('Stop', {'conversationId': conv_id, 'fullyIdle': False}, env=env1)
        for _ in range(4):
            agy_bridge.handle_statusline({'conversation_id': conv_id, 'agent_state': 'idle'}, env=env1)
        time.sleep(0.1)
        self.assertEqual(agy_bridge.followup_status(agent_dir, launch1, m_crash_hook), ('queued', 'subagent'))
        self.assertEqual(_delivered_texts().count('after hook crash'), 0)
        # Background task finishes -> wakeup turn fires PreInvocation, then Stop crashes -> idle pauses as cancel.
        agy_bridge.handle_hook('PreInvocation', {'conversationId': conv_id}, env=env1)
        with patch.object(agy_bridge, 'handle_hook', side_effect=RuntimeError('hook crash')), \
                patch('sys.stdin') as fake_stdin:
            fake_stdin.read.return_value = json.dumps({'conversationId': conv_id, 'fullyIdle': True})
            self.assertEqual(agy_bridge.main(['hook', 'Stop']), 0)
        for _ in range(3):
            agy_bridge.handle_statusline({'conversation_id': conv_id, 'agent_state': 'idle'}, env=env1)
        time.sleep(0.1)
        self.assertEqual(agy_bridge.followup_status(agent_dir, launch1, m_crash_hook), ('queued', 'cancel'))
        self.assertEqual(_delivered_texts().count('after hook crash'), 0)
        # A new idle follow-up resumes the paused FIFO head first and stays queued behind it.
        m_resume_tail = str(uuid4())
        live_after_crash = SessionHandle.from_records(read(agent_dir / 'properties.json'), read(agent_dir / 'runtime.json'), agent_dir)
        adapter.send(live_after_crash, Message(m_resume_tail, 'resume after crash', delivery='follow-up'), Mock())
        _wait_state(launch1, m_crash_hook, 'handed_off')
        self.assertEqual(_delivered_texts().count('after hook crash'), 1)
        self.assertEqual(
            agy_bridge.handle_hook('Stop', {'conversationId': conv_id, 'fullyIdle': True}, env=env1),
            {'decision': 'continue', 'reason': 'resume after crash'},
        )
        agy_bridge.handle_hook('Stop', {'conversationId': conv_id, 'fullyIdle': True}, env=env1)

        # 3. Stale agentapi_submitting claim (crashed helper):
        #    (a) receipt still stop-hook -> reverted to queued and delivered once;
        #    (b) receipt already agentapi -> transitioned to handed_off (not resent) so a later follow-up flushes once.
        agy_bridge.handle_hook('PreInvocation', {'conversationId': conv_id}, env=env1)
        agy_bridge.handle_hook('Stop', {'conversationId': conv_id, 'fullyIdle': True}, env=env1)
        m_pre_crash = str(uuid4())
        agy_bridge.write_receipt(agent_dir, m_pre_crash, launch_id=launch1, native_id=conv_id, route='stop-hook',
                                 submitted_at=now(), text_sha256=agy_storage.message_hash('pre-io crash'))
        agy_bridge.enqueue_followup(agent_dir, launch1, m_pre_crash, conv_id, 'pre-io crash')
        doc = read(qpath1)
        for item in doc['entries']:
            if item['id'] == m_pre_crash:
                item['state'] = 'agentapi_submitting'
                item['claimed_at'] = '2020-01-01T00:00:00+00:00'
        agy_bridge._write_private_json(qpath1, doc)
        agy_bridge.handle_statusline({'conversation_id': conv_id, 'agent_state': 'idle'}, env=env1)
        _wait_state(launch1, m_pre_crash, 'handed_off')
        self.assertEqual(_delivered_texts().count('pre-io crash'), 1)

        agy_bridge.handle_statusline({'conversation_id': conv_id, 'agent_state': 'working'}, env=env1)
        m_mid_crash, m_behind = str(uuid4()), str(uuid4())
        agy_bridge.write_receipt(agent_dir, m_mid_crash, launch_id=launch1, native_id=conv_id, route='agentapi',
                                 submitted_at=now(), text_sha256=agy_storage.message_hash('mid-io crash'),
                                 acknowledged=False)
        agy_bridge.enqueue_followup(agent_dir, launch1, m_mid_crash, conv_id, 'mid-io crash')
        agy_bridge.write_receipt(agent_dir, m_behind, launch_id=launch1, native_id=conv_id, route='stop-hook',
                                 submitted_at=now(), text_sha256=agy_storage.message_hash('unblocked behind'))
        agy_bridge.enqueue_followup(agent_dir, launch1, m_behind, conv_id, 'unblocked behind')
        doc = read(qpath1)
        for item in doc['entries']:
            if item['id'] == m_mid_crash:
                item['state'] = 'agentapi_submitting'
                item['claimed_at'] = '2020-01-01T00:00:00+00:00'
        agy_bridge._write_private_json(qpath1, doc)
        agy_bridge.handle_statusline({'conversation_id': conv_id, 'agent_state': 'idle'}, env=env1)
        _wait_state(launch1, m_behind, 'handed_off')
        self.assertEqual(agy_bridge.followup_state(agent_dir, launch1, m_mid_crash), 'handed_off')
        self.assertEqual(_delivered_texts().count('mid-io crash'), 0)
        self.assertEqual(_delivered_texts().count('unblocked behind'), 1)

        # 4. Agent killed mid-turn (with both queued and pre-IO agentapi_submitting entries): both report failed & retryable.
        m_kill_q, m_kill_sub = str(uuid4()), str(uuid4())
        for mid, body in ((m_kill_q, 'killed queued'), (m_kill_sub, 'killed claiming')):
            agy_bridge.write_receipt(agent_dir, mid, launch_id=launch1, native_id=conv_id, route='stop-hook',
                                     submitted_at=now(), text_sha256=agy_storage.message_hash(body))
            agy_bridge.enqueue_followup(agent_dir, launch1, mid, conv_id, body)
        doc = read(qpath1)
        doc['turn_active'] = True
        doc['turn_active_at'] = now()
        for item in doc['entries']:
            if item['id'] == m_kill_sub:
                item['state'] = 'agentapi_submitting'
                item['claimed_at'] = now()
        agy_bridge._write_private_json(qpath1, doc)
        write(agent_dir / 'runtime.json', {**read(agent_dir / 'runtime.json'), 'state': 'stopped'})
        stopped_session = SessionHandle.from_records(read(agent_dir / 'properties.json'), read(agent_dir / 'runtime.json'), agent_dir)
        for mid in (m_kill_q, m_kill_sub):
            res = adapter.get_delivery(stopped_session, mid)
            self.assertEqual(res.state, 'error', mid)
            self.assertTrue(res.value.get('definite_failure'), mid)

        # 5. Resume on a new launch: old launch's sticky turn_active/submitting never blocks the new launch,
        #    and a follow-up on the resumed launch delivers immediately via agentapi while old-launch items stay retryable.
        launch_files.write_request(agent_dir, launch2, {'expected_native_id': conv_id, 'argv': [str(fake_agy)]})
        write(agent_dir / 'runtime.json', {
            'launch_id': launch2, 'state': 'running', 'status': 'idle', 'started_at': now(),
            'control_endpoint': {'transport': 'unix-json', 'address': str(self.work / 'rpc.sock')},
            'native_mode': {'policy': 'auto', 'mode': 'bridge', 'fallback_reason': None},
            'native_bridge': BRIDGE,
        })
        agy_bridge.write_ls_info(agent_dir, launch2, server.address, 'tok-class-2', conv_id)
        resumed_session = SessionHandle.from_records(read(agent_dir / 'properties.json'), read(agent_dir / 'runtime.json'), agent_dir)
        send_exec = Mock()
        send_exec.command.return_value = subprocess.CompletedProcess(
            [str(fake_agy)], 0, json.dumps({'response': {'sendMessage': {'recipientId': conv_id}}}), '',
        )
        m_resumed = str(uuid4())
        sent_resumed = adapter.send(resumed_session, Message(m_resumed, 'resumed follow-up', delivery='follow-up'), send_exec)
        self.assertEqual(sent_resumed.state, 'native_queued')
        self.assertEqual(sent_resumed.value['route'], 'agentapi')
        send_exec.command.assert_called_once()
        # Old-launch undelivered entries remain definite_failure (retryable) even while launch2 is running.
        for mid in (m_kill_q, m_kill_sub):
            res = adapter.get_delivery(resumed_session, mid)
            self.assertEqual(res.state, 'error', mid)
            self.assertTrue(res.value.get('definite_failure'), mid)

        # 6a. Stalled claimer CAS protection — BEFORE reaper (< 20 s): claim_token still matches, so
        #     commit_followup_agentapi_receipt commits under lock and submits once via agentapi.
        env2 = {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': agent_id, 'MV2_LAUNCH_ID': launch2}
        # Finish the idle turn that Step 5 started so launch2's idle_flushed flag is cleared.
        agy_bridge.handle_hook('Stop', {'conversationId': conv_id, 'fullyIdle': True}, env=env2)
        qpath2 = agent_dir / 'launches' / launch2 / agy_bridge.FOLLOWUPS_FILE
        m_cas_before = str(uuid4())
        before_calls = {'n': 0}

        def _brief_stall_before_reap():
            before_calls['n'] += 1
            if before_calls['n'] == 2:
                # Reap runs while claim is still fresh (< 20 s): must leave claim intact.
                agy_bridge.should_queue_idle_followup(agent_dir, launch2, conv_id)

        before_exec = Mock()
        before_exec.check_session.side_effect = _brief_stall_before_reap
        before_exec.command.return_value = subprocess.CompletedProcess(
            [str(fake_agy)], 0, json.dumps({'response': {'sendMessage': {'recipientId': conv_id}}}), '',
        )
        with patch.object(agy_bridge, 'should_queue_idle_followup', return_value=True):
            before_res = adapter.send(resumed_session, Message(m_cas_before, 'cas before reap', delivery='follow-up'), before_exec)
        self.assertEqual((before_res.state, before_res.value['route']), ('native_queued', 'agentapi'))
        self.assertEqual(before_exec.command.call_count, 1)
        agy_bridge.handle_hook('Stop', {'conversationId': conv_id, 'fullyIdle': True}, env=env2)

        # 6b. Stalled claimer CAS protection — AFTER reaper (> 20 s): if send() claims the idle head and stalls
        #     past 20 s before commit_followup_agentapi_receipt, so the reaper reverts it and Stop hands it off,
        #     send() backs out cleanly without calling agentapi send-message (never double-delivered).
        m_cas = str(uuid4())
        stop_cas = []
        cas_calls = {'n': 0}

        def _stall_during_check():
            cas_calls['n'] += 1
            if cas_calls['n'] == 2:
                doc2 = read(qpath2)
                for item in doc2['entries']:
                    if item['id'] == m_cas:
                        item['claimed_at'] = '2020-01-01T00:00:00+00:00'
                agy_bridge._write_private_json(qpath2, doc2)
                stop_cas.append(agy_bridge.handle_hook('Stop', {'conversationId': conv_id, 'fullyIdle': True}, env=env2))

        cas_exec = Mock()
        cas_exec.check_session.side_effect = _stall_during_check
        with patch.object(agy_bridge, 'should_queue_idle_followup', return_value=True):
            cas_res = adapter.send(resumed_session, Message(m_cas, 'cas protected', delivery='follow-up'), cas_exec)
        self.assertEqual(stop_cas, [{'decision': 'continue', 'reason': 'cas protected'}])
        self.assertEqual(cas_exec.command.call_count, 0)
        self.assertEqual((cas_res.state, cas_res.value['route']), ('native_queued', 'stop-hook'))
        self.assertNotIn('queued', cas_res.value)
        agy_bridge.handle_hook('Stop', {'conversationId': conv_id, 'fullyIdle': True}, env=env2)

        # 7. Stop with terminationReason=ERROR pauses queued follow-ups, records runtime.json queue_state,
        #    reports paused=True + paused_reason='error' on deliver --check (never restarted by deliver --retry),
        #    resists idle statusLine renders, and resumes FIFO when a new mv2 follow-up arrives while idle.
        from multiverse.tools import deliver as deliver_tool
        from multiverse.universe.mailbox import append_message, update_message
        agy_bridge.handle_hook('PreInvocation', {'conversationId': conv_id}, env=env2)
        err_msg = append_message(agent_dir, 'after error', {'name': 'user'}, 'follow-up', target_session=conv_id)
        m_err = err_msg['id']
        agy_bridge.write_receipt(agent_dir, m_err, launch_id=launch2, native_id=conv_id, route='stop-hook',
                                 submitted_at=now(), text_sha256=agy_storage.message_hash('after error'))
        agy_bridge.enqueue_followup(agent_dir, launch2, m_err, conv_id, 'after error')
        update_message(agent_dir, m_err, submission_state='accepted', delivery_method='native', attempts=[{
            'state': 'native_queued', 'mode': 'follow-up', 'message_id': m_err,
            'native_session_id': conv_id, 'launch_id': launch2, 'at': now(),
        }])
        self.assertEqual(
            agy_bridge.handle_hook(
                'Stop', {'conversationId': conv_id, 'fullyIdle': True, 'terminationReason': 'ERROR'}, env=env2,
            ),
            {},
        )
        qs_err = read(agent_dir / 'runtime.json').get('queue_state')
        self.assertEqual(qs_err, {
            'native_session_id': conv_id, 'launch_id': launch2,
            'state': 'paused', 'reason': 'error', 'paused_count': 1,
            'paused_message_ids': [m_err],
        })
        # Historical handed-off follow-ups (like m_cas earlier on launch2) must never appear in paused_message_ids.
        self.assertNotIn(m_cas, qs_err['paused_message_ids'])
        err_deliv = adapter.get_delivery(resumed_session, m_err)
        self.assertEqual(err_deliv.state, 'native_queued')
        self.assertTrue(err_deliv.value.get('paused'))
        self.assertEqual(err_deliv.value.get('paused_reason'), 'error')
        # deliver --retry does NOT restart an accepted paused follow-up.
        with patch.object(native, 'send_or_wake', side_effect=AssertionError('retry must not restart paused queue')):
            retried_paused = deliver_tool.run(self.root, agent=agent_id, retry=True)
        self.assertEqual(retried_paused['messages'], [])
        for _ in range(4):
            agy_bridge.handle_statusline({'conversation_id': conv_id, 'agent_state': 'idle'}, env=env2)
        self.assertEqual(agy_bridge.followup_state(agent_dir, launch2, m_err), 'queued')
        self.assertEqual(_delivered_texts().count('after error'), 0)
        # A new mv2 follow-up on an idle agent unpauses the ERROR-paused queue, clears queue_state,
        # flushes the paused FIFO head first via agentapi, and leaves the new follow-up queued behind it.
        m_err_new = str(uuid4())
        idle_session2 = SessionHandle.from_records(read(agent_dir / 'properties.json'), read(agent_dir / 'runtime.json'), agent_dir)
        new_fu_res = adapter.send(idle_session2, Message(m_err_new, 'resume via followup', delivery='follow-up'), Mock())
        self.assertEqual((new_fu_res.state, new_fu_res.value['route']), ('native_queued', 'stop-hook'))
        self.assertTrue(new_fu_res.value.get('queued'))
        self.assertIsNone(read(agent_dir / 'runtime.json').get('queue_state'))
        _wait_state(launch2, m_err, 'handed_off')
        self.assertEqual(_delivered_texts().count('after error'), 1)
        self.assertEqual(agy_bridge.followup_state(agent_dir, launch2, m_err_new), 'queued')
        # Normal Stop delivers the new tail follow-up next in FIFO order.
        self.assertEqual(
            agy_bridge.handle_hook('Stop', {'conversationId': conv_id, 'fullyIdle': True}, env=env2),
            {'decision': 'continue', 'reason': 'resume via followup'},
        )
        agy_bridge.handle_hook('Stop', {'conversationId': conv_id, 'fullyIdle': True}, env=env2)

        # 8. USER_CANCELED pause stamps queue_state with reason='cancel', clears on terminal PreInvocation,
        #    and a stale paused queue_state copied onto a new launch clears at SessionStart.
        agy_bridge.handle_hook('PreInvocation', {'conversationId': conv_id}, env=env2)
        m_cancel = str(uuid4())
        agy_bridge.write_receipt(agent_dir, m_cancel, launch_id=launch2, native_id=conv_id, route='stop-hook',
                                 submitted_at=now(), text_sha256=agy_storage.message_hash('after cancel'))
        agy_bridge.enqueue_followup(agent_dir, launch2, m_cancel, conv_id, 'after cancel')
        self.assertEqual(
            agy_bridge.handle_hook(
                'Stop', {'conversationId': conv_id, 'fullyIdle': True, 'terminationReason': 'USER_CANCELED'}, env=env2,
            ),
            {},
        )
        self.assertEqual(read(agent_dir / 'runtime.json').get('queue_state'), {
            'native_session_id': conv_id, 'launch_id': launch2,
            'state': 'paused', 'reason': 'cancel', 'paused_count': 1,
            'paused_message_ids': [m_cancel],
        })
        # Real send -> old handed-off receipt + new paused send: only the currently paused mv2 message ID is projected.
        self.assertNotIn(m_err, read(agent_dir / 'runtime.json')['queue_state']['paused_message_ids'])
        self.assertNotIn(m_err_new, read(agent_dir / 'runtime.json')['queue_state']['paused_message_ids'])
        cancel_deliv = adapter.get_delivery(idle_session2, m_cancel)
        self.assertEqual((cancel_deliv.value.get('paused'), cancel_deliv.value.get('paused_reason')), (True, 'cancel'))
        agy_bridge.handle_hook('PreInvocation', {'conversationId': conv_id}, env=env2)
        self.assertIsNone(read(agent_dir / 'runtime.json').get('queue_state'))
        self.assertEqual(
            agy_bridge.handle_hook('Stop', {'conversationId': conv_id, 'fullyIdle': True}, env=env2),
            {'decision': 'continue', 'reason': 'after cancel'},
        )
        # New launch SessionStart clears stale queue_state carried over from an earlier launch.
        launch3 = str(uuid4())
        (agent_dir / 'launches' / launch3).mkdir(parents=True)
        write(agent_dir / 'runtime.json', {
            **read(agent_dir / 'runtime.json'),
            'launch_id': launch3,
            'queue_state': {'native_session_id': conv_id, 'launch_id': launch2, 'state': 'paused', 'reason': 'error', 'paused_count': 1},
        })
        env3 = {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': agent_id, 'MV2_LAUNCH_ID': launch3}
        agy_bridge.handle_hook('SessionStart', {'conversationId': conv_id}, env=env3)
        self.assertIsNone(read(agent_dir / 'runtime.json').get('queue_state'))
        # Stale same-launch callback for an old conversation cannot clear or overwrite the newly bound pause.
        stale_old_conv = str(uuid4())
        live_pause = {
            'native_session_id': conv_id, 'launch_id': launch3,
            'state': 'paused', 'reason': 'error', 'paused_count': 1,
        }
        for stale_entries in ([], [{'native_id': stale_old_conv, 'paused': True, 'paused_reason': 'cancel'}]):
            write(agent_dir / 'runtime.json', {**read(agent_dir / 'runtime.json'), 'queue_state': live_pause})
            agy_bridge._sync_runtime_queue_state_locked(agent_dir, launch3, stale_old_conv, stale_entries)
            self.assertEqual(read(agent_dir / 'runtime.json').get('queue_state'), live_pause)

    def test_model_catalog_launch_effort_and_display_names(self):
        """Antigravity's offline model table, --effort argv placement, suffixed transcript splitting and Silicon23
        short display names keep the dashboard from showing raw IDs like gemini-4-neon-xhigh and reject invalid
        or conflicting efforts before launching."""
        from multiverse import display
        from multiverse.runtime import model_choices
        from multiverse.universe import templates

        adapter = AgyAdapter.configured('agy', {})
        self.assertTrue(adapter.supports_launch_effort)
        catalog = adapter.models(environment={})
        self.assertEqual(catalog.source, 'table')
        self.assertTrue(catalog.free_form)
        by_id = {item.id: item for item in catalog.models}
        self.assertEqual(by_id['gemini-4-neon'].name, 'Gemini 4 Neon')
        self.assertEqual(by_id['gemini-4-neon'].efforts, ('low', 'medium', 'high', 'xhigh'))
        self.assertEqual(by_id['gemini-3.8-flash'].name, 'Gemini 3.8 Flash')
        self.assertEqual(by_id['gemini-3.8-flash'].efforts, ('low', 'medium', 'high'))
        self.assertEqual(by_id['gemini-pro-orchestrator-neon'].efforts, ())

        # Base IDs validate efforts; suffixed IDs stay usable unrestricted without --effort but reject conflicts.
        self.assertTrue(adapter.check_model('gemini-4-neon', effort='xhigh', environment={}).ok)
        self.assertTrue(adapter.check_model('gemini-4-neon', effort='xhigh', environment={}).verified)
        self.assertFalse(adapter.check_model('gemini-4-neon', effort='max', environment={}).ok)
        self.assertFalse(adapter.check_model('gemini-pro-orchestrator-neon', effort='low', environment={}).ok)
        suffixed_ok = adapter.check_model('gemini-4-neon-xhigh', environment={})
        self.assertTrue(suffixed_ok.ok)
        self.assertFalse(suffixed_ok.verified)
        self.assertEqual(suffixed_ok.effort, 'xhigh')
        self.assertFalse(adapter.check_model('gemini-4-neon-xhigh', effort='low', environment={}).ok)
        self.assertFalse(adapter.check_model('gemini-4-neon-xhigh', effort='xhigh', environment={}).ok)
        unknown = adapter.check_model('gemini-future-preview', effort='high', environment={})
        self.assertTrue(unknown.ok)
        self.assertFalse(unknown.verified)

        # Spawn and resume place single-token --effort=LEVEL before --prompt-interactive=... in both bridge and plain modes,
        # so an unchecked effort starting with '-' on an unlisted model can never be parsed as another CLI flag.
        conv_resume = str(uuid4())
        home_resume = self._create_saved_conversation(conv_resume)
        resume_session = SessionHandle('a1', 'agy', str(self.work), 'l1', conv_resume, home=os.path.realpath(home_resume))
        for bridge_flag in (True, False):
            configured = AgyAdapter.configured('agy', {'inbox_bridge': bridge_flag})
            values = self._values(task='do task')
            cmd = configured.spawn(LaunchContext('spawn', values, 'gemini-4-neon', {'HOME': str(self.work)}, effort='xhigh'))
            self.assertEqual(cmd.argv[-1], '--prompt-interactive=do task')
            idx = cmd.argv.index('--effort=xhigh')
            self.assertLess(idx, len(cmd.argv) - 1)
            rcmd = configured.resume(
                resume_session,
                LaunchContext('resume', self._values(task='wake now'), 'gemini-4-neon', {'HOME': str(self.work)},
                              resume_prompt='wake now', effort='high'),
            )
            self.assertEqual(rcmd.argv[-1], '--prompt-interactive=wake now')
            ridx = rcmd.argv.index('--effort=high')
            self.assertLess(ridx, len(rcmd.argv) - 1)
        dash_cmd = AgyAdapter.configured('agy', {}).spawn(
            LaunchContext('spawn', self._values(task='hi'), 'gemini-future-preview', {'HOME': str(self.work)}, effort='-weird'),
        )
        self.assertIn('--effort=-weird', dash_cmd.argv)
        self.assertNotIn('-weird', dash_cmd.argv)
        # 1.5.16 catalog version and additions are present.
        self.assertEqual(catalog.version, '1.5.16')
        self.assertIn('gemini-4-carbon', by_id)
        self.assertIn('gemini-pro-orchestrator', by_id)
        self.assertIn('gemini-pro-orchestrator-argon', by_id)

        # Running transcript with suffixed model splits into (model, effort) so base shows "Gemini 4 Neon (xhigh)"
        # and Silicon23 shows "Neon (xhigh)".
        for template_name, expected_label in (('base', 'Gemini 4 Neon (xhigh)'), ('Silicon23', 'Neon (xhigh)')):
            with self.subTest(template=template_name):
                scope = templates.create(self.work / f'tpl-{template_name}', template_name, template_name, self.work)
                conv_id = str(uuid4())
                home = self._create_saved_conversation(conv_id)
                transcript = home / 'brain' / conv_id / '.system_generated' / 'logs' / 'transcript.jsonl'
                transcript.parent.mkdir(parents=True, exist_ok=True)
                transcript.write_text(
                    json.dumps({'type': 'PLANNER_RESPONSE', 'model': 'gemini-4-neon-xhigh',
                                'input_tokens': 100, 'output_tokens': 20, 'created_at': now()}) + '\n'
                )
                measured, _ = agy_storage.measured_usage(conv_id, home)
                self.assertEqual((measured.model, measured.effort), ('gemini-4-neon', 'xhigh'))
                row = {
                    'harness': 'agy', 'live': 'alive',
                    'model': 'gemini-4-neon-xhigh', 'effort': None,
                    'model_name': model_choices.model_name(scope, {'harness': 'agy'}, 'gemini-4-neon-xhigh', {}),
                    'observed_model_name': model_choices.model_name(scope, {'harness': 'agy'}, measured.model, {}),
                    'usage': {'state': 'ok', 'value': {'model': measured.model, 'effort': measured.effort}},
                }
                self.assertEqual(display.model_label(row), expected_label)

    def test_queued_prompts_and_live_pane_display(self):
        """Antigravity's Stop-hook queue feeds core's queued-prompts send guard without exposing text, failing closed
        to unknown coverage on damaged/wrong-launch/in-flight files, and display_pane strips only the composer."""
        from multiverse.runtime import queued_prompts
        from multiverse.tools import send as send_tool
        from multiverse.universe.mailbox import append_message, update_message

        agent_id, launch_id, conv1, conv2 = str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4())
        peer_id = str(uuid4())
        agent_dir = self.root / 'state' / 'agents' / agent_id
        peer_dir = self.root / 'state' / 'agents' / peer_id
        launch_dir = agent_dir / 'launches' / launch_id
        launch_dir.mkdir(parents=True)
        peer_dir.mkdir(parents=True)
        home = self._create_saved_conversation(conv1)
        uni_id = read(self.root / 'universe.json')['id']
        write(agent_dir / 'properties.json', {
            'id': agent_id, 'name': 'worker', 'harness': 'agy', 'workspace': str(self.work),
            'universe_id': uni_id, 'native_session_provider': 'agy',
            'native_session_id': conv1, 'native_session_home': os.path.realpath(home),
        })
        write(agent_dir / 'runtime.json', {
            'launch_id': launch_id, 'state': 'running', 'status': 'working',
            'control_endpoint': {'transport': 'unix-json', 'address': str(self.work / 'rpc.sock')},
            'native_mode': {'policy': 'auto', 'mode': 'bridge', 'fallback_reason': None},
            'native_bridge': BRIDGE,
        })
        write(peer_dir / 'properties.json', {
            'id': peer_id, 'name': 'peer', 'harness': 'agy', 'workspace': str(self.work),
            'universe_id': uni_id, 'native_session_provider': 'agy',
        })
        write(peer_dir / 'runtime.json', {'launch_id': str(uuid4()), 'state': 'dormant', 'status': 'unknown'})

        adapter = AgyAdapter.configured('agy', {})
        session = SessionHandle.from_records(read(agent_dir / 'properties.json'), read(agent_dir / 'runtime.json'), agent_dir)

        # Unbound launch (before native_bridge=BRIDGE) or plain launch reports unavailable; once hooks bind
        # native_bridge=BRIDGE, queued_prompts works even before agy-ls.json is written.
        unbound_session = SessionHandle.from_records(
            read(agent_dir / 'properties.json'),
            {**read(agent_dir / 'runtime.json'), 'native_bridge': None},
            agent_dir,
        )
        self.assertEqual(adapter.queued_prompts(unbound_session).state, 'unavailable')
        self.assertEqual(AgyAdapter.configured('agy', {'inbox_bridge': False}).queued_prompts(session).state, 'unavailable')
        self.assertEqual(adapter.queued_prompts(session).state, 'ok')

        agy_bridge.write_ls_info(agent_dir, launch_id, '127.0.0.1:4242', 'tok-q', conv1)
        self.assertEqual(adapter.capabilities(session)['queued'].state, 'supported')

        # Missing queue file -> complete with empty entries.
        empty = adapter.queued_prompts(session)
        self.assertEqual(empty.state, 'ok')
        self.assertEqual((empty.value['coverage'], empty.value['entries']), ('complete', []))

        # Enqueue a follow-up from peer, verify queued_prompts lists its ID without text and blocks peer reply without --force.
        msg = append_message(agent_dir, 'secret queued body', {'agent_id': peer_id, 'name': 'peer', 'universe': str(self.root)}, 'follow-up', target_session=conv1)
        update_message(agent_dir, msg['id'], submission_state='accepted', attempts=[{
            'launch_id': launch_id, 'native_session_id': conv1, 'message_id': msg['id'],
            'mode': 'follow-up', 'state': 'native_queued', 'value': {'route': 'stop-hook'},
        }])
        agy_bridge.enqueue_followup(agent_dir, launch_id, msg['id'], conv1, 'secret queued body')
        # Another conversation's entry (pre-/clear) is ignored.
        agy_bridge.enqueue_followup(agent_dir, launch_id, str(uuid4()), conv2, 'other conv')

        listed = adapter.queued_prompts(session)
        self.assertEqual(listed.state, 'ok')
        self.assertEqual(listed.value['coverage'], 'complete')
        self.assertEqual([e['message_id'] for e in listed.value['entries']], [msg['id']])
        self.assertNotIn('secret queued body', json.dumps(listed.value))

        with patch.object(native.NativeExecutor, 'check_session', return_value=None):
            report, senders, _ = queued_prompts.for_send(self.root, agent_id)
        self.assertEqual((report.state, report.coverage), ('ok', 'complete'))
        self.assertEqual([e.message_id for e in report.entries], [msg['id']])
        self.assertEqual(senders, frozenset({peer_id}))

        # In-flight agentapi_submitting downgrades coverage to unknown; damaged/wrong-schema/wrong-launch fail closed.
        qpath = launch_dir / agy_bridge.FOLLOWUPS_FILE
        raw = json.loads(qpath.read_text())
        raw['entries'][0]['state'] = 'agentapi_submitting'
        agy_bridge._write_private_json(qpath, raw)
        self.assertEqual(adapter.queued_prompts(session).value['coverage'], 'unknown')

        for label, bad in (
            ('damaged json', '{"schema": 1, "entries": ['),
            ('bool schema', json.dumps({'schema': True, 'launch_id': launch_id, 'entries': []})),
            ('other launch', json.dumps({'schema': 1, 'launch_id': str(uuid4()), 'entries': []})),
        ):
            with self.subTest(label):
                qpath.write_text(bad)
                qpath.chmod(0o600)
                res = adapter.queued_prompts(session)
                self.assertEqual(res.state, 'ok')
                self.assertEqual((res.value['coverage'], res.value['entries']), ('unknown', []))


    def test_transcript_reading_compaction_and_turn_projection(self):
        """Antigravity's TranscriptProvider and TurnEventProvider expose real transcript_full.jsonl/transcript.jsonl
        rows, labels, CHECKPOINT compaction context boundaries, rewrite detection and turn projection through core's
        public transcript reader without touching live processes or leaking symlinks."""
        from time import monotonic
        from multiverse.harnesses.types import TurnEnded, TurnJoined, TurnStarted
        from multiverse.runtime import transcripts as core_transcripts

        agent_id, launch_id, conv1, conv2 = str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4())
        agent_dir = self.root / 'state' / 'agents' / agent_id
        agent_dir.mkdir(parents=True)
        home = self._create_saved_conversation(conv1)
        real_home = os.path.realpath(home)
        uni_id = read(self.root / 'universe.json')['id']
        write(agent_dir / 'properties.json', {
            'id': agent_id, 'name': 'reader', 'harness': 'agy', 'workspace': str(self.work),
            'universe_id': uni_id, 'native_session_provider': 'agy',
            'native_session_id': conv1, 'native_session_home': real_home,
        })
        write(agent_dir / 'runtime.json', {'launch_id': launch_id, 'state': 'running', 'status': 'idle'})

        # Before any transcript exists, info reports unavailable, view marks retry=True for event wait, readable=False.
        adapter = AgyAdapter.configured('agy', {})
        session = SessionHandle.from_records(read(agent_dir / 'properties.json'), read(agent_dir / 'runtime.json'), agent_dir)
        missing_info = core_transcripts.info(self.root, agent_dir)
        self.assertEqual(missing_info.state, 'unavailable')
        self.assertTrue(adapter.transcript_view(session).source.retry)
        self.assertFalse(adapter.transcript_readable(session))

        logs = home / 'brain' / conv1 / '.system_generated' / 'logs'
        logs.mkdir(parents=True, exist_ok=True)
        full_path = logs / 'transcript_full.jsonl'
        compact_path = logs / 'transcript.jsonl'
        rows_data = [
            {'step_index': 0, 'source': 'USER_EXPLICIT', 'type': 'USER_INPUT', 'status': 'DONE',
             'created_at': '2026-11-22T12:00:00Z', 'content': '<USER_REQUEST>\nFirst task\n</USER_REQUEST>'},
            {'step_index': 1, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'status': 'DONE',
             'created_at': '2026-11-22T12:00:01Z', 'model': 'gemini-4-neon-medium',
             'tool_calls': [{'name': 'run_command', 'args': {}}]},
            {'step_index': 2, 'source': 'MODEL', 'type': 'GENERIC', 'status': 'DONE',
             'created_at': '2026-11-22T12:00:02Z', 'content': 'command output'},
            {'step_index': 3, 'source': 'SYSTEM', 'type': 'SYSTEM_MESSAGE', 'status': 'DONE',
             'created_at': '2026-11-22T12:00:03Z',
             'content': '<SYSTEM_MESSAGE>\n[Message] sender=system content=Message from lead:\nmid-turn steer\n</SYSTEM_MESSAGE>'},
            {'step_index': 4, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'status': 'DONE',
             'created_at': '2026-11-22T12:00:04Z', 'model': 'gemini-4-neon-medium', 'content': 'First answer'},
            {'step_index': 5, 'source': 'SYSTEM', 'type': 'CHECKPOINT', 'status': 'DONE',
             'created_at': '2026-11-22T12:01:00Z', 'content': '# Resuming from a compaction\nSummary body'},
            {'step_index': 6, 'source': 'SYSTEM', 'type': 'SYSTEM_MESSAGE', 'status': 'DONE',
             'created_at': '2026-11-22T12:01:05Z',
             'content': '<SYSTEM_MESSAGE>\nStop hook blocked termination: Message from peer:\nfollow-up turn\n</SYSTEM_MESSAGE>'},
            {'step_index': 7, 'source': 'SYSTEM', 'type': 'SYSTEM_MESSAGE', 'status': 'DONE',
             'created_at': '2026-11-22T12:01:06Z', 'content': 'Reminder: work efficiently'},
            {'step_index': 8, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'status': 'DONE',
             'created_at': '2026-11-22T12:01:08Z', 'model': 'gemini-4-neon-medium', 'content': 'Second answer'},
        ]
        # Include one damaged JSONL line and one unfinished line (no trailing newline).
        full_path.write_text(
            ''.join(json.dumps(item) + '\n' for item in rows_data)
            + '{damaged json line\n'
            + '{"step_index": 99, "type": "USER_INPUT"'
        )
        # Compact sibling has fewer rows; transcript_full.jsonl must take precedence.
        compact_path.write_text(json.dumps(rows_data[0]) + '\n')

        info = core_transcripts.info(self.root, agent_dir)
        self.assertEqual(info.state, 'supported')
        self.assertEqual(info.format, 'agy-jsonl')
        self.assertEqual(info.rows, 10)  # 9 valid + 1 damaged; unfinished trailing line ignored
        self.assertEqual(info.turns, 2)
        self.assertEqual(info.context_rows, 5)  # CHECKPOINT (idx 5) through damaged row (idx 9)

        page = core_transcripts.rows(self.root, agent_dir, bodies=True)
        self.assertEqual(
            [row.label for row in page.rows],
            ['prompt', 'response', 'tool_result', 'mid_turn', 'response', 'compaction', 'prompt', 'injected', 'response', 'meta'],
        )
        self.assertEqual(page.rows[0].source, 'human')
        self.assertEqual(page.rows[3].source, 'plugin')
        self.assertTrue(page.rows[4].ends_turn)
        self.assertFalse(page.rows[4].in_context)
        self.assertTrue(page.rows[5].in_context)
        self.assertEqual(page.rows[6].source, 'plugin')
        self.assertEqual(page.rows[0].data['step_index'], 0)
        self.assertIsNone(page.rows[9].data)

        turn_page = core_transcripts.turns(self.root, agent_dir)
        self.assertEqual(len(turn_page.turns), 2)
        self.assertEqual(turn_page.turns[0].id, 'step-0')
        self.assertTrue(turn_page.turns[0].finished)
        self.assertIn('First task', turn_page.turns[0].preview)
        self.assertEqual(turn_page.turns[1].id, 'step-6')
        self.assertTrue(turn_page.turns[1].finished)

        # TurnEventProvider projects refs within the same view cleanly.
        edges = (
            TurnStarted(turn='step-0', ref='step-0', kind='prompt', source='person'),
            TurnJoined(turn='step-0', ref='step-3', source='harness'),
            TurnEnded(turn='step-0', ref='step-4', kind='prompt', state='finished'),
        )
        projected = core_transcripts.turn_references(
            self.root, agent_dir, launch_id, conv1, 0, edges, deadline=monotonic() + 5,
        )
        self.assertEqual(projected[0].turn, 'step-0')
        self.assertEqual(projected[0].ref, page.rows[0].cursor)
        self.assertEqual(projected[1].ref, page.rows[3].cursor)
        self.assertEqual(projected[2].ref, page.rows[4].cursor)

        # Tip slicing stops history at the chosen step and adjusts context_from; readable=True on regular transcript.
        self.assertTrue(adapter.transcript_readable(session))
        tipped = core_transcripts.info(self.root, agent_dir, tip='step-4')
        self.assertEqual((tipped.tip, tipped.turns, tipped.context_rows), ('step-4', 1, 5))

        # Rewriting the file invalidates earlier cursors cleanly with Changed('rewritten').
        rewritten_row = {**rows_data[0], 'content': '<USER_REQUEST>\nChanged\n</USER_REQUEST>'}
        full_path.write_text(json.dumps(rewritten_row) + '\n')
        with self.assertRaises(core_transcripts.Changed) as changed:
            core_transcripts.rows(self.root, agent_dir, after=page.rows[4].cursor, bodies=False)
        self.assertEqual(changed.exception.reason, 'rewritten')

        # Falling back to compact transcript.jsonl when transcript_full.jsonl is absent works cleanly.
        full_path.unlink()
        compact_info = core_transcripts.info(self.root, agent_dir)
        self.assertEqual((compact_info.state, compact_info.rows), ('supported', 1))

        # Background-task notifications (sender=<conv>/task-N) use source='task-notification', Antigravity's
        # MESSAGE_PRIORITY_LOW server-restart notice and other [Message] senders stay injected, and only
        # sender=system with high/unset priority confirms route='agentapi' delivery.
        full_path.unlink(missing_ok=True)
        task_text = 'Task task-213 completed'
        other_text = 'Subagent note'
        restart_text = (
            '[Notice] All your subagents and background tasks have been stopped due to server restart. '
            'If resuming work, please check on status and restart as needed.'
        )
        sys_text = 'Message from lead:\nactual agentapi delivery'
        env_rows = [
            {'step_index': 0, 'source': 'USER_EXPLICIT', 'type': 'USER_INPUT', 'status': 'DONE',
             'created_at': '2026-11-24T10:00:00Z', 'content': '<USER_REQUEST>\nstart\n</USER_REQUEST>'},
            {'step_index': 1, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'status': 'DONE',
             'created_at': '2026-11-24T10:00:01Z', 'content': 'done'},
            {'step_index': 2, 'source': 'SYSTEM', 'type': 'SYSTEM_MESSAGE', 'status': 'DONE',
             'created_at': '2026-11-24T10:00:02Z',
             'content': f'[Message] timestamp=2026-11-24T10:00:02Z sender={conv1}/task-213 content={task_text}'},
            {'step_index': 3, 'source': 'SYSTEM', 'type': 'SYSTEM_MESSAGE', 'status': 'DONE',
             'created_at': '2026-11-24T10:00:03Z',
             'content': f'[Message] timestamp=2026-11-24T10:00:03Z sender=other-agent content={other_text}'},
            {'step_index': 4, 'source': 'SYSTEM', 'type': 'SYSTEM_MESSAGE', 'status': 'DONE',
             'created_at': '2026-11-24T10:00:04Z',
             'content': (
                 'The following is a <SYSTEM_MESSAGE>...\n\n<SYSTEM_MESSAGE>\n'
                 f'[Message] timestamp=2026-11-24T10:00:04Z sender=system priority=MESSAGE_PRIORITY_LOW content={restart_text}\n'
                 '</SYSTEM_MESSAGE>'
             )},
            {'step_index': 5, 'source': 'SYSTEM', 'type': 'SYSTEM_MESSAGE', 'status': 'DONE',
             'created_at': '2026-11-24T10:00:05Z',
             'content': f'[Message] timestamp=2026-11-24T10:00:05Z sender=system priority=MESSAGE_PRIORITY_HIGH content={sys_text}'},
        ]
        full_path.write_text(''.join(json.dumps(item) + '\n' for item in env_rows))
        env_view = adapter.transcript_view(session)
        self.assertEqual(
            [(row.label, row.source) for row in env_view.rows[2:]],
            [('prompt', 'task-notification'), ('injected', None), ('injected', None), ('mid_turn', 'plugin')],
        )
        self.assertIs(agy_storage.transcript_has_message(
            conv1, home, text_sha256=agy_storage.message_hash(task_text), route='agentapi',
            since='2026-11-24T09:59:59Z',
        ), False)
        self.assertIs(agy_storage.transcript_has_message(
            conv1, home, text_sha256=agy_storage.message_hash(other_text), route='agentapi',
            since='2026-11-24T09:59:59Z',
        ), False)
        self.assertIs(agy_storage.transcript_has_message(
            conv1, home, text_sha256=agy_storage.message_hash(restart_text), route='agentapi',
            since='2026-11-24T09:59:59Z',
        ), False)
        self.assertIs(agy_storage.transcript_has_message(
            conv1, home, text_sha256=agy_storage.message_hash(sys_text), route='agentapi',
            since='2026-11-24T09:59:59Z',
        ), True)
        full_path.unlink()

        # Symlinked transcript fails closed with error (and readable=None) without falling back to transcript.jsonl.
        outside = self.work / 'outside.jsonl'
        outside.write_text(json.dumps(rows_data[0]) + '\n')
        full_path.symlink_to(outside)
        err_info = core_transcripts.info(self.root, agent_dir)
        self.assertEqual(err_info.state, 'error')
        self.assertIsNone(adapter.transcript_readable(session))

    def test_turn_events_from_hooks_and_esc_cancel(self):
        """Antigravity PreInvocation, Stop (finished, continue, error) and Esc-without-Stop idle transitions must
        publish ordered, transcript-resolved turn.started and turn.ended events, including wake turns followed by
        Antigravity's low-priority server-restart notice, because a broken emission or projection leaves plugins
        blind to live Antigravity turns."""
        from multiverse.plugins import events as plugin_events
        from multiverse.runtime import prompt_events, transcripts as core_transcripts
        from multiverse.universe.mailbox import append_message, update_message

        agent_id, launch_id, conv1 = str(uuid4()), str(uuid4()), str(uuid4())
        agent_dir = self.root / 'state' / 'agents' / agent_id
        launch_dir = agent_dir / 'launches' / launch_id
        launch_dir.mkdir(parents=True)
        home = self._create_saved_conversation(conv1)
        real_home = os.path.realpath(home)
        uni_id = read(self.root / 'universe.json')['id']
        write(agent_dir / 'properties.json', {
            'id': agent_id, 'name': 'turns', 'harness': 'agy', 'workspace': str(self.work),
            'universe_id': uni_id, 'native_session_provider': 'agy',
            'native_session_id': conv1, 'native_session_home': real_home,
        })
        write(agent_dir / 'runtime.json', {
            'launch_id': launch_id, 'state': 'running', 'status': 'idle', 'session_generation': 1,
            'native_bridge': BRIDGE,
        })
        launch_files.write_request(agent_dir, launch_id, {
            'argv': ['agy', '--prompt-interactive=first task'],
            'expected_native_id': None, 'task_launch': True,
        })
        logs = home / 'brain' / conv1 / '.system_generated' / 'logs'
        logs.mkdir(parents=True, exist_ok=True)
        full_path = logs / 'transcript_full.jsonl'
        env = {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': agent_id, 'MV2_LAUNCH_ID': launch_id}
        journal_path = agent_dir / 'prompts' / 'journal' / 'events.json'

        def append_rows(*rows):
            with full_path.open('a') as handle:
                for row in rows:
                    handle.write(json.dumps(row) + '\n')

        drained = []
        with patch.object(plugin_events, 'subscribers', return_value=['fixture']), \
                patch.object(prompt_events, '_journal_event', return_value='retry') as journal_mock:

            def drain_all():
                batch = []
                def capture(_directory, row):
                    batch.append((row['kind'], row['data']))
                    return 'unsubscribed'
                journal_mock.side_effect = capture
                try:
                    for _ in range(16):
                        if not journal_path.exists() or not read(journal_path).get('pending'):
                            break
                        prompt_events.publish(agent_dir, resolve_turns=True)
                finally:
                    journal_mock.side_effect = None
                    journal_mock.return_value = 'retry'
                drained.extend(batch)
                return batch

            # 1. Initial task turn: PreInvocation(0, initialNumSteps=1) -> tool loop PreInvocation(1, 3) -> Stop(continue).
            append_rows(
                {'step_index': 0, 'source': 'USER_EXPLICIT', 'type': 'USER_INPUT', 'status': 'DONE',
                 'created_at': '2026-11-24T10:00:00Z', 'content': '<USER_REQUEST>\nfirst task\n</USER_REQUEST>'},
            )
            agy_bridge.handle_hook('PreInvocation', {
                'conversationId': conv1, 'invocationNum': 0, 'initialNumSteps': 1,
            }, env=env)
            append_rows(
                {'step_index': 1, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'status': 'DONE',
                 'created_at': '2026-11-24T10:00:01Z', 'tool_calls': [{'name': 'run_command', 'args': {}}]},
                {'step_index': 2, 'source': 'MODEL', 'type': 'GENERIC', 'status': 'DONE',
                 'created_at': '2026-11-24T10:00:02Z', 'content': 'ok'},
            )
            # Mid-turn tool-loop PreInvocation must not emit a duplicate turn.started.
            agy_bridge.handle_hook('PreInvocation', {
                'conversationId': conv1, 'invocationNum': 1, 'initialNumSteps': 3,
            }, env=env)
            append_rows(
                {'step_index': 3, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'status': 'DONE',
                 'created_at': '2026-11-24T10:00:03Z', 'content': 'first done'},
            )
            # Enqueue a Stop-hook follow-up with a recorded mailbox attempt so turn 2 attributes source='mv2' + message ID.
            fu_msg = append_message(agent_dir, 'follow-up task', {'name': 'lead'}, 'follow-up', target_session=conv1)
            fu_id = fu_msg['id']
            update_message(agent_dir, fu_id, submission_state='accepted', delivery_method='native', attempts=[{
                'state': 'native_queued', 'mode': 'follow-up', 'message_id': fu_id,
                'native_session_id': conv1, 'launch_id': launch_id, 'at': now(),
            }])
            agy_bridge.write_receipt(agent_dir, fu_id, launch_id=launch_id, native_id=conv1, route='stop-hook',
                                     submitted_at=now(), text_sha256=agy_storage.message_hash('follow-up task'))
            agy_bridge.enqueue_followup(agent_dir, launch_id, fu_id, conv1, 'follow-up task')
            stop1 = agy_bridge.handle_hook('Stop', {'conversationId': conv1, 'fullyIdle': True}, env=env)
            self.assertEqual(stop1, {'decision': 'continue', 'reason': 'follow-up task'})
            b1 = drain_all()
            self.assertEqual([k for k, _ in b1], ['turn.started', 'agent.status', 'turn.ended'])
            self.assertTrue(b1[0][1]['resolved'])
            self.assertTrue(b1[0][1]['first_task'])
            self.assertEqual((b1[0][1]['source'], b1[0][1]['message']), ('mv2', None))
            self.assertEqual(b1[1][1], {'from': 'idle', 'to': 'working'})
            self.assertTrue(b1[2][1]['resolved'])
            self.assertEqual(b1[2][1]['state'], 'finished')
            self.assertEqual(b1[2][1]['turn'], b1[0][1]['turn'])

            # 2. Stop-hook continuation turn (invocationNum keeps incrementing to 2) ends on ERROR.
            append_rows(
                {'step_index': 4, 'source': 'SYSTEM', 'type': 'SYSTEM_MESSAGE', 'status': 'DONE',
                 'created_at': '2026-11-24T10:00:04Z',
                 'content': '<SYSTEM_MESSAGE>\nStop hook blocked termination: follow-up task\n</SYSTEM_MESSAGE>'},
            )
            agy_bridge.handle_hook('PreInvocation', {
                'conversationId': conv1, 'invocationNum': 2, 'initialNumSteps': 5,
            }, env=env)
            agy_bridge.handle_hook('Stop', {
                'conversationId': conv1, 'fullyIdle': True, 'terminationReason': 'ERROR',
            }, env=env)
            b2 = drain_all()
            self.assertEqual([k for k, _ in b2], ['turn.started', 'turn.ended'])
            self.assertTrue(b2[0][1]['resolved'])
            self.assertFalse(b2[0][1]['first_task'])
            self.assertEqual((b2[0][1]['source'], b2[0][1]['message']), ('mv2', fu_id))
            self.assertTrue(b2[1][1]['resolved'])
            self.assertEqual(b2[1][1]['state'], 'error')
            self.assertEqual(b2[1][1]['turn'], b2[0][1]['turn'])

            # 3. Wake-style turn where Antigravity appends a MESSAGE_PRIORITY_LOW restart notice right after
            #    USER_INPUT before PreInvocation (initialNumSteps lands on the notice step), cancelled by Esc
            #    (2 consecutive idle statusLine renders with no Stop hook).
            append_rows(
                {'step_index': 5, 'source': 'USER_EXPLICIT', 'type': 'USER_INPUT', 'status': 'DONE',
                 'created_at': '2026-11-24T10:01:00Z', 'content': '<USER_REQUEST>\nwake prompt\n</USER_REQUEST>'},
                {'step_index': 6, 'source': 'SYSTEM', 'type': 'SYSTEM_MESSAGE', 'status': 'DONE',
                 'created_at': '2026-11-24T10:01:00Z',
                 'content': (
                     '<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-24T10:01:00Z sender=system '
                     'priority=MESSAGE_PRIORITY_LOW content=[Notice] All your subagents and background tasks '
                     'have been stopped due to server restart.\n</SYSTEM_MESSAGE>'
                 )},
            )
            agy_bridge.handle_hook('PreInvocation', {
                'conversationId': conv1, 'invocationNum': 0, 'initialNumSteps': 7,
            }, env=env)
            agy_bridge.handle_statusline({'conversation_id': conv1, 'agent_state': 'idle'}, env=env)
            agy_bridge.handle_statusline({'conversation_id': conv1, 'agent_state': 'idle'}, env=env)
            b3 = drain_all()
            self.assertEqual([k for k, _ in b3], ['turn.started', 'agent.status', 'turn.ended'])
            rows_after = core_transcripts.rows(self.root, agent_dir, bodies=False).rows
            self.assertTrue(b3[0][1]['resolved'])
            self.assertEqual(b3[0][1]['source'], 'person')
            self.assertEqual(b3[0][1]['turn'], 'step-5')
            self.assertEqual(b3[0][1]['ref'], rows_after[5].cursor)
            self.assertTrue(b3[2][1]['resolved'])
            self.assertEqual(b3[2][1]['state'], 'interrupted')
            self.assertEqual(b3[2][1]['turn'], 'step-5')
            self.assertEqual(b3[2][1]['ref'], rows_after[6].cursor)
            self.assertNotEqual(b3[0][1]['turn'], b2[0][1]['turn'])

            # 4. Post-logged idle agentapi turn after a completed/cancelled response (PreInvocation fires BEFORE
            #    Antigravity appends step-8, and statusLine(working) beats PreInvocation): must still correlate
            #    message ID and resolve to step-8.
            append_rows(
                {'step_index': 7, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'status': 'CANCELLED',
                 'created_at': '2026-11-24T10:01:05Z', 'content': ''},
            )
            kite_msg = append_message(agent_dir, 'KITE', {'name': 'lead'}, 'follow-up', target_session=conv1)
            kite_id = kite_msg['id']
            update_message(agent_dir, kite_id, submission_state='accepted', delivery_method='native', attempts=[{
                'state': 'native_submitted', 'mode': 'follow-up', 'message_id': kite_id,
                'native_session_id': conv1, 'launch_id': launch_id, 'at': now(),
            }])
            agy_bridge.begin_idle_turn(agent_dir, launch_id, message_id=kite_id)
            agy_bridge.handle_statusline({'conversation_id': conv1, 'agent_state': 'working'}, env=env)
            agy_bridge.handle_hook('PreInvocation', {
                'conversationId': conv1, 'invocationNum': 0, 'initialNumSteps': 8,
            }, env=env)
            append_rows(
                {'step_index': 8, 'source': 'SYSTEM', 'type': 'SYSTEM_MESSAGE', 'status': 'DONE',
                 'created_at': '2026-11-24T10:02:00Z',
                 'content': '<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-24T10:02:00Z sender=system priority=MESSAGE_PRIORITY_HIGH content=KITE\n</SYSTEM_MESSAGE>'},
                {'step_index': 9, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'status': 'DONE',
                 'created_at': '2026-11-24T10:02:01Z', 'content': 'KITE'},
            )
            agy_bridge.handle_hook('Stop', {'conversationId': conv1, 'fullyIdle': False}, env=env)
            b4 = drain_all()
            b4_turns = [(k, d) for k, d in b4 if k.startswith('turn.')]
            self.assertEqual([k for k, _ in b4_turns], ['turn.started', 'turn.ended'])
            self.assertTrue(b4_turns[0][1]['resolved'])
            self.assertEqual(b4_turns[0][1]['turn'], 'step-8')
            self.assertEqual((b4_turns[0][1]['source'], b4_turns[0][1]['message']), ('mv2', kite_id))
            self.assertTrue(b4_turns[1][1]['resolved'])
            self.assertEqual(b4_turns[1][1]['turn'], 'step-8')

            # 5. Post-logged task-notification turn right after KITE (step-9 is PLANNER_RESPONSE):
            #    PreInvocation(0, initialNumSteps=10) fires before step-10 is appended; must resolve to step-10,
            #    never walking back across PLANNER_RESPONSE step-9 into previous turn step-8 (F6b regression).
            agy_bridge.handle_hook('PreInvocation', {
                'conversationId': conv1, 'invocationNum': 0, 'initialNumSteps': 10,
            }, env=env)
            append_rows(
                {'step_index': 10, 'source': 'SYSTEM', 'type': 'SYSTEM_MESSAGE', 'status': 'DONE',
                 'created_at': '2026-11-24T10:02:10Z',
                 'content': f'<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-24T10:02:10Z sender={conv1}/task-1 priority=MESSAGE_PRIORITY_HIGH content=done\n</SYSTEM_MESSAGE>'},
                {'step_index': 11, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'status': 'DONE',
                 'created_at': '2026-11-24T10:02:11Z', 'tool_calls': [{'name': 'run_command', 'args': {}}]},
            )
            # Cancelled tool call omits step-12; idle statusLine ends turn step-10 as interrupted.
            agy_bridge.handle_statusline({'conversation_id': conv1, 'agent_state': 'idle'}, env=env)
            agy_bridge.handle_statusline({'conversation_id': conv1, 'agent_state': 'idle'}, env=env)
            b5 = drain_all()
            b5_turns = [(k, d) for k, d in b5 if k.startswith('turn.')]
            self.assertEqual([k for k, _ in b5_turns], ['turn.started', 'turn.ended'])
            self.assertTrue(b5_turns[0][1]['resolved'])
            self.assertEqual(b5_turns[0][1]['turn'], 'step-10')
            self.assertEqual(b5_turns[0][1]['source'], 'harness')
            self.assertTrue(b5_turns[1][1]['resolved'])
            self.assertEqual(b5_turns[1][1]['turn'], 'step-10')

            # 6. Post-logged agentapi turn G1 after an Esc-cancelled tool call (step-12 omitted -> step-13):
            #    step-index gap closes prior open tool turn so step-13 classifies as prompt and resolves cleanly.
            g1_msg = append_message(agent_dir, 'G1', {'name': 'lead'}, 'follow-up', target_session=conv1)
            g1_id = g1_msg['id']
            update_message(agent_dir, g1_id, submission_state='accepted', delivery_method='native', attempts=[{
                'state': 'native_submitted', 'mode': 'follow-up', 'message_id': g1_id,
                'native_session_id': conv1, 'launch_id': launch_id, 'at': now(),
            }])
            agy_bridge.begin_idle_turn(agent_dir, launch_id, message_id=g1_id)
            agy_bridge.handle_hook('PreInvocation', {
                'conversationId': conv1, 'invocationNum': 0, 'initialNumSteps': 13,
            }, env=env)
            append_rows(
                {'step_index': 13, 'source': 'SYSTEM', 'type': 'SYSTEM_MESSAGE', 'status': 'DONE',
                 'created_at': '2026-11-24T10:03:00Z',
                 'content': '<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-24T10:03:00Z sender=system priority=MESSAGE_PRIORITY_HIGH content=G1\n</SYSTEM_MESSAGE>'},
            )
            agy_bridge.handle_statusline({'conversation_id': conv1, 'agent_state': 'idle'}, env=env)
            agy_bridge.handle_statusline({'conversation_id': conv1, 'agent_state': 'idle'}, env=env)
            b6 = drain_all()
            b6_turns = [(k, d) for k, d in b6 if k.startswith('turn.')]
            self.assertEqual([k for k, _ in b6_turns], ['turn.started', 'turn.ended'])
            self.assertEqual(b6_turns[0][1]['turn'], 'step-13')
            self.assertEqual((b6_turns[0][1]['source'], b6_turns[0][1]['message']), ('mv2', g1_id))
            self.assertEqual((b6_turns[1][1]['turn'], b6_turns[1][1]['state']), ('step-13', 'interrupted'))

            # 7. Direct project_turn_event on a PLANNER_RESPONSE step must never walk back into the preceding prompt.
            adapter = AgyAdapter.configured('agy', {})
            sess_obj = SessionHandle(agent_id, 'agy', str(self.work), launch_id=launch_id,
                                     native_id=conv1, home=real_home)
            view_now = adapter.transcript_view(sess_obj)
            from multiverse.harnesses.types import TurnStarted as _TurnStarted
            proj_resp = adapter.project_turn_event(
                sess_obj, view_now, _TurnStarted(turn='step-9', kind='prompt', ref='step-9', source='harness'),
            )
            self.assertIsNone(proj_resp.turn_position)

            # 8. Wake launch carrying resume_message_id attributes turn 1 as source='harness' (never 'person'),
            #    and a duplicate PreInvocation(0, same initialNumSteps) fired ~0.16 s later when Antigravity
            #    attaches its low-priority restart notice must not split the turn or emit a phantom person turn (F6c).
            launch2 = str(uuid4())
            (agent_dir / 'launches' / launch2).mkdir(parents=True)
            wake_msg = append_message(agent_dir, 'wake up', {'name': 'lead'}, 'follow-up', target_session=conv1)
            write(agent_dir / 'runtime.json', {
                'launch_id': launch2, 'state': 'running', 'status': 'idle', 'session_generation': 1,
                'native_bridge': BRIDGE,
            })
            launch_files.write_request(agent_dir, launch2, {
                'argv': ['agy', '--conversation', conv1, '--prompt-interactive=wake up'],
                'expected_native_id': conv1, 'task_launch': False, 'resume_message_id': wake_msg['id'],
            })
            append_rows(
                {'step_index': 14, 'source': 'USER_EXPLICIT', 'type': 'USER_INPUT', 'status': 'DONE',
                 'created_at': '2026-11-24T10:04:00Z', 'content': '<USER_REQUEST>\nwake up\n</USER_REQUEST>'},
            )
            env2 = {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': agent_id, 'MV2_LAUNCH_ID': launch2}
            agy_bridge.handle_hook('PreInvocation', {
                'conversationId': conv1, 'invocationNum': 0, 'initialNumSteps': 15,
            }, env=env2)
            # Duplicate PreInvocation(0, initialNumSteps=15) arrives as Antigravity logs the PRIORITY_LOW restart notice.
            append_rows(
                {'step_index': 15, 'source': 'SYSTEM', 'type': 'SYSTEM_MESSAGE', 'status': 'DONE',
                 'created_at': '2026-11-24T10:04:01Z',
                 'content': (
                     '<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-24T10:04:01Z sender=system '
                     'priority=MESSAGE_PRIORITY_LOW content=[Notice] All your subagents and background tasks '
                     'have been stopped due to server restart.\n</SYSTEM_MESSAGE>'
                 )},
            )
            agy_bridge.handle_hook('PreInvocation', {
                'conversationId': conv1, 'invocationNum': 0, 'initialNumSteps': 15,
            }, env=env2)
            append_rows(
                {'step_index': 16, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'status': 'DONE',
                 'created_at': '2026-11-24T10:04:02Z', 'content': 'AWAKE'},
            )
            agy_bridge.handle_hook('Stop', {'conversationId': conv1, 'fullyIdle': True}, env=env2)
            b8 = drain_all()
            b8_turns = [(k, d) for k, d in b8 if k.startswith('turn.')]
            self.assertEqual([k for k, _ in b8_turns], ['turn.started', 'turn.ended'])
            self.assertTrue(b8_turns[0][1]['resolved'])
            self.assertEqual(b8_turns[0][1]['turn'], 'step-14')
            self.assertEqual(b8_turns[0][1]['source'], 'harness')
            self.assertFalse(b8_turns[0][1]['first_task'])
            self.assertTrue(b8_turns[1][1]['resolved'])
            self.assertEqual(b8_turns[1][1]['turn'], 'step-14')
            self.assertEqual(b8_turns[1][1]['state'], 'finished')

            # 8b. Exact startup-input witness gate (F6a): an unrelated first prompt on a wake launch or spawn
            #     task launch, or a launch whose stored opening USER_INPUT cannot be proved, must stay unattributed
            #     (source='person', attempt=None, first_task=False); a proved wake opener emits attempt=wake_id
            #     with source='harness', first_task=False, and a second turn repeating that text is not attributed.
            launch_unrel = str(uuid4())
            (agent_dir / 'launches' / launch_unrel).mkdir(parents=True)
            unrel_wake = append_message(agent_dir, 'expected wake text', {'name': 'lead'}, 'follow-up', target_session=conv1)
            write(agent_dir / 'runtime.json', {
                'launch_id': launch_unrel, 'state': 'running', 'status': 'idle', 'session_generation': 1,
                'native_bridge': BRIDGE,
            })
            launch_files.write_request(agent_dir, launch_unrel, {
                'argv': ['agy', '--conversation', conv1, '--prompt-interactive=expected wake text'],
                'expected_native_id': conv1, 'task_launch': False, 'resume_message_id': unrel_wake['id'],
                'env': {'MV2_AGENT_ID': agent_id, 'MV2_LAUNCH_ID': launch_unrel},
            })
            append_rows(
                {'step_index': 17, 'source': 'USER_EXPLICIT', 'type': 'USER_INPUT', 'status': 'DONE',
                 'created_at': '2026-11-24T10:04:10Z', 'content': '<USER_REQUEST>\nunrelated typed prompt\n</USER_REQUEST>'},
                {'step_index': 18, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'status': 'DONE',
                 'created_at': '2026-11-24T10:04:11Z', 'content': 'ok'},
            )
            env_unrel = {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': agent_id, 'MV2_LAUNCH_ID': launch_unrel}
            unrel_edges = agy_bridge._on_turn_start(
                agent_dir, launch_unrel, conv1, invocation_num=0, initial_num_steps=18, env=env_unrel,
            )
            self.assertEqual(len(unrel_edges), 1)
            self.assertEqual((unrel_edges[0].source, unrel_edges[0].attempt, unrel_edges[0].first_task),
                             ('person', None, False))
            # Even if turn 2 later repeats the exact wake text, the one-shot startup gate does not attribute it.
            append_rows(
                {'step_index': 19, 'source': 'USER_EXPLICIT', 'type': 'USER_INPUT', 'status': 'DONE',
                 'created_at': '2026-11-24T10:04:12Z', 'content': '<USER_REQUEST>\nexpected wake text\n</USER_REQUEST>'},
            )
            agy_bridge.handle_hook('Stop', {'conversationId': conv1, 'fullyIdle': True}, env=env_unrel)
            turn2_edges = agy_bridge._on_turn_start(
                agent_dir, launch_unrel, conv1, invocation_num=0, initial_num_steps=20, env=env_unrel,
            )
            self.assertEqual(len(turn2_edges), 1)
            self.assertEqual((turn2_edges[0].source, turn2_edges[0].attempt, turn2_edges[0].first_task),
                             ('person', None, False))
            # Unrelated first prompt on a spawn --task launch must NOT mark first_task=True.
            conv_unrel_spawn = str(uuid4())
            launch_unrel_spawn = str(uuid4())
            (agent_dir / 'launches' / launch_unrel_spawn).mkdir(parents=True)
            spawn_logs = home / 'brain' / conv_unrel_spawn / '.system_generated' / 'logs'
            spawn_logs.mkdir(parents=True, exist_ok=True)
            (spawn_logs / 'transcript_full.jsonl').write_text(json.dumps({
                'step_index': 0, 'source': 'USER_EXPLICIT', 'type': 'USER_INPUT', 'status': 'DONE',
                'created_at': '2026-11-24T10:04:15Z', 'content': '<USER_REQUEST>\nsomething else\n</USER_REQUEST>',
            }) + '\n')
            launch_files.write_request(agent_dir, launch_unrel_spawn, {
                'argv': ['agy', '--prompt-interactive=expected spawn task'],
                'expected_native_id': None, 'task_launch': True, 'resume_message_id': None,
            })
            unrel_spawn_edges = agy_bridge._on_turn_start(
                agent_dir, launch_unrel_spawn, conv_unrel_spawn, invocation_num=0, initial_num_steps=1,
            )
            self.assertEqual(len(unrel_spawn_edges), 1)
            self.assertEqual((unrel_spawn_edges[0].source, unrel_spawn_edges[0].attempt, unrel_spawn_edges[0].first_task),
                             ('person', None, False))
            # Missing/unreadable transcript or missing initialNumSteps on a wake launch stays unattributed
            # with source='harness', attempt=None, first_task=False (never claiming the person typed it).
            conv_missing = str(uuid4())
            launch_missing = str(uuid4())
            (agent_dir / 'launches' / launch_missing).mkdir(parents=True)
            launch_files.write_request(agent_dir, launch_missing, {
                'argv': ['agy', '--conversation', conv_missing, '--prompt-interactive=expected wake text'],
                'expected_native_id': conv_missing, 'task_launch': False, 'resume_message_id': unrel_wake['id'],
            })
            missing_edges = agy_bridge._on_turn_start(
                agent_dir, launch_missing, conv_missing, invocation_num=0, initial_num_steps=1,
            )
            self.assertEqual(len(missing_edges), 1)
            self.assertEqual((missing_edges[0].source, missing_edges[0].attempt, missing_edges[0].first_task),
                             ('harness', None, False))
            launch_nosteps = str(uuid4())
            (agent_dir / 'launches' / launch_nosteps).mkdir(parents=True)
            launch_files.write_request(agent_dir, launch_nosteps, {
                'argv': ['agy', '--conversation', conv1, '--prompt-interactive=expected wake text'],
                'expected_native_id': conv1, 'task_launch': False, 'resume_message_id': unrel_wake['id'],
            })
            nosteps_edges = agy_bridge._on_turn_start(
                agent_dir, launch_nosteps, conv1, invocation_num=0, initial_num_steps=None,
            )
            self.assertEqual(len(nosteps_edges), 1)
            self.assertEqual((nosteps_edges[0].source, nosteps_edges[0].attempt, nosteps_edges[0].first_task),
                             ('harness', None, False))
            # Real wake opener WITH a trailing PRIORITY_LOW restart notice before PreInvocation proves step-20
            # and emits source='harness', attempt=real_wake['id'], first_task=False.
            launch_real_wake = str(uuid4())
            (agent_dir / 'launches' / launch_real_wake).mkdir(parents=True)
            real_wake = append_message(agent_dir, 'real wake opener', {'name': 'lead'}, 'follow-up', target_session=conv1)
            launch_files.write_request(agent_dir, launch_real_wake, {
                'argv': ['agy', '--conversation', conv1, '--prompt-interactive=real wake opener'],
                'expected_native_id': conv1, 'task_launch': False, 'resume_message_id': real_wake['id'],
                'env': {'MV2_AGENT_ID': agent_id, 'MV2_LAUNCH_ID': launch_real_wake},
            })
            append_rows(
                {'step_index': 20, 'source': 'USER_EXPLICIT', 'type': 'USER_INPUT', 'status': 'DONE',
                 'created_at': '2026-11-24T10:04:20Z',
                 'content': '<USER_REQUEST>\nreal wake opener\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\ntime\n</ADDITIONAL_METADATA>'},
                {'step_index': 21, 'source': 'SYSTEM', 'type': 'SYSTEM_MESSAGE', 'status': 'DONE',
                 'created_at': '2026-11-24T10:04:20Z',
                 'content': '<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-24T10:04:20Z sender=system priority=MESSAGE_PRIORITY_LOW content=[Notice] All your subagents and background tasks have been stopped due to server restart.\n</SYSTEM_MESSAGE>'},
            )
            real_wake_edges = agy_bridge._on_turn_start(
                agent_dir, launch_real_wake, conv1, invocation_num=0, initial_num_steps=22,
            )
            self.assertEqual(len(real_wake_edges), 1)
            self.assertEqual((real_wake_edges[0].source, real_wake_edges[0].attempt, real_wake_edges[0].first_task),
                             ('harness', real_wake['id'], False))
            # Close step-20's wake turn and restore runtime.json to launch2 before step 9.
            append_rows(
                {'step_index': 22, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'status': 'DONE',
                 'created_at': '2026-11-24T10:04:22Z', 'content': 'REAL_AWAKE'},
            )
            write(agent_dir / 'runtime.json', {
                'launch_id': launch2, 'state': 'running', 'status': 'idle', 'session_generation': 1,
                'native_bridge': BRIDGE,
            })

            # 9. Batched SYSTEM_MESSAGE rows delivered together with omitted odd steps between them (e.g. steps
            #    24, 26, 28 before one response at 29) must stay one turn: ['prompt', 'mid_turn', 'mid_turn'].
            append_rows(
                {'step_index': 24, 'source': 'SYSTEM', 'type': 'SYSTEM_MESSAGE', 'status': 'DONE',
                 'created_at': '2026-11-24T10:05:00Z',
                 'content': '<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-24T10:05:00Z sender=system priority=MESSAGE_PRIORITY_HIGH content=batch1\n</SYSTEM_MESSAGE>'},
                {'step_index': 26, 'source': 'SYSTEM', 'type': 'SYSTEM_MESSAGE', 'status': 'DONE',
                 'created_at': '2026-11-24T10:05:00Z',
                 'content': '<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-24T10:05:00Z sender=system priority=MESSAGE_PRIORITY_HIGH content=batch2\n</SYSTEM_MESSAGE>'},
                {'step_index': 28, 'source': 'SYSTEM', 'type': 'SYSTEM_MESSAGE', 'status': 'DONE',
                 'created_at': '2026-11-24T10:05:00Z',
                 'content': '<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-24T10:05:00Z sender=system priority=MESSAGE_PRIORITY_HIGH content=batch3\n</SYSTEM_MESSAGE>'},
                {'step_index': 29, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE', 'status': 'DONE',
                 'created_at': '2026-11-24T10:05:02Z', 'content': 'BATCH_DONE'},
            )
            batch_view = adapter.transcript_view(sess_obj)
            by_step = {r.ref: r.label for r in batch_view.rows}
            self.assertEqual(
                [by_step['step-24'], by_step['step-26'], by_step['step-28'], by_step['step-29']],
                ['prompt', 'mid_turn', 'mid_turn', 'response'],
            )

    def test_stop_hook_pins_launch_id_and_yields_to_idle_before_continuation_cap(self):
        """Retained .agents/hooks.json from an earlier resumed launch must never pop the current launch's
        follow-up queue, and Stop must yield to idle after MAX_STOP_CONTINUATIONS so Antigravity's 10-block
        cap cannot silently drop the next queued message."""
        agent_id = str(uuid4())
        old_launch = str(uuid4())
        cur_launch = str(uuid4())
        conv_id = str(uuid4())
        agent_dir = self.root / 'state' / 'agents' / agent_id
        (agent_dir / 'launches' / cur_launch).mkdir(parents=True)
        write(agent_dir / 'properties.json', {
            'id': agent_id, 'name': 'agy-stop-cap', 'harness': 'agy',
            'workspace': str(self.work), 'native_session_id': conv_id,
        })
        write(agent_dir / 'runtime.json', {
            'launch_id': cur_launch, 'state': 'running', 'status': 'working',
            'session_generation': 1, 'native_bridge': BRIDGE,
        })
        launch_files.write_request(agent_dir, cur_launch, {'expected_native_id': None})
        agy_bridge.write_ls_info(agent_dir, cur_launch, '127.0.0.1:4242', 'csrf-tok', conv_id)
        env = {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': agent_id, 'MV2_LAUNCH_ID': cur_launch}
        # Enqueue MAX_STOP_CONTINUATIONS + 2 follow-ups.
        total = agy_bridge.MAX_STOP_CONTINUATIONS + 2
        msg_ids = []
        for idx in range(total):
            mid = str(uuid4())
            msg_ids.append(mid)
            agy_bridge.write_receipt(agent_dir, mid, launch_id=cur_launch, native_id=conv_id,
                                     route='stop-hook', submitted_at=now(),
                                     text_sha256=agy_storage.message_hash(f'msg-{idx}'))
            agy_bridge.enqueue_followup(agent_dir, cur_launch, mid, conv_id, f'msg-{idx}')
        # 1. Stale prior launch's hook invocation (pinned --launch-id != MV2_LAUNCH_ID) is an instant no-op.
        self.assertEqual(
            agy_bridge.handle_hook('Stop', {'conversationId': conv_id, 'fullyIdle': True},
                                   env=env, expected_launch_id=old_launch),
            {},
        )
        self.assertEqual(agy_bridge.followup_status(agent_dir, cur_launch, msg_ids[0]), ('queued', None))
        # Also verify CLI main() parses --launch-id and ignores mismatched launch IDs.
        import io
        with patch.dict(os.environ, env, clear=False), \
                patch('sys.stdin', io.StringIO(json.dumps({'conversationId': conv_id, 'fullyIdle': True}))), \
                patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(agy_bridge.main(['hook', 'Stop', '--launch-id', old_launch]), 0)
            self.assertEqual(json.loads(out.getvalue()), {})
        self.assertEqual(agy_bridge.followup_status(agent_dir, cur_launch, msg_ids[0]), ('queued', None))
        # 2. First MAX_STOP_CONTINUATIONS stops continue via Stop hook in FIFO order.
        for idx in range(agy_bridge.MAX_STOP_CONTINUATIONS):
            reply = agy_bridge.handle_hook(
                'Stop', {'conversationId': conv_id, 'fullyIdle': True},
                env=env, expected_launch_id=cur_launch,
            )
            self.assertEqual(reply, {'decision': 'continue', 'reason': f'msg-{idx}'})
            self.assertEqual(agy_bridge.followup_status(agent_dir, cur_launch, msg_ids[idx]), ('handed_off', None))
        # 3. Next Stop hits the cap and yields `{}` without popping msg[MAX_STOP_CONTINUATIONS], resetting
        #    turn_active=False and stop_continue_streak=0 so idle statusLine can flush it via agentapi.
        yield_idx = agy_bridge.MAX_STOP_CONTINUATIONS
        self.assertEqual(
            agy_bridge.handle_hook('Stop', {'conversationId': conv_id, 'fullyIdle': True},
                                   env=env, expected_launch_id=cur_launch),
            {},
        )
        self.assertEqual(agy_bridge.followup_status(agent_dir, cur_launch, msg_ids[yield_idx]), ('queued', None))
        # Idle statusLine sees the queued message as immediately flushable.
        with patch.object(agy_bridge, '_spawn_idle_flush', return_value=True) as spawn_mock:
            agy_bridge.handle_statusline({'conversation_id': conv_id, 'agent_state': 'idle'}, env=env)
        spawn_mock.assert_called_once()
        # Once a fresh turn finishes normally after streak reset, Stop-hook continuations work again.
        reply_after = agy_bridge.handle_hook(
            'Stop', {'conversationId': conv_id, 'fullyIdle': True},
            env=env, expected_launch_id=cur_launch,
        )
        self.assertEqual(reply_after, {'decision': 'continue', 'reason': f'msg-{yield_idx}'})

    def test_agy_mouse_mode_on_task_and_wake_keeps_bridge_readiness(self):
        """Bridged agy --task and wake launches enable session-scoped tmux mouse via CommandSpec.mouse while keeping
        attachment=None so readiness stays held until AgyAdapter.attachment_ready() sees bridge registration; plain
        and promptless launches also request mouse."""
        fake_agy = self.work / 'agy-mouse-stub'
        fake_agy.write_text('#!/bin/sh\necho "agy 1.5.16"\n')
        fake_agy.chmod(0o755)
        adapter = AgyAdapter.configured('agy', {'executable': str(fake_agy)})
        plain_adapter = AgyAdapter.configured('agy', {'executable': str(fake_agy), 'inbox_bridge': False})
        values = self._values(task='')
        self.assertTrue(plain_adapter.spawn(LaunchContext('spawn', values, 'gemini-4-neon-medium')).mouse)
        promptless = adapter.spawn(LaunchContext('spawn', values, 'gemini-4-neon-medium'))
        self.assertTrue(promptless.mouse)
        self.assertEqual(promptless.attachment.readiness, 'process')

        backend = Mock()
        terminal = {'socket': str(self.work / 'fake-tmux.sock'), 'session': '$7', 'pane': '%7'}
        backend.create.return_value = backend.respawn.return_value = terminal
        with patch.object(lifecycle.harnesses, 'load', return_value=adapter), \
             patch.object(lifecycle, 'Tmux', return_value=backend), \
             patch.object(lifecycle, 'observe', return_value={'live': 'alive'}):
            # 1. Bridged --task spawn: mouse enabled on tmux session, readiness held before bridge handoff, released after.
            started = lifecycle.start(self.root, name='agy-task', task='run task', harness='agy')
            backend.run.assert_any_call('set-option', '-t', '$7', 'mouse', 'on')
            agent_dir = self.root / 'state' / 'agents' / started['id']
            launch_id = started['runtime']['launch_id']
            req = read(agent_dir / 'launches' / launch_id / 'request.json')
            self.assertTrue(req['mouse'])
            self.assertIsNone(req['attachment'])
            write(agent_dir / 'runtime.json', {
                **read(agent_dir / 'runtime.json'),
                'state': 'running', 'pid': 1001, 'startup_checked': True, 'startup_delivery': 'ready',
            })
            self.assertFalse(lifecycle.wait_ready(self.root, agent_dir, launch_id, adapter, .01)['ready'])
            conv_id = str(uuid4())
            self._create_saved_conversation(conv_id)
            write(agent_dir / 'properties.json', {**read(agent_dir / 'properties.json'), 'native_session_id': conv_id})
            write(agent_dir / 'runtime.json', {**read(agent_dir / 'runtime.json'), 'native_bridge': BRIDGE})
            self.assertTrue(lifecycle.wait_ready(self.root, agent_dir, launch_id, adapter, .01)['ready'])

            # 2. Bridged wake (resume with resume_prompt): mouse enabled, readiness held before bridge handoff, released after.
            backend.run.reset_mock()
            write(agent_dir / 'runtime.json', {**read(agent_dir / 'runtime.json'), 'state': 'dormant', 'native_bridge': None})
            with patch.object(lifecycle, 'observe', return_value={'live': 'dormant'}):
                woke = lifecycle.start(
                    self.root, resume=started['id'], harness='agy', resume_prompt='wake up',
                )
            backend.run.assert_any_call('set-option', '-t', '$7', 'mouse', 'on')
            wake_launch = woke['runtime']['launch_id']
            wake_req = read(agent_dir / 'launches' / wake_launch / 'request.json')
            self.assertTrue(wake_req['mouse'])
            self.assertIsNone(wake_req['attachment'])
            write(agent_dir / 'runtime.json', {
                **read(agent_dir / 'runtime.json'),
                'state': 'running', 'pid': 1002, 'startup_checked': True, 'startup_delivery': 'ready',
                'native_bridge': None,
            })
            self.assertFalse(lifecycle.wait_ready(self.root, agent_dir, wake_launch, adapter, .01)['ready'])
            write(agent_dir / 'runtime.json', {**read(agent_dir / 'runtime.json'), 'native_bridge': BRIDGE})
            self.assertTrue(lifecycle.wait_ready(self.root, agent_dir, wake_launch, adapter, .01)['ready'])


    def test_f2_f9_pre_ls_queue(self):
        """SessionStart/PreInvocation must unlock bridge readiness without agy-ls.json, and pre-agentapi busy and
        idle sends must queue visibly without failing or double-delivering."""
        fake_agy = self.work / 'agy-f235-stub'
        flush_log = self.work / 'f2-flush.jsonl'
        fake_agy.write_text(
            f'#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n'
            'if sys.argv[1:] == ["--version"]:\n    print("agy 1.5.16"); sys.exit(0)\n'
            'if sys.argv[1:3] == ["agentapi", "send-message"]:\n'
            f'    with Path({str(flush_log)!r}).open("a") as f: f.write(json.dumps({{"id": sys.argv[3], "text": sys.argv[4]}}) + "\\n")\n'
            '    print(json.dumps({"response": {"sendMessage": {"recipientId": sys.argv[3]}}})); sys.exit(0)\n'
        )
        fake_agy.chmod(0o755)
        adapter = AgyAdapter.configured('agy', {'executable': str(fake_agy)})

        # F2 & F9: SessionStart binds conversation and activates bridge readiness without agy-ls.json.
        agent_id, launch_id, conv_id = str(uuid4()), str(uuid4()), str(uuid4())
        agent_dir = self.root / 'state' / 'agents' / agent_id
        launch_dir = agent_dir / 'launches' / launch_id
        launch_dir.mkdir(parents=True)
        home = self._create_saved_conversation(conv_id)
        write(agent_dir / 'properties.json', {
            'id': agent_id, 'name': 'prels', 'harness': 'agy', 'workspace': str(self.work),
            'universe_id': read(self.root / 'universe.json')['id'],
            'native_session_provider': 'agy', 'native_session_id': None,
            'native_session_home': os.path.realpath(home),
        })
        write(agent_dir / 'runtime.json', {
            'launch_id': launch_id, 'state': 'running', 'status': 'unknown', 'pid': 4242,
            'startup_checked': True, 'startup_delivery': 'ready',
            'control_endpoint': {'transport': 'unix-json', 'address': str(self.work / 'rpc.sock')},
            'native_mode': {'policy': 'auto', 'mode': 'bridge', 'fallback_reason': None},
            'native_bridge': None,
        })
        launch_files.write_request(agent_dir, launch_id, {
            'expected_native_id': None, 'argv': [str(fake_agy), '--prompt-interactive=first'],
            'env': {}, 'unset_env': [], 'cleanup_files': [agy_bridge.LS_FILE], 'shell_callback': True,
        })
        env = {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': agent_id, 'MV2_LAUNCH_ID': launch_id}
        with patch.object(lifecycle, 'observe', return_value={'live': 'alive'}):
            self.assertFalse(lifecycle.wait_ready(self.root, agent_dir, launch_id, adapter, .01)['ready'])
            agy_bridge.handle_hook('SessionStart', {'conversationId': conv_id}, env=env)
            self.assertEqual(read(agent_dir / 'runtime.json')['native_bridge'], BRIDGE)
            self.assertTrue(lifecycle.wait_ready(self.root, agent_dir, launch_id, adapter, .01)['ready'])

        # Missing agy-ls.json before the first mv2 command is NOT a connection problem; corrupt agy-ls.json IS.
        sess = SessionHandle.from_records(read(agent_dir / 'properties.json'), read(agent_dir / 'runtime.json'), agent_dir)
        self.assertIsNone(adapter.connection_problem(sess))
        bad_ls = agy_bridge.ls_path(agent_dir, launch_id)
        bad_ls.write_text('{corrupt')
        bad_ls.chmod(0o600)
        self.assertIsNotNone(adapter.connection_problem(sess))
        bad_ls.unlink()

        # Busy follow-up BEFORE agy-ls.json queues for Stop hook and delivers once at Stop.
        agy_bridge.handle_hook('PreInvocation', {'conversationId': conv_id, 'invocationNum': 0, 'initialNumSteps': 1}, env=env)
        busy_sess = SessionHandle.from_records(read(agent_dir / 'properties.json'), read(agent_dir / 'runtime.json'), agent_dir)
        m_busy = str(uuid4())
        busy_exec = Mock()
        busy_res = adapter.send(busy_sess, Message(m_busy, 'busy before ls', delivery='follow-up'), busy_exec)
        self.assertEqual((busy_res.state, busy_res.value['route']), ('native_queued', 'stop-hook'))
        self.assertNotIn('idle_start_error', busy_res.value)
        busy_exec.command.assert_not_called()
        self.assertEqual(
            agy_bridge.handle_hook('Stop', {'conversationId': conv_id, 'fullyIdle': True}, env=env),
            {'decision': 'continue', 'reason': 'busy before ls'},
        )
        agy_bridge.handle_hook('Stop', {'conversationId': conv_id, 'fullyIdle': True}, env=env)

        # Idle send (steer or follow-up) BEFORE agy-ls.json queues visibly with idle_start_error=True (never fails),
        # delivers once via Stop when a new turn runs, and delivers once via agentapi once agy-ls.json connects.
        agy_bridge.handle_statusline({'conversation_id': conv_id, 'agent_state': 'idle'}, env=env)
        idle_sess = SessionHandle.from_records(read(agent_dir / 'properties.json'), read(agent_dir / 'runtime.json'), agent_dir)
        m_idle_stop = str(uuid4())
        res_idle_stop = adapter.send(idle_sess, Message(m_idle_stop, 'queued while idle 1', delivery='steer'), Mock())
        self.assertEqual(res_idle_stop.state, 'native_queued')
        self.assertTrue(res_idle_stop.value.get('idle_start_error'))
        self.assertIn(
            'prels has your message queued; its next turn has not started.',
            _messaging.delivery_line('prels', res_idle_stop.to_dict(), delivery='steer'),
        )
        # Idle statusLine before agy-ls.json does not fail or spawn flush-idle.
        agy_bridge.handle_statusline({'conversation_id': conv_id, 'agent_state': 'idle'}, env=env)
        self.assertEqual(agy_bridge.followup_state(agent_dir, launch_id, m_idle_stop), 'queued')
        # Next turn's Stop hook delivers it once (and a second Stop never resends it).
        agy_bridge.handle_hook('PreInvocation', {'conversationId': conv_id, 'invocationNum': 0, 'initialNumSteps': 3}, env=env)
        self.assertEqual(
            agy_bridge.handle_hook('Stop', {'conversationId': conv_id, 'fullyIdle': True}, env=env),
            {'decision': 'continue', 'reason': 'queued while idle 1'},
        )
        self.assertEqual(agy_bridge.handle_hook('Stop', {'conversationId': conv_id, 'fullyIdle': True}, env=env), {})
        agy_bridge.handle_statusline({'conversation_id': conv_id, 'agent_state': 'idle'}, env=env)

        # Second idle message before agy-ls.json flushes automatically once agy-ls.json connects, and never twice.
        m_idle_ls = str(uuid4())
        res_idle_ls = adapter.send(idle_sess, Message(m_idle_ls, 'queued until agentapi', delivery='follow-up'), Mock())
        self.assertTrue(res_idle_ls.value.get('idle_start_error'))
        self.assertEqual(adapter.get_delivery(idle_sess, m_idle_ls).value.get('queued'), True)
        server = _LoopbackListener()
        self.addCleanup(server.close)
        agy_bridge.write_ls_info(agent_dir, launch_id, server.address, 'tok-prels', conv_id)
        agy_bridge.handle_statusline({'conversation_id': conv_id, 'agent_state': 'idle'}, env=env)
        until(lambda: agy_bridge.followup_state(agent_dir, launch_id, m_idle_ls) == 'handed_off', 5)
        self.assertEqual(agy_bridge.handle_hook('Stop', {'conversationId': conv_id, 'fullyIdle': True}, env=env), {})
        delivered = [json.loads(line)['text'] for line in flush_log.read_text().splitlines() if line.strip()]
        self.assertEqual(delivered, ['queued until agentapi'])

class AgyTmuxLaunchTest(TerminalBase):
    def setUp(self):
        super().setUp()
        bad_python = forbidden_python(self.work)
        self.enterContext(patch.dict(os.environ, {
            'HOME': str(self.work),
            'PATH': str(bad_python) + os.pathsep + os.environ['PATH'],
        }))
        self.enterContext(patch.object(lifecycle, '_cli_path', return_value=mv2_launcher(self.work)))

    def test_tmux_bridge_shell_handoff_drains_startup_queue_and_cleans_ls_file(self):
        """A real tmux launch with a fake agy binary scrubs PARENT_AGY, writes 0600 launch_files, activates the bridge
        via mv2 report, flushes a held startup message through agentapi, and unlinks agy-ls.json on exit; fragile
        because runner StartupDelivery, shell callback handoff and exit cleanup span three separate processes."""
        server = _LoopbackListener()
        self.addCleanup(server.close)
        conv_id = str(uuid4())
        delivered_log = self.work / 'delivered.json'
        seen_log = self.work / 'seen.json'
        fake_agy = self.root / 'harnesses' / 'agy' / 'fake-agy'
        fake_agy.write_text(
            f"#!{sys.executable}\n"
            "import json, os, subprocess, sys, time\nfrom pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "if args == ['--version']:\n"
            "    print('agy 1.5.14', flush=True)\n"
            "    sys.exit(0)\n"
            "if args[:2] == ['agentapi', 'send-message']:\n"
            f"    Path({str(delivered_log)!r}).write_text(json.dumps({{'conv': args[2], 'text': args[3], 'addr': os.environ.get('ANTIGRAVITY_LS_ADDRESS'), 'has_tok': bool(os.environ.get('ANTIGRAVITY_CSRF_TOKEN'))}}))\n"
            "    print(json.dumps({'response': {'sendMessage': {'recipientId': args[2]}}}), flush=True)\n"
            "    sys.exit(0)\n"
            f"Path({str(seen_log)!r}).write_text(json.dumps({{'argv': args, 'leaked': [k for k in os.environ if k.startswith('ANTIGRAVITY_')]}}))\n"
            "time.sleep(0.4)\n"
            "child_env = {**os.environ, "
            f"'ANTIGRAVITY_LS_ADDRESS': {server.address!r}, "
            "'ANTIGRAVITY_CSRF_TOKEN': 'pane-csrf-999', "
            f"'ANTIGRAVITY_CONVERSATION_ID': {conv_id!r}}}\n"
            "subprocess.run([os.environ['MV2_CLI'], 'report', '--phase', 'working', '--summary', 'ready'], env=child_env, check=True)\n"
            "print('? for shortcuts', flush=True)\n"
            "while True: time.sleep(0.5)\n"
        )
        fake_agy.chmod(0o755)
        spec = read(self.root / 'harnesses' / 'agy' / 'harness.json')
        spec['settings']['executable'] = str(fake_agy)
        write(self.root / 'harnesses' / 'agy' / 'harness.json', spec)

        with patch.dict(os.environ, {'ANTIGRAVITY_CSRF_TOKEN': 'parent-secret', 'ANTIGRAVITY_CONVERSATION_ID': str(uuid4())}):
            started = lifecycle.start(self.root, 'agy-agent', 'do work', 'agy', socket_path=self.socket)
        agent_dir = self.root / 'state' / 'agents' / started['id']
        launch_id = started['runtime']['launch_id']
        launch_dir = agent_dir / 'launches' / launch_id

        # Queue a steer while startup_delivery is waiting (or immediately after).
        from multiverse.tools import send as send_tool
        send_tool.run(self.root, agent='agy-agent', text='held startup ping', delivery='steer')

        until(lambda: delivered_log.exists(), 15)
        # Session-scoped tmux mouse mode is enabled for the agy pane without changing global tmux options.
        session_mouse = subprocess.run(
            ['tmux', '-S', str(self.socket), 'show-options', '-v', '-t', started['runtime']['tmux']['session'], 'mouse'],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        global_mouse = subprocess.run(
            ['tmux', '-S', str(self.socket), 'show-options', '-gv', 'mouse'],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(session_mouse, 'on')
        self.assertNotEqual(global_mouse, 'on')
        seen = read(seen_log)
        self.assertEqual(seen['leaked'], [])
        self.assertEqual(stat.S_IMODE((launch_dir / 'AGENTS.md').stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((launch_dir / 'settings.json').stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((launch_dir / '.agents' / 'hooks.json').stat().st_mode), 0o600)
        ls_file = launch_dir / 'agy-ls.json'
        self.assertTrue(ls_file.exists())
        self.assertEqual(stat.S_IMODE(ls_file.stat().st_mode), 0o600)

        delivered = read(delivered_log)
        self.assertEqual(delivered['conv'], conv_id)
        self.assertIn('held startup ping', delivered['text'])
        self.assertEqual(delivered['addr'], server.address)
        self.assertTrue(delivered['has_tok'])
        self.assertEqual(read(agent_dir / 'properties.json')['native_session_id'], conv_id)
        self.assertEqual(read(agent_dir / 'runtime.json')['native_bridge'], BRIDGE)

        # Queue a busy stop-hook follow-up, kill before Stop fires, --check marks it failed, and --retry wakes
        # directly with --prompt-interactive on the first try (not promptless _queue_wake).
        saved_conv = self.work / '.gemini' / 'antigravity-cli' / 'conversations' / f'{conv_id}.db'
        saved_conv.parent.mkdir(parents=True, exist_ok=True)
        saved_conv.write_bytes(b'SQLite format 3\x00')
        send_tool.run(self.root, agent='agy-agent', text='wake after kill', delivery='follow-up')
        lifecycle.kill(self.root, started['id'])
        self.assertFalse(ls_file.exists())

        from multiverse.tools import deliver as deliver_tool
        checked = deliver_tool.run(self.root, agent='agy-agent', check=True)
        self.assertFalse(checked['ok'], checked)
        seen_log.unlink()
        retry_lines = []
        retried = deliver_tool.run(self.root, agent='agy-agent', retry=True, _present=retry_lines.append)
        self.assertTrue(retried['ok'], (retried, retry_lines))
        self.assertTrue(any('resuming it with your message' in line for line in retry_lines), retry_lines)
        self.assertFalse(any('first mv2 command' in line for line in retry_lines), retry_lines)
        until(lambda: seen_log.exists(), 15)
        wake_seen = read(seen_log)
        self.assertTrue(any(arg.startswith('--prompt-interactive=') and 'wake after kill' in arg
                            for arg in wake_seen['argv']), wake_seen['argv'])
        # The child can write seen_log before the runner publishes pid; wait before kill (see test_kill).
        until(lambda: (facts := agents.runtime(agent_dir)).get('launch_id') != launch_id
              and bool(facts.get('pid')), 15)
        lifecycle.kill(self.root, started['id'])

    def test_tmux_promptless_spawn_wait_becomes_ready_without_shell_callback(self):
        """A bridged Antigravity launch with no task/prompt never runs an mv2 command on its own, so --wait must
        succeed once the Antigravity UI starts rather than timing out; an unbound fresh launch still advises
        mv2 kill + mv2 deliver --retry, while a bound idle session before its first mv2 command queues visibly
        without failing and stays retryable after mv2 kill."""
        conv_id = str(uuid4())
        fake_agy = self.root / 'harnesses' / 'agy' / 'fake-agy-idle'
        fake_agy.write_text(
            f"#!{sys.executable}\n"
            "import json, os, shlex, subprocess, sys, time\nfrom pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "if args == ['--version']:\n"
            "    print('agy 1.5.16', flush=True)\n"
            "    sys.exit(0)\n"
            "if '--settings' in args and not any(a.startswith('--prompt-interactive=') for a in args):\n"
            "    settings_path = Path(args[args.index('--settings') + 1])\n"
            "    cfg = json.loads(settings_path.read_text())\n"
            "    cmd = shlex.split(cfg['statusLine']['command'])\n"
            f"    subprocess.run(cmd, input=json.dumps({{'conversation_id': {conv_id!r}, 'agent_state': 'idle'}}), text=True, capture_output=True)\n"
            "print('? for shortcuts', flush=True)\n"
            "while True: time.sleep(0.5)\n"
        )
        fake_agy.chmod(0o755)
        spec = read(self.root / 'harnesses' / 'agy' / 'harness.json')
        spec['settings']['executable'] = str(fake_agy)
        write(self.root / 'harnesses' / 'agy' / 'harness.json', spec)

        started = lifecycle.start(self.root, 'agy-idle', '', 'agy', socket_path=self.socket, wait=True, wait_timeout=15)
        self.assertTrue(started['ok'], started)
        self.assertTrue(started['ready'], started)
        self.assertIsInstance(started['runtime'].get('pid'), int)
        self.assertTrue(any('first mv2 command' in n for n in started.get('notices', ())), started.get('notices'))
        agent_dir = self.root / 'state' / 'agents' / started['id']
        until(lambda: read(agent_dir / 'properties.json').get('native_session_id') == conv_id, 10)
        until(lambda: read(agent_dir / 'runtime.json').get('native_bridge') == BRIDGE, 10)
        # Sending to a bound idle session before any mv2 command queues visibly without failing.
        from multiverse.tools import deliver as deliver_tool, send as send_tool
        lines = []
        send_tool.run(self.root, agent='agy-idle', text='initial task', delivery='steer', _present=lines.append)
        line = '\n'.join(lines)
        self.assertIn('agy-idle has your message queued; its next turn has not started.', line)
        check_lines = []
        checked = deliver_tool.run(self.root, agent='agy-idle', check=True, _present=check_lines.append)
        self.assertTrue(checked['ok'], (checked, check_lines))
        self.assertTrue(any('queued for its next turn' in item for item in check_lines), check_lines)
        # Stopping the agent settles the undelivered queued message as failed so deliver --retry wakes it.
        saved_conv = self.work / '.gemini' / 'antigravity-cli' / 'conversations' / f'{conv_id}.db'
        saved_conv.parent.mkdir(parents=True, exist_ok=True)
        saved_conv.write_bytes(b'SQLite format 3\x00')
        lifecycle.kill(self.root, started['id'])
        after_kill = deliver_tool.run(self.root, agent='agy-idle', check=True)
        self.assertFalse(after_kill['ok'], after_kill)
        retry_lines = []
        retried = deliver_tool.run(self.root, agent='agy-idle', retry=True, _present=retry_lines.append)
        self.assertTrue(retried['ok'], (retried, retry_lines))
        self.assertTrue(any('resuming it with your message' in item for item in retry_lines), retry_lines)
        # deliver --retry wakes without --wait; wait for the runner to register pid before kill (see test_kill).
        until(lambda: (facts := agents.runtime(agent_dir)).get('launch_id') != started['runtime']['launch_id']
              and bool(facts.get('pid')), 15)
        lifecycle.kill(self.root, started['id'])


if __name__ == '__main__':
    unittest.main()
