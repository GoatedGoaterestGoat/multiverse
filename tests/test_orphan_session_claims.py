"""Plugin-only leftovers cannot reserve a native conversation in another scope."""
import errno
from pathlib import Path
from unittest.mock import patch

from multiverse.runtime import lifecycle
from multiverse.store import read, uid, write
from multiverse.tools import doctor
from multiverse.universe import agents, sessions, templates
from test_multiverse import BaseTest


class OrphanSessionClaimsTest(BaseTest):
    def setUp(self):
        super().setUp()
        self.child = templates.create(self.root / 'subverses/qa', 'qa', workdir=self.work)
        self.target, self.data, _, _ = lifecycle._record(self.child, 'target', '', 'codex', None, None)
        self.native = uid()
        self.data.update(workspace=str(self.work), native_session_provider='codex')
        write(self.target / 'properties.json', self.data)
        self.orphan = self.root / 'state/agents' / uid()
        (self.orphan / 'compaction').mkdir(parents=True)

    def claim(self):
        with sessions.namespace(self.child):
            sessions.claim(self.child, self.target, self.data, self.native)

    def test_plugin_only_root_folder_does_not_block_subverse_claim_or_bind(self):
        self.claim()
        bound = agents.bind_session(self.child, self.target.name, self.native)
        self.assertEqual(bound['native_session_id'], self.native)
        self.assertEqual(read(self.target / 'properties.json')['native_session_id'], self.native)
        self.assertEqual(list(self.orphan.iterdir()), [self.orphan / 'compaction'])

    def test_doctor_names_orphan_and_working_removal_advice_without_rewriting_it(self):
        result = doctor.run(self.root)
        problems = [line for line in result['problems'] if self.orphan.name in line]
        self.assertEqual(len(problems), 1, result)
        self.assertIn(str(self.orphan), problems[0])
        self.assertIn('orphan', problems[0].lower())
        self.assertIn('remove', problems[0].lower())
        self.assertEqual(list(self.orphan.iterdir()), [self.orphan / 'compaction'])

    def test_any_core_evidence_keeps_a_missing_properties_peer_conservative(self):
        for name in ('runtime.json', 'sessions.jsonl', 'launches'):
            with self.subTest(name=name):
                path = self.orphan / name
                if name == 'launches':
                    path.mkdir()
                else:
                    path.write_text('{}\n')
                try:
                    with self.assertRaisesRegex(sessions.SessionConflict, self.orphan.name):
                        self.claim()
                finally:
                    path.rmdir() if name == 'launches' else path.unlink()

    def test_non_enoent_errors_never_prove_an_orphan_has_no_claim(self):
        original = Path.stat
        target = self.orphan / 'runtime.json'
        for code in (errno.EACCES, errno.EIO, errno.ENOTDIR):
            def failed_stat(path, *args, **kwargs):
                if path == target:
                    raise OSError(code, 'fixture cannot inspect', str(path))
                return original(path, *args, **kwargs)
            with self.subTest(code=code), patch.object(Path, 'stat', failed_stat), \
                    self.assertRaisesRegex(sessions.SessionConflict, self.orphan.name):
                self.claim()
