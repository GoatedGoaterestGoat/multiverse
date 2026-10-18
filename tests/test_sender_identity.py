"""Legacy sends retain a full, current launch identity; operators stay unscoped."""
from multiverse.runtime import agent_identity
from multiverse.store import uid, write, read
from test_multiverse import BaseTest


class SenderIdentityTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.directory = self.root / 'state' / 'agents' / uid()
        self.properties = {'id': self.directory.name, 'universe_id': read(self.root / 'universe.json')['id'],
                           'name': 'sender', 'harness': 'claude'}
        write(self.directory / 'properties.json', self.properties)
        write(self.directory / 'runtime.json', {'launch_id': 'current'})
        self.env = {'MV2_UNIVERSE': str(self.root), 'MV2_AGENT_ID': self.directory.name,
                    'MV2_AGENT_DIR': str(self.directory), 'MV2_LAUNCH_ID': 'current'}

    def test_each_declared_legacy_identity_field_is_required(self):
        for key in self.env:
            with self.subTest(key=key), self.assertRaises(ValueError):
                agent_identity.sender_actor({name: value for name, value in self.env.items() if name != key})

    def test_operator_and_current_legacy_sender(self):
        self.assertIsNone(agent_identity.sender_actor({'MV2_UNIVERSE': str(self.root)}))
        self.assertEqual(agent_identity.sender_actor(self.env).sender()['name'], 'sender')

    def test_wrong_directory_generation_or_saved_identity_cannot_send(self):
        for changed in ({'MV2_AGENT_DIR': str(self.work)}, {'MV2_LAUNCH_ID': 'old'},
                        {'MV2_AGENT_ID': self.directory.name.upper()}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                agent_identity.sender_actor({**self.env, **changed})
        write(self.directory / 'properties.json', {**self.properties, 'universe_id': uid()})
        with self.assertRaises(ValueError):
            agent_identity.sender_actor(self.env)
        write(self.directory / 'properties.json', self.properties)
        (self.directory / 'runtime.json').unlink()
        with self.assertRaisesRegex(ValueError, 'Outdated launch'):
            agent_identity.sender_actor(self.env)
        (self.directory / 'properties.json').write_text('[]')
        with self.assertRaisesRegex(ValueError, 'properties.json'):
            agent_identity.sender_actor(self.env)
