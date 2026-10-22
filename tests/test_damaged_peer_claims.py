"""Damaged peer evidence blocks only a possible ownership or name collision."""
import errno
import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from multiverse.runtime import lifecycle
from multiverse.store import read, write
from multiverse.universe import agents, records, relocate, sessions, templates
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
        self.home = str(self.work / 'native-home')

    def test_new_record_bind_and_move_ignore_proven_unrelated_readable_damage(self):
        """Damage that provably cannot hold the name or session never blocks spawn, session bind or move; fragile
        because the fail-closed scan easily over-blocks, and one bad file then blocks every spawn in the universe."""
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

    def pi_agent(self, name):
        """An agent with a native session on the fixture provider, as test_session_lineage writes them."""
        identity, native_id = str(uuid4()), str(uuid4())
        directory = self.root / 'state' / 'agents' / identity
        write(directory / 'properties.json', {'id': identity, 'name': name, 'harness': 'pi',
              'workspace': str(self.work), 'native_session_id': native_id,
              'native_session_provider': 'fixture', 'native_session_home': self.home})
        write(directory / 'runtime.json', {'launch_id': 'launch', 'state': 'running'})
        return directory, native_id

    def test_damaged_or_partial_peers_refuse_only_claims_they_could_hold(self):
        """A damaged, unreadable or record-less peer (a plugin-only folder included) refuses, by name, exactly the
        names and session ids it could hold, and only an ENOENT proves it holds none; fragile because the raw scan
        must handle escapes, chunk boundaries and size bounds, and a break gives two agents one conversation or blocks
        every claim (3503e0e, 7bc1d3d, cca7cc3)."""
        path = self.peer / 'properties.json'
        encoded_id = self.native.replace(self.native[0], '\\u%04x' % ord(self.native[0]))
        for raw in ('{"name": "reserved"', '{"name": "\\u0072eserved"'):
            with self.subTest(name_in=raw):
                path.write_text(raw)
                with self.assertRaisesRegex(ValueError, self.peer.name) as caught:
                    agents.check_available_name(self.root, 'reserved')
                self.assertEqual(caught.exception.file, path)
                self.assertTrue(str(caught.exception).startswith('properties.json:'))
                self.assertNotIn('mv2 remove', str(caught.exception))
        with self.subTest('a missing identity still offers removal of the leftover folder'):
            path.unlink()
            write(self.peer / 'runtime.json', {'name': 'reserved', 'state': 'failed'})
            with self.assertRaisesRegex(ValueError, 'mv2 remove ' + self.peer.name):
                agents.check_available_name(self.root, 'reserved')
            (self.peer / 'runtime.json').unlink()
        for raw, journal in (('{"native_session_id": "' + self.native, None),
                             ('{"name": [], "native_session_id": "' + encoded_id + '"}', None),
                             ('{"native_session_id": "' + encoded_id, None),
                             ('[]', '{"to": "' + self.native)):
            with self.subTest(session_in=raw, journal=journal):
                path.write_text(raw)
                if journal:
                    (self.peer / 'sessions.jsonl').write_text(journal)
                with sessions.namespace(self.root), self.assertRaisesRegex(sessions.SessionConflict, self.peer.name):
                    sessions.claim(self.root, self.healthy, self.data, self.native)
        (self.peer / 'sessions.jsonl').unlink()
        with self.subTest('the raw scan'):
            path.write_text('x' * 65534 + '\\u0052eserved')
            self.assertTrue(records.mentions(path, 'reserved'))
            with patch.object(records, 'MAX_BYTES', 32), self.assertRaisesRegex(ValueError, 'too large'):
                records.mentions(path, 'absent')
        with self.subTest('an unreadable peer'):
            if os.geteuid() == 0:
                self.skipTest('root reads mode-zero files')
            path.write_text('[]')
            path.chmod(0)
            try:
                with self.assertRaisesRegex(ValueError, self.peer.name):
                    agents.check_available_name(self.root, 'new-agent')
                with sessions.namespace(self.root), \
                        self.assertRaisesRegex(sessions.SessionConflict, self.peer.name):
                    sessions.claim(self.root, self.healthy, self.data, self.native)
            finally:
                path.chmod(0o600)
        path.write_text('[]')
        agent, old = self.pi_agent('one')
        current = str(uuid4())
        agents.rebind_session(self.root, agent.name, old, current, 'clear', 'launch', home=self.home)
        journal = agent / 'sessions.jsonl'
        original = journal.read_text()
        for suffix in ('{"schema":', '\n{}\n'):
            with self.subTest(own_journal=suffix):
                journal.write_text(original + suffix)
                with self.assertRaisesRegex(ValueError, 'session journal'):
                    agents.session_properties(self.root, agent)
        journal.write_text(original)
        peer, previous = self.pi_agent('damaged-peer')
        later, torn = str(uuid4()), str(uuid4())
        agents.rebind_session(self.root, peer.name, previous, later, 'clear', 'launch', home=self.home)
        with (peer / 'sessions.jsonl').open('ab') as stream:
            stream.write(b'{"schema":1,"to":"' + torn.encode() + b'"')
        before = (peer / 'sessions.jsonl').read_bytes()
        healthy = str(uuid4())
        with self.subTest('a torn peer journal allows an id it cannot hold'):
            agents.rebind_session(self.root, agent.name, current, healthy, 'clear', 'launch', home=self.home)
            self.assertEqual(read(agent / 'properties.json')['native_session_id'], healthy)
        for requested in (previous, later, torn):
            with self.subTest(torn_peer_journal=requested), \
                    self.assertRaisesRegex(ValueError, 'damaged-peer.*sessions.jsonl'):
                agents.rebind_session(self.root, agent.name, healthy, requested, 'clear', 'launch', home=self.home)
        self.assertEqual((peer / 'sessions.jsonl').read_bytes(), before)
        self.assertEqual(read(agent / 'properties.json')['native_session_id'], healthy)
        unreadable, previous = self.pi_agent('permission-peer')
        agents.rebind_session(self.root, unreadable.name, previous, str(uuid4()), 'clear', 'launch', home=self.home)
        blocked, opened_original = unreadable / 'sessions.jsonl', Path.open
        reads = []

        def opened(path, *args, **kwargs):
            if path == blocked:
                reads.append(path)
                raise PermissionError('permission denied')
            return opened_original(path, *args, **kwargs)
        for requested in (str(uuid4()), read(unreadable / 'properties.json')['native_session_id']):
            with self.subTest(unreadable_peer_journal=requested), patch.object(Path, 'open', opened), \
                    self.assertRaisesRegex(ValueError, 'permission-peer.*sessions.jsonl.*read'):
                agents.rebind_session(self.root, agent.name, healthy, requested, 'clear', 'launch', home=self.home)
        data = read(unreadable / 'properties.json')
        for provider, home, allowed in (('pi', self.home + '-pi', True),
                                        ('fixture', self.home + '-other', True),
                                        ('pi', self.home, True),
                                        ('fixture', self.home, False),
                                        (None, self.home + '-other', False),
                                        ([], self.home + '-other', False),
                                        ('fixture', None, False),
                                        ('fixture', 'relative/home', False)):
            with self.subTest(provider=provider, home=home):
                # Keep the core-written journal intact. Only its permissions are unavailable.
                value = {**data, 'native_session_provider': provider, 'native_session_home': home}
                if provider is None or provider == []:
                    value.pop('harness', None)  # Truly unknown provider, not an alias resolved by config.
                write(unreadable / 'properties.json', value)
                reads.clear()
                with patch.object(Path, 'open', opened):
                    target = str(uuid4())
                    if allowed:
                        with sessions.namespace(self.root):
                            sessions.claim(self.root, agent, read(agent / 'properties.json'), target, self.home)
                        self.assertEqual(reads, [])
                    else:
                        with self.assertRaisesRegex(ValueError, 'permission-peer.*sessions.jsonl.*read'):
                            with sessions.namespace(self.root):
                                sessions.claim(self.root, agent, read(agent / 'properties.json'), target, self.home)
                        self.assertTrue(reads)
        child = templates.create(self.root / 'subverses/qa', 'qa', workdir=self.work)
        target, data, _, _ = lifecycle._record(child, 'target', '', 'codex', None, None)
        native = str(uuid4())
        data.update(workspace=str(self.work), native_session_provider='codex')
        write(target / 'properties.json', data)
        orphan = self.root / 'state/agents' / str(uuid4())
        (orphan / 'compaction').mkdir(parents=True)

        def claim():
            with sessions.namespace(child):
                sessions.claim(child, target, data, native)
        for name in ('runtime.json', 'sessions.jsonl', 'launches/' + str(uuid4()) + '/request.json'):
            with self.subTest(orphan_with=name):
                evidence = orphan / name
                evidence.parent.mkdir(parents=True, exist_ok=True)
                evidence.write_text('{"native_session_id": "' + native + '"}\n')
                try:
                    with self.assertRaisesRegex(sessions.SessionConflict, orphan.name):
                        claim()
                    agents.check_available_name(self.root, 'unreserved')
                finally:
                    evidence.unlink()
        stat, runtime = Path.stat, orphan / 'runtime.json'
        for code in (errno.EACCES, errno.EIO, errno.ENOTDIR):
            def failed_stat(path, *args, **kwargs):
                if path == runtime:
                    raise OSError(code, 'fixture cannot inspect', str(path))
                return stat(path, *args, **kwargs)
            with self.subTest(orphan_stat=code), patch.object(Path, 'stat', failed_stat):
                with self.assertRaisesRegex(sessions.SessionConflict, orphan.name):
                    claim()
                with self.assertRaisesRegex(records.RecordError, orphan.name):
                    agents.check_available_name(self.root, 'unreserved')
        with self.subTest('a plugin-only folder holds nothing'):
            claim()
            bound = agents.bind_session(child, target.name, native)
            self.assertEqual(bound['native_session_id'], native)
            self.assertEqual(read(target / 'properties.json')['native_session_id'], native)
        with self.subTest('main lost-spawn launch folders reserve no name or session'):
            launch = orphan / 'launches' / str(uuid4())
            launch.mkdir(parents=True)
            (launch / 'bootstrap.md').write_text('bootstrap')
            (launch / 'task.md').write_text('task')
            claim()
            agents.check_available_name(self.root, 'unreserved')
        with self.subTest('a missing properties file cannot claim an unrelated runtime-only session'):
            write(orphan / 'runtime.json', {'state': 'failed', 'native_session_id': str(uuid4())})
            claim()
            agents.check_available_name(self.root, 'unreserved')
        with self.subTest('a non-directory launch entry cannot hold a launch request'):
            (orphan / 'launches' / str(uuid4())).write_text('leftover')
            claim()
        with self.subTest('a regular launches file cannot hold a name or session claim'):
            import shutil
            shutil.rmtree(orphan / 'launches')
            (orphan / 'launches').write_text('leftover')
            claim()  # The unrelated runtime above must not make this file unreadable authority.
            agents.check_available_name(self.root, 'unreserved')
            (orphan / 'runtime.json').unlink()
            self.assertTrue(agents.orphaned(orphan))
            claim()
            agents.check_available_name(self.root, 'unreserved')
