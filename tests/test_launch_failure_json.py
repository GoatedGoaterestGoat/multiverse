"""Building a launch refusal's result never changes or hides an existing agent's records."""
from multiverse.store import write, uid
from multiverse.tools._launch import failure_record
from test_multiverse import BaseTest


class LaunchFailureJsonTest(BaseTest):
    def test_enrichment_never_mutates_existing_records_or_masks_damage(self):
        """Building a refusal result for a name in use never writes the existing agent's runtime.json and keeps the
        original refusal when that record is damaged; fragile because failure_record runs on every refusal path,
        and a break silently corrupts a running agent's runtime (e7920ce)."""
        directory = self.root / 'state' / 'agents' / uid()
        write(directory / 'properties.json', {'id': directory.name, 'name': 'existing', 'harness': 'claude',
            'workspace': str(self.work)})
        write(directory / 'runtime.json', {'state': 'running', 'launch_id': uid(), 'pid': 123})
        before = (directory / 'runtime.json').read_bytes()
        result = failure_record(self.root, 'existing', ValueError('name already reserved'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['runtime']['state'], 'running')
        self.assertEqual((directory / 'runtime.json').read_bytes(), before)
        (directory / 'properties.json').write_text('[]')
        result = failure_record(self.root, directory.name, ValueError('original refusal'))
        self.assertEqual(result['reason'], 'original refusal')
        self.assertEqual(result['name'], directory.name)
