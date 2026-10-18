"""Damaged peer evidence blocks only a possible ownership or name collision."""
import os
from uuid import uuid4

from multiverse.runtime import lifecycle
from multiverse.store import read, write
from multiverse.universe import agents, relocate, sessions, templates
from test_multiverse import BaseTest


class DamagedPeerClaimsTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.peer = self.root / 'state/agents' / str(uuid4())
        self.native = str(uuid4())
        self.healthy, self.data, _, _ = lifecycle._record(self.root, 'healthy', '', 'codex', None, None)
        self.data.update(workspace=str(self.work), native_session_id=self.native, native_session_provider='codex')
        write(self.healthy / 'properties.json', self.data)
        write(self.peer / 'properties.json', [])

    def test_new_record_bind_and_move_ignore_proven_unrelated_readable_damage(self):
        destination = templates.create(self.root / 'subverses/destination', 'destination', workdir=self.work)
        damaged_destination = destination / 'state/agents' / str(uuid4())
        write(damaged_destination / 'properties.json', [])
        for raw in ('[]', '{"name":'):
            with self.subTest(raw=raw):
                (self.peer / 'properties.json').write_text(raw)
                (damaged_destination / 'properties.json').write_text(raw)
                lifecycle._record(self.root, 'new-agent', '', 'codex', None, None)
                with sessions.namespace(self.root):
                    sessions.claim(self.root, self.healthy, self.data, self.native)
        result = relocate.move_agent(self.root, self.healthy.name, destination)
        self.assertEqual(result['native_session_id'], self.native)
        self.assertEqual(agents.directory(destination, 'healthy').name, self.healthy.name)

    def test_damaged_raw_name_and_uuid_or_journal_reservations_are_named_refusals(self):
        path = self.peer / 'properties.json'
        path.write_text('{"name": "reserved"')
        with self.assertRaisesRegex(ValueError, self.peer.name):
            agents.check_available_name(self.root, 'reserved')
        path.write_text('{"native_session_id": "' + self.native)
        with sessions.namespace(self.root), self.assertRaisesRegex(sessions.SessionConflict, self.peer.name):
            sessions.claim(self.root, self.healthy, self.data, self.native)
        path.write_text('[]')
        (self.peer / 'sessions.jsonl').write_text('{"to": "' + self.native)
        with sessions.namespace(self.root), self.assertRaisesRegex(sessions.SessionConflict, self.peer.name):
            sessions.claim(self.root, self.healthy, self.data, self.native)

    def test_escaped_parsed_and_truncated_reservations_are_not_released(self):
        path = self.peer / 'properties.json'
        encoded_id = self.native.replace(self.native[0], '\\u%04x' % ord(self.native[0]))
        path.write_text('{"name": [], "native_session_id": "' + encoded_id + '"}')
        with sessions.namespace(self.root), self.assertRaisesRegex(sessions.SessionConflict, self.peer.name):
            sessions.claim(self.root, self.healthy, self.data, self.native)
        path.write_text('{"native_session_id": "' + encoded_id)
        with sessions.namespace(self.root), self.assertRaisesRegex(sessions.SessionConflict, self.peer.name):
            sessions.claim(self.root, self.healthy, self.data, self.native)
        path.write_text('{"name": "\\u0072eserved"')
        with self.assertRaisesRegex(ValueError, self.peer.name):
            agents.check_available_name(self.root, 'reserved')

    def test_raw_scan_decodes_ascii_case_and_chunk_boundaries_but_is_bounded(self):
        from unittest.mock import patch
        from multiverse.universe import records
        path = self.peer / 'properties.json'
        path.write_text('x' * 65534 + '\\u0052eserved')
        self.assertTrue(records.mentions(path, 'reserved'))
        with patch.object(records, 'MAX_BYTES', 32), self.assertRaisesRegex(ValueError, 'too large'):
            records.mentions(path, 'absent')

    def test_unreadable_peers_never_silently_release_names_or_sessions(self):
        if os.geteuid() == 0:
            self.skipTest('root reads mode-zero files')
        path = self.peer / 'properties.json'
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        with self.assertRaisesRegex(ValueError, self.peer.name):
            agents.check_available_name(self.root, 'new-agent')
        with sessions.namespace(self.root), self.assertRaisesRegex(sessions.SessionConflict, self.peer.name):
            sessions.claim(self.root, self.healthy, self.data, self.native)
