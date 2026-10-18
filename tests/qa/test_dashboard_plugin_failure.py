"""Outside-in dashboard regressions: mv2 CLI, documented files and tmux only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


class DashboardFixture(unittest.TestCase):
    def setUp(self):
        if not shutil.which('tmux') or not shutil.which('mv2'):
            self.skipTest('mv2 and tmux are required')
        self.scratch = tempfile.TemporaryDirectory(prefix='qd-bb-', dir='/tmp')
        self.addCleanup(self.remove_scratch)
        self.base = Path(self.scratch.name)
        self.work = self.base / 'work'
        self.work.mkdir()
        self.home = self.base / 'home'
        self.home.mkdir()
        self.plugins = self.base / 'plugins'
        self.plugins.mkdir()
        self.socket = str(self.base / 'ui.sock')
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith('MV2_') and k not in (
                        'TMUX', 'TMUX_PANE', 'CODEX_THREAD_ID', 'CLAUDECODE',
                        'CLAUDE_CODE_CHILD_SESSION')}
        self.env.update(HOME=str(self.home), CODEX_HOME=str(self.home / '.codex'),
                        CLAUDE_CONFIG_DIR=str(self.home / '.claude'),
                        MV2_TMUX_SOCKET=str(self.base / 'agents.sock'),
                        MV2_PLUGIN_PATH=str(self.plugins), TERM='xterm-256color')
        self.log = []
        self.root = self.work / '.multiverse'
        self.addCleanup(self.stop_servers)
        self.run_cli('init', str(self.work))
        self.root = self.work / '.multiverse'

    def command(self, argv, check=True):
        result = subprocess.run(argv, cwd=self.work, env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
        self.log.append({'argv': argv, 'returncode': result.returncode, 'output': result.stdout})
        if check:
            self.assertEqual(result.returncode, 0, result.stdout)
        return result

    def run_cli(self, *args):
        return self.command(['mv2', *args])

    def tmux(self, *args, check=True):
        return self.command(['tmux', '-S', self.socket, *args], check=check)

    def remove_scratch(self):
        # Runners may finish writing their documented cleanup records after tmux exits.
        deadline = time.monotonic() + 5
        while True:
            try:
                self.scratch.cleanup()
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.1)

    def stop_servers(self):
        # These records belong only to this test's isolated universe.
        for properties in self.root.glob('state/agents/*/properties.json'):
            try:
                record = json.loads(properties.read_text())
                identity = record['id']
            except (OSError, ValueError, TypeError, KeyError):
                continue
            self.command(['mv2', 'kill', identity], check=False)
        self.command(['mv2', 'plugins', 'host', 'stop'], check=False)
        for socket in (self.socket, self.env['MV2_TMUX_SOCKET']):
            self.command(['tmux', '-S', socket, 'kill-server'], check=False)
        evidence = os.environ.get('DASHBOARD_QA_EVIDENCE')
        if evidence:
            destination = Path(evidence)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / (self.id().split('.')[-1] + '.json')).write_text(json.dumps(self.log, indent=2))

    def screen(self):
        return self.tmux('capture-pane', '-p', '-t', 'ui').stdout

    def until(self, predicate, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            screen = self.screen()
            if predicate(screen):
                return screen
            time.sleep(.1)
        self.fail('Dashboard did not reach expected screen:\n' + screen)

    def plugin(self, name, source):
        directory = self.plugins / name
        directory.mkdir()
        (directory / 'plugin.json').write_text(json.dumps({
            'schema': 1, 'name': name, 'version': '1.0.0', 'api': '1.0',
            'module': 'plugin', 'description': 'QA public API fixture',
            'extends': ['dashboard.action']}))
        (directory / 'plugin.py').write_text(source)
        return directory

    def start_dashboard(self):
        self.tmux('new-session', '-d', '-s', 'ui', '-x', '120', '-y', '36', 'mv2')
        return self.until(lambda s: 'Universe / agent' in s and 'q Quit' in s)


class DashboardPluginFailureTests(DashboardFixture):
    def test_failed_plugin_load_keeps_plugins_screen_and_refresh_available(self):
        """docs/use/plugins.md: a broken plugin loses only its own part of mv2."""
        self.plugin('broken', 'def register(mv):\n raise RuntimeError("QA load failure")\n')
        (self.root / 'plugins.json').write_text(json.dumps({'schema': 1, 'enabled': {'broken': True}}))
        self.start_dashboard()
        self.tmux('send-keys', '-t', 'ui', 'P')
        screen = self.until(lambda s: 'QA load failure' in s or 'Plugins unavailable' in s)
        self.assertNotIn('Plugins unavailable', screen,
                         'A failed plugin made the whole Plugins screen unavailable:\n' + screen)
        self.assertIn('broken', screen)
        self.tmux('send-keys', '-t', 'ui', 'Escape')
        self.until(lambda s: 'Plugins ·' not in s)
        self.tmux('send-keys', '-t', 'ui', 'F5')
        screen = self.until(lambda s: 'q Quit' in s)
        self.assertNotIn('Could not refresh:', screen)

    def test_invalid_settings_field_keeps_refresh_and_plugins_available(self):
        """docs/use/plugins.md: no plugin can stop the dashboard or another plugin."""
        directory = self.plugin('badfield', 'from multiverse.plugins import api\ndef register(mv):\n mv.dashboard.settings(fields=[api.Field("count", "Count", "integer")])\n')
        manifest = json.loads((directory / 'plugin.json').read_text())
        manifest.update(extends=['dashboard.settings'], settings={'version': 1, 'schema': 'settings.schema.json'})
        (directory / 'plugin.json').write_text(json.dumps(manifest))
        (directory / 'settings.schema.json').write_text(json.dumps({'type': 'object', 'properties': {'count': {'type': 'integer'}}}))
        self.plugin('healthy', 'def register(mv):\n mv.dashboard.action("Healthy", lambda context: "healthy")\n')
        (self.root / 'plugins.json').write_text(json.dumps({'schema': 1, 'enabled': {'badfield': True, 'healthy': True}}))
        self.start_dashboard()
        for index in range(3):
            marker = 'Refresh marker ' + str(index)
            (self.root / 'views' / 'default.json').write_text(json.dumps({'title': marker}))
            self.tmux('send-keys', '-t', 'ui', 'F5')
            self.until(lambda s: marker in s, timeout=5)
            self.tmux('send-keys', '-t', 'ui', 'P')
            screen = self.until(lambda s: 'badfield' in s or 'Plugins unavailable' in s)
            self.assertNotIn('Plugins unavailable', screen)
            self.tmux('send-keys', '-t', 'ui', 'Escape')
            self.until(lambda s: 'Plugins ·' not in s)

    def test_enabling_a_plugin_that_fails_load_keeps_plugins_available(self):
        """docs/use/plugins.md: changing plugins never stops another dashboard part."""
        self.start_dashboard()
        directory = self.plugin('latebad', 'from multiverse.plugins import api\ndef register(mv):\n mv.dashboard.settings(fields=[api.Field("count", "Count", "integer")])\n')
        manifest = json.loads((directory / 'plugin.json').read_text())
        manifest.update(extends=['dashboard.settings'], settings={'version': 1, 'schema': 'settings.schema.json'})
        (directory / 'plugin.json').write_text(json.dumps(manifest))
        (directory / 'settings.schema.json').write_text(json.dumps({'type': 'object', 'properties': {'count': {'type': 'integer'}}}))
        self.run_cli('plugins', 'enable', 'latebad')
        self.tmux('send-keys', '-t', 'ui', 'F5')
        self.tmux('send-keys', '-t', 'ui', 'P')
        self.until(lambda s: 'latebad' in s or 'Plugins unavailable' in s)
        self.tmux('send-keys', '-t', 'ui', 'Escape')
        self.until(lambda s: 'Plugins ·' not in s)
        for index in range(3):
            marker = 'Late refresh marker ' + str(index)
            (self.root / 'views' / 'default.json').write_text(json.dumps({'title': marker}))
            self.tmux('send-keys', '-t', 'ui', 'F5')
            self.until(lambda s: marker in s, timeout=5)
            self.tmux('send-keys', '-t', 'ui', 'P')
            screen = self.until(lambda s: 'latebad' in s or 'Plugins unavailable' in s)
            self.assertNotIn('Plugins unavailable', screen)
            self.tmux('send-keys', '-t', 'ui', 'Escape')
            self.until(lambda s: 'Plugins ·' not in s)

    def test_repairing_failed_settings_plugin_after_configuring_it_recovers(self):
        """docs/use/plugins.md: failed code retries when its files change."""
        source = 'from multiverse.plugins import api\ndef register(mv):\n mv.dashboard.settings(fields=[api.Field("count", "Count", "integer")])\n'
        directory = self.plugin('badfield', source)
        manifest = json.loads((directory / 'plugin.json').read_text())
        manifest.update(extends=['dashboard.settings'], settings={'version': 1, 'schema': 'settings.schema.json', 'defaults': 'defaults.json'})
        (directory / 'plugin.json').write_text(json.dumps(manifest))
        (directory / 'settings.schema.json').write_text(json.dumps({'type': 'object', 'properties': {'count': {'type': 'integer'}}}))
        (directory / 'defaults.json').write_text(json.dumps({'schema': 1, 'count': 2}))
        self.run_cli('plugins', 'enable', 'badfield')
        self.start_dashboard()
        self.tmux('send-keys', '-t', 'ui', 'P')
        self.until(lambda s: 'badfield' in s and 'failed to load' in s)
        self.tmux('send-keys', '-t', 'ui', 'Enter')
        self.until(lambda s: 'Settings · badfield' in s)
        self.tmux('send-keys', '-t', 'ui', 'Tab')
        self.tmux('send-keys', '-t', 'ui', 'C-u')
        self.tmux('send-keys', '-l', '-t', 'ui', '4')
        self.tmux('send-keys', '-t', 'ui', '-H', '1b', '5b', '31', '33', '3b', '35', '75')
        self.until(lambda s: 'Plugins ·' in s and 'Settings ·' not in s)
        self.tmux('send-keys', '-t', 'ui', 'Escape')
        self.until(lambda s: 'Plugins ·' not in s)
        (directory / 'plugin.py').write_text(source.replace('"integer"', '"int"'))
        marker = 'Recovered settings plugin'
        (self.root / 'views' / 'default.json').write_text(json.dumps({'title': marker}))
        self.tmux('send-keys', '-t', 'ui', 'F5')
        screen = self.until(lambda s: marker in s or 'Could not refresh:' in s)
        self.assertNotIn('Could not refresh:', screen)
        self.tmux('send-keys', '-t', 'ui', 'P')
        screen = self.until(lambda s: 'badfield' in s or 'Plugins unavailable' in s)
        self.assertNotIn('Plugins unavailable', screen)

    def test_slow_plugin_load_does_not_delay_first_screen_or_quitting(self):
        """docs/use/plugins.md: no plugin can stop the dashboard, even while loading."""
        self.plugin('slow', 'import time\ndef register(mv):\n time.sleep(90)\n')
        (self.root / 'plugins.json').write_text(json.dumps({'schema': 1, 'enabled': {'slow': True}}))
        began = time.monotonic()
        self.start_dashboard()
        self.assertLess(time.monotonic() - began, 5)
        self.tmux('send-keys', '-t', 'ui', 'q')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.tmux('has-session', '-t', 'ui', check=False).returncode:
                return
            time.sleep(.1)
        self.fail('q did not exit while a plugin was still loading')

    def test_plugin_host_stop_reports_success_without_traceback(self):
        """docs/use/plugins.md, The plugin host: mv2 plugins host stop stops the host."""
        directory = self.plugin('hostprobe', 'def register(mv):\n mv.events.on("agent.reported", lambda event: None)\n')
        manifest = json.loads((directory / 'plugin.json').read_text())
        manifest.update(extends=['events'], events=['agent.reported'])
        (directory / 'plugin.json').write_text(json.dumps(manifest))
        self.run_cli('plugins', 'enable', 'hostprobe')
        self.run_cli('plugins', 'host', 'start')
        result = self.command(['mv2', 'plugins', 'host', 'stop'], check=False)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn('Traceback', result.stdout)
        self.assertIn('not running', self.run_cli('plugins', 'host').stdout)

    def test_hung_plugin_action_does_not_prevent_quitting(self):
        """docs/use/plugins.md: a broken plugin loses only its own part of mv2."""
        self.plugin('hung', 'import time\ndef register(mv):\n mv.dashboard.action("Hang forever", lambda context: time.sleep(90))\n')
        (self.root / 'plugins.json').write_text(json.dumps({'schema': 1, 'enabled': {'hung': True}}))
        self.start_dashboard()
        self.tmux('send-keys', '-t', 'ui', 'C-p')
        self.until(lambda s: '🔎' in s)
        self.tmux('send-keys', '-l', '-t', 'ui', 'Hang forever')
        self.until(lambda s: 'hung: Hang forever' in s)
        self.tmux('send-keys', '-t', 'ui', 'Enter')
        self.until(lambda s: '🔎' not in s)
        self.tmux('send-keys', '-t', 'ui', 'q')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.tmux('has-session', '-t', 'ui', check=False).returncode:
                return
            time.sleep(.1)
        self.fail('q did not exit while a plugin action was still running; screen:\n' + self.screen())

    def test_corrected_failed_plugin_recovers_without_restarting_dashboard(self):
        """docs/use/plugins.md, failed state: retry once the plugin files change."""
        directory = self.plugin('repairable', 'def register(mv):\n raise RuntimeError("QA first load failure")\n')
        (self.root / 'plugins.json').write_text(json.dumps({'schema': 1, 'enabled': {'repairable': True}}))
        self.start_dashboard()
        self.tmux('send-keys', '-t', 'ui', 'P')
        self.until(lambda s: 'QA first load failure' in s or 'Plugins unavailable' in s)
        self.tmux('send-keys', '-t', 'ui', 'Escape')
        self.until(lambda s: 'Plugins ·' not in s)
        (directory / 'plugin.py').write_text('def register(mv):\n mv.dashboard.action("Recovered action", lambda context: "Recovered result")\n')
        self.tmux('send-keys', '-t', 'ui', 'F5')
        self.tmux('send-keys', '-t', 'ui', 'C-p')
        self.until(lambda s: '🔎' in s)
        self.tmux('send-keys', '-l', '-t', 'ui', 'Recovered action')
        screen = self.until(lambda s: 'repairable: Recovered action' in s)
        self.assertIn('repairable: Recovered action', screen)


if __name__ == '__main__':
    unittest.main()
