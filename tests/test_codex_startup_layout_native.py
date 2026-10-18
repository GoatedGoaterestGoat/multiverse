"""Configurable healthy native composers settle plain startup without the 60s fallback."""
import json
import os
import shutil
import subprocess
import unittest
from unittest.mock import patch

from native_processes import PrivateProcessGroup
from native_safety import install, isolated_home
from multiverse import harnesses
from multiverse.runtime import lifecycle
from multiverse.store import write
from multiverse.universe import agents
from test_multiverse import TerminalBase

install()


@unittest.skipUnless(shutil.which('codex') and shutil.which('tmux'), 'Codex and tmux required')
class NativeStartupLayoutTest(TerminalBase):
    def test_plain_wait_recognizes_custom_disabled_and_clipped_composers(self):
        if subprocess.check_output(['codex', '--version'], stdin=subprocess.DEVNULL,
                                   text=True).strip() != 'codex-cli 0.163.0':
            self.skipTest('native startup layout evidence pinned to 0.163.0')
        home = isolated_home(self.work / 'native-home', config=
            'model="fixture"\nmodel_provider="fixture"\n'
            '[model_providers.fixture]\nname="Private layout fixture"\n'
            'base_url="http://127.0.0.1:9/v1"\nwire_api="responses"\nrequires_openai_auth=false\n'
            f'[projects.{json.dumps(str(self.work))}]\ntrust_level="trusted"\n')
        seven = ['model-with-reasoning', 'current-dir', 'git-branch', 'context-remaining',
                 'five-hour-limit', 'weekly-limit', 'codex-version']
        original_create = lifecycle.Tmux.create
        for name, width, items in (('seven', 120, seven), ('off', 120, []),
                                   ('one', 40, ['model-name']), ('clipped', 24, [])):
            with self.subTest(footer=name, width=width):
                state = {'id': None, 'group': None, 'closed': False}

                def cleanup(state=state):
                    if state['closed']:
                        return
                    try:
                        if state['id']:
                            lifecycle.kill(self.root, state['id'])
                    finally:
                        if state['group']:
                            state['group'].close()
                        state['closed'] = True

                self.addCleanup(cleanup)

                def create_at_width(backend, *args, **kwargs):
                    terminal = original_create(backend, *args, **kwargs)
                    backend.run('resize-window', '-t', terminal['pane'], '-x', str(width), '-y', '40')
                    return terminal

                write(self.root / 'harnesses/codex/harness.json', {'schema': 2, 'adapter': 'codex', 'settings': {
                    'inbox_bridge': False, 'args': ['--no-daemon', '--dangerously-bypass-approvals-and-sandbox',
                                                   '-c', 'tui.status_line=' + json.dumps(items)]}})
                with patch.dict(os.environ, {'CODEX_HOME': str(home)}), \
                        patch.object(lifecycle.Tmux, 'create', new=create_at_width):
                    started = lifecycle.start(self.root, 'layout-' + name, '', 'codex', socket_path=self.socket)
                state['id'] = started['id']
                directory = agents.directory(self.root, started['id'])
                facts = agents.runtime(directory)
                pane = facts['tmux']['pane']
                # The runner and its embedded native process belong to this
                # private pane's group. Capture before assertions/explicit stop.
                state['group'] = PrivateProcessGroup(int(self.backend.run('display-message', '-p', '-t', pane, '#{pane_pid}')))
                self.assertEqual(int(self.backend.run('display-message', '-p', '-t', pane, '#{pane_width}')), width)
                waited = lifecycle.wait_ready(self.root, directory, facts['launch_id'],
                                               harnesses.load(self.root, 'codex'), 20)
                self.assertTrue(waited['ready'], {key: waited.get(key) for key in ('error', 'ready', 'notices')})
                current = agents.runtime(directory)
                self.assertEqual(current['native_startup_observation']['state'], 'started')
                self.assertFalse(waited.get('notices'), 'Healthy native UI used unverified process readiness')
                cleanup()
