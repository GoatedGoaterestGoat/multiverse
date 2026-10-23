"""Read-only proof of an exact legacy Codex conversation's storage."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from multiverse.harnesses.codex import CodexAdapter
from multiverse.harnesses.types import SessionHandle


class CodexStorageTest(unittest.TestCase):
    def test_rollout_proof_distinguishes_absence_from_uncertain_storage(self):
        """Legacy adoption validates bounded native metadata and read stores; treating missing stores or incomplete scans as proven absence silently strands conversations."""
        adapter = CodexAdapter.configured('codex', {})
        fixture = (Path(__file__).parent / 'fixtures/codex/rollout-session-meta.jsonl').read_bytes()
        row = json.loads(fixture)
        native = row['payload']['id']
        session = SessionHandle('agent', 'codex', '/fixture/project', native_id=native)
        for environment, expected in (
                ({'HOME': '/candidate/user'}, '/candidate/user/.codex'),
                ({'HOME': '/candidate/user', 'CODEX_HOME': ''}, '/candidate/user/.codex'),
                ({'HOME': '/candidate/user', 'CODEX_HOME': '~/custom'}, '/candidate/user/custom'),
                ({'CODEX_HOME': '/explicit/home'}, '/explicit/home'),
                ({}, None), ({'CODEX_HOME': '~'}, None)):
            with self.subTest(environment=environment), patch.dict('os.environ', {'HOME': '/ambient/ignored'}):
                self.assertEqual(adapter.session_home(environment), expected)
        for case, expected in (('missing', False), ('empty-sessions', False), ('other-id', False),
                               ('captured', True), ('later-damaged-row', True), ('legacy-id-only', True),
                               ('reverted', True), ('archived', True), ('archived-reverted', True), ('fork-root', True),
                               ('archive-only', True), ('archive-only-empty', False), ('unreadable-archive', None),
                               ('compressed', None), ('compressed-with-plain', True),
                               ('foreign-header', None), ('invalid-root-id', None), ('duplicate-id', None), ('damaged', None),
                               ('incomplete', None), ('directory', None), ('symlink', None),
                               ('linked-sessions-root', None), ('linked-dated-folder', None),
                               ('unreadable', None), ('scan-budget', None), ('header-budget', None)):
            with self.subTest(case=case), tempfile.TemporaryDirectory(prefix='mv2-codex-proof-') as temporary:
                home = Path(temporary).resolve()
                path = home / 'sessions/2026/10/17' / f'rollout-2026-10-17T18-54-10-{native}.jsonl'
                if case != 'missing':
                    path.parent.mkdir(parents=True)
                raw = fixture
                if case == 'duplicate-id':
                    raw = fixture.replace(f'"id":"{native}"'.encode(),
                        f'"id":"00000000-0000-0000-0000-000000000001","id":"{native}"'.encode())
                if case in ('other-id', 'foreign-header', 'invalid-root-id', 'fork-root', 'legacy-id-only'):
                    changed = deepcopy(row)
                    if case in ('other-id', 'foreign-header'):
                        changed['payload']['id'] = '00000000-0000-0000-0000-000000000001'
                        if case == 'other-id':
                            changed['payload']['session_id'] = changed['payload']['id']
                    elif case in ('invalid-root-id', 'fork-root'):
                        changed['payload']['session_id'] = ('not-a-uuid' if case == 'invalid-root-id' else
                                                           '00000000-0000-0000-0000-000000000001')
                    else:
                        del changed['payload']['session_id']
                    raw = json.dumps(changed).encode() + b'\n'
                if case not in ('missing', 'empty-sessions', 'directory', 'symlink'):
                    path.write_bytes(b'{damaged\n' if case == 'damaged' else
                                     fixture.rstrip(b'\n') if case == 'incomplete' else
                                     fixture + b'{damaged later row\n' if case == 'later-damaged-row' else raw)
                if case == 'directory':
                    path.mkdir()
                elif case == 'symlink':
                    target = home / 'elsewhere.jsonl'
                    target.write_bytes(fixture)
                    path.symlink_to(target)
                elif case in ('linked-sessions-root', 'linked-dated-folder'):
                    source = home / 'sessions' if case == 'linked-sessions-root' else path.parent
                    target = home / 'linked-target'
                    source.rename(target)
                    source.symlink_to(target, target_is_directory=True)
                elif case == 'other-id':
                    path.rename(path.with_name('rollout-2026-10-17T18-54-10-00000000-0000-0000-0000-000000000001.jsonl'))
                if case in ('reverted', 'archived-reverted'):
                    path = path.rename(path.with_name(path.stem + '_00000000-0000-0000-0000-000000000002.jsonl'))
                if case in ('archived', 'archived-reverted'):
                    archive = home / 'archived_sessions'
                    archive.mkdir()
                    path.rename(archive / path.name)
                if case in ('archive-only', 'archive-only-empty', 'unreadable-archive'):
                    archive = home / 'archived_sessions'
                    archive.mkdir()
                    if case == 'archive-only':
                        path.rename(archive / path.name)
                    else:
                        path.unlink()
                    if case.startswith('archive-only'):
                        (home / 'sessions').rename(home / 'not-a-native-store')
                if case in ('compressed', 'compressed-with-plain'):
                    compressed = path.with_suffix('.jsonl.zst')
                    compressed.write_bytes(b'unsupported compressed data')
                    if case == 'compressed':
                        path.unlink()
                with self._patch_case(case):
                    result = adapter.session_storage_evidence(session, str(home))
                self.assertEqual(result.state, 'unknown' if expected is None else 'ok', result)
                if expected is None:
                    self.assertIn(str(home), result.reason)
                else:
                    proof = {'present': expected}
                    if not expected:
                        proof['store_read'] = case not in ('missing', 'archive-only-empty')
                    self.assertEqual(result.value, proof)
        for candidate, target in (('relative', session), ('/candidate', replace(session, native_id=None))):
            with self.subTest(candidate=candidate, native_id=target.native_id):
                self.assertEqual(adapter.session_storage_evidence(target, candidate).state, 'unknown')

    def _patch_case(self, case):
        if case == 'unreadable':
            return patch('multiverse.harnesses.codex.storage.os.scandir', side_effect=PermissionError('denied'))
        if case == 'unreadable-archive':
            from multiverse.harnesses.codex.storage import os
            scandir = os.scandir
            def read_directory(path):
                if Path(path).name == 'archived_sessions':
                    raise PermissionError('archive denied')
                return scandir(path)
            return patch('multiverse.harnesses.codex.storage.os.scandir', side_effect=read_directory)
        if case == 'scan-budget':
            return patch('multiverse.harnesses.codex.storage.MAX_ENTRIES', 1)
        if case == 'header-budget':
            return patch('multiverse.harnesses.codex.storage.HEADER_BYTES', 16)
        from contextlib import nullcontext
        return nullcontext()
