"""Agent file damage must stay local to its row (mv2-lead feature clarification)."""
import json
from pathlib import Path
import unittest

from qa import test_dashboard_plugin_failure as dashboard


class DamagedAgentRecordTests(dashboard.DashboardFixture):
    def setUp(self):
        super().setUp()
        harness = self.root / 'harnesses' / 'fake'
        harness.mkdir()
        (harness / 'harness.json').write_text(json.dumps({
            'schema': 1, 'description': 'QA terminal', 'session': 'assigned',
            'spawn': ['/bin/cat'], 'resume': ['/bin/cat']}))
        self.rows = {}
        for name in ('healthy', 'damaged'):
            self.run_cli('spawn', '--name', name, '--harness', 'fake', '--wait')
        for row in json.loads(self.run_cli('list', '--json').stdout):
            self.rows[row['name']] = row
        self.directory = self.root / 'state' / 'agents' / self.rows['damaged']['id']
        self.addCleanup(self.restore)
        self.originals = {name: (self.directory / name).read_bytes()
                          for name in ('properties.json', 'runtime.json')}
        self.start_dashboard()

    def restore(self):
        for name, data in getattr(self, 'originals', {}).items():
            path = self.directory / name
            path.chmod(0o600)
            path.write_bytes(data)

    def damage(self, file, kind):
        path = self.directory / file
        if kind == 'list':
            path.write_text('[]')
        elif kind == 'truncated':
            path.write_text('{"id":')
        elif kind == 'empty':
            path.write_text('')
        elif kind == 'name-list':
            value = json.loads(self.originals[file]);value['name'] = ['not-a-name']
            path.write_text(json.dumps(value))
        elif kind == 'harness-list':
            value = json.loads(self.originals[file]);value['harness'] = ['not-a-harness']
            path.write_text(json.dumps(value))
        elif kind == 'unreadable':
            path.chmod(0)
        else:
            raise AssertionError(kind)

    def check_dashboard_damage(self, file, kind):
        self.run_cli('report', '--agent', self.rows['healthy']['id'], '--phase', 'working',
                     '--summary', 'HEALTHY-REFRESH-MARKER')
        self.damage(file, kind)
        # Make a separate documented view change to prove a full scan completed.
        marker = 'Healthy agents still refresh'
        (self.root / 'views' / 'default.json').write_text(json.dumps({'title': marker}))
        self.tmux('send-keys', '-t', 'ui', 'F5')
        screen = self.until(lambda s: marker in s or 'Could not refresh:' in s)
        self.assertNotIn('Could not refresh:', screen,
                         'One damaged record prevented a complete refresh:\n' + screen)
        self.until(lambda s: 'HEALTHY-REFRESH-MARKER' in s)
        screen = self.screen()
        self.assertIn('damaged', screen.lower(), 'The broken row needs a damage diagnostic')
        self.assertIn(file, screen, 'Damage diagnostic needs the file and reason')

    def check_cli_damage(self, command, file, kind):
        self.damage(file, kind)
        args = ['mv2', command]
        if command == 'inspect': args += [self.rows['healthy']['id']]
        result = self.command(args, check=False)
        self.assertNotIn('Traceback', result.stdout, result.stdout)
        self.assertIn('healthy' if command in ('list', 'inspect') else file,
                      result.stdout.lower(), result.stdout)


def dashboard_test(file, kind):
    def test(self):
        """mv2-lead: one damaged record must not stop other dashboard rows refreshing."""
        self.check_dashboard_damage(file, kind)
    return test


for file, kinds in (
        ('properties.json', ('list', 'truncated', 'empty', 'name-list', 'harness-list', 'unreadable')),
        ('runtime.json', ('list', 'truncated', 'empty', 'unreadable'))):
    for kind in kinds:
        setattr(DamagedAgentRecordTests, 'test_dashboard_' + file.split('.')[0] + '_' + kind.replace('-', '_'),
                dashboard_test(file, kind))

for command in ('list', 'inspect', 'doctor'):
    def test(self, command=command):
        """mv2-lead: list/inspect/doctor must isolate and diagnose damaged records."""
        self.check_cli_damage(command, 'properties.json', 'list')
    setattr(DamagedAgentRecordTests, 'test_' + command + '_with_damaged_properties', test)


if __name__ == '__main__':
    unittest.main()
