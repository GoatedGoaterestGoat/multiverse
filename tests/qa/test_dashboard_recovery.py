"""External checks for the dashboard fixes delegated on 500655d."""
import json
import re
import time
import unittest
from qa import test_dashboard_plugin_failure as dashboard


class PluginHostUnknownReasonTests(dashboard.DashboardFixture):
    def test_plugins_screen_shows_why_host_status_is_unknown(self):
        """docs/use/plugins.md The plugin host; mv2-lead: P must show the unknown-status reason."""
        status = self.root / 'state' / 'plugins' / 'host' / 'status.json'
        status.parent.mkdir(parents=True, exist_ok=True)
        status.write_text('{truncated')
        cli = self.command(['mv2', 'plugins', 'host'], check=False)
        self.assertIn('unknown', cli.stdout.lower())
        self.start_dashboard()
        self.tmux('send-keys', '-t', 'ui', 'P')
        screen = self.until(lambda s: 'status unknown' in s)
        self.assertIn('state/plugins/host/status.json', screen)
        self.assertRegex(screen.lower(), r'(not json|cut short|empty|invalid json)',
                         'P needs a meaningful corruption reason, not only status unknown')
        self.tmux('send-keys', '-t', 'ui', 'Escape')
        self.until(lambda s: 'Plugins ·' not in s)
        status.unlink()
        self.tmux('send-keys', '-t', 'ui', 'F5')
        self.tmux('send-keys', '-t', 'ui', 'P')
        self.until(lambda s: 'Plugins ·' in s and 'status unknown' not in s)

    def test_plugins_screen_shows_non_object_host_status_reason_at_60x20(self):
        """The host-status file and its reason remain readable on a small dashboard."""
        status = self.root / 'state' / 'plugins' / 'host' / 'status.json'
        status.parent.mkdir(parents=True, exist_ok=True)
        status.write_text('[]')
        self.start_dashboard()
        self.tmux('resize-window', '-t', 'ui', '-x', '60', '-y', '20')
        self.tmux('send-keys', '-t', 'ui', 'P')
        screen = self.until(lambda s: 'status unknown' in s)
        self.assertIn('status.json', screen)
        self.assertIn('object', screen.lower())
        self.tmux('send-keys', '-t', 'ui', 'Escape')
        self.until(lambda s: 'Plugins ·' not in s)
        self.tmux('send-keys', '-t', 'ui', 'q')


class RepairedPluginProofTests(dashboard.DashboardFixture):
    def select_plugin(self, name):
        """Put the Plugins screen's cursor on `name`: which row that is depends on the other plugins installed.

        The details under the table start with the selected plugin's name and version."""
        pattern = re.compile(rf'│ {re.escape(name)} \S+ ·')
        for _ in range(20):
            if pattern.search(self.screen()):
                return
            self.tmux('send-keys', '-t', 'ui', 'Down')
            time.sleep(.3)
        self.fail(f'The Plugins screen never selected {name}:\n' + self.screen())

    def test_repaired_plugin_runs_its_new_action_after_failed_configure_save(self):
        """docs/use/plugins.md: changed plugin files retry and repaired dashboard contributions work."""
        source = ('from multiverse.plugins import api\ndef register(mv):\n'
                  ' mv.dashboard.settings(fields=[api.Field("count", "Count", "integer")])\n'
                  ' mv.dashboard.action("Repair proof", lambda context: "REPAIRED-ACTION-RESULT")\n')
        directory = self.plugin('repairable', source)
        manifest = json.loads((directory / 'plugin.json').read_text())
        manifest.update(extends=['dashboard.settings', 'dashboard.action'],
                        settings={'version': 1, 'schema': 'settings.schema.json', 'defaults': 'defaults.json'})
        (directory / 'plugin.json').write_text(json.dumps(manifest))
        (directory / 'settings.schema.json').write_text(json.dumps({
            'type': 'object', 'properties': {'schema': {'const': 1}, 'count': {'type': 'integer'}}}))
        (directory / 'defaults.json').write_text(json.dumps({'schema': 1, 'count': 2}))
        self.run_cli('plugins', 'enable', 'repairable')
        self.start_dashboard()
        self.tmux('send-keys', '-t', 'ui', 'P')
        self.until(lambda s: 'repairable' in s and 'failed to load' in s)
        self.select_plugin('repairable')
        self.tmux('send-keys', '-t', 'ui', 'Enter')
        self.until(lambda s: 'Settings · repairable' in s)
        self.tmux('send-keys', '-t', 'ui', 'Tab')
        self.tmux('send-keys', '-t', 'ui', 'C-u')
        self.tmux('send-keys', '-l', '-t', 'ui', '4')
        self.tmux('send-keys', '-t', 'ui', '-H', '1b', '5b', '31', '33', '3b', '35', '75')
        self.until(lambda s: 'Plugins ·' in s and 'Settings ·' not in s)
        self.tmux('send-keys', '-t', 'ui', 'Escape')
        self.until(lambda s: 'Plugins ·' not in s)
        (directory / 'plugin.py').write_text(source.replace('"integer"', '"int"'))
        self.tmux('send-keys', '-t', 'ui', 'F5')
        self.tmux('send-keys', '-t', 'ui', 'P')
        self.select_plugin('repairable')
        self.until(lambda s: 'Repair proof' in s and 'failed to load' not in s)
        self.tmux('send-keys', '-t', 'ui', 'Enter')
        self.until(lambda s: 'Settings · repairable' in s and 'Count' in s)
        self.tmux('send-keys', '-t', 'ui', 'Escape')
        self.until(lambda s: 'Plugins ·' in s and 'Settings ·' not in s)
        self.tmux('send-keys', '-t', 'ui', 'Escape')
        self.until(lambda s: 'Plugins ·' not in s)
        self.tmux('send-keys', '-t', 'ui', 'C-p')
        self.until(lambda s: '🔎' in s)
        self.tmux('send-keys', '-l', '-t', 'ui', 'Repair proof')
        self.until(lambda s: 'repairable: Repair proof' in s)
        self.tmux('send-keys', '-t', 'ui', 'Enter')
        self.until(lambda s: 'REPAIRED-ACTION-RESULT' in s)
        self.assertEqual(json.loads((self.root / 'plugin-settings/repairable.json').read_text())['count'], 4)

    def test_quit_during_hung_action_leaves_the_live_agent_running(self):
        """docs/reference/dashboard.md q quits while agents keep running, including plugin errors."""
        import time
        directory = self.root / 'harnesses' / 'fake'
        directory.mkdir()
        (directory / 'harness.json').write_text(json.dumps({
            'schema': 1, 'spawn': ['/bin/cat'], 'resume': ['/bin/cat'], 'session': 'assigned'}))
        self.run_cli('spawn', '--name', 'sentinel', '--harness', 'fake', '--wait')
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
                break
            time.sleep(.1)
        else:
            self.fail('q did not exit during a hung plugin action')
        rows = json.loads(self.run_cli('list', '--json').stdout)
        self.assertTrue(any(r['name'] == 'sentinel' and r['live'] == 'alive' for r in rows))


class DamagedNeighbourTransitionTests(dashboard.DashboardFixture):
    def test_healthy_agent_exit_keeps_refreshing_beside_a_damaged_record(self):
        """mv2-lead: a damaged row must not stop other agents changing state on the dashboard."""
        import os
        import re
        import signal
        import subprocess
        self.env['TZ'] = 'UTC'
        harness = self.root / 'harnesses' / 'fake'
        harness.mkdir()
        (harness / 'harness.json').write_text(json.dumps({
            'schema': 1, 'spawn': ['/bin/cat'], 'resume': ['/bin/cat'], 'session': 'assigned'}))
        for name in ('healthy', 'broken'):
            self.run_cli('spawn', '--name', name, '--harness', 'fake', '--wait')
        rows = {r['name']: r for r in json.loads(self.run_cli('list', '--json').stdout)}
        self.start_dashboard()
        properties = self.root / 'state' / 'agents' / rows['broken']['id'] / 'properties.json'
        original = properties.read_bytes()
        self.addCleanup(properties.write_bytes, original)
        properties.write_text('[]')
        self.tmux('send-keys', '-t', 'ui', 'F5')
        self.until(lambda s: 'properties.json' in s and 'healthy' in s)
        runtime = rows['healthy']['runtime']
        pid = runtime['pid']
        started = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], text=True,
                                 capture_output=True, timeout=5, env=self.env).stdout.split()
        self.assertEqual(started, runtime['pid_started'].split(), 'Only signal our exact owned test process')
        os.kill(pid, signal.SIGTERM)
        self.tmux('send-keys', '-t', 'ui', 'F5')
        screen = self.until(lambda s: re.search(r'\bhealthy\b.*\b(stopped|dormant)\b', s), timeout=15)
        self.assertIn('properties.json', screen)
        self.assertNotIn('Could not refresh:', screen)
