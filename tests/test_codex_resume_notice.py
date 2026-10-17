"""A resume explains why it retained the conversation's saved Codex home."""
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from multiverse.harnesses.codex import CodexAdapter
from multiverse.harnesses.types import SessionHandle


class ResumeNoticeTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='mv2-codex-notice-', dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name).resolve()
        self.saved, self.requested = self.work / 'saved', self.work / 'requested'
        self.saved.mkdir()
        self.requested.mkdir()
        self.adapter = CodexAdapter.configured('codex', {'inbox_bridge': True})
        self.session = SessionHandle(str(uuid4()), 'codex', str(self.work), native_id=str(uuid4()))

    def test_different_explicit_home_is_explained_without_changing_environment(self):
        caller = {'CODEX_HOME': str(self.requested)}
        effective = {'CODEX_HOME': str(self.saved)}
        notice = self.adapter.resume_notice(self.session, caller, effective)
        self.assertIn('Kept its saved Codex home ' + json.dumps(str(self.saved)), notice)
        self.assertIn('ignored CODEX_HOME=' + json.dumps(str(self.requested)), notice)
        self.assertEqual(caller, {'CODEX_HOME': str(self.requested)})
        self.assertEqual(effective, {'CODEX_HOME': str(self.saved)})

    def test_equal_aliased_or_unspecified_home_needs_no_notice(self):
        alias = self.work / 'alias'
        alias.symlink_to(self.saved, target_is_directory=True)
        for caller in ({}, {'CODEX_HOME': ''}, {'CODEX_HOME': str(self.saved)}, {'CODEX_HOME': str(alias)},
                       {'CODEX_SQLITE_HOME': str(self.requested)}):
            with self.subTest(caller=caller):
                self.assertIsNone(self.adapter.resume_notice(self.session, caller, {'CODEX_HOME': str(self.saved)}))

    def test_malformed_paths_do_not_create_a_misleading_notice(self):
        for caller, effective in (({'CODEX_HOME': None}, {'CODEX_HOME': str(self.saved)}),
                                  ({'CODEX_HOME': []}, {'CODEX_HOME': str(self.saved)}),
                                  ({'CODEX_HOME': 'bad\0path'}, {'CODEX_HOME': str(self.saved)}),
                                  ({'CODEX_HOME': str(self.requested)}, {})):
            with self.subTest(caller=caller, effective=effective):
                self.assertIsNone(self.adapter.resume_notice(self.session, caller, effective))

    def test_notice_escapes_control_characters_as_one_line(self):
        requested = str(self.work / 'request\nwith\tcontrol')
        notice = self.adapter.resume_notice(self.session, {'CODEX_HOME': requested}, {'CODEX_HOME': str(self.saved)})
        self.assertEqual(len(notice.splitlines()), 1)
        self.assertIn('\\n', notice)
        self.assertIn('\\t', notice)
